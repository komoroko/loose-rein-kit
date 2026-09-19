"""`rein lens` — read the library, see what this cycle would apply, and count what it found.

Three questions, and the third is the one that keeps the library from rotting. `--list` is what
exists. `--select <stage>` is what would be pointed at this cycle's deliverable and what a human is
being asked about. `--stats` is how often each lens was applied and how often it found something,
read out of the audit chain across every cycle this repository has archived.

The stats have no threshold attached and never will. A ceiling on how many lenses may exist gets
answered by deleting whichever is cheapest to delete, not whichever has stopped earning its place;
what a person needs is the name of the lens that has been applied eleven times and found nothing,
so they can look at it and decide whether the condition is wrong or the cause is gone.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

from rein import common, event_chain, lens_judge, lenses, models, review_reading
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)


def stats(events: Sequence[models.Event]) -> dict[str, dict[str, int]]:
    """Per lens: how often it was selected into a plan, applied, and found something.

    `selected` counts `lens_selected`, which names every lens the resolution wrote into the plan —
    the `proposed` ones as much as the `applied` ones. It is here because the retirement rule could
    not see a whole class of lens without it. A lens a human drops at the gate is deleted from the
    plan before the freeze, so it leaves no `lens_applied` behind and reads exactly like a lens
    whose condition never held: both are simply absent. The difference was always on record — the
    `lens_selected` event still names it — and this tally was reading the other event.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"selected": 0, "applied": 0, "found": 0})
    for event in events:
        if event.event == "lens_selected":
            for lens_id in event.subject_ids:
                counts[str(lens_id)]["selected"] += 1
            continue
        if event.event != "lens_applied":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        lens_id = str(detail.get("lens") or "")
        if not lens_id:
            continue
        counts[lens_id]["applied"] += 1
        if detail.get("found") is True:
            counts[lens_id]["found"] += 1
    return dict(counts)


#: Said wherever the output invites an edit to the library. The counts come from this repository's
#: audit chain and its archives; the library they point at is user-global. `--stats` used to end
#: "Narrow it in <user-global path>, or drop it" on the strength of one repository's numbers, so
#: the reading it directed was wider than the reading it had. Which range the statistics should
#: cover is a separate question (`20-open.md` item 9); saying which one they *do* cover is not.
_SCOPE_NOTE = (
    "These counts are this repository's chain and archives only. The library is shared across "
    "every repository you use, so check the others before narrowing, dropping or adding anything "
    "in it."
)

#: The other half. Everything above is a reason to remove a lens: a tally over lenses that exist
#: cannot see the one nobody wrote, so a library read through this output alone only ever shrinks —
#: with no threshold anywhere, which is what makes the drift quiet. Entry is a human's judgement at
#: a named occasion rather than a verb here, because the material for it (what went wrong this
#: cycle, against what was being watched for) is what the retrospective already puts side by side.
_ENTRY_NOTE = (
    "This tally can only ever argue for removal — it counts the lenses you have, never the one "
    "that was missing. Entry is section 2 of docs/retrospective.md, held against the root causes "
    "in section 1: rework whose cause no applied lens was watching for is a lens to write, by "
    "hand, into {path}."
)


def render_stats(
    counts: Mapping[str, Mapping[str, int]],
    library: Sequence[lenses.Lens],
    *,
    events: Sequence[models.Event] = (),
) -> str:
    known = {lens.id: lens for lens in library}
    rows = sorted(counts.items(), key=lambda kv: (-kv[1]["applied"], kv[0]))
    # Computed before the empty case, because a chain can hold verdicts and no applications: a
    # cycle whose decider answered and whose reviewers have not recorded anything yet. Returning
    # "nothing to say" there would hide the half that *is* there.
    decided = render_verdicts_stats(verdicts(events), events)
    if not rows:
        empty = "no lens has been selected or applied yet — nothing to say about which ones earn their place"
        return f"{empty}\n{decided}\n\n{_SCOPE_NOTE}" if decided else empty
    lines = [f"{'lens':<32} {'selected':>8} {'applied':>8} {'found':>6}  condition"]
    for lens_id, count in rows:
        lens = known.get(lens_id)
        where = lens.applies_when if lens is not None else "(no longer in the library)"
        lines.append(f"{lens_id:<32} {count['selected']:>8} {count['applied']:>8} {count['found']:>6}  {where}")
    silent = [lens_id for lens_id, c in rows if c["applied"] >= 2 and c["found"] == 0]
    if silent:
        lines.append("")
        lines.append(
            f"{len(silent)} lens(es) applied and never found anything: {', '.join(silent)}. "
            "Either the condition is wider than the failure, or the cause is gone. Narrow it in "
            f"{lenses.library_path()}, or drop it."
        )
    # Selected and never recorded as applied. Three things produce it and the tally cannot tell
    # them apart, so it names the count and not a cause: a human dropped it at the gate, the
    # reviewer never ran `--record`, or the cycle is still open. Naming it is what makes the first
    # one visible at all — a lens whose condition is wide enough to keep being proposed and keep
    # being dropped costs a judgement every cycle and reads, today, as a lens that never came up.
    unapplied = [lens_id for lens_id, c in rows if c["selected"] > 0 and c["applied"] == 0]
    if unapplied:
        lines.append("")
        lines.append(
            f"{len(unapplied)} lens(es) selected into a plan and never recorded as applied: "
            f"{', '.join(unapplied)}. Dropped at the gate, not recorded by the reviewer, or still "
            "in an open cycle — this tally cannot tell which, only that it was not simply absent."
        )
    # Before the two notes, never after: both of them are about what this whole report may be read
    # to justify, and a section that arrived after the scope note would be a set of numbers with
    # nothing saying how far they reach (CR-8's rule, applied to the half added later).
    if decided:
        lines.append(decided)
    lines.append("")
    lines.append(_ENTRY_NOTE.format(path=lenses.library_path()))
    lines.append("")
    lines.append(_SCOPE_NOTE)
    return "\n".join(lines)


# --- where each lens went, and where it did not ------------------------------------
#
# The counts say which lenses earn their place. They cannot say *where* a lens went, and that is
# the question a person asks after a review: this task was read for these six things and not for
# those nineteen — why not? Every answer is already in the chain and the frozen plan; what was
# missing was a shape that put them beside each other.

#: The states a cell may take. **A cell reports what the record says, never why.** Three of these
#: are an absence with a different provenance, and the difference is exactly what the chain can
#: still tell: `dropped` is named in `lens_selected` and gone from the frozen plan, `absent` is in
#: neither, `pending` is in both and has no application recorded — which is a reviewer that has not
#: reported, a cycle that is still open, or a lens nobody got to. The tally says the same thing
#: about the same three, and neither of them guesses between them.
FOUND = "found"
APPLIED = "applied"
NARROWED = "narrowed"
DECLINED = "declined"
UNJUDGED = "unjudged"
DROPPED = "dropped"
ABSENT = "absent"
PENDING = "pending"

#: One line each, for the reader who is looking at a colour and wants to know what it claims.
CELL_MEANING: dict[str, str] = {
    FOUND: "applied, and it found something",
    APPLIED: "applied, and it found nothing — which is a fact about this change, not about the lens",
    NARROWED: "its paths are outside this task's scope, so the hand-off dropped it",
    DECLINED: "a verdict put its condition below the threshold, and the settings let that remove it",
    UNJUDGED: "its condition takes reading the deliverable and the decider could not be asked",
    DROPPED: "frozen into no plan: named in the selection and removed before the mandate closed",
    ABSENT: "its condition did not hold for this cycle at all",
    PENDING: "frozen and not recorded as applied — not yet, not reported, or not by this reviewer",
}

#: The column for a lens whose stage has no tasks in it. `requirements`, `design` and `tasks` are
#: judged against the plan as a whole, and giving them a column per task would copy one answer
#: across the row and invite it to be read as several.
PLAN_COLUMN = "(plan)"


def _applications(events: Sequence[models.Event]) -> dict[tuple[str, str], bool]:
    """`{(lens, task): found}` — `task` is `""` for one recorded without a place."""
    out: dict[tuple[str, str], bool] = {}
    for event in events:
        if event.event != "lens_applied":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        lens_id = str(detail.get("lens") or "")
        if not lens_id:
            continue
        key = (lens_id, str(detail.get("task") or ""))
        out[key] = bool(out.get(key)) or detail.get("found") is True
    return out


def _verdict_state(events: Sequence[models.Event]) -> dict[tuple[str, str], tuple[str, float | None]]:
    """`{(lens, task): (outcome, probability)}` from the recorded verdicts, with `may_drop` folded in.

    A `does_not_hold` that the settings did not act on is not a cell state: the lens went to the
    reviewer, and the grid has to show where it went. The probability travels with it so the cell
    can say how far from the line it was without the reader opening the chain.
    """
    out: dict[tuple[str, str], tuple[str, float | None]] = {}
    for event in events:
        if event.event != "lens_judged":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        task = str(detail.get("task") or "")
        may_drop = detail.get("may_drop") is True
        rows = detail.get("verdicts")
        if not isinstance(rows, list):
            continue
        for row in rows:
            verdict = lens_judge.Verdict.from_dict(row) if isinstance(row, Mapping) else None
            if verdict is None:
                continue
            if verdict.outcome == lens_judge.UNAVAILABLE:
                out[(verdict.lens_id, task)] = (UNJUDGED, None)
            elif verdict.outcome == lens_judge.DOES_NOT_HOLD and may_drop:
                out[(verdict.lens_id, task)] = (DECLINED, verdict.probability)
    return out


def grid(
    plan: Any,
    library: Sequence[lenses.Lens],
    events: Sequence[models.Event],
    *,
    tracked: Sequence[str] = (),
) -> dict[str, Any]:
    """Task by lens, for this cycle, out of the chain and the frozen plan.

    Built from the audit chain rather than the observation store, which is what makes it answerable
    for a cycle nobody had the dashboard open during — the same reason the stop counts and the
    stopped time moved there. Nothing here is a new record: `lens_selected` says what the
    resolution wrote, the frozen plan says what survived the gate, `lens_judged` says what a
    decider was asked, `lens_applied` says what a reviewer did, and the path narrowing is
    recomputed from the scopes the plan already froze.
    """
    selected: set[str] = set()
    for event in events:
        if event.event == "lens_selected":
            selected |= {str(lens_id) for lens_id in event.subject_ids}
    frozen_ids = {entry.id for entry in plan.lenses} if plan is not None else set()
    applications = _applications(events)
    verdicts_by = _verdict_state(events)
    tasks = [task.id for task in plan.tasks] if plan is not None else []
    known = {lens.id: lens for lens in library}

    def cell(lens: lenses.Lens, task: Any, column: str) -> dict[str, Any]:
        for key in ((lens.id, column), (lens.id, "")):
            if key in applications:
                return {"state": FOUND if applications[key] else APPLIED}
        if lens.id not in frozen_ids:
            return {"state": DROPPED if lens.id in selected else ABSENT}
        if (
            task is not None
            and task.scope_include
            and not lens.when.paths_hold(lenses.scope_paths(task.scope_include, tracked))
        ):
            return {"state": NARROWED}
        for key in ((lens.id, column), (lens.id, "")):
            if key in verdicts_by:
                state, probability = verdicts_by[key]
                return {"state": state, **({"probability": probability} if probability is not None else {})}
        return {"state": PENDING}

    rows: list[dict[str, Any]] = []
    for lens in library:
        columns: list[dict[str, Any]] = []
        if lens.stage == "code" and tasks:
            for task in plan.tasks:
                columns.append({"column": task.id, **cell(lens, task, task.id)})
        else:
            columns.append({"column": PLAN_COLUMN, **cell(lens, None, "")})
        rows.append(
            {
                "lens": lens.id,
                "stage": lens.stage,
                "class": lens.lens_class,
                "applies_when": known[lens.id].applies_when if lens.id in known else "",
                "cells": columns,
            }
        )
    placeless = sorted({lens_id for lens_id, task in applications if not task})
    return {
        "columns": [PLAN_COLUMN, *tasks],
        "rows": rows,
        "meaning": CELL_MEANING,
        # Read where the reader is about to be invited to edit something (CR-8). The library is
        # user-global; these cells are one repository's cycle.
        "scope_note": _SCOPE_NOTE,
        # Named rather than placed. An application recorded before `--stage`/`--task` existed, or by
        # a reviewer that did not pass them, belongs to no column — putting it in one would be the
        # grid inventing a fact the record does not hold.
        "unplaced": placeless,
    }


def render_grid(built: Mapping[str, Any]) -> str:
    """The grid at a terminal. Same cells, same refusal to say why."""
    rows = built["rows"]
    if not rows:
        return "no lens in the library — nothing to place"
    columns: list[str] = list(built["columns"])
    width = max(len(row["lens"]) for row in rows)
    head = f"{'lens':<{width}}  " + "  ".join(f"{column:<12}" for column in columns)
    lines = [head]
    for row in rows:
        by_column = {cell["column"]: cell for cell in row["cells"]}
        cells = "  ".join(f"{by_column.get(column, {}).get('state', '-'):<12}" for column in columns)
        lines.append(f"{row['lens']:<{width}}  {cells}")
    lines.append("")
    for state, meaning in built["meaning"].items():
        lines.append(f"  {state:<10} {meaning}")
    if built["unplaced"]:
        lines.append("")
        lines.append(
            f"{len(built['unplaced'])} application(s) recorded without a stage or task: "
            f"{', '.join(built['unplaced'])}. They count, and they are shown in every column they "
            "could belong to rather than placed in one."
        )
    lines.append("")
    lines.append(built["scope_note"])
    return "\n".join(lines)


# --- what a verdict would have been at another threshold ---------------------------


#: What the counterfactual is computed at, beside whatever was in force. Fixed rather than derived
#: from the recorded probabilities: a grid that moves with the data cannot be compared between two
#: readings of it, and the question this answers — "what would I get if I moved the knob" — is
#: about settings somebody might choose, not about the values that happened to come back.
THRESHOLDS: tuple[float, ...] = (0.3, 0.5, 0.7, 0.9)


def verdicts(events: Sequence[models.Event]) -> list[tuple[str, str, float | None, float]]:
    """`(cycle_id, lens_id, probability, threshold)` for every verdict in these events.

    Flat rather than grouped because both readers want different groupings, and a shape that
    already picked one would make the second reader undo it.
    """
    out: list[tuple[str, str, float | None, float]] = []
    for event in events:
        if event.event != "lens_judged":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        threshold = detail.get("threshold")
        in_force = float(threshold) if isinstance(threshold, (int, float)) and not isinstance(threshold, bool) else 0.5
        rows = detail.get("verdicts")
        if not isinstance(rows, list):
            continue
        for row in rows:
            verdict = lens_judge.Verdict.from_dict(row) if isinstance(row, Mapping) else None
            if verdict is None:
                continue
            out.append((str(event.cycle_id or ""), verdict.lens_id, verdict.probability, in_force))
    return out


def verdict_counts(rows: Sequence[tuple[str, str, float | None, float]]) -> dict[str, dict[str, int]]:
    """Per lens: judged, and how the answers fell. `unavailable` is counted, never folded.

    A decider that was never reachable and one that decided nothing applies are different facts,
    and a tally that cannot tell them apart reads an outage as a finding about the library.
    """
    counts: dict[str, dict[str, int]] = defaultdict(
        lambda: {"judged": 0, "holds": 0, "does_not_hold": 0, "unavailable": 0}
    )
    for _cycle, lens_id, probability, threshold in rows:
        counts[lens_id]["judged"] += 1
        if probability is None:
            counts[lens_id]["unavailable"] += 1
        elif probability >= threshold:
            counts[lens_id]["holds"] += 1
        else:
            counts[lens_id]["does_not_hold"] += 1
    return dict(counts)


def at_threshold(rows: Sequence[tuple[str, str, float | None, float]], threshold: float) -> tuple[int, int]:
    """`(holds, does_not_hold)` this set of probabilities would have given at `threshold`.

    Only the verdicts that carried a probability. An `unavailable` one is not a number on the
    wrong side of a line — nothing was asked, or nothing came back, and no setting of the knob
    would have changed that.
    """
    answered = [probability for _cycle, _lens, probability, _t in rows if probability is not None]
    holds = sum(1 for probability in answered if probability >= threshold)
    return holds, len(answered) - holds


def found_anyway(rows: Sequence[tuple[str, str, float | None, float]], events: Sequence[models.Event]) -> list[str]:
    """Lenses a verdict placed below the line, that found something in the same cycle regardless.

    **The other arm, and a lower bound rather than a rate.** The first arm — a lens that keeps
    being applied and never finds — is already in the main tally, and reading only that one makes a
    narrowing selection look better the more it removes. This is what the chain can say about the
    opposite error: the lens was judged not to apply *somewhere*, and somewhere else in the same
    cycle it was applied and did find something.

    What it cannot say is anything about a lens nothing else looked for. That is the whole of the
    counterfactual and it is not in any record, which is why this counts occurrences and never
    divides by anything.
    """
    below: set[tuple[str, str]] = {
        (cycle, lens_id)
        for cycle, lens_id, probability, threshold in rows
        if probability is not None and probability < threshold
    }
    if not below:
        return []
    found: set[tuple[str, str]] = set()
    for event in events:
        if event.event != "lens_applied":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        if detail.get("found") is True:
            found.add((str(event.cycle_id or ""), str(detail.get("lens") or "")))
    return sorted({lens_id for cycle, lens_id in below & found})


def render_verdicts_stats(rows: Sequence[tuple[str, str, float | None, float]], events: Sequence[models.Event]) -> str:
    """The verdict half of `--stats`: what was decided, and what another threshold would have given.

    The point of printing the counterfactual is that **a threshold is a knob somebody has to be
    able to move, and moving it is only an informed act beside the distribution it would have been
    applied to.** Which side of the line a verdict fell on says nothing about how far, and a report
    that showed only the outcomes would leave the knob to be turned by feel.

    Nothing here moves it. There is no path from this function to `config.yaml`, and the threshold
    in force is frozen with the mandate — the settings a reader might choose are shown, and a
    person chooses.
    """
    if not rows:
        return ""
    counts = verdict_counts(rows)
    in_force = sorted({threshold for _c, _l, _p, threshold in rows})
    force_note = f"{in_force[0]:.2f}" if len(in_force) == 1 else ", ".join(f"{t:.2f}" for t in in_force)
    lines = [
        "",
        f"{len(rows)} verdict(s) on {len(counts)} conditional lens(es), at threshold {force_note}:",
        f"{'lens':<32} {'judged':>7} {'holds':>7} {'does not':>9} {'unavailable':>12}",
    ]
    for lens_id, count in sorted(counts.items(), key=lambda kv: (-kv[1]["judged"], kv[0])):
        lines.append(
            f"{lens_id:<32} {count['judged']:>7} {count['holds']:>7} "
            f"{count['does_not_hold']:>9} {count['unavailable']:>12}"
        )
    answered = sum(1 for _c, _l, probability, _t in rows if probability is not None)
    if answered:
        lines.append("")
        lines.append(f"the same {answered} answered verdict(s), had the threshold been set elsewhere:")
        for threshold in THRESHOLDS:
            holds, misses = at_threshold(rows, threshold)
            mark = "  <- in force" if len(in_force) == 1 and abs(threshold - in_force[0]) < 1e-9 else ""
            lines.append(f"  {threshold:.2f}   {holds:>4} would hold, {misses:>4} would not{mark}")
        lines.append(
            "  Moving it is a human edit to `review_policy.lens_judgement.threshold`, which the "
            "mandate freezes. Nothing here changes it, and nothing reads these numbers to decide "
            "anything."
        )
    missed = found_anyway(rows, events)
    if missed:
        lines.append("")
        lines.append(
            f"{len(missed)} lens(es) a verdict placed below the line, that found something in the "
            f"same cycle anyway: {', '.join(missed)}. **A lower bound, not a rate** — it can only "
            "see a lens that was applied somewhere else in the same cycle, and says nothing about "
            "one nothing else looked for. Read it beside the tally above, which is the opposite "
            "error: a selection that removes too much and one that removes too little do not show "
            "up in the same number."
        )
    return "\n".join(lines)


# --- the conditions that take reading --------------------------------------------


def _text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _deliverable(repo: repo_mod.Repo, plan: models.Plan, state: models.State | None, stage: str, task_id: str) -> str:
    """What a `conditional` lens's prose condition is decided against, for one stage.

    Each of the six packaged ones names its own source and says the plan is not it: the
    requirements, the design document, `docs/decisions/`, the tickets, the diff. So this is not a
    second reading of `plan.yaml` — everything decidable from the plan is a `standard` lens's
    `when:` block, already settled before anyone gets here.

    An empty string is the honest answer for a deliverable that is missing, unreadable, or (at the
    code stage) a diff git could not produce, and it ends in `unavailable` rather than in a verdict.
    """
    root = repo.root
    if stage == "requirements":
        return _text(root / "docs" / "10-requirements.md")
    if stage == "design":
        # The ADR lens asks whether a rejected option's downside is underplayed, and says the plan
        # does not index `docs/decisions/`. Nothing else would carry that.
        parts = [_text(root / "docs" / "20-design.md")]
        parts += [_text(adr) for adr in sorted((root / "docs" / "decisions").glob("ADR-*.md"))]
        return "\n\n".join(part for part in parts if part.strip())
    if stage == "tasks":
        tickets = sorted((root / "docs" / "tasks").glob("T-*.md"))
        if task_id:
            tickets = [ticket for ticket in tickets if ticket.stem == task_id]
        return "\n\n".join(part for part in (_text(ticket) for ticket in tickets) if part.strip())
    if stage != "code":
        return ""
    base = plan.base_commit
    if not base or set(base) == {"0"}:
        return ""  # a cycle that never recorded what it branched from has no diff to read
    include: tuple[str, ...] = ()
    if task_id:
        task = next((t for t in plan.tasks if t.id == task_id), None)
        include = tuple(task.scope_include) if task is not None else ()
    try:
        return review_reading.diff_of(repo, base, "HEAD", review_reading.not_the_product(repo, state), include=include)
    except Exception as exc:  # noqa: BLE001 - every failure here is "review more than necessary"
        logger.warning(f"the diff this stage's conditions are decided against could not be read: {exc}")
        return ""


def _recorded(events: Sequence[models.Event], stage: str, task_id: str) -> list[lens_judge.Verdict]:
    """The verdicts already in the chain for this hand-off, newest first, or `[]`.

    Asked once. The selection is resolved once against the plan and read back from it for the rest
    of the cycle, and a judgement re-run on every call would put the review's inputs back where the
    freeze took them from — a reviewer and the person who approved the gate could be looking at two
    different answers to the same question, with nothing saying so.
    """
    for event in reversed(events):
        if event.event != "lens_judged":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        if str(detail.get("stage") or "") != stage or str(detail.get("task") or "") != task_id:
            continue
        raw = detail.get("verdicts")
        if not isinstance(raw, list):
            return []
        found = [lens_judge.Verdict.from_dict(row) for row in raw if isinstance(row, Mapping)]
        return [verdict for verdict in found if verdict is not None]
    return []


def render_verdicts(verdicts: Sequence[lens_judge.Verdict], settings: lens_judge.Settings) -> str:
    """What was asked, what came back, and — when it changed nothing — that it changed nothing."""
    lines: list[str] = []
    for verdict in verdicts:
        at = "" if verdict.probability is None else f" p={verdict.probability:.2f}"
        note = f" ({verdict.reason})" if verdict.reason else ""
        lines.append(f"  {verdict.lens_id}: {verdict.outcome}{at}{note}")
    if not settings.may_drop and any(v.outcome == lens_judge.DOES_NOT_HOLD for v in verdicts):
        lines.append(
            "  (recorded only — `review_policy.lens_judgement.may_drop` is off, so none of these "
            "was removed from the hand-off)"
        )
    return "\n".join(lines)


def _judge_handoff(
    repo: repo_mod.Repo,
    store: store_mod.Store,
    plan: models.Plan,
    state: models.State | None,
    stage: str,
    task_id: str,
    proposed: Sequence[lenses.Lens],
    settings: lens_judge.Settings,
) -> tuple[list[lens_judge.Verdict], list[lenses.Lens]]:
    """`(verdicts, what still goes to the reviewer)` for the `conditional` half of a hand-off.

    This is the place because it is where the narrowing already happens. The selection is resolved
    once, against the whole plan, and that unit is right — `min_claims` counts what the plan states
    and no single task has a value for it. What was wrong was handing one list to readers with
    different reach, and the fix put the narrowing at the hand-off. A condition that takes reading
    the deliverable belongs at the same point, and for the same reason: **this is the first moment
    the deliverable exists.**

    Asked once per stage and task. A judgement re-run on every call would let a reviewer and the
    person who approved the gate hold two different answers to one question, which is the property
    the freeze exists to remove.
    """
    questions = {lens.id: lens.applies_when for lens in proposed if lens.applies_when}
    if not questions:
        return [], list(proposed)
    live, _ = event_chain.scan(repo.events)
    verdicts = _recorded(live, stage, task_id)
    if not verdicts:
        if not settings.configured:
            # Nothing is asked and nothing is recorded. The lenses stay candidates, which is what
            # this repository does today, and an event per call saying so would fill the chain with
            # the absence of a feature.
            return [], list(proposed)
        verdicts = lens_judge.judge(
            settings,
            state=_deliverable(repo, plan, state, stage, task_id),
            questions=questions,
        )
        with store.transaction() as tx:
            tx.append(
                "lens_judged",
                cycle_id=state.cycle_id if state is not None else "",
                subject_ids=sorted(questions),
                detail={
                    "stage": stage,
                    "task": task_id,
                    # Recorded beside the verdicts rather than looked up later: `config.yaml` is
                    # frozen at the mandate, but a reading of this chain years from now should not
                    # have to reconstruct which value was in force to know what the probabilities
                    # were compared against.
                    "threshold": settings.threshold,
                    "may_drop": settings.may_drop,
                    "verdicts": [verdict.as_dict() for verdict in verdicts],
                },
            )
    removed = lens_judge.dropped(verdicts, settings)
    return verdicts, [lens for lens in proposed if lens.id not in removed]


def _select(repo: repo_mod.Repo, library: Sequence[lenses.Lens], stage: str, task_id: str = "") -> int:
    """Print the selection for `stage`, freezing it into the plan the first time it is resolved.

    The library is user-global and a person edits it between cycles. If the reviewer at the code
    stage re-derived its own list, a change to one line of an overlay would change what this cycle
    was reviewed for, with nothing in the audit chain saying so — and the same repository would
    answer differently on another laptop. So the selection is resolved once, against the plan, and
    written into the plan, where the mandate freezes it with everything else.

    Before the freeze that write is re-done on every call, because the facts it is resolved against
    grow: `/req` runs with no tasks in the plan yet and `/tasks` runs with all of them. The last
    write before the mandate is the one that gets frozen. After the freeze nothing is re-derived.
    """
    store = store_mod.Store(repo)
    try:
        plan = store.read_plan()
    except models.DocumentError as exc:
        logger.error(str(exc))
        return 2
    if plan is None:
        logger.error("no .rein/plan.yaml — the selection is resolved against the plan")
        return 2
    state = store.read_state()
    frozen_plan = state is not None and "mandate" in state.approved_gates

    # What the scope entries in this plan actually cover. `None` is git failing to answer, which is
    # said out loud rather than passed on as an empty listing: a path condition decided against no
    # files at all holds nowhere, and a selection that quietly lost its path lenses is the failure
    # this whole hand-off exists to stop.
    tracked: tuple[str, ...] = ()
    if not frozen_plan or task_id:  # the only two readings that resolve a `paths` condition
        listing = repo.tracked_paths()
        if listing is None:
            logger.warning(
                "git could not list this repository's files, so a lens condition on `paths` is decided "
                "against the scope entries alone and may hold for fewer tasks than it should."
            )
        tracked = listing or ()

    if not frozen_plan:
        resolved = lenses.resolve(library, lenses.Facts.of(plan, tracked=tracked))
        lens_changed = resolved != [dict(entry.raw) for entry in plan.lenses]
        # The reaches this draft carries right now, against the last ones rein saw. This verb is
        # where the snapshot lives because it is the one plan-reading command every drafting
        # command already runs (`/req`, `/design`, `/tasks`), and it is the only "before" the
        # freeze can be compared against: a human moving a decision from `mandate` to `local` edits
        # the file, and nothing else in the harness is watching when they do.
        reaches = {d.id: d.reach for d in plan.decisions}
        live, _ = event_chain.scan(repo.events)
        reach_changed = reaches != event_chain.derived_reaches(live)
        if lens_changed or reach_changed:
            cycle_id = state.cycle_id if state is not None else ""
            with store.transaction() as tx:
                # Only when the selection actually moved. A snapshot of the reaches is a record
                # *about* the draft and changes nothing in it, so pairing it with a plan write
                # would re-write the document every time a decision was added.
                if lens_changed:
                    raw = json.loads(json.dumps(dict(plan.raw)))
                    raw["lenses"] = resolved
                    tx.write("plan", raw, expect_digest=store_mod.read_digest(plan))
                    tx.append(
                        "lens_selected",
                        cycle_id=cycle_id,
                        subject_ids=sorted({entry["id"] for entry in resolved}),
                        detail={"count": len(resolved), "stages": sorted({e["stage"] for e in resolved})},
                    )
                if reach_changed:
                    tx.append(
                        "decisions_derived",
                        cycle_id=cycle_id,
                        subject_ids=sorted(reaches),
                        detail={"reaches": reaches},
                    )
            if lens_changed:
                plan = store.read_plan()
                if plan is None:  # written and then unreadable: a defect, not a selection
                    logger.error("the plan could not be read back after writing the lens selection")
                    return 2

    applied_ids, proposed_ids = lenses.frozen(plan, stage=stage)
    applied, missing_applied = lenses.by_id(library, applied_ids)
    proposed, missing_proposed = lenses.by_id(library, proposed_ids)

    where = "frozen with the mandate" if frozen_plan else "resolved against this plan and written into it"
    narrowed = ""
    if task_id:
        task = next((t for t in plan.tasks if t.id == task_id), None)
        if task is None:
            # Refused rather than falling back to the whole list: a reviewer that asked for one
            # task's lenses and silently got every task's would be reviewing against a wider
            # selection than it thinks, with nothing on screen saying so.
            logger.error(f"no task {task_id!r} in this plan — `rein dag` names the ones it holds")
            return 2
        before = len(applied) + len(proposed)
        applied = lenses.for_task(applied, task, tracked=tracked)
        proposed = lenses.for_task(proposed, task, tracked=tracked)
        dropped = before - len(applied) - len(proposed)
        narrowed = f", narrowed to {task_id}"
        if dropped:
            narrowed += f" — {dropped} dropped, their paths are outside this task's scope"

    # The `conditional` half, decided against the thing its condition names. Only after the freeze:
    # before it there is no design document, no ticket and no diff to read, which is the whole
    # reason these lenses could not be settled from the plan in the first place.
    verdicts: list[lens_judge.Verdict] = []
    settings = lens_judge.Settings.of(store.read_config())
    if frozen_plan and proposed:
        verdicts, proposed = _judge_handoff(repo, store, plan, state, stage, task_id, proposed, settings)

    print(f"{stage}: {len(applied)} applied, {len(proposed)} proposed ({where}{narrowed})")
    if applied:
        print("\napplied (the condition is decidable, so nobody is asked):")
        print(lenses.render(applied))
    if proposed:
        print("\nproposed (deciding the condition takes judgement — keep or drop at the mandate):")
        print(lenses.render(proposed))
    if verdicts:
        print("\ndecided against this stage's deliverable:")
        print(render_verdicts(verdicts, settings))
    off = [lens.id for lens in library if lens.stage == stage and lens.lens_class == lenses.CLASS_UNCLASSIFIED]
    if off:
        print(f"\noff, no condition written down yet: {', '.join(off)}")
    if frozen_plan and not plan.lenses:
        logger.warning(
            "this mandate froze no lens selection, so there is nothing to read back. The review "
            "runs without lenses rather than against whatever the library says today; `rein revise "
            "--to mandate` reopens the plan if this cycle should have had them."
        )
    for lens_id in missing_applied + missing_proposed:
        # Named, never skipped. The frozen list is the record of what this cycle was reviewed for,
        # and an id the library no longer holds is precisely the drift the freeze exists to expose.
        logger.warning(f"{lens_id} was frozen into this plan and is no longer in the library — it cannot be applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the review lens library: what exists, what applies, what it found")
    parser.add_argument("--list", action="store_true", help="every lens in the library, packaged and your own")
    parser.add_argument("--select", metavar="STAGE", help=f"what would apply at a stage ({', '.join(lenses.STAGES)})")
    parser.add_argument(
        "--task",
        metavar="T-NNN",
        default="",
        help="narrow --select to one task: lenses whose paths are outside its scope are dropped",
    )
    parser.add_argument("--stats", action="store_true", help="applied/found counts per lens, across archived cycles")
    parser.add_argument("--record", metavar="LENS", help="record that this lens was applied (with --found)")
    parser.add_argument("--found", choices=("yes", "no"), help="whether --record's lens found anything")
    parser.add_argument(
        "--stage",
        metavar="STAGE",
        default="",
        help="which stage --record's application was at; without it the grid cannot place the row",
    )
    parser.add_argument("--grid", action="store_true", help="task by lens: where each one was applied, and where not")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    library = lenses.library()
    if args.list:
        print(f"{len(library)} lens(es) — packaged: {lenses.packaged_path()}, yours: {lenses.library_path()}")
        for lens_class in (lenses.CLASS_STANDARD, lenses.CLASS_CONDITIONAL, lenses.CLASS_UNCLASSIFIED):
            chosen = [lens for lens in library if lens.lens_class == lens_class]
            if chosen:
                print(f"\n{lens_class} ({len(chosen)}):")
                print(lenses.render(chosen))
        return 0

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    if args.task and not (args.select or args.record):
        logger.error("--task narrows --select or places --record; on its own it names a task with nothing to do")
        return 2
    if args.stage and not args.record:
        logger.error("--stage places --record; --select already names the stage it is asked about")
        return 2

    if args.select:
        if args.select not in lenses.STAGE_VALUES:
            logger.error(f"unknown stage {args.select!r} — one of {', '.join(lenses.STAGES)}")
            return 2
        return _select(repo, library, args.select, args.task)

    if args.record:
        if args.found is None:
            logger.error("--record needs --found yes|no — an application nobody scored says nothing about the lens")
            return 2
        known = {lens.id for lens in library}
        if args.record not in known:
            # Refused rather than recorded: a count against an id no library holds can never be
            # read back beside a condition, which is the only thing the count is for.
            logger.error(f"unknown lens {args.record!r} — `rein lens --list` names the ones that exist")
            return 2
        store = store_mod.Store(repo)
        state = store.read_state()
        if state is None:
            logger.error("no .rein/state.yaml — run `rein init` first")
            return 2
        if args.stage and args.stage not in lenses.STAGE_VALUES:
            logger.error(f"unknown stage {args.stage!r} — one of {', '.join(lenses.STAGES)}")
            return 2
        with store.transaction() as tx:
            tx.append(
                "lens_applied",
                cycle_id=state.cycle_id,
                subject_ids=[args.record],
                # `stage` and `task` say *where* it was applied. Without them the count is still
                # right and the grid cannot place the row: a lens applied to one task reads the
                # same as one applied to the whole cycle. Optional rather than required because a
                # recording that refuses to happen is a count lost for a field, and the grid names
                # the ones that arrived without a place rather than putting them somewhere.
                detail={
                    "lens": args.record,
                    "found": args.found == "yes",
                    **({"stage": args.stage} if args.stage else {}),
                    **({"task": args.task} if args.task else {}),
                },
            )
        where = f" at {args.stage}" if args.stage else ""
        where += f" on {args.task}" if args.task else ""
        print(f"recorded: {args.record} applied{where}, found={args.found}")
        return 0

    if args.grid:
        store = store_mod.Store(repo)
        try:
            plan = store.read_plan()
        except models.DocumentError as exc:
            logger.error(str(exc))
            return 2
        # The live cycle only. The chain of a closed one is archived and readable, but the path
        # narrowing is recomputed against the tree as it is *now*, and a scope resolved against a
        # different tree is an answer to a question nobody asked.
        live, _ = event_chain.scan(repo.events)
        print(render_grid(grid(plan, library, live, tracked=repo.tracked_paths() or ())))
        return 0

    if args.stats:
        # Across archived cycles too. A lens that has stopped earning its place stopped doing so
        # over several cycles, and `cycle-close` moves the chain that would show it into the
        # archive — reading only the live one makes the answer go blank exactly when it matters.
        live, _ = event_chain.scan(repo.events)
        sources, unreadable = events_mod.cycle_sources(repo, live)
        every = [event for source in sources for event in source.events]
        print(render_stats(stats(every), library, events=every))
        for rel in unreadable:
            logger.warning(f"{rel} could not be verified, so its lens counts are not included")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
