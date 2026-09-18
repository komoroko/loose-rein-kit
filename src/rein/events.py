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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rein import common, cycle, event_chain, models, run_record
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


#: Chain events that each mark one stop at a gate: the work halted and a human acted. Both need a
#: person — `gate_approved` is one opening a gate and `changes_requested` is one refusing to.
#:
#: This exists because the stop *count* and the stop *duration* had looked like one question by
#: living in one store. Neither needs the watcher: every stop that ended is already in the chain,
#: written inside a `store.Transaction` on every host and every path, and the chain's own order
#: bounds how long it lasted (:func:`stop_durations`). What does need `rein ui` is the narrower
#: `waited_seconds` — the span a notification is supposed to move, which starts when the decision
#: became derivable rather than when the work stopped.
#:
#: `gate_revised` is deliberately out. `/revise` reopens a gate a person has usually just refused,
#: and that refusal is `changes_requested` — counting both would count one stop twice. So is
#: `decision_declared`, which five call sites use for salvage branches, the PR-stack ledger and
#: task declarations; a name that means several things cannot be one of them here.
GATE_STOP_EVENTS = frozenset({"gate_approved", "changes_requested"})


def task_outcomes(events: Sequence[models.Event]) -> dict[str, str]:
    """The last task status **the chain itself** recorded, per subject.

    `state.yaml` is the authority while a cycle is live, and it is exactly what an archived chain
    does not come with. Every status the loop writes goes out with its event
    (`build_loop.set_task_status` puts it in `detail.status`), so the chain can answer the same
    question about its own past — which is what lets one retirement rule serve both.
    """
    outcomes: dict[str, str] = {}
    for event in events:
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        status = detail.get("status")
        if isinstance(status, str):
            outcomes.update(dict.fromkeys(event.subject_ids, status))
    return outcomes


def stops(events: Sequence[models.Event]) -> int:
    """How many times this chain says the work stopped and a human had to act.

    The gate stops above, plus the conditions that actually reached a person —
    :func:`open_conditions` over this chain's own outcomes, so the *same* rule that decides what a
    board calls pending decides what this counts. It read raw `ATTENTION_EVENTS` before, with no
    retirement at all: a task that failed twice and passed on the third attempt was counted as a
    stop a human had to act on, while every surface that asks "what awaits you" correctly said
    nothing did. A figure about human contact points may not count the loop recovering by itself.

    Counted over whatever chain it is handed, so an archived cycle counts the same as the live one.
    Never a ceiling: nothing reads this back to decide anything, which is the invariant that keeps
    a figure from becoming a budget (`observations.STOP_COUNT_CLAIM`).
    """
    gates = sum(1 for e in events if e.event in GATE_STOP_EVENTS)
    return gates + len(open_conditions(events, task_outcomes(events)))


def stop_durations(events: Sequence[models.Event]) -> list[float]:
    """How long each gate stop held the work, in seconds, read off the chain's own order.

    Nothing is added to the chain to get this. The stop was already recorded — a pending gate is
    the record of it, which is why no escalation is written beside one — and the chain is totally
    ordered, so the last thing the loop wrote before a human opened or refused that gate is the
    moment the work stopped, and the human's own event is the moment it started again.

    **Not the same quantity as `waited_seconds`, and never pooled with it.** That one runs from
    the decision becoming *derivable* to it being answered, and only a running `notify.Watcher`
    can see the near end of it: noticing the moment a blocker clears takes something that is
    watching. Between the loop's last event and that moment a person may still be clearing what
    blocked the gate, and some of that leaves no trace here at all — `approve.readiness` reads
    `docs/10-requirements.md` and `docs/20-design.md` through `dag_trace`, and editing those
    writes no event, because they are not documents this harness owns. That work is inside this
    figure and outside the timed one. So this answers "how long did the work sit", which is the
    cost a contact point has, and the timed one answers "how long did the decision sit", which is
    what a notification is supposed to move.

    Available wherever the chain is: every host, both approval paths, archived cycles included.
    Only gate stops are timed — the conditions `stops` also counts are the ones still open, and an
    open stop has no end to measure to.
    """
    durations: list[float] = []
    previous: models.Event | None = None
    for event in events:
        if event.event in GATE_STOP_EVENTS:
            if previous is not None and (elapsed := _elapsed(previous, event)) is not None:
                durations.append(elapsed)
            # `previous` is deliberately not advanced. Two gates opened in one sitting were both
            # held by the same stop, and reading the second one's from the first one's approval
            # would report it as instant — which is the shape a fan of crossings produces every
            # time somebody answers them together.
            continue
        previous = event
    return durations


def _elapsed(start: models.Event, end: models.Event) -> float | None:
    """Seconds between two events, or None when the chain's own clocks cannot say.

    Refused rather than clamped. These timestamps come from whatever machine wrote each event, so
    two of them can disagree; a figure that quietly read that as zero would hide the disagreement
    inside a mean, which is the one place it could never be found.
    """
    try:
        began = datetime.fromisoformat(start.ts)
        ended = datetime.fromisoformat(end.ts)
    except ValueError:
        logger.warning(f"event {start.seq} or {end.seq} carries an unreadable timestamp; that stop is not timed")
        return None
    if began.tzinfo is None or ended.tzinfo is None:
        logger.warning(f"event {start.seq} or {end.seq} has a timestamp with no offset; that stop is not timed")
        return None
    seconds = (ended - began).total_seconds()
    if seconds < 0:
        logger.warning(f"event {end.seq} is stamped before event {start.seq}; that stop is not timed")
        return None
    return seconds


#: `ATTENTION_EVENTS` a task's own later success can retire. Both are the build loop's per-attempt
#: verdicts about a task, so the task's status is authoritative over what they reported.
_TASK_SCOPED = frozenset({"task_failed", "knowledge_gap"})

#: `ATTENTION_EVENTS` that a later event in the same chain answers, and which event answers them.
#: Not an inference about what somebody decided — each pair is a thing that *undoes* the state the
#: first event reported: a plan that was invalidated has since been re-frozen, and a review
#: pipeline that could not produce an acceptance has since produced one.
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


def open_conditions(
    events: Sequence[models.Event], task_status: Mapping[str, str] | None = None
) -> list[tuple[models.Event, int]]:
    """The distinct conditions still waiting on a human: `(the newest record of each, how many)`.

    A repeated escalation is one thing to decide, however many attempts recorded it. Eight
    supervised attempts against one session limit filed sixteen rows, so "39 item(s) waiting on
    you" and "1 item waiting on you" read the same — and the number at the top of a board is what
    an operator reads first.

    **One rule, so every surface narrows the list the same way.** `task_status` is threaded through
    :func:`open_attention` for exactly this reason already; a grouping that lived in the status
    board alone would make `rein start` say three and `rein events --summary` say thirty-nine about
    the same question, which is the failure that comment was written against.

    Grouped by `(kind, subjects)` — the pair that identifies the condition — and the newest `seq`
    is the one returned, because that is the record whose `detail` describes what is true now.
    Insertion-ordered, so a condition keeps the position of its first occurrence. `events.ndjson`
    is untouched: every occurrence is still in the chain, and still in `render`.
    """
    grouped: dict[tuple[str, tuple[str, ...]], list[models.Event]] = {}
    for event in open_attention(events, task_status):
        grouped.setdefault((event.event, tuple(event.subject_ids)), []).append(event)
    return [(max(group, key=lambda e: e.seq), len(group)) for group in grouped.values()]


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
    """Counts per kind plus the conditions still awaiting a human decision.

    `task_status` is passed so this and the status board narrow the same list by the same rule; without
    it a task's later success retires its `task_failed` on one screen and not the other. Repeats are
    collapsed by :func:`open_conditions` for the same reason — one rule, or the two screens report
    different numbers for one question.
    """
    counts = event_chain.summarize(events)
    lines = ["### Aggregates", f"- events: {len(events)}", f"- chain root: {event_chain.chain_root(events)}"]
    lines.append("- by kind: " + (", ".join(f"{k}×{n}" for k, n in counts.items()) or "(none)"))
    conditions = open_conditions(events, task_status)
    lines.append(f"- needing a human decision: {len(conditions)}")
    for e, seen in conditions:
        subjects = ", ".join(e.subject_ids) or "-"
        lines.append(f"  - #{e.seq} {e.event} ({subjects})" + (f" \u00d7{seen}" if seen > 1 else ""))
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


@dataclass(frozen=True)
class CycleSource:
    """One cycle's own records: what to call it, and where that cycle's documents live.

    `label` names the *cycle* — the archive directory, or `""` for the one still open, which the
    renderers print as the current one. A defect is reported against the file it was found in
    instead (`cycle_sources`' second list): the axis of a report is the cycle, and the axis of a
    broken chain is the chain.

    `rein_dir` and `docs_dir` differ between the two cases because `cycle-close` lays an archive
    out as `<base>/rein/` and `<base>/`, while the open cycle is `.rein/` and `docs/`; a reader
    that guessed would read the wrong half of one of them.
    """

    label: str
    events: Sequence[models.Event]
    rein_dir: Path
    docs_dir: Path


def cycle_sources(repo: repo_mod.Repo, live: Sequence[models.Event]) -> tuple[list[CycleSource], list[str]]:
    """`(what to read, what could not be read)` — this cycle plus every archived one, oldest first.

    **The one enumeration of cycles.** Anything asked across cycles — what runs cost, which lenses
    earned their place, how often the work stopped, what was decided — is asked of this list, so
    that a second glob cannot drift from this one about which archives count.

    `cycle-close` moves a cycle's records into `docs/archive/<date>-<slug>/`. Reading only the live
    ones would make every such report go blank the moment a cycle is closed, which is exactly when
    the comparison becomes interesting.

    Each archive is scanned on its own (`scan`, not `load`): one damaged archive must not take the
    current cycle's figures down with it, and must not be folded in as though it were readable
    either. It comes back in the second list, to be named in the report.
    """
    sources: list[CycleSource] = []
    unreadable: list[str] = []
    archives = repo.path(cycle.ARCHIVE_DIR)
    # Archives first, and the live cycle last, because `run_record.costs` renders in the order it
    # is handed: an archive directory is `<YYYY-MM-DD>-<slug>`, so sorting the paths sorts the
    # cycles, and the one still open belongs at the bottom where the trend ends.
    for path in sorted(archives.glob(f"*/rein/{repo.events.name}")):
        archived, defects = event_chain.scan(path)
        if defects:
            unreadable.append(path.relative_to(repo.root).as_posix())
        else:
            base = path.parent.parent
            label = base.relative_to(repo.root).as_posix()
            sources.append(CycleSource(label, archived, rein_dir=path.parent, docs_dir=base))
    sources.append(CycleSource("", live, rein_dir=repo.rein_dir, docs_dir=repo.path(cycle.DOCS_DIR)))
    return sources, unreadable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="read the hash-chained audit log (read-only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="print the chain as a table (default)")
    group.add_argument("--summary", action="store_true", help="print aggregates and open decisions")
    group.add_argument("--verify", action="store_true", help="verify the chain and report every defect")
    group.add_argument("--root", action="store_true", help="print the chain root digest only")
    group.add_argument("--cost", action="store_true", help="what each cycle's launches actually cost, by role")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    # A window into an append-only log that only ever grows. `rein start` points here with the
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
    # Also whole-chain, and for the same reason: a cycle's total computed over a window is not
    # that cycle's total. `--cost` is answered per cycle, which is the axis spending has.
    if args.cost:
        sources, unreadable = cycle_sources(repo, events)
        billed = [(source.label, source.events) for source in sources]
        print(run_record.render_costs(run_record.costs(billed), unreadable=unreadable))
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
