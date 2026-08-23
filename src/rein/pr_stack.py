"""`rein pr-stack` — the cycle's work branch, read out as a stack of pull requests.

One cycle has always shipped as one pull request. A cycle that touched eight tasks then arrives
as one diff, and the reviewer who has to read it is handed the whole of it at once. Everything
needed to cut it apart was already recorded: `build_loop` writes the work-branch commit that
landed each task into `state.yaml` as `completed_commit`, and the plan holds the DAG that says
what each task was for. This module turns those two into an ordered list of **slices** — one per
task — each of which becomes a pull request based on the one below it.

Three rules hold the design together, and none of them is a preference.

**History is never rewritten.** The usual way to keep a stack tidy is `rebase` plus a force-push.
Here that would strand every `completed_commit`, `review.subject_head_sha`, and gate receipt on
commits that no longer exist — the audit chain binds *real* commits, so rewriting history is
destroying the record. Fixes propagate upward by merge instead (`--restack`), which moves nothing.

**A slice's boundary freezes when its pull request opens.** Before that, the trailing slice tracks
the branch tip. After it, the slice is what its branch says it is: a review fix committed onto it
moves the branch forward, and :func:`derive` stops recomputing where that slice begins and ends.
Which slices are open is read back out of the audit log rather than from GitHub — the log is
already the record, it works offline, and it needs no schema change.

**Nothing here talks to the network.** Deriving and materialising are local git reads and one
`git branch -f` per slice. Pushing and opening pull requests are outward-facing, and live in the
CLI half behind an interactive confirmation.
"""

from __future__ import annotations

import argparse
import logging
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from rein import common, conflict, dag, event_chain, models, pr_draft, status_api
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The audit-log event a pull-request action is recorded as. `event.schema.json` closes the event
#: vocabulary, and a pull request is a decision the loop made about an artefact rather than a new
#: kind of thing that happens to a task — the same slot `build_git` records a salvaged branch in.
LEDGER_EVENT = "decision_declared"
#: The `detail` key that marks one of those records as belonging to this module, and its value:
#: which action was taken. Anything else in `detail` is descriptive.
LEDGER_KEY = "pr_stack"
LEDGER_OPENED = "opened"
LEDGER_READY = "ready"

#: Slice branches live in their own namespace so they never collide with the per-task leaf
#: branches `build_git` creates (`<work_branch>-T-NNN`), which outlive a blocked or salvaged task.
#: `-` rather than `/` for the same reason `build_git.branch_for` gives: git refuses a ref that is
#: a path prefix of another.
BRANCH_INFIX = "-pr-"
TAIL_SUFFIX = "tail"


class StackError(RuntimeError):
    """The recorded stack and the repository disagree. Always fail closed: never guess a boundary."""


@dataclass(frozen=True)
class Slice:
    """One pull request's worth of the cycle: a task's landing, or the commits after the last one."""

    index: int
    #: The task this slice delivers. Empty for the tail slice — the commits that belong to no task
    #: (a phase deliverable committed at a gate approval, a `git merge main`).
    task_id: str
    title: str
    branch: str
    #: The ref a pull request for this slice takes as its base: the previous slice's branch, or the
    #: stack's base (`main`) for the first one.
    base_ref: str
    base_sha: str
    head_sha: str
    commits: tuple[str, ...] = ()
    #: True once a pull request has been opened for this slice: its boundary no longer moves, and
    #: :func:`materialize` refuses to repoint its branch.
    opened: bool = False
    #: The head the pull request was opened against. Differs from `head_sha` once a review fix has
    #: been committed onto the branch — which is the whole point of committing it there.
    opened_at: str = ""

    @property
    def is_tail(self) -> bool:
        return not self.task_id

    @property
    def label(self) -> str:
        """How this slice is named to a human: its task id, or `tail`."""
        return self.task_id or TAIL_SUFFIX


@dataclass(frozen=True)
class LedgerRecord:
    """What the audit log remembers about one slice whose pull request was opened."""

    index: int
    task_id: str
    branch: str
    base_ref: str
    head: str
    url: str = ""
    ready: bool = False


@dataclass(frozen=True)
class Documents:
    """The four SSOT documents plus the audit log, read once.

    Every entry point here needs most of them, and reading the store per call is how two halves of
    one pull-request body end up describing different states of the repository.
    """

    plan: models.Plan
    state: models.State
    config: models.Config
    review: models.Review | None
    events: tuple[models.Event, ...]
    defects: tuple[event_chain.ChainDefect, ...]

    @classmethod
    def read(cls, repo: repo_mod.Repo) -> Documents:
        store = store_mod.Store(repo)
        plan, state, config = store.read_plan(), store.read_state(), store.read_config()
        if plan is None:
            raise StackError(f"no plan at {repo.plan} — a stack is cut along the plan's tasks")
        if state is None:
            raise StackError(f"no state at {repo.state} — nothing records which tasks landed where")
        if config is None:
            raise StackError(f"no config at {repo.config} — nothing names the work branch")
        events, defects = event_chain.scan(repo.events)
        return cls(plan, state, config, store.read_review(), tuple(events), tuple(defects))


@dataclass(frozen=True)
class Preconditions:
    """What stands in the way of one `pr-stack` mode, split by whether it stops the run."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


# --- git reads ----------------------------------------------------------------


def _rev_parse(repo: repo_mod.Repo, ref: str) -> str:
    """The commit `ref` names, or "" when it names nothing. Never raises."""
    return repo._git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")


def _first_parent(repo: repo_mod.Repo, base: str, tip: str) -> list[str]:
    """`base..tip` along first parents, oldest first.

    First-parent rather than the full set because the work branch's shape is *made* of merges:
    every parallel leaf lands as one `--no-ff` merge commit, and `completed_commit` names that
    merge. Walking every parent would interleave the leaves' own commits with the merges and
    destroy the one ordering this module depends on.
    """
    rc, out = repo._git_rc("rev-list", "--first-parent", "--reverse", f"{base}..{tip}")
    if rc != 0:
        raise StackError(f"cannot walk {base}..{tip} — is the base commit still in this repository?")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _commits_between(repo: repo_mod.Repo, base: str, head: str) -> tuple[str, ...]:
    """Every commit reachable from `head` but not from `base`, oldest first — what a PR would show.

    Full reachability, not first parents: the first-parent walk finds *boundaries*, but a slice's
    content is everything the pull request would list, and each task's own work sits on the second
    parent of the merge that landed it.
    """
    rc, out = repo._git_rc("rev-list", "--reverse", f"{base}..{head}")
    if rc != 0:
        return ()
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def has_diff(repo: repo_mod.Repo, base: str, head: str) -> bool:
    """Whether `base..head` changes any file. Commits alone are not a change.

    A `--restack` merges each slice forward, and those merge commits are reachable from the work
    branch and not from the slice below — so a purely commit-counting reading would find a trailing
    slice there. It carries no diff, and a pull request with no diff is not a pull request.
    """
    rc, _ = repo._git_rc("diff", "--quiet", base, head)
    return rc != 0


def is_ancestor(repo: repo_mod.Repo, ancestor: str, descendant: str) -> bool:
    """Whether `ancestor` is reachable from `descendant`. False when either does not resolve."""
    if not ancestor or not descendant:
        return False
    rc, _ = repo._git_rc("merge-base", "--is-ancestor", ancestor, descendant)
    return rc == 0


def conflicts_with(repo: repo_mod.Repo, ours: str, theirs: str) -> bool:
    """Whether merging `theirs` into `ours` would conflict, without touching the working tree.

    Used only to warn: a base that has moved is normal and its pull-request diffs stay correct
    (GitHub reads them from the merge base). What is *not* normal is a base that can no longer be
    merged, and the remedy for that — merge `main` into the work branch, regenerate the review,
    take gate ④ again — is a human's call, so this reports and never acts.
    """
    if not ours or not theirs:
        return False
    rc, out = repo._git_rc("merge-tree", "--write-tree", "--name-only", ours, theirs)
    if rc == 0:
        return False
    # `--write-tree` exits 1 for a conflicted merge and >1 for a failure to even try; only the
    # former is an answer to the question asked. An older git without `--write-tree` lands here too,
    # and reporting "no conflict" is the honest degradation: the warning is a courtesy, not a gate.
    return rc == 1 and bool(out.strip())


# --- the ledger ---------------------------------------------------------------


def ledger(events: Iterable[models.Event]) -> list[LedgerRecord]:
    """The slices whose pull requests have been opened, in the order they were opened.

    Read from the audit log rather than asked of GitHub: the log is the record this repository
    already keeps, it answers the same way offline, and a later `ready` for the same slice updates
    the record rather than adding a second one.
    """
    found: dict[int, LedgerRecord] = {}
    for event in events:
        if event.event != LEDGER_EVENT:
            continue
        detail = event.detail
        action = detail.get(LEDGER_KEY)
        if action not in (LEDGER_OPENED, LEDGER_READY):
            continue
        index = detail.get("index")
        if not isinstance(index, int):
            continue
        previous = found.get(index)
        found[index] = LedgerRecord(
            index=index,
            task_id=str(detail.get("task_id", "") or (previous.task_id if previous else "")),
            branch=str(detail.get("branch", "") or (previous.branch if previous else "")),
            base_ref=str(detail.get("base_ref", "") or (previous.base_ref if previous else "")),
            head=str(detail.get("head", "") or (previous.head if previous else "")),
            url=str(detail.get("url", "") or (previous.url if previous else "")),
            ready=action == LEDGER_READY or bool(previous and previous.ready),
        )
    return [found[i] for i in sorted(found)]


def opened_event_detail(slice_: Slice, url: str, action: str = LEDGER_OPENED) -> dict[str, object]:
    """The `detail` mapping one pull-request action is recorded with. One writer, one reader."""
    return {
        LEDGER_KEY: action,
        "index": slice_.index,
        "task_id": slice_.task_id,
        "branch": slice_.branch,
        "base_ref": slice_.base_ref,
        "head": slice_.head_sha,
        "url": url,
    }


# --- derivation ---------------------------------------------------------------


def branch_name(work_branch: str, index: int, task_id: str) -> str:
    return f"{work_branch}{BRANCH_INFIX}{index:02d}-{task_id or TAIL_SUFFIX}"


def _landing_order_problems(graph: dag.Graph, landed: Sequence[str]) -> list[str]:
    """Whether `landed` is a topological order of the tasks in it.

    Not a comparison against :meth:`dag.Graph._topo_order`: that is *one* topological order, with
    ties broken by id, and the loop is free to land an equally-valid different one. The invariant
    that actually has to hold is the one the DAG states — a task lands after every dependency of
    its that landed. Checking the stronger thing would fail runs that were correct.
    """
    position = {task_id: i for i, task_id in enumerate(landed)}
    problems: list[str] = []
    for task_id in landed:
        task = graph.get(task_id)
        for dep in task.blocked_by:
            if dep in position and position[dep] > position[task_id]:
                problems.append(f"{task_id} landed before {dep}, which it is blocked by")
    return problems


def derive(repo: repo_mod.Repo, docs: Documents, *, base: str = "main") -> list[Slice]:
    """The stack: one slice per task that landed, plus a tail for whatever landed outside a task.

    Slices whose pull request is already open are restored from the ledger and **not recomputed** —
    their boundary is fixed and their head is whatever their branch now points at. Everything past
    the last opened slice is cut fresh out of the work branch's first-parent history.
    """
    plan, state, config, events = docs.plan, docs.state, docs.config, docs.events
    work_branch = config.work_branch
    if not work_branch:
        raise StackError("config.yaml names no project.work_branch — there is no branch to slice")
    tip = _rev_parse(repo, work_branch)
    if not tip:
        raise StackError(f"the work branch {work_branch} does not resolve to a commit in this repository")
    base_commit = plan.base_commit
    if not _rev_parse(repo, base_commit):
        raise StackError(
            f"plan.yaml's cycle.base_commit ({base_commit[:12] or '(empty)'}) is not a commit in this "
            "repository — the stack has no floor to stand on"
        )

    graph = dag.join(plan, state)
    commit_of = status_api.completed_commits(state)
    done = {t.id for t in graph.tasks if t.is_done}
    landed_commits = {commit_of[t]: t for t in sorted(done) if commit_of.get(t)}

    records = ledger(events)
    slices: list[Slice] = []
    previous_branch = base
    previous_sha = _rev_parse(repo, base) or base_commit

    for record in records:
        head = _rev_parse(repo, record.branch)
        if not head:
            raise StackError(
                f"slice {record.index:02d} has an open pull request on {record.branch}, but that branch is "
                "not in this repository — fetch it, or the stack cannot be read"
            )
        task = plan.task(record.task_id) if record.task_id else None
        slices.append(
            Slice(
                index=record.index,
                task_id=record.task_id,
                title=task.title if task is not None else "(commits outside any task)",
                branch=record.branch,
                base_ref=previous_branch,
                base_sha=previous_sha,
                head_sha=head,
                commits=_commits_between(repo, previous_sha, head),
                opened=True,
                opened_at=record.head,
            )
        )
        previous_branch, previous_sha = record.branch, head

    # Where the fresh cut starts: after the last *recorded* head, which is a point on the work
    # branch's first-parent chain. The branch may have moved past it since (a review fix), and that
    # extra work belongs to the slice it was committed onto, not to a new one.
    chain = _first_parent(repo, base_commit, tip)
    frontier = records[-1].head if records else ""
    if frontier:
        if frontier not in chain:
            raise StackError(
                f"slice {records[-1].index:02d} was opened at {frontier[:12]}, which is no longer on "
                f"{work_branch}'s first-parent history — the history was rewritten"
            )
        chain = chain[chain.index(frontier) + 1 :]

    consumed = {c for s in slices for c in s.commits}
    missing = sorted(t for t in done if commit_of.get(t) and commit_of[t] not in chain and commit_of[t] not in consumed)
    if missing:
        raise StackError(
            "these task(s) are done but the commit that landed them is not on the work branch: "
            f"{', '.join(missing)} — the history was rewritten, or the state was hand-edited"
        )

    index = (records[-1].index if records else 0) + 1
    segment: list[str] = []
    fresh_order: list[str] = []
    for commit in chain:
        segment.append(commit)
        task_id = landed_commits.get(commit)
        if task_id is None:
            continue
        fresh_order.append(task_id)
        task = plan.task(task_id)
        slices.append(
            Slice(
                index=index,
                task_id=task_id,
                title=task.title if task is not None else task_id,
                branch=branch_name(work_branch, index, task_id),
                base_ref=previous_branch,
                base_sha=previous_sha,
                head_sha=commit,
                commits=_commits_between(repo, previous_sha, commit),
            )
        )
        previous_branch, previous_sha = slices[-1].branch, commit
        index += 1
        segment = []

    problems = _landing_order_problems(graph, [s.task_id for s in slices if s.task_id])
    if problems:
        raise StackError(
            "the order tasks landed in is not a topological order of the plan's DAG: " + "; ".join(problems)
        )

    if segment and has_diff(repo, previous_sha, segment[-1]):
        slices.append(
            Slice(
                index=index,
                task_id="",
                title="commits outside any task",
                branch=branch_name(work_branch, index, ""),
                base_ref=previous_branch,
                base_sha=previous_sha,
                head_sha=segment[-1],
                commits=_commits_between(repo, previous_sha, segment[-1]),
            )
        )
    return slices


# --- preconditions ------------------------------------------------------------

MODES = ("push", "restack", "ready")


def preconditions(
    repo: repo_mod.Repo, docs: Documents, slices: Sequence[Slice], *, mode: str, base: str = "main"
) -> Preconditions:
    """What has to hold before `mode` may run. Errors stop the run; warnings are printed and passed.

    The gate-④ split is the load-bearing one. `--push` and `--restack` are how a change gets *to*
    a reviewer, so they belong to the window before the gate opens; `--ready` is what says a human
    approved, so it may not run before one has. Changing code after gate ④ is approved is rewinding
    an approval, which is a human's privilege (AGENTS.md) — hence `--restack` refuses there rather
    than quietly moving approved commits around.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r} — one of {', '.join(MODES)}")
    state, review, config, defects = docs.state, docs.review, docs.config, docs.defects
    errors: list[str] = []
    warnings: list[str] = []

    if defects:
        errors.append(
            f"the audit chain has {len(defects)} defect(s) — nothing may be published off a log that "
            "cannot be verified (`rein doctor` lists them)"
        )

    approved = state.gate_status("build") == "approved"
    if mode == "ready" and not approved:
        errors.append(
            "gate ④ (build) is not approved — a slice may not leave draft before the grounded review "
            "a human signed off on. Present it with `rein review` and let a human run `rein approve build`."
        )
    if mode == "restack" and approved:
        errors.append(
            "gate ④ (build) is already approved — propagating new commits now would change what that "
            "approval covers. Rewinding an approval is a human's call: `rein revise --to build`."
        )
    if mode == "push" and approved:
        warnings.append(
            "gate ④ (build) is already approved — these pull requests still open as drafts. "
            "Run `rein pr-stack --ready` straight after to lift them."
        )

    if mode in ("push", "ready") and not any(s.task_id for s in slices):
        errors.append("no task has landed on the work branch yet — there is nothing to slice")

    if mode == "ready":
        head = _rev_parse(repo, config.work_branch)
        subject = review.subject_head_sha if review is not None else ""
        if not subject:
            errors.append("no machine review has been generated — there is nothing binding these slices")
        elif subject != head:
            errors.append(
                f"the review was generated against {subject[:12]} but the work branch is at {head[:12]} — "
                "something landed after the review. Regenerate it with `rein review generate`."
            )
        stranded = [s.label for s in slices if not is_ancestor(repo, s.head_sha, head)]
        if stranded:
            errors.append(
                f"slice(s) {', '.join(stranded)} are not contained in the work branch — run "
                "`rein pr-stack --restack` so what was reviewed is what the pull requests hold"
            )
        unopened = [s.label for s in slices if not s.opened]
        if unopened:
            errors.append(f"slice(s) {', '.join(unopened)} have no pull request yet — run `rein pr-stack --push` first")

    base_sha = _rev_parse(repo, base)
    tip = _rev_parse(repo, config.work_branch)
    if base_sha and tip and not is_ancestor(repo, base_sha, tip):
        if conflicts_with(repo, tip, base_sha):
            warnings.append(
                f"{base} has moved and no longer merges cleanly into {config.work_branch}. Do not rebase: "
                f"`git merge {base}` on the work branch, regenerate the review, and take gate ④ again."
            )
        else:
            warnings.append(f"{base} has moved since this cycle branched — the pull requests will merge on top of it")
    return Preconditions(errors=errors, warnings=warnings)


# --- materialising the refs ---------------------------------------------------


def _git_write(repo: repo_mod.Repo, *args: str) -> tuple[int, str]:
    """The one place this module *changes* the repository: `git branch -f`, and nothing else.

    `Repo._git` says read-only and means it, so writing through it would make that docstring a lie
    for every other caller. Kept to its own function for the same reason `build_git` keeps every
    git call in one class: a grep for what mutates the repository has to have one answer.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo.root), *args],
        capture_output=True,
        text=True,
        timeout=repo_mod.GIT_TIMEOUT_SEC,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


@dataclass(frozen=True)
class Materialized:
    """What one `materialize` run did, per slice — so the caller can say it rather than guess."""

    created: tuple[str, ...] = ()
    advanced: tuple[str, ...] = ()
    unchanged: tuple[str, ...] = ()

    @property
    def touched(self) -> tuple[str, ...]:
        return self.created + self.advanced


def materialize(repo: repo_mod.Repo, slices: Sequence[Slice], *, dry_run: bool = False) -> Materialized:
    """Point each slice's branch at the commit the slice ends on. Creates no commits.

    Bottom first, so a reader watching the refs appear sees the stack build in the order it will be
    merged. Three rules, and the first two are what keep an open pull request honest:

    * A slice whose pull request is **open** may not move. Repointing it is a force-push in all but
      name, and the whole design rests on not doing that (module docstring). Normally unreachable —
      :func:`derive` freezes those slices at whatever their branch says — so reaching it means the
      repository and the audit log disagree, and guessing which is right is not this function's
      business.
    * A branch that already exists may only **fast-forward**. Anything else would drop commits that
      are on the branch and not on the new head, which is the same loss by a quieter route.
    * Nothing is created for a slice already pointing where it should. Re-running is free.
    """
    created: list[str] = []
    advanced: list[str] = []
    unchanged: list[str] = []
    for slice_ in slices:
        current = _rev_parse(repo, slice_.branch)
        if current == slice_.head_sha:
            unchanged.append(slice_.branch)
            continue
        if current and slice_.opened:
            raise StackError(
                f"{slice_.branch} has an open pull request and points at {current[:12]}, but the stack "
                f"says {slice_.head_sha[:12]}. Moving it would be a force-push onto a pull request "
                "somebody may already be reading — reconcile the repository and the audit log first."
            )
        if current and not is_ancestor(repo, current, slice_.head_sha):
            raise StackError(
                f"{slice_.branch} points at {current[:12]}, which is not an ancestor of "
                f"{slice_.head_sha[:12]} — moving it there would drop commits the branch already has"
            )
        if dry_run:
            (created if not current else advanced).append(slice_.branch)
            continue
        rc, out = _git_write(repo, "branch", "-f", slice_.branch, slice_.head_sha)
        if rc != 0:
            raise StackError(f"could not point {slice_.branch} at {slice_.head_sha[:12]}: {out}")
        (created if not current else advanced).append(slice_.branch)
    return Materialized(tuple(created), tuple(advanced), tuple(unchanged))


# --- pull-request bodies ------------------------------------------------------


def _subjects(repo: repo_mod.Repo, slice_: Slice) -> list[str]:
    """`<short sha> <subject>` for each commit the slice carries, oldest first."""
    rc, out = repo._git_rc("log", "--reverse", "--format=%h %s", f"{slice_.base_sha}..{slice_.head_sha}")
    if rc != 0:
        return []
    return [line.rstrip() for line in out.splitlines() if line.strip()]


def _axis(result: Mapping[str, object], name: str) -> str:
    value = result.get(name)
    return str(value.get("status", "unknown")) if isinstance(value, dict) else "unknown"


def _claim_lines(docs: Documents, slice_: Slice, *, approved: bool) -> list[str]:
    """What this slice was for, and — once the gate is open — what the review made of it.

    The three axes are printed separately and never collapsed into one word. A claim has no single
    `verified`: integrity, semantic support and conformance are decided apart, and rendering them
    as one verdict is the precise misreading gate ④ exists to prevent.
    """
    task = docs.plan.task(slice_.task_id) if slice_.task_id else None
    if task is None:
        return ["- (no task — these commits belong to none, so there is no claim to answer)"]
    if not task.claim_ids:
        return ["- (this task answers no claim)"]
    verdicts = {
        str(r.get("claim_id")): r
        for r in (docs.review.claim_results if docs.review is not None and docs.review.is_generated else ())
    }
    lines: list[str] = []
    for claim_id in task.claim_ids:
        claim = docs.plan.claim(claim_id)
        statement = claim.statement if claim is not None else "(no such claim in the plan)"
        lines.append(f"- **{claim_id}** — {statement}")
        if not approved:
            continue
        result = verdicts.get(claim_id)
        if result is None:
            lines.append("  - verdict: **not reviewed** — the machine review says nothing about this claim")
            continue
        lines.append(
            f"  - verdict: **{result.get('verdict', 'unknown')}** — integrity {_axis(result, 'integrity')} / "
            f"semantic support {_axis(result, 'semantic_support')} / conformance {_axis(result, 'conformance')}"
        )
    return lines


def _acceptance_lines(docs: Documents, slice_: Slice) -> list[str]:
    """This task's own bar, and for the criteria the loop cannot establish, whether anybody saw it."""
    task = docs.plan.task(slice_.task_id) if slice_.task_id else None
    if task is None or not task.acceptance:
        return ["- (none declared — the criterion is the gate ④ review itself)"]
    recorded = {str(item.get("id")): item for item in docs.state.recorded_acceptance(slice_.task_id)}
    lines: list[str] = []
    for criterion in task.acceptance:
        ac_id = str(criterion.get("id", "?"))
        spec = criterion.get("evidence")
        kind = str(spec.get("kind", "")) if isinstance(spec, dict) else ""
        how = f"`{kind}`" if kind else "prose only — left to the gate ④ review"
        lines.append(f"- **{ac_id}** — {criterion.get('statement', '')} · {how}")
        if kind != "external":
            continue
        observation = recorded.get(ac_id)
        if observation is None:
            lines.append("  - **awaiting evidence** — nobody has recorded an observation yet")
        else:
            tree = str(observation.get("tree", ""))[:12]
            lines.append(f"  - recorded against tree `{tree}`: {observation.get('note', '')}")
    return lines


def _stack_table(slices: Sequence[Slice], current: int, *, base: str) -> list[str]:
    lines = ["| # | slice | branch | base | state |", "|---|---|---|---|---|"]
    for position, s in enumerate(slices):
        state = "open" if s.opened else "not pushed"
        here = " ← **this one**" if position == current else ""
        lines.append(f"| {s.index:02d} | {s.label} | `{s.branch}` | `{s.base_ref or base}` | {state}{here} |")
    return lines


def slice_body(
    repo: repo_mod.Repo, docs: Documents, slices: Sequence[Slice], current: int, *, base: str = "main"
) -> str:
    """One slice's pull-request body.

    The opening banner is the load-bearing part, and it says a different thing on each side of
    gate ④. Before the gate: this has not been reviewed, which is why it is a draft. After it: the
    review ran **once, over the whole stack** — so a reader of this pull request alone must be told
    that this slice was never judged on its own, and what the review that covers it was bound to.

    The cycle-wide figures come in only once the gate is open. Printing digests off an unapproved
    review would dress a draft in the authority of a finished one.
    """
    slice_ = slices[current]
    approved = docs.state.gate_status("build") == "approved"
    heading = f"{slice_.label} — {slice_.title}" if slice_.task_id else slice_.title
    lines = [f"## {heading}", ""]

    position = f"Slice {current + 1} of {len(slices)} of cycle `{docs.state.cycle_id}`"
    if approved:
        binding = docs.review.machine.get("binding") if docs.review and docs.review.is_generated else None
        change = binding.get("change_digest") if isinstance(binding, dict) else None
        head = binding.get("subject_head_sha") if isinstance(binding, dict) else None
        lines += [
            f"> {position}. The grounded review ran **once, over the whole stack** "
            f"(change digest `{change or 'not recorded'}`, head `{str(head or '')[:12] or 'not recorded'}`).",
            "> **This slice was not reviewed on its own.**",
        ]
    else:
        lines += [
            f"> ⚠️ **Draft.** {position}. The grounded review has not run over it yet (gate ④: "
            f"{docs.state.gate_status('build')}).",
            "> It leaves draft when a human approves that review — not before.",
        ]

    lines += ["", "### What this slice is for", ""]
    lines += _claim_lines(docs, slice_, approved=approved)
    lines += ["", "### Acceptance", ""]
    lines += _acceptance_lines(docs, slice_)

    subjects = _subjects(repo, slice_)
    lines += ["", f"### Commits ({len(subjects)})", ""]
    lines += [f"- `{line}`" for line in subjects] or ["- (none)"]

    lines += ["", "### The stack", ""]
    lines += _stack_table(slices, current, base=base)
    lines += [
        "",
        "Merge bottom first with `gh pr merge --merge --delete-branch`. Never squash and never "
        "rebase-merge: either puts content into the base under a different commit, and every "
        "pull request above this one would then show this diff again. Deleting the branch is what "
        "retargets the next one.",
    ]

    if approved:
        lines += ["", "### Cycle facts", ""]
        lines += pr_draft.cycle_facts(docs.state, docs.plan, docs.review, docs.events, docs.defects, base=base)
    return "\n".join(lines) + "\n"


# --- restacking ---------------------------------------------------------------

#: Where the propagation happens for every branch that is not checked out anywhere. The work
#: branch usually *is* checked out, in the canonical checkout, and git refuses the same branch in
#: two worktrees — so that hop is merged at the root, exactly where `build_git.merge_leaf` already
#: merges every leaf.
RESTACK_WORKTREE = "_restack"


@dataclass(frozen=True)
class Propagation:
    """One `--restack` run: what reached where, and what stopped it."""

    merged: tuple[str, ...] = ()
    resolved: tuple[str, ...] = ()
    stopped_at: str = ""
    resolution: conflict.Resolution | None = None

    @property
    def ok(self) -> bool:
        return not self.stopped_at


def _worktree(repo: repo_mod.Repo, config: models.Config, branch: str) -> str:
    """A throwaway worktree with `branch` checked out. Removed by :func:`_drop_worktree`."""
    path = repo.path(config.worktree_dir) / RESTACK_WORKTREE
    _git_write(repo, "worktree", "remove", "--force", str(path))
    _git_write(repo, "worktree", "prune")
    rc, out = _git_write(repo, "worktree", "add", "--force", str(path), branch)
    if rc != 0:
        raise StackError(f"could not create the restack worktree at {path}: {out}")
    return str(path)


def _drop_worktree(repo: repo_mod.Repo, path: str) -> None:
    _git_write(repo, "worktree", "remove", "--force", path)
    _git_write(repo, "worktree", "prune")


def restack(
    repo: repo_mod.Repo,
    docs: Documents,
    slices: Sequence[Slice],
    *,
    implement: conflict.Implementer,
    quality_gate: conflict.QualityGate,
    run: Callable[..., tuple[int, str]] = common.run,
) -> Propagation:
    """Carry each slice's fixes up into the one above it, and finally into the work branch.

    A fix for a review finding is committed onto the slice that introduced the code — that is the
    whole point of freezing slice boundaries — and the slices above it then do not have it. This
    walks the chain bottom-up with `git merge`, so every commit stays where it is: no slice below
    is rewritten, so no already-open pull request is force-pushed.

    A conflict here is classified before it is resolved (`conflict`), and only the mechanical kind
    is carried through. The other kinds stop the walk, because a stack propagated past a
    disagreement between two frozen intentions would bury the disagreement in the merge.
    """
    # Only branches that exist: a slice nobody materialised has nothing to propagate, and asking
    # git to check it out would report a missing ref where the real answer is "not yet a stack".
    chain = [*(s.branch for s in slices if _rev_parse(repo, s.branch)), docs.config.work_branch]
    if len(chain) < 2:
        return Propagation()
    merged: list[str] = []
    resolved: list[str] = []
    path = _worktree(repo, docs.config, chain[0])
    try:
        for position in range(1, len(chain)):
            source, target = chain[position - 1], chain[position]
            cwd = _checkout_for(repo, target, path, run)
            resolution = conflict.merge_with_resolution(
                docs.plan,
                cwd=cwd,
                source_ref=source,
                ours_task=_task_of(slices, target),
                theirs_task=_task_of(slices, source),
                implement=implement,
                quality_gate=quality_gate,
                run=run,
            )
            if not resolution.merged:
                return Propagation(tuple(merged), tuple(resolved), stopped_at=target, resolution=resolution)
            if resolution.kind == "mechanical":
                resolved.append(target)
            if resolution.kind != "noop":
                merged.append(target)
                print(f"  {source} → {target}: {resolution.kind}")
    finally:
        _drop_worktree(repo, path)
    return Propagation(tuple(merged), tuple(resolved))


def _checkout_for(repo: repo_mod.Repo, branch: str, worktree: str, run: Callable[..., tuple[int, str]]) -> str:
    """Where `branch` can be merged into: the worktree that already holds it, else the throwaway one.

    git refuses to check a branch out in two worktrees at once, and the work branch is normally
    checked out in the canonical checkout — so merging into it happens there, which is also where
    the build loop merges every leaf.
    """
    for root in _worktree_heads(repo, run):
        if root[1] == branch:
            return root[0]
    rc, out = run(["git", "switch", branch], cwd=worktree)
    if rc != 0:
        raise StackError(f"could not check out {branch} in the restack worktree: {out}")
    return worktree


def _worktree_heads(repo: repo_mod.Repo, run: Callable[..., tuple[int, str]]) -> list[tuple[str, str]]:
    """`(path, branch)` for every worktree that has a branch checked out."""
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


def _task_of(slices: Sequence[Slice], branch: str) -> str:
    return next((s.task_id for s in slices if s.branch == branch), "")


def propagation_gaps(repo: repo_mod.Repo, docs: Documents, slices: Sequence[Slice]) -> list[str]:
    """Slices the work branch does not contain — what a finished `--restack` must leave empty."""
    tip = _rev_parse(repo, docs.config.work_branch)
    return [s.label for s in slices if not is_ancestor(repo, _rev_parse(repo, s.branch) or s.head_sha, tip)]


# --- publishing ---------------------------------------------------------------

#: Where the bodies are written. One file per slice, named so the stack's order is the sort order.
OUT_DIR = ".rein/pr-stack"

#: How long one `git push` or `gh` call may take before it is killed. Generous — a push over a slow
#: link is normal — but finite, because a stack half-published by a hung command is the worst state.
NETWORK_TIMEOUT_SEC = 300

MERGE_NOTE = (
    "Merge bottom first:\n"
    "  gh pr merge <url> --merge --delete-branch\n"
    "Never --squash and never --rebase: either lands this content in the base as a different\n"
    "commit, and every pull request above it then shows this diff again. --delete-branch is not\n"
    "optional either — deleting the base branch is what retargets the next pull request at main."
)


class PublishError(RuntimeError):
    """Publishing stopped part-way. The message says how far it got; nothing is retried silently."""


def write_bodies(repo: repo_mod.Repo, docs: Documents, slices: Sequence[Slice], *, base: str) -> list[str]:
    """One body per slice under :data:`OUT_DIR`. Returns the paths, in stack order."""
    out = repo.path(OUT_DIR)
    out.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for position, slice_ in enumerate(slices):
        path = out / f"{slice_.index:02d}-{slice_.label}.md"
        path.write_text(slice_body(repo, docs, slices, position, base=base), encoding="utf-8")
        written.append(f"{OUT_DIR}/{path.name}")
    return written


def create_command(slice_: Slice, body_path: str) -> list[str]:
    """The `gh` invocation that opens this slice's pull request — **always as a draft**.

    One function so the printed line and the executed argv cannot diverge: a human who copies what
    was printed must get exactly what `--push` would have run.
    """
    return [
        "gh",
        "pr",
        "create",
        "--draft",
        "--base",
        slice_.base_ref,
        "--head",
        slice_.branch,
        "--title",
        f"{slice_.label}: {slice_.title}",
        "--body-file",
        body_path,
    ]


def _confirm_push(slices: Sequence[Slice], remote: str, *, base: str) -> None:
    """The pause before anything leaves this machine. Same rule as a gate approval, different subject.

    A terminal is required and there is no flag that skips it, the default is no, and what is on
    screen is specific: every branch, its base, and the fact that all of them open as drafts. The
    gate rule this sits beside is `doctor.GATE_OPENING_VERBS` — `rein pr-stack` may not be
    pre-authorized, so this prompt cannot be configured away.
    """
    if not common.stdin_is_terminal():
        raise PublishError(
            "pushing and opening pull requests needs a confirmation typed at a terminal, and stdin "
            "is not one. Run this in your shell — there is deliberately no flag that skips it."
        )
    print(f"This will push {len(slices)} branch(es) to '{remote}' and open {len(slices)} DRAFT pull request(s):\n")
    print(render(slices, base=base))
    print(
        "\nThey open as drafts because the grounded review has not approved them yet. "
        "`rein pr-stack --ready` lifts them once gate ④ is open."
    )
    if not common.ask_yes_no(f"Push and open {len(slices)} draft pull request(s)?"):
        raise PublishError("nothing was pushed.")


def publish(
    repo: repo_mod.Repo,
    docs: Documents,
    slices: Sequence[Slice],
    body_paths: Sequence[str],
    *,
    remote: str,
    run: Callable[..., tuple[int, str]] = common.run,
) -> list[str]:
    """Push each branch and open its draft pull request, bottom first. Returns the URLs.

    **Stops at the first failure.** A stack is an ordered thing: carrying on past a branch that did
    not push would open a pull request based on a branch the remote does not have. What is already
    published stays published and is named in the error — half a stack that says so is recoverable,
    half a stack that pretends to be whole is not.

    Each pull request is recorded in the audit log as it is created, one transaction each, so an
    interrupted run leaves a log that matches what actually exists on the remote.
    """
    urls: list[str] = []
    for slice_, body_path in zip(slices, body_paths, strict=True):
        rc, out = run(["git", "push", "-u", remote, slice_.branch], cwd=str(repo.root), timeout=NETWORK_TIMEOUT_SEC)
        if rc != 0:
            raise PublishError(_stopped_at(slice_, urls, f"pushing {slice_.branch} failed: {out}"))
        rc, out = run(create_command(slice_, body_path), cwd=str(repo.root), timeout=NETWORK_TIMEOUT_SEC)
        if rc != 0:
            raise PublishError(_stopped_at(slice_, urls, f"opening the pull request for {slice_.branch} failed: {out}"))
        url = out.strip().splitlines()[-1].strip() if out.strip() else ""
        record(repo, docs, slice_, url, LEDGER_OPENED)
        urls.append(url)
        print(f"  {slice_.index:02d} {slice_.label}: {url or '(gh printed no url)'}")
    return urls


def _confirm_ready(docs: Documents, slices: Sequence[Slice], records: Sequence[LedgerRecord]) -> None:
    """The pause before draft pull requests become ready. Same discipline as :func:`_confirm_push`.

    What is specific on this screen is the receipt: lifting a draft is the act that says *a human
    approved this*, so the approval id and the digests it covers are what the person answering has
    to be looking at. If those are not what they approved, the answer is no.
    """
    if not common.stdin_is_terminal():
        raise PublishError(
            "lifting pull requests out of draft needs a confirmation typed at a terminal, and stdin "
            "is not one. Run this in your shell — there is deliberately no flag that skips it."
        )
    receipt = docs.state.gate_receipt("build") or {}
    print(f"gate ④ (build) is approved: {receipt.get('approval_id', '(no approval id)')}\n")
    print("That approval covers:")
    for key in ("validation_digest", "attested_chain_root", "result_chain_root"):
        print(f"  {key.ljust(20)}  {receipt.get(key, '(not recorded)')}")
    print(f"\n{len(records)} draft pull request(s) would become ready for review:")
    for record_ in records:
        print(f"  {record_.index:02d} {record_.task_id or TAIL_SUFFIX}: {record_.url or record_.branch}")
    print("\nEach body is rewritten first, so it states what the review found rather than that it is still pending.")
    if not common.ask_yes_no(f"Lift {len(records)} pull request(s) out of draft?"):
        raise PublishError("nothing was lifted; the pull requests are still drafts.")


def lift(
    repo: repo_mod.Repo,
    docs: Documents,
    slices: Sequence[Slice],
    body_paths: Sequence[str],
    *,
    run: Callable[..., tuple[int, str]] = common.run,
) -> list[str]:
    """Rewrite each body with the approved facts, then take the pull request out of draft.

    Bottom first, and the body goes first for each one: a pull request that turned ready while its
    body still said "the grounded review has not run over it yet" would be contradicting itself at
    the moment a reviewer arrives. Stops at the first failure for the reason :func:`publish` does.
    """
    lifted: list[str] = []
    records = {r.index: r for r in ledger(docs.events)}
    for slice_, body_path in zip(slices, body_paths, strict=True):
        record_ = records.get(slice_.index)
        if record_ is None or not record_.url:
            raise PublishError(
                _stopped_at(slice_, lifted, f"slice {slice_.index:02d} has no recorded pull request", state="ready")
            )
        if record_.ready:
            continue
        rc, out = run(
            ["gh", "pr", "edit", record_.url, "--body-file", body_path], cwd=str(repo.root), timeout=NETWORK_TIMEOUT_SEC
        )
        if rc != 0:
            raise PublishError(
                _stopped_at(slice_, lifted, f"rewriting the body of {record_.url} failed: {out}", state="ready")
            )
        rc, out = run(["gh", "pr", "ready", record_.url], cwd=str(repo.root), timeout=NETWORK_TIMEOUT_SEC)
        if rc != 0:
            raise PublishError(
                _stopped_at(slice_, lifted, f"lifting {record_.url} out of draft failed: {out}", state="ready")
            )
        record(repo, docs, slice_, record_.url, LEDGER_READY)
        lifted.append(record_.url)
        print(f"  {slice_.index:02d} {slice_.label}: ready — {record_.url}")
    return lifted


def _stopped_at(slice_: Slice, done: Sequence[str], why: str, *, state: str = "open") -> str:
    already = f"{len(done)} pull request(s) are already {state} and stay {state}" if done else "nothing changed"
    return f"{why}\n  stopped at slice {slice_.index:02d} ({slice_.label}); {already}"


def record(repo: repo_mod.Repo, docs: Documents, slice_: Slice, url: str, action: str) -> None:
    """Write one pull-request action into the audit log — the ledger :func:`derive` reads back."""
    with store_mod.Store(repo).transaction() as tx:
        tx.append(
            LEDGER_EVENT,
            cycle_id=docs.state.cycle_id,
            actor="local-confirmation",
            subject_ids=[slice_.task_id] if slice_.task_id else [],
            detail=opened_event_detail(slice_, url, action),
        )


def render(slices: Sequence[Slice], *, base: str = "main") -> str:
    """The stack as a human reads it: bottom first, each line naming what it is based on."""
    if not slices:
        return "(no slices — nothing has landed on the work branch yet)"
    width = max(len(s.branch) for s in slices)
    lines = [f"stack of {len(slices)} slice(s), bottom first — base: {base}", ""]
    for s in slices:
        state = "open" if s.opened else "not pushed"
        lines.append(
            f"  {s.index:02d}  {s.branch:<{width}}  → {s.base_ref}   "
            f"[{state}] {len(s.commits)} commit(s)  {s.label}: {s.title}"
        )
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------------


def _report(result: Preconditions) -> bool:
    for warning in result.warnings:
        logger.warning(warning)
    for problem in result.errors:
        logger.error(problem)
    return result.ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="cut the work branch into one draft pull request per task (stacked)",
        epilog="Push and pull-request creation are outward-facing: --push confirms at a terminal first.",
    )
    parser.add_argument("--base", default="main", help="the branch the bottom of the stack targets (default: main)")
    parser.add_argument("--remote", default="origin", help="the remote to push to (default: origin)")
    parser.add_argument(
        "--push",
        action="store_true",
        help="after a confirmation typed at a terminal, push the branches and open the draft pull requests",
    )
    parser.add_argument(
        "--ready",
        action="store_true",
        help="once gate ④ is approved: rewrite each body and lift the drafts (confirms at a terminal)",
    )
    parser.add_argument(
        "--restack",
        action="store_true",
        help="carry each slice's review fixes up into the slices above it and into the work branch (by merge)",
    )
    parser.add_argument("--dry-run", action="store_true", help="derive and report; touch neither refs nor files")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    chosen = [name for name, on in (("--push", args.push), ("--ready", args.ready), ("--restack", args.restack)) if on]
    if len(chosen) > 1:
        logger.error(f"{' and '.join(chosen)} are separate steps with a human between them, not one flag")
        return 2
    mode = "ready" if args.ready else "restack" if args.restack else "push"

    try:
        docs = Documents.read(repo)
        slices = derive(repo, docs, base=args.base)
        if not _report(preconditions(repo, docs, slices, mode=mode, base=args.base)):
            return 2
        # Neither `--ready` nor `--restack` repoints a ref. The pull requests they touch are open
        # on the branches as they stand, and moving one now would be a force-push under a reviewer;
        # `--restack` moves them forward by merging, which is a commit, not a repoint.
        skip = args.ready or args.restack
        outcome = Materialized() if skip else materialize(repo, slices, dry_run=args.dry_run)
    except (StackError, dag.DagError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 2

    if not slices:
        print("no task has landed on the work branch yet — nothing to stack")
        return 0

    print(render(slices, base=args.base))
    if args.dry_run:
        print(f"\ndry run: would create {len(outcome.created)} branch(es), advance {len(outcome.advanced)}")
        return 0

    if args.restack:
        try:
            return _run_restack(repo, docs, slices)
        except (StackError, store_mod.StoreError) as exc:
            logger.error(str(exc))
            return 2

    try:
        bodies = write_bodies(repo, docs, slices, base=args.base)
    except (StackError, OSError) as exc:
        logger.error(f"could not write the pull-request bodies: {exc}")
        return 2

    if args.ready:
        try:
            _confirm_ready(docs, slices, ledger(docs.events))
            lifted = lift(repo, docs, slices, bodies)
        except (PublishError, store_mod.StoreError) as exc:
            logger.error(str(exc))
            return 2
        print(f"\n{len(lifted)} pull request(s) are ready for review.\n\n{MERGE_NOTE}")
        return 0

    if not args.push:
        print("\nReview the bodies, then open the stack bottom first:")
        for slice_, body_path in zip(slices, bodies, strict=True):
            print("  " + " ".join(create_command(slice_, body_path)))
        print("\n" + MERGE_NOTE)
        return 0

    try:
        _confirm_push(slices, args.remote, base=args.base)
        publish(repo, docs, slices, bodies, remote=args.remote)
    except (PublishError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 2
    print("\n" + MERGE_NOTE)
    return 0


def _run_restack(repo: repo_mod.Repo, docs: Documents, slices: Sequence[Slice]) -> int:
    """`--restack`, with a live control plane so the implementer can report an outcome at all.

    Imported here rather than at module scope: `build_loop` pulls in the whole execution stack, and
    every other path through this module — including the one the gate-guard hook shares a process
    with — has no use for it.
    """
    from rein import build_loop

    with build_loop.resolving(repo) as orchestrator:
        result = restack(
            repo,
            docs,
            slices,
            implement=orchestrator.resolve_conflict,
            quality_gate=orchestrator.task_gate,
        )
    if result.ok:
        gaps = propagation_gaps(repo, docs, slices)
        if gaps:
            logger.error(f"slice(s) {', '.join(gaps)} still are not in {docs.config.work_branch} — nothing propagated")
            return 2
        print(
            f"\n{len(result.merged)} branch(es) advanced ({len(result.resolved)} needed a conflict resolved).\n"
            "The work branch moved, so the machine review no longer binds it: run `rein review generate`."
        )
        return 0

    resolution = result.resolution
    assert resolution is not None
    logger.error(f"propagation stopped at {result.stopped_at}: {resolution.escalation}")
    marked = conflict.escalate(repo, resolution)
    if marked:
        logger.error(f"marked needs-revision: {', '.join(marked)} — `rein status` shows them")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
