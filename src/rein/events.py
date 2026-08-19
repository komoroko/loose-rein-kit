"""`rein events` — read and query the hash-chained audit log.

The log itself lives in :mod:`rein.event_chain` and is written only inside a
:class:`rein.store.Transaction`. This module is the human-facing *view* over it, and it is
read-only by design: render the chain, aggregate it, verify it. There is deliberately no way to
append or resolve an entry by hand — an audit log an operator can hand-write is not evidence of
anything, and closing a record is a disposition in `review.yaml`, which is signed.

`--verify` is the verb that matters: it is how a human checks that the record they are about to
sign for has not been edited, reordered, truncated, or regenerated.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence

from rein import common, event_chain, models
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: Events that mean a human has to decide something. They are not "closed" here — a
#: disposition is recorded in review.yaml and signed, not ticked off in a log.
ATTENTION_EVENTS = frozenset(
    {
        "knowledge_gap",
        "task_failed",
        "actual_extraction_failed",
        "review_failed",
        "expert_requested",
        "plan_invalidated",
    }
)


#: `ATTENTION_EVENTS` a task's own later success can retire. Both are the build loop's per-attempt
#: verdicts about a task, so the task's status is authoritative over what they reported.
_TASK_SCOPED = frozenset({"task_failed", "knowledge_gap"})

#: `ATTENTION_EVENTS` that a later event in the same chain answers, and which event answers them.
#: Not an inference about what somebody decided — each pair is a thing that *undoes* the state the
#: first event reported: a plan that was invalidated has since been re-frozen, and a review
#: pipeline that could not produce a gate ④ has since produced one.
#:
#: The gap this closes: these three had no retirement condition at all, so the queue carried
#: "waiting for you" rows for a rollback that was re-approved weeks ago, and for a generation that
#: failed once and succeeded on the retry. A queue that only grows is one people stop reading, and
#: the rows it buries are the ones that mattered.
_SUPERSEDED_BY: Mapping[str, str] = {
    "plan_invalidated": "plan_frozen",
    "review_failed": "review_generated",
    "actual_extraction_failed": "review_generated",
}


def _task_outcome_resolved(event: models.Event, task_status: Mapping[str, str]) -> bool:
    """Has the task(s) this event named since reached `done`, making its own report stale?

    A later successful attempt is the event's own resolution: the outcome it reported no longer
    holds, so it stops being something to wait on.
    """
    if event.event not in _TASK_SCOPED or not event.subject_ids:
        return False
    return all(task_status.get(subject) == "done" for subject in event.subject_ids)


def _superseded(event: models.Event, latest_seq: Mapping[str, int]) -> bool:
    """Has a later event in the chain undone what this one reported?

    Ordered by `seq`, not by timestamp: the sequence is the chain's own order and a clock is not.
    Strictly later, so an event can supersede neither itself nor a sibling from the same transaction.
    """
    answer = _SUPERSEDED_BY.get(event.event)
    return answer is not None and latest_seq.get(answer, -1) > event.seq


def open_attention(events: Sequence[models.Event], task_status: Mapping[str, str] | None = None) -> list[models.Event]:
    """The attention events still waiting on a human — everything answered since, dropped.

    `events.ndjson` itself is untouched: this narrows what a *view* calls pending, the same way
    every other row of the queue is derived rather than stored. There is still no way to close a
    record by hand, which is the property this module exists to keep.

    Pure, so that "what is still open" is a policy something can test rather than one that can only
    be exercised through a repository on disk.
    """
    statuses = task_status or {}
    latest_seq: dict[str, int] = {}
    for event in events:
        if event.event in _SUPERSEDED_BY.values():
            latest_seq[event.event] = max(latest_seq.get(event.event, -1), event.seq)
    return [
        e
        for e in events
        if e.event in ATTENTION_EVENTS and not _task_outcome_resolved(e, statuses) and not _superseded(e, latest_seq)
    ]


def render(events: list[models.Event]) -> str:
    """The chain as a table, newest last (reading order matches append order)."""
    if not events:
        return "no events yet"
    lines = ["| seq | when | event | actor | subjects |", "|-----|------|-------|-------|----------|"]
    for e in events:
        subjects = ", ".join(e.subject_ids) or "-"
        lines.append(f"| {e.seq} | {e.ts[:19]} | {e.event} | {e.actor or '-'} | {subjects} |")
    return "\n".join(lines)


def render_summary(events: list[models.Event], task_status: Mapping[str, str] | None = None) -> str:
    """Counts per kind plus the events still awaiting a human decision.

    `task_status` is passed so this and `rein status` narrow the same list by the same rule; without
    it a task's later success retires its `task_failed` on one screen and not the other.
    """
    counts = event_chain.summarize(events)
    lines = ["### Aggregates", f"- events: {len(events)}", f"- chain root: {event_chain.chain_root(events)}"]
    lines.append("- by kind: " + (", ".join(f"{k}×{n}" for k, n in counts.items()) or "(none)"))
    attention = open_attention(events, task_status)
    lines.append(f"- needing a human decision: {len(attention)}")
    for e in attention:
        subjects = ", ".join(e.subject_ids) or "-"
        lines.append(f"  - #{e.seq} {e.event} ({subjects})")
    return "\n".join(lines)


def render_verification(path: str, defects: list[event_chain.ChainDefect]) -> str:
    if not defects:
        return "PASS event-chain: intact"
    body = "\n".join(f"  - {d}" for d in defects)
    return (
        f"FAIL event-chain: {len(defects)} defect(s) in {path}\n{body}\n"
        "The log is append-only evidence. Restore it from git — never rewrite it to agree "
        "with the current state."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read the hash-chained audit log (read-only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="print the chain as a table (default)")
    group.add_argument("--summary", action="store_true", help="print aggregates and open decisions")
    group.add_argument("--verify", action="store_true", help="verify the chain and report every defect")
    group.add_argument("--root", action="store_true", help="print the chain root digest only")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    # A window into an append-only log that only ever grows. `rein resume` points here with the
    # reader's own watermark, so "what happened while I was gone" does not mean reading the whole log.
    parser.add_argument("--since", type=int, default=None, metavar="SEQ", help="only events after this seq")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    path = str(repo.events)
    events, defects = event_chain.scan(path)

    if args.verify:
        print(render_verification(path, defects))
        return 1 if defects else 0

    if defects:
        # Every other view refuses to display a damaged chain as though it were the record:
        # a table rendered from a broken log reads exactly like a table rendered from a good one.
        logger.error(render_verification(path, defects))
        return 1

    # The root and the verification are statements about the *whole* chain, so `--since` must not
    # narrow them — a root computed over a window would not be the root any receipt bound.
    if args.root:
        print(event_chain.chain_root(events))
        return 0
    if args.since is not None:
        events = [e for e in events if e.seq > args.since]
    if args.summary:
        from rein import status_api

        print(render_summary(events, status_api.task_status_of(repo)))
        return 0
    print(render(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
