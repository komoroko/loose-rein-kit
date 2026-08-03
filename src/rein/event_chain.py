"""The hash-chained audit log: `events.ndjson` and the verification that makes it evidence.

An append-only file is only evidence if removing a line from it is *detectable*, so the log is
chained and never rotates. Each record carries:

  ``event_digest``       the canonical digest of every *other* field of that record
  ``prev_event_digest``  the predecessor's ``event_digest`` ("" only for seq 1)
  ``seq``                monotonic within the store lock

That catches a rewritten record (its own digest no longer matches), and a deleted or
reordered one (the next record's link no longer matches). It does **not** catch a wholesale
re-hash of the entire log — an attacker who rewrites every record can produce a
self-consistent chain. What catches that is :func:`chain_root`: a gate receipt records
the root it was issued against, so a regenerated chain no longer matches the roots past
approvals were confirmed against (plan §7.6, E2E-29). The chain alone is tamper-*evidence*;
the chain plus a receipt that pins a root is tamper-*detection*.

Reads are strict and fail closed. A corrupt line means the audit record is unreadable, and
mutating on top of an unreadable audit record is precisely the failure mode — skipping it to
keep the orchestrator alive would be the wrong trade. :func:`load` therefore raises, and only
`doctor` reads with :func:`scan` to *report* damage without acting on it.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rein import digests, models, strict_yaml

#: The chain root of an empty log. A distinct constant rather than "" so that "no events yet"
#: and "field absent" cannot be confused when a receipt binds a root.
EMPTY_CHAIN_ROOT = digests.of_texts([])


class ChainError(RuntimeError):
    """The event log is unreadable or its chain is broken. Always fail closed."""


@dataclass(frozen=True)
class ChainDefect:
    """One problem found by :func:`scan`. `line` is 1-based; 0 means "the file as a whole"."""

    line: int
    kind: str
    detail: str

    def __str__(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"{where}{self.kind}: {self.detail}"


def now_iso() -> str:
    """Current local time, seconds resolution, with an explicit offset.

    An offset-naive timestamp is ambiguous across machines, and these end up inside signed
    receipt payloads.
    """
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id() -> str:
    """A fresh event/transaction id (uuid4 hex-with-dashes, matching the schema pattern)."""
    return str(uuid.uuid4())


# --- digest and chaining ------------------------------------------------------


def event_digest(event: models.Event) -> str:
    """The digest of everything in `event` except the digest field itself."""
    return digests.of(event.payload())


def sealed(event: models.Event) -> models.Event:
    """`event` with its `event_digest` computed from its current payload."""
    from dataclasses import replace

    return replace(event, event_digest=event_digest(event))


def link(previous: models.Event | None, event: models.Event) -> models.Event:
    """`event` chained onto `previous`: its seq set, prev digest linked, own digest sealed."""
    from dataclasses import replace

    chained = replace(
        event,
        seq=(previous.seq + 1) if previous else 1,
        prev_event_digest=previous.event_digest if previous else "",
    )
    return sealed(chained)


def chain_root(events: Sequence[models.Event]) -> str:
    """The digest of the whole chain — what a gate receipt binds.

    Computed over every record's digest rather than just the last one: truncating the log
    would otherwise be invisible whenever the last surviving record's digest was already
    known.
    """
    return digests.of_texts(e.event_digest for e in events)


# --- serialization ------------------------------------------------------------


def dumps(event: models.Event) -> str:
    """One NDJSON line. Canonical form, so a re-serialized log is byte-identical."""
    return digests.canonical(event.to_mapping()).decode("utf-8")


def scan(path: str | Path) -> tuple[list[models.Event], list[ChainDefect]]:
    """Read the log, returning what parsed and every defect found. Never raises on damage.

    This is `doctor`'s entry point: a read-only diagnosis of a log that may be broken. Every
    other caller uses :func:`load`, which refuses to hand back a partially-readable chain.
    """
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        return [], [ChainDefect(0, "unreadable", str(exc))]
    return scan_text(text)


def scan_text(text: str) -> tuple[list[models.Event], list[ChainDefect]]:
    """The same scan over log *text* — for a log read out of a git tree rather than off disk.

    A base-side check reads the head commit, not the working copy: on a pull request the
    checkout is the merge commit, so a path-based read verifies a log nobody proposed.
    """
    events: list[models.Event] = []
    defects: list[ChainDefect] = []
    seen_seq: set[int] = set()
    seen_id: set[str] = set()
    previous: models.Event | None = None

    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            defects.append(ChainDefect(number, "blank_line", "the log has no blank lines by construction"))
            continue
        try:
            raw = strict_yaml.load_json_mapping(line, limits=strict_yaml.EVENT_LIMITS, what=f"event line {number}")
        except strict_yaml.StrictParseError as exc:
            defects.append(ChainDefect(number, "unparseable", str(exc)))
            continue
        problems = models.schema_errors(raw, "event")
        if problems:
            defects.append(ChainDefect(number, "schema", "; ".join(problems)))
            continue

        event = models.Event.from_mapping(raw)
        if event.seq in seen_seq:
            defects.append(ChainDefect(number, "duplicate_seq", f"seq {event.seq} already used"))
        if event.id in seen_id:
            defects.append(ChainDefect(number, "duplicate_id", f"id {event.id} already used"))
        seen_seq.add(event.seq)
        seen_id.add(event.id)

        expected_seq = (previous.seq + 1) if previous else 1
        if event.seq != expected_seq:
            defects.append(
                ChainDefect(number, "seq_gap", f"expected seq {expected_seq}, found {event.seq} (a record is missing)")
            )
        expected_prev = previous.event_digest if previous else ""
        if event.prev_event_digest != expected_prev:
            defects.append(ChainDefect(number, "broken_link", "prev_event_digest does not name the preceding record"))
        if event.event_digest != event_digest(event):
            defects.append(ChainDefect(number, "digest_mismatch", "the record does not hash to its own event_digest"))

        events.append(event)
        previous = event

    return events, defects


def load(path: str | Path) -> list[models.Event]:
    """Every event, with the chain verified. Raises :class:`ChainError` on any defect."""
    events, defects = scan(path)
    if defects:
        listed = "\n".join(f"  - {d}" for d in defects[:20])
        more = f"\n  … and {len(defects) - 20} more" if len(defects) > 20 else ""
        raise ChainError(
            f"{path}: the audit chain is damaged ({len(defects)} defect(s)); refusing to act on it.\n"
            f"{listed}{more}\n"
            "Run `rein doctor` for the full diagnosis. The log is append-only evidence — "
            "repair means restoring it from git, never rewriting it to agree."
        )
    return events


def verify_root(events: Sequence[models.Event], expected_root: str) -> bool:
    """True when `events` hash to `expected_root` — how a signed checkpoint is re-checked."""
    return digests.matches(chain_root(events), expected_root)


def append_lines(path: str | Path, events: Iterable[models.Event]) -> None:
    """Append sealed events to the log. Caller holds the store lock; see :mod:`rein.store`.

    Deliberately not public API for command code: an event appended outside a store
    transaction is an event with no corresponding state change (or vice versa).
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        for event in events:
            handle.write(dumps(event) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def make(
    event: str,
    cycle_id: str,
    *,
    actor: str = "",
    subject_ids: Sequence[str] = (),
    detail: dict[str, object] | None = None,
    tx_id: str = "",
) -> models.Event:
    """An unchained event ready for :func:`link`. Unknown event names are refused here, not later.

    Rejecting the vocabulary at construction is what keeps the log aggregatable: a typo would
    otherwise create a kind no query knows to look for.
    """
    if event not in models.EVENT_VALUES:
        raise ValueError(f"unknown event {event!r} — one of: {', '.join(models.EVENT_ORDER)}")
    return models.Event(
        seq=0,
        id=new_id(),
        tx_id=tx_id or new_id(),
        ts=now_iso(),
        event=event,
        cycle_id=cycle_id,
        actor=actor,
        subject_ids=tuple(subject_ids),
        detail=dict(detail or {}),
    )


def summarize(events: Sequence[models.Event]) -> dict[str, int]:
    """Counts per event kind, in the vocabulary's declaration order (deterministic display)."""
    counts = {kind: 0 for kind in models.EVENT_ORDER}
    for event in events:
        if event.event in counts:
            counts[event.event] += 1
    return {kind: n for kind, n in counts.items() if n}
