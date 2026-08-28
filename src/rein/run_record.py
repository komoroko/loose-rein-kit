"""The one writer of `run_measured` — what a run of launches did, and what it cost.

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
from collections.abc import Mapping
from typing import Any

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
