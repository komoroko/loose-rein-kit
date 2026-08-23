"""One merge conflict, **classified before it is resolved**.

A conflict is not primarily a mechanical problem here. `scope` in the frozen plan cuts each task's
territory, and the build loop already refuses to land a diff that reached outside it — so when two
of this cycle's branches collide, the collision is itself a reading of the plan. It says one of
three things:

**Mechanical.** Both sides added to a surface that is shared by construction — a verb table, an
import block, a changelog. Each side's hunks are inside its own declared scope. This is the only
kind an agent may resolve.

**Semantic.** The two sides say incompatible things about the same behaviour. That is a defect in
the plan (the scope split, or a missing `blocked_by`), not in the merge, and resolving it in the
merge is precisely the stopgap: taking one side, or gluing both together until it compiles, hides
the fact that two frozen intentions disagree. AGENTS.md gate rule 3 already says what to do with a
problem in the plan — mark the task `needs-revision`, record the gap, raise it to a human — and
that is the *root* fix. Patching the merge is the symptom fix.

**Out of scope.** A conflicted path neither side's declared scope covers. The existing rule stands:
work that reaches into another task's territory does not land.

The classification is never the agent's word for it. `rein report --outcome` is how an implementer
states a claim, and a claim is not a verdict (AGENTS.md). `mechanical` is established only when the
merged tree passes the quality gate the caller supplies and the caller reads the exit status — the
same reason a delegated agent's textual "green" is never evidence anywhere else in this codebase.

This module holds the decision. Launching an implementer and running the gate are injected, because
the two callers that need them — the build loop and `pr-stack --restack` — already own that
plumbing, and importing it here would be a cycle.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from rein import common, event_chain, models, revise
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Every way :func:`merge_with_resolution` can end. The first three merged; the last three did not.
MERGED_KINDS = ("noop", "clean", "mechanical")
UNRESOLVED_KINDS = ("semantic", "scope-violation", "unresolved")

#: What an implementer may report back. The vocabulary `rein report --outcome` already uses.
OUTCOME_IMPLEMENTED = "implemented"

#: How many times an implementer may be asked before the conflict is escalated. One retry, because
#: a second red quality gate is evidence about the resolution rather than about a flaky attempt.
MAX_ATTEMPTS = 2


@dataclass(frozen=True)
class Conflict:
    """One collision between two of this cycle's branches, named by the tasks on either side."""

    ours_task: str
    theirs_task: str
    paths: tuple[str, ...]

    def describe(self) -> str:
        return f"{self.theirs_task or 'the incoming branch'} into {self.ours_task or 'the current branch'}"


@dataclass(frozen=True)
class Resolution:
    """What became of one merge. `kind` is the classification; nothing else stands in for it."""

    kind: str
    commit: str = ""
    conflict: Conflict | None = None
    #: What a human is told, when a human has to be told. Empty for the merged kinds.
    escalation: str = ""
    #: The quality gate's output when it decided the outcome; kept so a red one is readable.
    log: str = ""

    @property
    def merged(self) -> bool:
        return self.kind in MERGED_KINDS

    @property
    def needs_a_human(self) -> bool:
        return self.kind in UNRESOLVED_KINDS


#: Launch an implementer against a conflicted worktree. Returns what it reported.
Implementer = Callable[[Conflict, str], str]
#: Run the task-stage quality gate over a worktree. Returns `(exit status, output)`.
QualityGate = Callable[[str], tuple[int, str]]
Runner = Callable[..., tuple[int, str]]


# --- classification -----------------------------------------------------------


def out_of_scope(plan: models.Plan, conflict: Conflict) -> tuple[str, ...]:
    """The conflicted paths neither side's declared scope covers.

    An **undeclared** scope means unbounded, not empty — the reading `dag.Task.scope_include`
    already documents. So a side that declared nothing can own any path, and the question only has
    an answer when both sides declared something.
    """
    sides = [plan.task(conflict.ours_task), plan.task(conflict.theirs_task)]
    includes = [task.scope_include for task in sides if task is not None]
    if len(includes) < 2 or any(not include for include in includes):
        return ()
    return tuple(
        path for path in conflict.paths if not any(common.longest_cover(path, include) for include in includes)
    )


def _conflicted_paths(cwd: str, run: Runner) -> tuple[str, ...]:
    rc, out = run(["git", "diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    if rc != 0:
        return ()
    return tuple(line.strip() for line in out.splitlines() if line.strip())


def _has_staged_changes(cwd: str, run: Runner) -> bool:
    rc, _ = run(["git", "diff", "--cached", "--quiet"], cwd=cwd)
    return rc != 0


def _head(cwd: str, run: Runner) -> str:
    rc, out = run(["git", "rev-parse", "HEAD"], cwd=cwd)
    return out.strip() if rc == 0 else ""


def resolution_message(conflict: Conflict) -> str:
    """The merge commit's message when a conflict was resolved into it.

    Both task ids and every resolved path are named. A resolution buried inside merge glue is
    invisible to `git log` and to anyone asking later why these two lines look like that; the
    point of the commit is that the answer is in it.
    """
    listed = "\n".join(f"- {path}" for path in conflict.paths)
    return (
        f"merge {conflict.describe()}: resolved {len(conflict.paths)} conflicted path(s)\n\n"
        f"Both sides changed these, and the resolution is mechanical — each side's hunks stay\n"
        f"inside its own declared scope, and the merged tree passed the task-stage quality gate:\n"
        f"{listed}\n"
    )


# --- the merge ----------------------------------------------------------------


def merge_with_resolution(
    plan: models.Plan,
    *,
    cwd: str,
    source_ref: str,
    ours_task: str,
    theirs_task: str,
    implement: Implementer,
    quality_gate: QualityGate,
    run: Runner = common.run,
    attempts: int = MAX_ATTEMPTS,
) -> Resolution:
    """Merge `source_ref` into whatever `cwd` has checked out, resolving only what may be resolved.

    Nothing is left half-merged: every path out of here either commits or runs `git merge --abort`,
    because a worktree stuck mid-merge is a state the next run cannot tell from a deliberate one.
    """
    rc, out = run(["git", "merge", "--no-commit", "--no-ff", source_ref], cwd=cwd)
    if rc == 0:
        if not _has_staged_changes(cwd, run):
            return Resolution(kind="noop")
        message = f"merge {theirs_task or source_ref} into {ours_task or 'the current branch'}\n"
        return _commit(cwd, run, message, kind="clean")

    conflict = Conflict(ours_task, theirs_task, _conflicted_paths(cwd, run))
    if not conflict.paths:
        # `git merge` failed for something that is not a conflict at all — a dirty tree, a missing
        # ref. Reporting it as a conflict would send a human looking for a disagreement that is
        # not there.
        run(["git", "merge", "--abort"], cwd=cwd)
        return Resolution(kind="unresolved", conflict=conflict, escalation=f"git merge failed: {out}", log=out)

    outside = out_of_scope(plan, conflict)
    if outside:
        run(["git", "merge", "--abort"], cwd=cwd)
        return Resolution(
            kind="scope-violation",
            conflict=conflict,
            escalation=(
                f"{conflict.describe()} conflicts on path(s) neither task's declared scope covers: "
                f"{', '.join(outside)}. Work that reaches into another task's territory does not land — "
                "the scope split in the plan is what has to change, not this merge."
            ),
        )

    last_log = ""
    for attempt in range(1, max(1, attempts) + 1):
        outcome = implement(conflict, cwd)
        if outcome != OUTCOME_IMPLEMENTED:
            run(["git", "merge", "--abort"], cwd=cwd)
            return Resolution(
                kind="semantic",
                conflict=conflict,
                escalation=(
                    f"{conflict.describe()}: the implementer reported '{outcome}' rather than a resolution. "
                    "Two frozen intentions disagree about the same behaviour, which is a defect in the plan "
                    "(the scope split, or a missing blocked_by) — not something a merge can settle."
                ),
            )
        remaining = _conflicted_paths(cwd, run)
        if remaining:
            last_log = f"conflict markers are still in: {', '.join(remaining)}"
            logger.warning("attempt %d left %d path(s) unmerged", attempt, len(remaining))
            continue
        status, last_log = quality_gate(cwd)
        if status == 0:
            return _commit(cwd, run, resolution_message(conflict), kind="mechanical", conflict=conflict)
        logger.warning("attempt %d resolved the conflict but the quality gate exited %d", attempt, status)

    run(["git", "merge", "--abort"], cwd=cwd)
    return Resolution(
        kind="unresolved",
        conflict=conflict,
        escalation=(
            f"{conflict.describe()}: {attempts} attempt(s) produced a resolution the quality gate refused. "
            "A resolution that does not pass is not a mechanical one, whatever it was reported as."
        ),
        log=last_log,
    )


def _commit(cwd: str, run: Runner, message: str, *, kind: str, conflict: Conflict | None = None) -> Resolution:
    """Commit the staged merge.

    `--no-verify` for the reason `build_git.finalize_commit` gives: a worktree may not carry the
    repository's hooks, so the caller re-checks the branch diff rather than trusting one to have
    fired.
    """
    rc, out = run(["git", "commit", "--no-verify", "-m", message], cwd=cwd)
    if rc != 0:
        run(["git", "merge", "--abort"], cwd=cwd)
        return Resolution(
            kind="unresolved",
            conflict=conflict,
            escalation=f"the merge resolved but would not commit: {out}",
            log=out,
        )
    return Resolution(kind=kind, commit=_head(cwd, run), conflict=conflict)


# --- escalation ---------------------------------------------------------------


def escalate(repo: repo_mod.Repo, resolution: Resolution, *, category: str = "merge_conflict") -> list[str]:
    """Park the tasks a human has to look at, and record why. Returns the ids marked.

    Exactly what AGENTS.md gate rule 3 prescribes for a problem in the plan: `needs-revision` on
    the impacted closure — the dependents too, because missing one is the dangerous direction —
    plus a `knowledge_gap` event carrying the statement. One transaction, so the status change and
    the reason for it cannot exist apart.
    """
    if not resolution.needs_a_human or resolution.conflict is None:
        return []
    store = store_mod.Store(repo)
    state, plan = store.read_state(), store.read_plan()
    if state is None or plan is None:
        raise store_mod.StoreError("cannot escalate a conflict without state.yaml and plan.yaml")
    seeds = [t for t in (resolution.conflict.ours_task, resolution.conflict.theirs_task) if t]
    valid, ripple = revise.impacted_closure(plan, state, seeds)
    marked = sorted(set(valid) | set(ripple))

    raw = _with_status(state, marked, "needs-revision")
    seen = store_mod.read_digest(state)
    with store.transaction() as tx:
        if marked:
            tx.write("state", raw, expect_digest=seen)
        tx.append(
            "knowledge_gap",
            cycle_id=state.cycle_id,
            subject_ids=marked or seeds,
            detail={
                "statement": resolution.escalation[:2000],
                "category": category,
                "risk": "high",
                "anchor": ",".join(resolution.conflict.paths[:8]),
                "classification": resolution.kind,
            },
        )
    return marked


def _with_status(state: models.State, task_ids: Sequence[str], status: str) -> dict[str, Any]:
    """A deep copy of the state with `task_ids` moved to `status` — the way `revise.apply` does it."""
    raw: dict[str, Any] = json.loads(json.dumps(state.raw))
    tasks = raw.setdefault("tasks", {})
    for task_id in task_ids:
        entry = tasks.get(task_id)
        tasks[task_id] = {**entry, "status": status} if isinstance(entry, dict) else {"status": status}
    raw["updated_at"] = event_chain.now_iso()
    return raw
