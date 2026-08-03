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


def render(events: list[models.Event]) -> str:
    """The chain as a table, newest last (reading order matches append order)."""
    if not events:
        return "no events yet"
    lines = ["| seq | when | event | actor | subjects |", "|-----|------|-------|-------|----------|"]
    for e in events:
        subjects = ", ".join(e.subject_ids) or "-"
        lines.append(f"| {e.seq} | {e.ts[:19]} | {e.event} | {e.actor or '-'} | {subjects} |")
    return "\n".join(lines)


def render_summary(events: list[models.Event]) -> str:
    """Counts per kind plus the events still awaiting a human decision."""
    counts = event_chain.summarize(events)
    lines = ["### Aggregates", f"- events: {len(events)}", f"- chain root: {event_chain.chain_root(events)}"]
    lines.append("- by kind: " + (", ".join(f"{k}×{n}" for k, n in counts.items()) or "(none)"))
    attention = [e for e in events if e.event in ATTENTION_EVENTS]
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
        print(render_summary(events))
        return 0
    print(render(events))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
