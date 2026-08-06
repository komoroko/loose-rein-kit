"""The git/worktree mechanics build_loop drives — isolation, preservation, salvage, merge.

Kept apart from the Orchestrator so the data-loss-avoidance rules (nothing unmerged may be
the only copy) can be read and tested without the scheduling machinery around them; the
Orchestrator's git methods are thin delegates into one `GitWorkspace`. The subprocess runner
is injected and late-bound through `build_loop._run` — the single patch point the tests and
doctor/pr_draft already rely on (see build_loop's `_late_run`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from rein import common
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
    ) -> None:
        self.repo = repo
        self.root = str(repo.root)
        self.branch = branch
        self.dry_run = dry_run
        self.worktree_dir = worktree_dir
        self.branch_pattern = branch_pattern
        self._run = run
        # Where this layer reports what it did. Injected rather than imported: git surgery
        # happens inside leaf worktrees, and a worktree writing its own event log is how a
        # decision recorded during a parallel build used to disappear (plan §11.1).
        self.on_event: EventSink = on_event or (lambda event, subject, detail: None)
        # Where preserved work is reported so the *next* attempt can find it. Injected for the
        # same reason as on_event: this runs inside a worktree that is about to be replaced.
        self.on_salvage: SalvageSink = on_salvage or (lambda task_id, branch, state: None)

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

    def worktree_path(self, task_id: str) -> str:
        """The leaf worktree path under worktree_dir."""
        return str(self.repo.path(self.worktree_dir) / task_id)

    def tree_state(self, cwd: str) -> tuple[str, str]:
        """(HEAD hash, porcelain status) — the change-detection fingerprint."""
        _, head = self._run(["git", "rev-parse", "HEAD"], cwd=cwd)
        _, dirty = self._run(["git", "status", "--porcelain"], cwd=cwd)
        return head.strip(), dirty.strip()

    def head(self, cwd: str | None = None) -> str:
        """The current HEAD hash ("" when unavailable)."""
        _, out = self._run(["git", "rev-parse", "HEAD"], cwd=cwd or self.root)
        return out.strip()

    def add_worktree(self, task_id: str, restore_from: str = "") -> str:
        """Create a worktree for a leaf task and return the branch name. Clean up any existing one first.

        To avoid .git index.lock contention, worktree creation must be called **serially on the main thread**.

        `restore_from` is an earlier attempt's salvage branch. The new branch still starts from the
        work branch — branching from the salvage point instead would silently drop whatever the
        work branch gained in the meantime — and the preserved work is merged in on top. That is
        the difference between "the interrupted run's work was kept" and "the next run continues
        it": preservation alone left every restarted task re-implemented from zero.
        """
        branch = self.branch_for(task_id)
        path = self.worktree_path(task_id)
        salvaged = "" if self.dry_run else self._salvage_leftovers(task_id, branch, path)
        self.git(["worktree", "add", "-b", branch, path, self.branch])
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

        The clean-up used to be unconditional (`worktree remove --force` + `branch -D`), which
        destroyed a crashed run's committed work — and the branch cleanup_worktree deliberately
        keeps for human inspection — the moment the loop was re-invoked. Nothing unmerged may be
        the only copy: an uncommitted diff is finalized onto the leaf branch first (same principle
        as finalize_commit's), and a branch holding commits the work branch does not have is
        renamed to a salvage name instead of deleted (recorded as a branch_salvaged event). A
        fully-merged branch is deleted as before — its content is already in the work branch.
        """
        if Path(path).is_dir() and not self.finalize_commit(path, f"{task_id}: WIP (salvaged at restart)"):
            # The tree may hold the only copy of the previous run's diff — stop rather than destroy it
            # (the finalize failure is already escalated with the repair pointer).
            raise StopLoop(f"{task_id}: could not preserve the leftover worktree {path}; kept for manual recovery")
        self._run(["git", "worktree", "remove", "--force", path], cwd=self.root)
        if self._run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=self.root)[0] == 0:
            rc, out = self._run(["git", "rev-list", "-n", "1", branch, "--not", self.branch], cwd=self.root)
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

    def merge_leaf(self, task_id: str, branch: str) -> bool:
        """Merge a leaf branch into work and remove the worktree. On a conflict, abort and return False."""
        if self.dry_run:
            print(f"    [dry-run] git merge --no-ff {branch} → {self.branch}, remove worktree")
            return True
        rc, out = self._run(["git", "merge", "--no-ff", "--no-edit", branch], cwd=self.root)
        if rc != 0:
            self._run(["git", "merge", "--abort"], cwd=self.root)
            self.on_event(
                "task_failed",
                task_id,
                {"kind": "merge_conflict", "detail": f"conflict merging into work: {out[-500:]}"},
            )
            return False
        self.git(["worktree", "remove", "--force", self.worktree_path(task_id)])
        return True

    def branch_changed_paths(self, branch: str) -> list[str]:
        """Paths a leaf branch changed since it forked off the work branch (merge-base diff)."""
        rc, out = self._run(["git", "diff", "--name-only", f"{self.branch}...{branch}"], cwd=self.root)
        return [p for p in out.splitlines() if p.strip()] if rc == 0 else []

    def changed_since(self, base: str) -> list[str]:
        """Paths a serial task changed on the work branch: commits since `base` plus the dirty tree."""
        paths: set[str] = set()
        rc, out = self._run(["git", "diff", "--name-only", f"{base}..HEAD"], cwd=self.root)
        if rc == 0:
            paths.update(p for p in out.splitlines() if p.strip())
        rc, out = self._run(["git", "status", "--porcelain", "-uall", "--", ".", ":(exclude).rein"], cwd=self.root)
        if rc == 0:
            for line in out.splitlines():
                if len(line) < 4:
                    continue
                path = line[3:]
                if " -> " in path:
                    path = path.split(" -> ", 1)[1]
                paths.add(path.strip('"'))
        return sorted(paths)

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
        pathspec = [".", ":(exclude).rein"]
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
