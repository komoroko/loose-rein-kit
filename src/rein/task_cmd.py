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
  - It does not open anything. Gate approval has its own verb, its own TTY requirement, and its
    own receipt; nothing here touches `gates.*`.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass
from typing import Any

from rein import common, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Where a reset may send a task. `done` is absent on purpose: a task is `done` because it
#: passed the quality gate and landed a commit, and declaring that by hand would forge exactly
#: the evidence gate ④ reviews.
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein task", description="operate on one task's record in state.yaml")
    sub = parser.add_subparsers(dest="action", required=True)
    reset_parser = sub.add_parser("reset", help="put a task back on the frontier, with the reason recorded")
    reset_parser.add_argument("task_id", help="the task to reset (T-NNN)")
    reset_parser.add_argument(
        "--reason",
        required=True,
        help="why this task should be tried again — recorded in the audit chain beside the change",
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
    args = parser.parse_args(argv)
    common.configure_logging()

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
        left = result.handoff.get("retries_left")
        step = result.handoff.get("failed_step", "?")
        print(f"  handoff kept — last failed step: {step}, retries left: {left or 'n/a'}")
    print("  the escalation stays in the log; it is concluded by a disposition in the review, not by this.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
