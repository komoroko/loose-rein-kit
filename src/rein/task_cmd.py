"""`rein task reset <T-NNN> --reason "…"` — the supported way to put a task back on the frontier.

`state.yaml` is written only inside a Central Store transaction, and `rein guard` denies a hand
edit outright — rule 1, never relaxed, on the premise that a status change with no audit record
explaining it cannot happen. That premise left a hole: when a human legitimately decides a
`blocked` task should be tried again — they fixed the flaky dependency, corrected the ticket,
installed the missing tool — there was no write path for that decision. The documentation said
to edit `state.yaml`; the guard refused. What was left was calling `build_loop.set_task_status`
from a Python shell, which reaches the same transaction only by accident, and which nothing
sanctions.

So this is not an escape hatch from the guard. It is the write path the guard's rule presumes
exists: the decision goes through the same transaction as the event that records it, with a
reason the human had to type.

What it deliberately does **not** do:

  - It does not refill retry budgets. The handoff — which gate step failed, what it said, how
    much of that step's budget is actually left — is kept, so a task that cannot pass does not
    get an unlimited allowance by being reset in a loop. `--fresh` discards it, and says so in
    the record, because "start this one over from nothing" is a different decision and should
    read as one.
  - It does not close the escalation. An escalation is concluded by a signed disposition in the
    review, never by a status somebody flipped (`rein events` is read-only by design).
  - It does not re-open a task under work that stands on it. A dependent that is `done`,
    `awaiting-evidence` or `in-progress` was started because this task was `done`; putting this
    one back on the frontier would leave it downstream of a task the DAG now says is unfinished,
    and nothing would ever re-check it against whatever this one becomes. The refusal names them,
    deepest first — the order they can be reset in.
  - It does not open anything. Gate approval has its own verb, its own TTY requirement, and its
    own receipt; nothing here touches `gates.*`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any

from rein import common, dag, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Where a reset may send a task. `done` is absent on purpose: a task is `done` because it
#: passed the quality gate and landed a commit, and declaring that by hand would forge exactly
#: the evidence acceptance reviews.
RESETTABLE_TO = ("todo", "needs-revision")

_REASON_MAX = 500


@dataclass(frozen=True)
class ResetResult:
    """What the reset moved, and what the next attempt still inherits."""

    previous: str
    handoff: dict[str, Any]


def reset(repo: repo_mod.Repo, task_id: str, *, status: str, reason: str, fresh: bool = False) -> ResetResult:
    """Move one task's status and record why, in one transaction.

    Retried on a lost race the same way every other task-status write is: a leaf reporting its
    own status through the control plane can land between the read and the write.
    """
    if status not in RESETTABLE_TO:
        raise ValueError(f"`rein task reset` moves a task to one of {', '.join(RESETTABLE_TO)}, not {status!r}")
    return store_mod.retry_on_stale(lambda: _reset_once(repo, task_id, status, reason, fresh))


def _reset_once(repo: repo_mod.Repo, task_id: str, status: str, reason: str, fresh: bool) -> ResetResult:
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ValueError("no .rein/state.yaml — run `rein init` first")
    seen = store_mod.read_digest(state)

    _refuse_under_started_work(store, state, task_id)

    raw = json.loads(json.dumps(state.raw))
    tasks = raw.setdefault("tasks", {})
    entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {}
    previous = str(entry.get("status", "todo"))
    carried = entry.get("handoff")
    handoff: dict[str, Any] = dict(carried) if isinstance(carried, dict) else {}

    updated = {**entry, "status": status}
    if fresh:
        updated.pop("handoff", None)
    # `completed_commit` says which commit *completed* the task; a task leaving `done` has none.
    updated.pop("completed_commit", None)
    tasks[task_id] = {k: v for k, v in updated.items() if v != ""}
    raw["updated_at"] = event_chain.now_iso()

    detail: dict[str, object] = {
        "kind": "task_reset",
        "from": previous,
        "to": status,
        "reason": reason[:_REASON_MAX],
        "handoff": "discarded" if fresh else "kept",
    }
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append("decision_declared", cycle_id=state.cycle_id, subject_ids=[task_id], detail=detail)
    return ResetResult(previous=previous, handoff={} if fresh else handoff)


def order(repo: repo_mod.Repo, task_id: str, *, after: str, reason: str) -> None:
    """Make `task_id` wait for `after`, beyond what the frozen plan declared, and record why.

    **No roll back, by design.** The mandate authorizes what is built and what it must meet; the
    order it is built in is the loop's to settle (`00-concept.md`, 論点 B), and an edge changes the
    order and nothing else. So it is written beside the frozen plan in `state.yaml` rather than
    into it, the plan's digest and every receipt bound to it stand, and the chain records the edge
    with its reason for acceptance to show. A roll back used to be the only way to add one — a
    design pass, a tasks pass, an adversarial review and a re-approval, for a one-line fact (#93).

    Refused when it would make a cycle, and when `task_id` has already started or finished: an
    edge in front of work that has run orders nothing.
    """
    store_mod.retry_on_stale(lambda: _order_once(repo, task_id, after, reason))


def _order_once(repo: repo_mod.Repo, task_id: str, after: str, reason: str) -> None:
    store = store_mod.Store(repo)
    state, plan = store.read_state(), store.read_plan()
    if state is None or plan is None:
        raise ValueError("no .rein/state.yaml or .rein/plan.yaml — run `rein init` first")
    seen = store_mod.read_digest(state)
    graph = dag.join(plan, state)
    for tid in (task_id, after):
        if tid not in {t.id for t in graph.tasks}:
            raise ValueError(f"{tid} is not a task in .rein/plan.yaml — `rein dag` lists them")
    if task_id == after:
        raise ValueError(f"{task_id} cannot wait for itself")
    status = graph.get(task_id).status
    if status in _STARTED:
        raise ValueError(f"{task_id} is {status}: an edge in front of work that has run orders nothing")
    if after in graph.get(task_id).blocked_by:
        raise ValueError(f"{task_id} already waits for {after}")

    raw = json.loads(json.dumps(state.raw))
    entry = raw.setdefault("tasks", {}).setdefault(task_id, {"status": status})
    entry["after"] = [*entry.get("after", []), after]
    try:
        dag.join(plan, models.State(raw))
    except dag.DagError as exc:
        raise ValueError(f"{task_id} after {after} would make the graph cyclic: {exc}") from exc
    raw["updated_at"] = event_chain.now_iso()
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append(
            "decision_declared",
            cycle_id=state.cycle_id,
            subject_ids=[task_id],
            detail={"kind": "edge_added", "after": after, "reason": reason[:_REASON_MAX]},
        )


#: A dependent in one of these was started on this task's current work and has not been parked.
_STARTED = frozenset({"done", "awaiting-evidence", "in-progress"})


def _refuse_under_started_work(store: store_mod.Store, state: models.State, task_id: str) -> None:
    """Raise when a transitive dependent of `task_id` has work standing on it."""
    plan = store.read_plan()
    if plan is None:
        return
    graph = dag.join(plan, state)
    started = {tid for tid in graph.dependents_closure([task_id]) if graph.get(tid).status in _STARTED}
    if started:
        # Deepest first: each of them is refused in turn while anything below it is still started.
        ahead = [tid for layer in reversed(graph.layers()) for tid in layer if tid in started]
        listed = ", ".join(f"{tid} ({graph.get(tid).status})" for tid in ahead)
        raise ValueError(
            f"{task_id} has dependents already started on its current work: {listed}. Re-opening it "
            "would leave them downstream of an unfinished task, never re-checked against what it "
            f"becomes. Reset them first, in that order (`rein task reset <id> --reason ...`), then {task_id}."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein task", description="operate on one task's record in state.yaml")
    sub = parser.add_subparsers(dest="action", required=True)
    reset_parser = sub.add_parser("reset", help="put a task back on the frontier, with the reason recorded")
    reset_parser.add_argument("task_id", help="the task to reset (T-NNN)")
    reset_parser.add_argument(
        "--reason",
        required=True,
        help="what changed since it stopped, addressed to the next attempt — recorded in the audit chain "
        "and handed to every later launch of this task in its dossier's history, --fresh included",
    )
    reset_parser.add_argument(
        "--status",
        default="todo",
        choices=RESETTABLE_TO,
        help="where to send it (default: todo)",
    )
    reset_parser.add_argument(
        "--fresh",
        action="store_true",
        help="also discard the handoff, so the next attempt starts with full retry budgets",
    )
    reset_parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    order_parser = sub.add_parser(
        "order", help="make a task wait for another, beyond the frozen plan — order only, no roll back"
    )
    order_parser.add_argument("task_id", help="the task that has to wait (T-NNN)")
    order_parser.add_argument("--after", required=True, help="the task it waits for (T-NNN)")
    order_parser.add_argument("--reason", required=True, help="why — recorded in the audit chain, shown at acceptance")
    order_parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    if args.action == "order":
        return _order_main(args)

    reason = args.reason.strip()
    if not reason:
        logger.error("--reason cannot be empty: the record is the point of having this verb")
        return 2
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    task_id = args.task_id.strip()
    plan = store_mod.Store(repo).read_plan()
    if plan is not None and task_id not in {t.id for t in plan.tasks}:
        logger.error(f"{task_id} is not a task in .rein/plan.yaml — `rein dag` lists them")
        return 2
    try:
        result = reset(repo, task_id, status=args.status, reason=reason, fresh=args.fresh)
    except (OSError, ValueError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1

    print(f"{task_id}: {result.previous} → {args.status} ({reason})")
    if args.fresh:
        print("  handoff discarded — the next attempt starts with the configured retry budgets")
    elif result.handoff:
        step = result.handoff.get("failed_step")
        left = result.handoff.get("retries_left")
        print(f"  handoff kept — last failed step: {step or 'n/a'}, retries left: {left or 'n/a'}")
        escalation = result.handoff.get("escalation")
        if isinstance(escalation, dict) and escalation.get("tree"):
            # Without this line, a reset that produces no implementer launch looks like a bug.
            print(
                f"  the last attempt ended '{escalation.get('kind', '?')}' before the quality gate, over "
                "the tree as it stands: the next `rein build` re-raises that verdict rather than paying "
                "for a launch that reaches it again. `--fresh` is how you say you repaired something "
                "outside the tree."
            )
    print("  the escalation stays in the log; it is concluded by a disposition in the review, not by this.")
    return 0


def _order_main(args: argparse.Namespace) -> int:
    reason = args.reason.strip()
    if not reason:
        logger.error("--reason cannot be empty: acceptance shows why the order changed")
        return 2
    try:
        repo = repo_mod.get(args.repo)
        order(repo, args.task_id.strip(), after=args.after.strip(), reason=reason)
    except (repo_mod.RepoNotFoundError, OSError, ValueError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    print(f"{args.task_id} now waits for {args.after} ({reason}) — the plan and its approval stand")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
