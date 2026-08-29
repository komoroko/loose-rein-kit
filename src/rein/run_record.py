"""The one writer of `run_measured` — and its one reader. What a run of launches did, and cost.

Two runs in this system launch models: `rein build` and `rein review generate`. Both ended by
appending a `run_measured` event, both said in their own docstring that the event exists so that
summing it over a cycle gives the cycle's total, and both wrote a **different set of keys** —
the build's carried byte counters and no `outcome`, the review's carried an `outcome` and a plan
and no byte counters. `event.schema.json` does not constrain `detail`, so nothing caught it, and
a total nobody could compute was the whole point of writing them.

One shape, written here:

    {kind, run_id, outcome, plan?, by_role?, billed_by_role?, reused_by_role?}

- `kind` — which run this was (`build` / `review`). Two producers under one event name is fine as
  long as a reader can tell them apart.
- `outcome` — how it ended, in that run's own vocabulary. Recorded for **every** ending: the two
  runs that most need measuring are the ones that used to record nothing, a regeneration whose
  output came out byte-identical (launches paid for, no artefact event) and a failure, which is
  the most expensive outcome there is.
- `by_role` — whatever the run counts per role that is not a provider bill (the build's prompt and
  handover bytes). The totals the build used to write beside this were the sums of these columns,
  and a sum recorded next to its own addends is a field that can disagree with itself.
- `billed_by_role` / `reused_by_role` — what the provider charged, and what was replayed from a
  cache rather than launched. `measured: false` is the record for an adapter that reports nothing;
  summing this must never read a silent role as a free one.

Nothing is appended when a run never got as far as doing anything — no cycle to record under, or
nothing measured at all. That run did not measure zero, it measured nothing, and the failure
events are already the record of it. A failure to append is swallowed: this is the last thing a
run does, it is a measurement, and losing the number is a better outcome than replacing the run's
real result with a bookkeeping traceback.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rein import models
from rein import store as store_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: The event this module writes. In the hash-chained audit log rather than in `state.yaml` because
#: the chain never rotates: each run stays separately readable and a cycle's total is a sum over
#: it, where a `state.yaml` field would only ever hold the last run's figure.
EVENT = "run_measured"


def record(
    store: store_mod.Store,
    *,
    kind: str,
    cycle: str,
    actor: str = "",
    run_id: str,
    outcome: str,
    plan: Mapping[str, Any] | None = None,
    by_role: Mapping[str, Mapping[str, int]] | None = None,
    billed: Mapping[str, usage_mod.Usage] | None = None,
    reused: Mapping[str, usage_mod.Usage] | None = None,
) -> None:
    """Append this run's measurement, however it ended. Never raises."""
    counted = {role: dict(row) for role, row in (by_role or {}).items()}
    paid = {role: row.to_detail() for role, row in (billed or {}).items() if row.launches}
    carried = {role: row.to_detail() for role, row in (reused or {}).items() if row.launches}
    if not cycle or not (plan or counted or paid or carried):
        return
    detail: dict[str, Any] = {"kind": kind, "run_id": run_id, "outcome": outcome}
    if plan:
        detail["plan"] = dict(plan)
    if counted:
        detail["by_role"] = counted
    if paid:
        detail["billed_by_role"] = paid
    if carried:
        detail["reused_by_role"] = carried
    try:
        with store.transaction() as tx:
            tx.append(EVENT, cycle_id=cycle, actor=actor, detail=detail)
    except Exception as exc:  # noqa: BLE001 — a bookkeeping write must not replace the outcome
        logger.warning(f"could not record what this {kind} run cost: {exc}")


# --- reading it back ----------------------------------------------------------
#
# The docstring above has always said a cycle's total is the sum of these events. Nothing summed
# them: one writer, no readers. So "where did the tokens go" had no answer inside the repository
# that recorded it, and the question got answered by installing things on faith instead.
#
# The reader lives here rather than in `events.py` because the shape of the detail is this
# module's own — splitting "what is written" from "what it means" across two files is how the two
# producers drifted apart in the first place.


@dataclass(frozen=True)
class CycleCost:
    """One cycle's measured spend, by role, from every `run_measured` under it.

    `billed` and `reused` are kept apart because they are different facts: one is what the
    provider charged, the other is what a cache replayed and nobody paid for a second time. Adding
    them would make a well-cached cycle look like an expensive one.
    """

    cycle_id: str
    runs: int = 0
    billed: dict[str, usage_mod.Usage] = field(default_factory=dict)
    reused: dict[str, usage_mod.Usage] = field(default_factory=dict)
    #: Where these events were read from. Empty for the live chain, else the archive's path.
    source: str = ""


def _fold(into: dict[str, usage_mod.Usage], rows: Any) -> None:
    """Add one event's per-role block into a running total. Unmeasured stays unmeasured.

    `Usage.from_detail` reads `measured: false` back as an unavailable row carrying its launch
    count, and `Usage.__add__` ORs `available` — so a role whose adapter reports nothing keeps
    saying so however many runs are summed, instead of quietly becoming a row of zeros.
    """
    if not isinstance(rows, Mapping):
        return
    for role, detail in rows.items():
        if isinstance(detail, Mapping):
            usage_mod.merged(into, str(role), usage_mod.Usage.from_detail(detail))


def costs(sources: Iterable[tuple[str, Sequence[models.Event]]]) -> list[CycleCost]:
    """Every cycle's spend, in the order `sources` is given — so a reader sees the trend.

    `sources` is `(where it was read from, its events)`: whatever archived cycles the caller
    found, oldest first, and the live chain last (`events.cost_sources` orders them). Ordering is
    the caller's because only the caller knows which chain is which; this function will not
    re-sort by a cycle id it has no calendar for. A cycle id appearing in two sources would be two
    different chains saying different things, so they stay separate rows rather than merged.
    """
    order: list[tuple[str, str]] = []
    runs: dict[tuple[str, str], int] = {}
    billed: dict[tuple[str, str], dict[str, usage_mod.Usage]] = {}
    reused: dict[tuple[str, str], dict[str, usage_mod.Usage]] = {}
    for source, events in sources:
        for event in events:
            if event.event != EVENT:
                continue
            key = (source, event.cycle_id)
            if key not in runs:
                order.append(key)
                runs[key], billed[key], reused[key] = 0, {}, {}
            runs[key] += 1
            _fold(billed[key], event.detail.get("billed_by_role"))
            _fold(reused[key], event.detail.get("reused_by_role"))
    return [
        CycleCost(cycle_id=cycle, runs=runs[key], billed=billed[key], reused=reused[key], source=source)
        for key in order
        for source, cycle in (key,)
    ]


#: What `render_costs` says when there is nothing to say. Not an error: a repository that has not
#: run a build yet has measured nothing, which is different from having measured zero.
NOTHING_RECORDED = (
    "no run has recorded what it cost yet — `rein build` and `rein review generate` write `run_measured` when they end."
)


def render_costs(rows: Sequence[CycleCost], *, unreadable: Sequence[str] = ()) -> str:
    """The cost report, one block per cycle. `unreadable` names sources that could not be counted.

    An archive whose chain is damaged is named rather than skipped in silence: leaving it out
    quietly would make the totals read as the whole history, which is the same lie as pricing an
    unmeasured role at zero.
    """
    blocks: list[str] = []
    for row in rows:
        head = f"{row.cycle_id} — {row.runs} run(s)" + (f"  [{row.source}]" if row.source else "")
        lines = [head]
        for label, measured, charged in (("billed", row.billed, True), ("replayed", row.reused, False)):
            if line := usage_mod.summarize(measured, what=label, charged=charged):
                lines.append(f"  {line}")
        if len(lines) == 1:
            # A run that recorded its plan and then launched nothing. A bare header under it would
            # read as a cost of zero rather than as an absence of one.
            lines.append("  nothing measured — these runs recorded no launch")
        blocks.append("\n".join(lines))
    if not blocks:
        blocks.append(NOTHING_RECORDED)
    for path in unreadable:
        blocks.append(f"! {path}: the audit chain is damaged — this cycle is NOT counted above.")
    return "\n\n".join(blocks)
