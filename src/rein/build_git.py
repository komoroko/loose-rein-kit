"""The git/worktree mechanics build_loop drives — isolation, preservation, salvage, merge.

Kept apart from the Orchestrator so the data-loss-avoidance rules (nothing unmerged may be
the only copy) can be read and tested without the scheduling machinery around them; the
Orchestrator's git methods are thin delegates into one `GitWorkspace`. The subprocess runner
is injected and late-bound through `build_loop._run` — the single patch point the tests and
doctor/pr_draft already rely on (see build_loop's `_late_run`).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from rein import common, digests
from rein import repo as repo_mod
from rein.common import StopLoop


class EventSink(Protocol):
    """Where the git layer reports what happened. The orchestrator supplies a Store-backed one."""

    def __call__(self, event: str, subject: str, detail: dict[str, object]) -> None: ...


class SalvageSink(Protocol):
    """Where preserved work-in-progress is recorded, so the next attempt inherits it."""

    def __call__(self, task_id: str, branch: str, state: str) -> None: ...


class Runner(Protocol):
    """common.run's shape: (returncode, stdout+stderr merged)."""

    def __call__(self, cmd: list[str], cwd: str | None = None, timeout: float | None = None) -> tuple[int, str]: ...


def worktree_heads(repo: repo_mod.Repo, run: Runner) -> list[tuple[str, str]]:
    """`(path, branch)` for every worktree of `repo` that has a branch checked out.

    git refuses one branch in two worktrees, so anything that wants to merge into a branch has to
    ask first whether some worktree already holds it — and merge there if one does.
    """
    rc, out = run(["git", "worktree", "list", "--porcelain"], cwd=str(repo.root))
    if rc != 0:
        return []
    found: list[tuple[str, str]] = []
    path = ""
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree ") :].strip()
        elif line.startswith("branch ") and path:
            found.append((path, line[len("branch refs/heads/") :].strip()))
    return found


#: Held across the `worktree remove` / `prune` / `add` triple below and across the teardown, and
#: never across the body in between.
#:
#: `git worktree prune` is a **repository-wide** operation — it rewrites `.git/worktrees` for every
#: worktree at once, not for the one being made — and this used to be called only from the merge
#: step, which runs after the parallel phase and therefore alone. The negative control calls it from
#: inside each leaf's own thread, so up to `max_parallel` of them now overlap. Serialising the admin
#: calls is what keeps two threads out of one `.git/worktrees`; serialising the *body* would undo
#: the parallelism the control runs inside.
_WORKTREE_ADMIN = threading.Lock()


@contextmanager
def scratch_worktree(repo: repo_mod.Repo, worktree_dir: str, name: str, branch: str, run: Runner) -> Iterator[str]:
    """A throwaway worktree with `branch` checked out, removed on the way out.

    For merging into a branch nothing else has checked out, and for the negative control's
    re-establishment over the base. A leftover from a killed run is cleared first — it holds no work
    of its own, so removing it cannot lose anything, which is exactly what is *not* true of the leaf
    worktrees above.
    """
    path = str(repo.path(worktree_dir) / name)
    with _WORKTREE_ADMIN:
        run(["git", "worktree", "remove", "--force", path], cwd=str(repo.root))
        run(["git", "worktree", "prune"], cwd=str(repo.root))
        rc, out = run(["git", "worktree", "add", "--force", path, branch], cwd=str(repo.root))
    if rc != 0:
        raise StopLoop(f"could not create the scratch worktree at {path}: {out}")
    try:
        yield path
    finally:
        with _WORKTREE_ADMIN:
            run(["git", "worktree", "remove", "--force", path], cwd=str(repo.root))
            run(["git", "worktree", "prune"], cwd=str(repo.root))


class GitWorkspace:
    """Every git call of one build run, anchored to the repo root and the work branch."""

    def __init__(
        self,
        repo: repo_mod.Repo,
        branch: str,
        *,
        dry_run: bool,
        worktree_dir: str,
        branch_pattern: str,
        run: Runner,
        on_event: EventSink | None = None,
        on_salvage: SalvageSink | None = None,
        landing: Mapping[str, str] | None = None,
    ) -> None:
        self.repo = repo
        self.root = str(repo.root)
        self.branch = branch
        self.dry_run = dry_run
        self.worktree_dir = worktree_dir
        #: Prefixes that are never part of what "the tree" means, and the git pathspec spelling
        #: the same thing. Four answers read them — the fingerprint, the paths a task is credited
        #: with, the commit it produces, and the `git add` an implementer is told to type — and
        #: they have to agree, which is why they are derived here once rather than spelled out
        #: four times (`test_the_tree_exclusion_lives_in_exactly_one_place`).
        #:
        #: `.rein/` is orchestration state (see `repo.SSOT_DIR`). The leaf worktree root is the
        #: other half and had been left out: it holds *other* tasks' work in progress, so a
        #: serial task running beside a leaf counted that leaf's whole worktree as its own change
        #: — against its declared scope, into its fingerprint, and, through `git add -A`, into its
        #: commit as a gitlink. It is configurable (`execution.worktree_dir`), which is why this
        #: is per-instance where `repo.SSOT_PATHSPEC` is a constant.
        self.excluded: tuple[str, ...] = (repo_mod.SSOT_DIR, worktree_dir.rstrip("/") + "/")
        self.pathspec: tuple[str, ...] = repo_mod.pathspec_excluding(self.excluded)
        self.branch_pattern = branch_pattern
        self._run = run
        # Where this layer reports what it did. Injected rather than imported: git surgery
        # happens inside leaf worktrees, and a worktree that writes its own event log loses the
        # record when it is deleted (plan §11.1).
        self.on_event: EventSink = on_event or (lambda event, subject, detail: None)
        # Where preserved work is reported so the *next* attempt can find it. Injected for the
        # same reason as on_event: this runs inside a worktree that is about to be replaced.
        self.on_salvage: SalvageSink = on_salvage or (lambda task_id, branch, state: None)
        # Where a re-run task's work lands, when it is not the work branch. A task whose pull
        # request is already open has to have its fix land *on that pull request* — appending it
        # to the work branch instead would put the fix for slice 2 in a slice nobody has reviewed.
        # Empty for a first build, which is why nothing about that path changes.
        self.landing: dict[str, str] = dict(landing or {})

    def git(self, args: list[str], cwd: str | None = None) -> None:
        """Run one git command; StopLoop on failure; prints and no-ops under dry-run."""
        cwd = cwd or self.root
        if self.dry_run:
            print(f"    [dry-run] git {' '.join(args)} (cwd={cwd})")
            return
        rc, out = self._run(["git", *args], cwd=cwd)
        if rc != 0:
            raise StopLoop(f"git {' '.join(args)} failed (rc={rc})\n{out[-1000:]}")

    def branch_for(self, task_id: str) -> str:
        """The leaf branch name per branch_pattern."""
        return self.branch_pattern.format(branch=self.branch, task_id=task_id)

    def target_branch(self, task_id: str) -> str:
        """Where this task's work is based on and merged back into. The work branch by default.

        A task with an open pull request lands on *that* branch instead: the fix for a review
        finding belongs to the slice that introduced the code, and `pr-stack --restack` carries it
        upward afterwards. With no stack the map is empty and every task lands on the work branch,
        which is every existing run.
        """
        return self.landing.get(task_id) or self.branch

    def merge_cwd(self, task_id: str) -> str:
        """The checkout to merge this task's leaf into — whichever worktree holds its target branch.

        "" when nothing has it checked out and the caller has to make a scratch worktree
        (:func:`scratch_worktree`); git refuses the same branch in two worktrees at once.
        """
        target = self.target_branch(task_id)
        for path, branch in worktree_heads(self.repo, self._run):
            if branch == target:
                return path
        return ""

    def landed(self, task_id: str) -> str:
        """The commit this task's target branch now points at — what `completed_commit` records."""
        _, out = self._run(["git", "rev-parse", self.target_branch(task_id)], cwd=self.root)
        return out.strip()

    def worktree_path(self, task_id: str) -> str:
        """The leaf worktree path under worktree_dir."""
        return str(self.repo.path(self.worktree_dir) / task_id)

    def fingerprint(self, cwd: str) -> str:
        """A content digest of the working tree at `cwd`, or "" when it cannot be computed.

        This is what "the same tree" means everywhere downstream: the change-detection probe
        around an agent step, and the subject of every evidence-ledger fact. Both need *content*.
        The predecessor hashed HEAD plus `git status --porcelain`, which is a list of names and
        status codes — so a second edit to a file that was already modified left the fingerprint
        byte-identical, and "did the agent change anything?" answered no while the tree had moved.

        Three inputs, and each covers what the others cannot:

          committed blobs           `git ls-tree -r -z HEAD`, hashed by path/mode/blob id. Not the
                                    commit id: that moves when history does, so a salvage merge
                                    changing not one byte would invalidate every fact established
                                    about that content — and an observation a human recorded
                                    against it would stop matching the code it was about.
          `git diff HEAD --binary`  every tracked modification and deletion, content and all
                                    (`--binary` so an image or a compiled fixture is not reduced
                                    to "Binary files differ", which is a name again)
          untracked blob ids        `git hash-object` over what `git ls-files -o` lists, since a
                                    brand-new file appears in neither of the first two

        **`.rein/` is excluded throughout**, for the same reason `finalize_commit` excludes it
        from a task commit: orchestration state is not the product. Including it made the
        fingerprint move every time anything recorded anything — so the very act of writing down
        that a step had passed changed the tree that step had passed against.

        Returns "" — never a partial digest — when any of that is unavailable or was truncated by
        the output cap. Callers read "" as "unknown", which is a cache miss and a changed tree:
        the direction that costs a re-run rather than a wrong verdict.
        """
        # `-z`, because `parse_ls_tree` requires it: the default format C-escapes a path holding
        # a newline, and an escaped path is a different string from the one on disk. Fed the
        # unseparated form it parsed the whole listing as one entry, whose name began `.rein/` —
        # so the exclusion below dropped everything and every tree hashed identically. A
        # fingerprint that is silently constant is worse than none, which is what the emptiness
        # check underneath is for.
        rc, listed = self._run(["git", "ls-tree", "-r", "-z", "HEAD"], cwd=cwd)
        if rc != 0 or common.was_truncated(listed):
            return ""
        try:
            entries = digests.parse_ls_tree(listed)
        except digests.DigestError:
            return ""
        if listed.strip() and not entries:
            return ""
        committed = digests.tree_digest(digests.filter_tree(entries, exclude_prefixes=self.excluded))
        rc, diff = self._run(["git", "diff", "HEAD", "--binary", "--", *self.pathspec], cwd=cwd)
        if rc != 0 or common.was_truncated(diff):
            return ""
        rc, listing = self._run(["git", "ls-files", "-o", "--exclude-standard", "--", *self.pathspec], cwd=cwd)
        if rc != 0 or common.was_truncated(listing):
            return ""
        untracked = [name for name in listing.splitlines() if name.strip()]
        blobs = ""
        if untracked:
            rc, blobs = self._run(["git", "hash-object", "--", *untracked], cwd=cwd)
            if rc != 0 or common.was_truncated(blobs):
                return ""
        return digests.of_texts(["tree", committed, "diff", diff, "untracked", *untracked, "blobs", blobs])

    def head(self, cwd: str | None = None) -> str:
        """The current HEAD hash ("" when unavailable)."""
        _, out = self._run(["git", "rev-parse", "HEAD"], cwd=cwd or self.root)
        return out.strip()

    def add_worktree(self, task_id: str, restore_from: str = "") -> str:
        """Create a worktree for a leaf task and return the branch name. Clean up any existing one first.

        To avoid .git index.lock contention, worktree creation must be called **serially on the main thread**.

        `restore_from` is an earlier attempt's salvage branch. The new branch still starts from the
        task's target branch (:meth:`target_branch`, the work branch unless a pull request already
        holds this task) — branching from the salvage point instead would silently drop whatever
        that branch gained in the meantime — and the preserved work is merged in on top. That is
        the difference between "the interrupted run's work was kept" and "the next run continues
        it": preservation alone left every restarted task re-implemented from zero.
        """
        branch = self.branch_for(task_id)
        path = self.worktree_path(task_id)
        salvaged = "" if self.dry_run else self._salvage_leftovers(task_id, branch, path)
        self.git(["worktree", "add", "-b", branch, path, self.target_branch(task_id)])
        self._restore_salvaged(task_id, path, salvaged or restore_from)
        return branch

    def _restore_salvaged(self, task_id: str, path: str, salvage: str) -> None:
        """Merge a salvage branch into the fresh worktree; report what happened either way.

        A conflict is not fatal — the branch keeps the work and the implementer is told where it
        is — because failing the task here would strand the very run meant to recover it.
        """
        if not salvage or self.dry_run:
            return
        if self._run(["git", "rev-parse", "--verify", "--quiet", salvage], cwd=self.root)[0] != 0:
            return
        rc, _ = self._run(["git", "merge", "--no-edit", salvage], cwd=path)
        if rc == 0:
            print(f"  [handoff] {task_id}: restored the previous attempt's work from {salvage}")
            self.on_salvage(task_id, salvage, "restored")
            return
        self._run(["git", "merge", "--abort"], cwd=path)
        print(f"  [handoff] {task_id}: {salvage} conflicts with the work branch — left for the implementer")
        self.on_salvage(task_id, salvage, "conflict")

    def _salvage_name(self, branch: str) -> str:
        """A free salvage-branch name: `<branch>-salvage-<UTC stamp>`, suffixed on collision."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        candidate = f"{branch}-salvage-{stamp}"
        n = 1
        while self._run(["git", "rev-parse", "--verify", "--quiet", candidate], cwd=self.root)[0] == 0:
            n += 1
            candidate = f"{branch}-salvage-{stamp}-{n}"
        return candidate

    def _salvage_leftovers(self, task_id: str, branch: str, path: str) -> str:
        """Preserve, then clear, a previous run's leftover worktree/branch so `worktree add -b` can re-run.

        Returns the salvage branch it created, or "" when there was nothing to preserve.

        Nothing unmerged may be the only copy, so the clean-up is conditional: an uncommitted
        diff is finalized onto the leaf branch first (same principle as finalize_commit's), and a
        branch holding commits the work branch does not have is renamed to a salvage name instead
        of deleted (recorded as a branch_salvaged event). A fully-merged branch is deleted — its
        content is already in the work branch.
        """
        if Path(path).is_dir() and not self.finalize_commit(path, f"{task_id}: WIP (salvaged at restart)"):
            # The tree may hold the only copy of the previous run's diff — stop rather than destroy it
            # (the finalize failure is already escalated with the repair pointer).
            raise StopLoop(f"{task_id}: could not preserve the leftover worktree {path}; kept for manual recovery")
        self._run(["git", "worktree", "remove", "--force", path], cwd=self.root)
        if self._run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=self.root)[0] == 0:
            target = self.target_branch(task_id)
            rc, out = self._run(["git", "rev-list", "-n", "1", branch, "--not", target], cwd=self.root)
            if rc != 0 or out.strip():  # unmerged commits — or unable to prove there are none
                salvage = self._salvage_name(branch)
                self.git(["branch", "-m", branch, salvage])
                self.on_event(
                    "decision_declared",
                    task_id,
                    {"branch_salvaged": f"{branch} -> {salvage}", "why": "unmerged work preserved at restart"},
                )
                print(f"  [salvage] {task_id}: {branch} held unmerged work — renamed to {salvage}")
                self.on_salvage(task_id, salvage, "pending")
                self._run(["git", "worktree", "prune"], cwd=self.root)
                return salvage
            self._run(["git", "branch", "-D", branch], cwd=self.root)
        self._run(["git", "worktree", "prune"], cwd=self.root)
        return ""

    def cleanup_worktree(self, task_id: str) -> None:
        """Remove a leaf's worktree without merging (blocked / merge conflict).

        Blocked tasks leave the frontier, so the startup cleanup in add_worktree never reaches
        their worktrees — without this they orphan under .worktrees/. The branch is kept: it holds
        the diff a human needs to inspect or resolve, so any uncommitted leftovers are finalized
        onto it first (otherwise the forced removal would silently drop them).
        """
        if self.dry_run:
            return
        if not self.finalize_commit(self.worktree_path(task_id), f"{task_id}: WIP (blocked)"):
            return  # the worktree may hold the only copy of the diff — keep it rather than destroy it
        self._run(["git", "worktree", "remove", "--force", self.worktree_path(task_id)], cwd=self.root)
        self._run(["git", "worktree", "prune"], cwd=self.root)

    def merge_leaf(self, task_id: str, branch: str, cwd: str = "") -> bool:
        """Merge a leaf branch into its target and remove the worktree. On a conflict, abort and return False.

        `cwd` is the checkout holding the target branch; the repository root by default, which is
        where the work branch lives. A task landing on a slice branch is merged wherever that
        branch is checked out (:meth:`merge_cwd`, or a scratch worktree the caller made).
        """
        target = self.target_branch(task_id)
        if self.dry_run:
            print(f"    [dry-run] git merge --no-ff {branch} → {target}, remove worktree")
            return True
        rc, out = self._run(["git", "merge", "--no-ff", "--no-edit", branch], cwd=cwd or self.root)
        if rc != 0:
            self._run(["git", "merge", "--abort"], cwd=cwd or self.root)
            self.on_event(
                "task_failed",
                task_id,
                {"kind": "merge_conflict", "detail": f"conflict merging into {target}: {out[-500:]}"},
            )
            return False
        self.git(["worktree", "remove", "--force", self.worktree_path(task_id)])
        return True

    def branch_changed_paths(self, task_id: str, cwd: str = "") -> list[str]:
        """Paths a leaf changed: committed since it forked off **its target branch**, plus its dirty tree.

        Takes the task rather than the branch, and derives both ends from it. The two used to be
        separate arguments, and the base was always the work branch — so a leaf landing on a slice
        branch was diffed against a branch it did not fork from, and the result carried every
        commit the slices below it had added. The gate-violation check then judged paths the task
        never touched, and the review step was told a scope that was not its own.

        `cwd` is the leaf's worktree. Without it this answered on committed work alone, which is
        the *branch*'s state rather than the attempt's: the implementer is told to commit, but the
        loop's own `finalize_commit` exists precisely because it sometimes does not — so "the
        implementer produced nothing" and "the implementer has not committed yet" were the same
        answer. They are not the same thing, and one of them is a failure.
        """
        paths: set[str] = set()
        branch = self.branch_for(task_id)
        rc, out = self._run(["git", "diff", "--name-only", f"{self.target_branch(task_id)}...{branch}"], cwd=self.root)
        if rc == 0:
            paths.update(p for p in out.splitlines() if p.strip())
        if cwd:
            paths.update(self.dirty_paths(cwd))
        return sorted(paths)

    def dirty_paths(self, cwd: str) -> list[str]:
        """Uncommitted paths in `cwd` — modified, staged and untracked — excluding `.rein/`.

        `.rein/` is excluded for the same reason `finalize_commit` excludes it: orchestration
        state is not any task's work, and counting it would make every task look like it changed
        something.
        """
        paths: set[str] = set()
        rc, out = self._run(["git", "status", "--porcelain", "-uall", "--", *self.pathspec], cwd=cwd)
        if rc == 0:
            for line in out.splitlines():
                if len(line) < 4:
                    continue
                path = line[3:]
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                paths.add(path.strip('"'))
        return sorted(paths)

    def changed_since(self, base: str) -> list[str]:
        """Paths a serial task changed on the work branch: commits since `base` plus the dirty tree."""
        paths: set[str] = set()
        rc, out = self._run(["git", "diff", "--name-only", f"{base}..HEAD"], cwd=self.root)
        if rc == 0:
            paths.update(p for p in out.splitlines() if p.strip())
        paths.update(self.dirty_paths(self.root))
        return sorted(paths)

    def fork_point(self, ref: str, cwd: str) -> str:
        """The commit `cwd`'s HEAD forked off `ref` at, or "" when it cannot be determined.

        The base a leaf's change is a change *to*. `branch_changed_paths` already reads the leaf
        with `git diff <ref>...HEAD`, whose three dots mean exactly this commit; naming it here
        lets a caller that needs the base itself — rather than the paths — ask for it instead of
        re-deriving the same merge base with a different spelling.
        """
        rc, out = self._run(["git", "merge-base", ref, "HEAD"], cwd=cwd)
        return out.strip() if rc == 0 else ""

    def diff_from(self, base: str, cwd: str, paths: Sequence[str]) -> str | None:
        """`git diff <base> -- <paths>` taken in `cwd`: the change against `base`, dirty tree included.

        Two dots and not `base..HEAD` on purpose. A serial task's work may still be uncommitted
        when the gate runs (`finalize_commit` is what puts it on the branch, and it runs later), so
        a diff that stops at HEAD would describe a tree nobody is about to gate. This one describes
        what is actually there.

        **An untracked file is part of that tree and `git diff` cannot see one**, which is what the
        `--intent-to-add` below is for. The path lists this takes come from `dirty_paths`, which
        reads `git status -uall` and therefore *does* name untracked files — so a brand-new test
        file the implementer had not staged was listed as changed and then silently absent from the
        diff, with rc 0 and nothing anywhere saying a file had been dropped. The negative control is
        built on this diff: without the new test it re-established the base's own suite, found it
        green, and reported that no test in the change exercised it. A verdict about a file the
        experiment never contained.

        `--intent-to-add` is how git is told a new file exists without staging its content: the
        diff then carries it as `new file mode`.

        **And it is taken back before returning.** `fingerprint` hashes the committed blobs, the
        diff against HEAD, and the ids of whatever `git ls-files -o` still calls untracked — so
        promoting a file out of that third list changes the digest of a tree whose *content* nobody
        touched. That digest is what the evidence ledger keys every gate step on and what the
        futility probe compares one attempt against the next, so a reading of the tree must not be
        a write to it. Only the paths git itself reported as untracked a moment ago are un-staged
        again, which is why they are collected rather than inferred: `git rm --cached` on a path
        that was genuinely tracked would untrack it for real.

        **None when git could not be asked**, and `""` only when the answer is genuinely an empty
        diff. Collapsing the two was the same defect one layer up: a caller that reads a failed
        `git diff` as "no change" reports a verdict about a change it never saw, which is what the
        `--intent-to-add` above exists to stop happening for one missing file.
        """
        if not base or not paths:
            return ""
        added, staged = self._intent_to_add(cwd, paths)
        try:
            if not staged:
                return None
            rc, out = self._run(["git", "diff", base, "--", *paths], cwd=cwd)
        finally:
            if added:
                self._run(["git", "rm", "--cached", "--force", "--quiet", "--", *added], cwd=cwd)
        return out if rc == 0 else None

    def _intent_to_add(self, cwd: str, paths: Sequence[str]) -> tuple[list[str], bool]:
        """Record the untracked members of `paths` in the index as empty; say which, and whether it took.

        `--exclude-standard` so an ignored file stays ignored: git does not consider it part of the
        change and neither does anything upstream of here.

        The two halves of the answer are separate on purpose. **The paths are returned whether or
        not `git add` succeeded**, because a failed `git add` is not a `git add` that did nothing:
        it can stage some of its arguments and then fail on one, and the caller's `finally` is the
        only thing that takes any of it back. Returning `[]` there left intent-to-add entries in the
        index for a tree nobody edited, which moves the `fingerprint` the evidence ledger keys every
        gate step on — the exact harm the un-staging exists to prevent. Whether it took is the
        second half, and it is what stops a diff that is silently missing a file from being read as
        the change.

        `-z` because `git ls-files` quotes a path with a special character in it, and a quoted path
        is not the path: `git add -- '"tests/caf\303\251.py"'` matches nothing.
        """
        rc, out = self._run(["git", "ls-files", "-o", "-z", "--exclude-standard", "--", *paths], cwd=cwd)
        if rc != 0:
            return [], False
        untracked = [entry for entry in out.split("\0") if entry]
        if not untracked:
            return [], True
        rc, _ = self._run(["git", "add", "--intent-to-add", "--", *untracked], cwd=cwd)
        return untracked, rc == 0

    def finalize_commit(self, cwd: str, message: str) -> bool:
        """Commit any outstanding diff in `cwd` (excluding .rein/) — a no-op on a clean tree.

        The implementer is instructed to commit, but an uncommitted tree must never be the only
        copy: a leaf's worktree is removed with --force after the merge (or when blocked), and only
        what is on the branch survives. Finalizing here makes the branch the complete record.

        Returns False (after escalating) when a dirty tree could not be committed — a real failure
        (unset git identity, index lock, disk full) is the precursor of data loss, so the caller
        must keep the tree/worktree intact instead of removing it. The clean-tree no-op is decided
        by `git status --porcelain` up front, which is what makes a non-zero commit rc a genuine
        failure rather than "nothing to commit". The commit runs --no-verify: this is a
        preservation commit, not a quality decision (the quality gate already ran), and a hook
        rejection that silently drops the WIP would defeat its purpose. That also bypasses the
        commit-stage gate guard — covered instead by the loop's own merge/finalize-stage check
        (_gate_violations): everything a task changed is re-evaluated against the gate rules
        before it merges into the work branch (leaf) or is marked done (serial), so a stray
        out-of-scope edit escalates instead of landing silently in HEAD.
        """
        if self.dry_run:
            return True
        pathspec = list(self.pathspec)
        rc, out = self._run(["git", "status", "--porcelain", "--", *pathspec], cwd=cwd)
        if rc == 0 and not out.strip():
            return True  # clean tree — nothing to preserve
        if rc == 0:
            rc, out = self._run(["git", "add", "-A", "--", *pathspec], cwd=cwd)
        if rc == 0:
            rc, out = self._run(["git", "commit", "--no-verify", "-m", message], cwd=cwd)
        if rc != 0:
            task_id = message.split(":", 1)[0]
            self.on_event(
                "task_failed",
                task_id,
                {
                    "kind": "finalize_commit",
                    "detail": f"finalize commit failed in {cwd} (rc={rc}); the uncommitted diff exists "
                    f"only in that tree, which is kept for manual recovery. "
                    f"{common.summarize_failure('git finalize commit', rc, out)}",
                },
            )
            return False
        return True
