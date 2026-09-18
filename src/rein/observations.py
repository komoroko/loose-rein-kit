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
selection by reach         `reach_overruled`, in two arms — `too_local`, a `local`
                           decision a human overruled at the gate, and `too_mandate`,
                           a `mandate` decision that ended up `local` before the
                           freeze. One criterion, two ways to be wrong. Measured on
                           both sides because a one-sided figure only ever reads as
                           "ask more", and the criterion exists to ask less.
honesty buys interventions `unknown_at_mandate` beside `judgement_raised` — mandates
                           that admitted what they did not know, against findings that
                           came back needing a human to sort code from plan.
comprehension is a         `acceptance_reopened` — acceptance approved and then rolled
by-product of deciding     back. The heaviest row: somebody said yes to something they
                           turned out not to have understood.
the harness owns waiting   `waited_seconds` — from the decision being derived to it
                           being answered, under each of the two conditions it could
                           be spent in. What this falsifies is that the harness owns
                           the channel at all, not how much a notification helps: the
                           arm is a property of the machine, so the columns sit side
                           by side and are not a controlled comparison.
selection by reach         the *count* of those same readings: one per wait, so it is
settles how often work     how often work stopped. Read off `waited_seconds` rather
stops                      than recorded again, and never split by arm — whether a
                           channel was configured has nothing to do with whether the
                           criterion settles the number of stops.
a contact point costs      how long the work sat stopped, read off the audit chain's
time as well as count      own order (`events.stop_durations`) rather than recorded
                           here. Derived on read for the same reason the chained count
                           is: it is a fact about one repository, and this store holds
                           several. Unarmed, and never pooled with `waited_seconds` —
                           a different span, described at `_STOP_TIME_SOURCES`.
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

#: The conditions a reading can be taken under, per kind. A measurement with no control is a
#: number, not evidence, and the two kinds below each carry a claim that only a comparison can
#: falsify. Per kind rather than one shared set: `notified` against a `reach_overruled` reading
#: would place it in a comparison nobody is making, and a store that accepts it is one nobody can
#: aggregate.
#:
#: `notified` means a notification **was delivered** for that wait, not that a channel was
#: configured for it. The two came apart wherever a channel was configured and broken, and the arm
#: read the config: every such wait landed in the treatment group having told nobody. A control
#: condition is also what a failed delivery *is* — obtained without anyone turning a channel off
#: to get it.
ARM_NOTIFIED = "notified"
ARM_SILENT = "silent"
#: The two ways selection by reach can be wrong. `too_local` is the loop calling a decision cheap
#: to undo and the person who would pay disagreeing; `too_mandate` is the loop routing one to a
#: human and that reach not surviving to the freeze. Both readings are about the criterion rather
#: than about who moved it: what each one says is that a reach the loop derived did not hold. One
#: claim, two directions — never pooled, because a pooled figure reads as "the criterion was wrong
#: N times" and says nothing about which way to move it.
ARM_TOO_LOCAL = "too_local"
ARM_TOO_MANDATE = "too_mandate"


@dataclass(frozen=True)
class Arms:
    """What one kind's readings can be grouped by, and what to say when only some groups are there.

    One record per kind rather than three mappings keyed alike. The arms, the note for a one-sided
    record and the arm that pre-arm readings belong to are three facts about the same comparison,
    and holding them apart is holding a pair that can drift — the drift showing up as a `KeyError`
    in front of somebody reading their own figures.
    """

    #: Closed. Every reading of this kind is in exactly one of them; there is no unarmed bucket,
    #: because a figure pooled from readings that never shared a condition is what arms prevent.
    values: frozenset[str]
    #: Printed when the record has some arms and not others, saying what the missing side would
    #: take — which is not always something anybody can, or should, go and do. For
    #: `reach_overruled` it is a gesture somebody makes in the normal course of work; for
    #: `waited_seconds` it is a condition of this machine that may simply not recur, and saying so
    #: is the point: a note that reads as a chore leaves a person arranging their work around a
    #: comparison that will not become identifiable however they arrange it.
    one_sided: str
    #: Where readings written before this kind was armed belong. Provenance rather than a guess:
    #: it is read off the one code path that wrote them. "" means the kind was armed from its
    #: first reading, so an unarmed one on disk was never written by this harness.
    legacy: str = ""


ARMS: Mapping[str, Arms] = {
    "waited_seconds": Arms(
        values=frozenset({ARM_NOTIFIED, ARM_SILENT}),
        one_sided=(
            "Not a gap to go and fill. The arm is this machine's `command:` setting — one setting "
            "for every project here — and a cycle run without `rein ui` records no wait on either "
            "side, so the other column appears only by accident: the cycles somebody ran before "
            "setting a channel up, or a delivery that failed. Nothing anybody should do about it; "
            "the only way to make the two columns comparable on purpose is to stop notifying "
            "somebody."
        ),
    ),
    "reach_overruled": Arms(
        values=frozenset({ARM_TOO_LOCAL, ARM_TOO_MANDATE}),
        one_sided=(
            "The criterion can be wrong in either direction and only one is on record. The missing side is a "
            "gesture, not a setting: `too_local` is a change request against a `local` decision, `too_mandate` "
            "is a `mandate` decision that ended up `local` before the freeze."
        ),
        # 0.6.0 recorded this kind unarmed, and `change_request.add` was the only thing that wrote
        # it — which is this arm exactly. The label is new; the readings are not, and re-filing
        # them is what keeps an upgrade from turning an existing record into a third bucket
        # printed under a claim about arms it does not have.
        legacy=ARM_TOO_LOCAL,
    ),
}
#: Kinds whose claim is a comparison, so their readings are grouped by `arm` and never pooled.
ARMED_KINDS = frozenset(ARMS)

#: Why each kind exists, printed beside the figure — a number whose claim is not on screen beside
#: it is one somebody will read as a score.
CLAIMS: Mapping[str, str] = {
    "reach_overruled": (
        "selection by reach: each arm is the criterion misjudging one way — `too_local` too loose, "
        "`too_mandate` too tight"
    ),
    "unknown_at_mandate": "honesty at the mandate is what buys fewer interventions later",
    "judgement_raised": "...measured against this: findings that needed a human to sort code from plan",
    "acceptance_reopened": "comprehension is a by-product of deciding — a reopened acceptance says it was not",
    "waited_seconds": (
        "the harness owns waiting: how long a decision sat, kept apart for the waits somebody was told "
        "about and the waits nobody was — two conditions recorded, never a controlled comparison"
    ),
}

#: Kinds whose value is seconds. Everything else is a count, and the two are not rendered alike: a
#: mean of durations answers "how long", a total of counts answers "how often". This is not the
#: armed/unarmed split — `reach_overruled` is armed and is still a count.
DURATION_KINDS = frozenset({"waited_seconds"})

#: Read off `waited_seconds` rather than recorded again: one reading per wait, so the count *is*
#: how often work stopped. It falsifies a different claim than the durations do, so it is printed
#: with its own claim beside it, and pooled across arms — whether a channel was configured has
#: nothing to do with whether the criterion settles the number of stops.
#:
#: Counted, never capped, and the store is where that is guaranteed rather than promised: nothing
#: reads this file to decide anything, so there is no path by which the figure could become a
#: ceiling. A ceiling on how often a human may be asked gets answered by not asking, which is the
#: failure selection by reach exists to prevent.
STOP_COUNT_CLAIM = "selection by reach settles how often work stops — the count of blocking points, never a ceiling"

#: The other half of what a contact point costs. `STOP_COUNT_CLAIM` is how often the work stopped;
#: this is how long it stayed stopped, which `00-concept.md` names in the same breath and which
#: nothing measured across every cycle until now.
#:
#: Unarmed, exactly like the count. The chain says the work stopped and when it started again; it
#: does not say whether anybody was told, so splitting this by `notified`/`silent` would label
#: readings with a condition they were never observed under.
STOP_TIME_CLAIM = (
    "what a contact point costs is also how long the work sat stopped — every cycle and every "
    "host, never split by arm: a chain records that it stopped, not who was told"
)

#: Printed when both stopped-time figures are on screen, because they are not the same span.
_STOP_TIME_SOURCES = (
    "the two measure different spans: timed = from the decision becoming derivable to it being "
    "answered, which is what a notification moves; chained = from the loop's last event to the "
    "human's, which also holds whatever they had to fix before the gate would open"
)

#: Said whenever both stop counts are printed, because they are not the same quantity and a reader
#: who takes them for one will read the gap as drift. The timed count is waits the dashboard saw
#: the SSOT surface, so it is blind to any cycle run without `rein ui` and spans every project in
#: this store. The chained count is human interventions one repository's audit chain recorded, so
#: it misses nothing and covers only that repository. Neither is a correction of the other.
_STOP_SOURCES = (
    "the two count different things: timed = waits `rein ui` saw, across every project here; "
    "chained = human interventions in this repository's chain, with no dashboard needed"
)

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
    arm: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def store_path() -> Path:
    """User-global, beside the project registry — this is a question about the harness, not about
    one repository, and the answer only appears across several."""
    return store_mod.config_home() / "rein" / STORE_NAME


def record(kind: str, *, project: str, cycle_id: str, value: float = 1.0, subject: str = "", arm: str = "") -> bool:
    """Append one observation. False when it was refused or could not be written.

    Never raises. Every caller is doing something else — opening a gate, finishing a review — and
    an observation store that can fail a gate would be an input to the thing it is measuring.
    """
    if kind not in KIND_VALUES:
        logger.warning(f"unknown observation kind {kind!r} — not recorded")
        return False
    # An armed kind with no arm is a reading that cannot be placed in the comparison it exists for,
    # and an unarmed kind carrying one is a comparison nobody is making. Both are refused rather
    # than filed under "": a figure pooled from readings that never shared a condition is the thing
    # `ARMS` exists to prevent.
    spec = ARMS.get(kind)
    allowed = spec.values if spec is not None else frozenset({""})
    if arm not in allowed:
        wanted = f"one of {sorted(allowed)}" if spec is not None else "no arm — its claim needs no comparison"
        logger.warning(f"observation {kind!r} carried arm {arm!r}; it takes {wanted}. Not recorded.")
        return False
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        # "Never raises" has to survive the caller that hands this a None it computed from a clock
        # that was not running. The reading is lost; the gate the caller was opening is not.
        logger.warning(f"observation {kind!r} carried a non-numeric value {value!r} — not recorded")
        return False
    entry = Observation(
        kind=kind,
        project=project,
        cycle_id=cycle_id,
        value=numeric,
        at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        subject=subject,
        arm=arm,
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


def _placed_arm(kind: str, arm: str, path: Path) -> str | None:
    """The arm this reading belongs in, or `None` when it belongs in none of them.

    Every reading of an armed kind sits in exactly one arm, so the two ways a stored line can fail
    to are handled here rather than by coercing it to `""` and letting `summarize` open a bucket
    for it. That bucket was the bug: readings written before a kind was armed reappeared as a
    third group, printed under a claim about arms they did not have, and the missing-arm warning —
    which looks only at real arms — stayed silent about a record that had none.

    Pre-arm readings are re-filed into `Arms.legacy`, which is provenance and not a guess. Anything
    else is refused: a reading nobody can place is not a reading, and dropping it loudly beats
    pooling it quietly into a figure that then means nothing.
    """
    spec = ARMS.get(kind)
    if spec is None:
        return ""  # unarmed kind: a stray label on disk names a comparison nobody is making
    if not arm and spec.legacy:
        return spec.legacy
    if arm not in spec.values:
        logger.warning(
            f"{path}: a {kind!r} reading in arm {arm!r} belongs to no arm of that kind "
            f"({', '.join(sorted(spec.values))}) — skipped rather than pooled."
        )
        return None
    return arm


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
        try:
            value = float(raw.get("value", 1))
        except (TypeError, ValueError):
            # Well-formed JSON carrying a value nothing can average. Skipped for the same reason a
            # torn line is: one unreadable row must not cost every reading taken before it.
            continue
        kind = str(raw["kind"])
        arm = _placed_arm(kind, str(raw.get("arm", "")), target)
        if arm is None:
            continue
        out.append(
            Observation(
                kind=kind,
                project=str(raw.get("project", "")),
                cycle_id=str(raw.get("cycle_id", "")),
                value=value,
                at=str(raw.get("at", "")),
                subject=str(raw.get("subject", "")),
                arm=arm,
            )
        )
    return out


def summarize(entries: Sequence[Observation], *, project: str = "") -> dict[str, dict[str, float]]:
    """`{key: {count, total, mean}}` — totals only, which is all counts and classes can support.

    `waited_seconds` is reported per arm (`waited_seconds/notified`, `waited_seconds/silent`) and
    never pooled. Pooling them would answer "how long do people wait" when the question is whether
    the channel changes that, and the pooled mean moves with whichever arm was recorded more.
    """
    chosen = [e for e in entries if not project or e.project == project]
    out: dict[str, dict[str, float]] = {}
    for kind in KINDS:
        of_kind = [e for e in chosen if e.kind == kind]
        if not of_kind:
            continue
        groups: dict[str, list[float]] = {}
        for entry in of_kind:
            key = f"{kind}/{entry.arm}" if kind in ARMED_KINDS else kind
            groups.setdefault(key, []).append(entry.value)
        for key, values in sorted(groups.items()):
            out[key] = {"count": len(values), "total": sum(values), "mean": sum(values) / len(values)}
    return out


def render(
    summary: Mapping[str, Mapping[str, float]],
    chain_stops: int | None = None,
    chain_stopped: Sequence[float] = (),
) -> str:
    """The figures, each beside the claim it tests.

    `chain_stops` and `chain_stopped` are the count and the duration of the same stops, asked of a
    source that does not need `rein ui` (`events.stops`, `events.stop_durations`). Both are passed
    in rather than read here because they are facts about one repository and this store is
    user-global — and both are printed *beside* the timed figures, never instead of them, because
    neither is the same quantity as the one it sits next to. See `_STOP_SOURCES` and
    `_STOP_TIME_SOURCES`.
    """
    # An empty store with a chain behind it is the case this figure was added for: a cycle run from
    # the terminal alone records no observation and still stopped for a human every time it did.
    # Returning the "nothing recorded yet" line here would have withheld the count at exactly the
    # moment it is the only one there is.
    if not summary and not chain_stops and not chain_stopped:
        nothing = (
            f"nothing recorded yet ({store_path()}).\n"
            "Observations accumulate as cycles run; one cycle answers none of the questions they "
            "are for, which are all about whether a rule in this harness was a good one."
        )
        if chain_stops is None:
            return nothing
        return f"{nothing}\nThis repository's chain records no stop yet either."
    lines: list[str] = []
    seen_claims: set[str] = set()
    for key, figures in summary.items():
        kind = key.split("/", 1)[0]
        if kind in DURATION_KINDS:
            lines.append(f"{key:<30} {int(figures['count']):>5} waits, mean {figures['mean'] / 60:.1f} min")
        else:
            lines.append(f"{key:<30} {int(figures['total']):>5}")
        if kind not in seen_claims:
            lines.append(f"  {CLAIMS[kind]}")
            seen_claims.add(kind)

    # The same readings, counted instead of averaged, and pooled across arms. A number printed with
    # no claim beside it gets read as a score, so this one carries its own.
    stops = sum(int(f["count"]) for k, f in summary.items() if k.split("/", 1)[0] == "waited_seconds")
    if stops or chain_stops is not None:
        if stops:
            lines.append(f"{'stops (timed, every arm)':<30} {stops:>5}")
        if chain_stops is not None:
            lines.append(f"{'stops (this repo, chained)':<30} {chain_stops:>5}")
        lines.append(f"  {STOP_COUNT_CLAIM}")
        if stops and chain_stops is not None:
            lines.append(f"  {_STOP_SOURCES}")

    # The durations of those same chained stops. Printed under their own claim rather than folded
    # into `waited_seconds`: one is how long the work sat, the other is how long the decision sat,
    # and a mean over both would answer neither question.
    if chain_stopped:
        mean = sum(chain_stopped) / len(chain_stopped)
        timed = f"{'stopped (this repo, chained)':<30} {len(chain_stopped):>5} stops, mean {mean / 60:.1f} min"
        lines.append(timed)
        lines.append(f"  {STOP_TIME_CLAIM}")
        if any(key.split("/", 1)[0] == "waited_seconds" for key in summary):
            lines.append(f"  {_STOP_TIME_SOURCES}")

    for kind, spec in ARMS.items():
        present = {key.split("/", 1)[1] for key in summary if key.startswith(f"{kind}/")}
        if present and present != spec.values:
            missing = ", ".join(sorted(spec.values - present))
            lines.append("")
            lines.append(f"  `{kind}` has no readings in: {missing}. {spec.one_sided}")
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
