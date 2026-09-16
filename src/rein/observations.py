"""What gets measured about the harness itself, and the rule for what does not.

Several rules in this repository already rest on a measurement nobody was taking. "Counted, never
capped" is the shape of the lens library's retirement rule and of the reasoning that removed the
acceptance budget; `rein lens --stats` counts one of those things. The rest were arguments about
numbers that existed nowhere.

**Measured is what would move if a design decision here were wrong.** Not what is easy to collect.
A general event log answers "what happened" and answers nothing about whether a rule was a good
one, and a pile of metrics nobody reads fails the same way an unfiltered lens library does: the
figures that matter get lost among the ones that were merely available.

So each observation below is attached to a claim this harness makes about itself, and each is the
quantity that would move if that claim were false:

=========================  ==========================================================
claim                      what would move if it were wrong
=========================  ==========================================================
selection by reach         `reach_overruled` — a `local` decision a human overruled at
                           the gate. The loop called it cheap to undo and the person
                           who would pay disagreed.
honesty buys interventions `unknown_at_mandate` beside `judgement_raised` — mandates
                           that admitted what they did not know, against findings that
                           came back needing a human to sort code from plan.
comprehension is a         `acceptance_reopened` — acceptance approved and then rolled
by-product of deciding     back. The heaviest row: somebody said yes to something they
                           turned out not to have understood.
the harness owns waiting   `waited_seconds` — from the decision being derived to it
                           being answered.
=========================  ==========================================================

**An observation is read-only and never an input.** Nothing here is read by a gate, a review or a
build: a cycle's outcome must not depend on what previous cycles happened to record, or the same
repository answers differently on another machine. That constraint is also what makes the store
safe to keep **across projects** — it holds counts and classes, never a requirement's text, never a
diff, never a path. Whatever needs the content is in that cycle's own archive, and an observation
carries the cycle id that finds it.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The kinds worth recording, each the quantity that would move if a claim this harness makes about
#: itself were false. Closed, for the same reason the event vocabulary is closed: a store anybody
#: can add a key to is one nobody can aggregate.
KINDS: tuple[str, ...] = (
    "reach_overruled",
    "unknown_at_mandate",
    "judgement_raised",
    "acceptance_reopened",
    "waited_seconds",
)
KIND_VALUES = frozenset(KINDS)

#: Why each kind exists, printed beside the figure — a number whose claim is not on screen beside
#: it is one somebody will read as a score.
CLAIMS: Mapping[str, str] = {
    "reach_overruled": "selection by reach: a `local` decision the human overruled was one the loop misjudged",
    "unknown_at_mandate": "honesty at the mandate is what buys fewer interventions later",
    "judgement_raised": "...measured against this: findings that needed a human to sort code from plan",
    "acceptance_reopened": "comprehension is a by-product of deciding — a reopened acceptance says it was not",
    "waited_seconds": "the harness owns waiting: how long a decision sat before it was answered",
}

STORE_NAME = "observations.ndjson"


@dataclass(frozen=True)
class Observation:
    """One measurement. Counts and classes only — never a requirement's text, never a diff."""

    kind: str
    project: str
    cycle_id: str
    value: float
    at: str
    subject: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def store_path() -> Path:
    """User-global, beside the project registry — this is a question about the harness, not about
    one repository, and the answer only appears across several."""
    return store_mod.config_home() / "rein" / STORE_NAME


def record(kind: str, *, project: str, cycle_id: str, value: float = 1.0, subject: str = "") -> bool:
    """Append one observation. False when it was refused or could not be written.

    Never raises. Every caller is doing something else — opening a gate, finishing a review — and
    an observation store that can fail a gate would be an input to the thing it is measuring.
    """
    if kind not in KIND_VALUES:
        logger.warning(f"unknown observation kind {kind!r} — not recorded")
        return False
    entry = Observation(
        kind=kind,
        project=project,
        cycle_id=cycle_id,
        value=float(value),
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        subject=subject,
    )
    path = store_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
    except OSError as exc:
        logger.debug(f"could not record an observation: {exc}")
        return False
    return True


def read(path: Path | None = None) -> list[Observation]:
    """Every observation, skipping lines that do not parse rather than refusing the file.

    An append-only file written by long-lived processes will eventually hold a torn line. Refusing
    the whole file over it would lose every reading before it, and this store is not evidence
    anybody signs — it is the material for a judgement somebody makes later.
    """
    target = path if path is not None else store_path()
    try:
        body = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError as exc:
        logger.warning(f"{target} could not be read: {exc}")
        return []
    out: list[Observation] = []
    for line in body.splitlines():
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, Mapping) or raw.get("kind") not in KIND_VALUES:
            continue
        out.append(
            Observation(
                kind=str(raw["kind"]),
                project=str(raw.get("project", "")),
                cycle_id=str(raw.get("cycle_id", "")),
                value=float(raw.get("value", 1)),
                at=str(raw.get("at", "")),
                subject=str(raw.get("subject", "")),
            )
        )
    return out


def summarize(entries: Sequence[Observation], *, project: str = "") -> dict[str, dict[str, float]]:
    """`{kind: {count, total, mean}}` — totals only, which is all counts and classes can support."""
    chosen = [e for e in entries if not project or e.project == project]
    out: dict[str, dict[str, float]] = {}
    for kind in KINDS:
        values = [e.value for e in chosen if e.kind == kind]
        if not values:
            continue
        out[kind] = {"count": len(values), "total": sum(values), "mean": sum(values) / len(values)}
    return out


def render(summary: Mapping[str, Mapping[str, float]]) -> str:
    if not summary:
        return (
            f"nothing recorded yet ({store_path()}).\n"
            "Observations accumulate as cycles run; one cycle answers none of the questions they "
            "are for, which are all about whether a rule in this harness was a good one."
        )
    lines: list[str] = []
    for kind, figures in summary.items():
        if kind == "waited_seconds":
            lines.append(f"{kind:<22} {figures['count']:>5} waits, mean {figures['mean'] / 60:.1f} min")
        else:
            lines.append(f"{kind:<22} {int(figures['total']):>5}")
        lines.append(f"  {CLAIMS[kind]}")
    lines.append("")
    lines.append(
        "No thresholds, and none are coming. These are the material for deciding whether a rule "
        "here holds, not a score to stay under — a number with a ceiling on it gets managed "
        "instead of read."
    )
    return "\n".join(lines)


def prune(keep: int = 5000, path: Path | None = None) -> int:
    """Keep the most recent `keep` entries. Returns how many were dropped.

    Bounded because the store is user-global and append-only, and an unbounded file on somebody's
    laptop is a thing that eventually gets deleted wholesale rather than trimmed. Not a retention
    policy about evidence: the evidence is each cycle's own archive, which this never replaces.
    """
    target = path if path is not None else store_path()
    entries = read(target)
    if len(entries) <= keep:
        return 0
    kept = entries[-keep:]
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(target.parent), delete=False, suffix=".tmp"
        ) as handle:
            for entry in kept:
                handle.write(json.dumps(entry.as_dict(), ensure_ascii=False) + "\n")
            temp = Path(handle.name)
        os.replace(temp, target)
    except OSError as exc:
        logger.warning(f"could not prune {target}: {exc}")
        return 0
    return len(entries) - len(kept)
