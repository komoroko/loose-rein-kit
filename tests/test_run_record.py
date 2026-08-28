"""`run_measured` has one shape, because it has one writer.

Two runs launch models — `rein build` and `rein review generate` — and both appended this event
with a *different* set of keys while both said, in their own docstring, that the event exists so a
cycle's cost is a sum over it. `event.schema.json` does not constrain `detail`, so nothing caught
it and the total nobody could compute was the whole point.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from rein import build_loop, event_chain, review, run_record
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod
from tests._support import make_state, seed_repo

#: Every key `run_record.record` is allowed to write. A producer that needs another one adds it
#: here, in the shape both producers share, rather than beside its own call.
FIELDS = {"kind", "run_id", "outcome", "plan", "by_role", "billed_by_role", "reused_by_role"}

#: What is on every measurement whatever the run did.
ALWAYS = {"kind", "run_id", "outcome"}


def _store(tmp_path: Path) -> store_mod.Store:
    seed_repo(tmp_path, state=make_state(project="rr", phase="build"))
    return store_mod.Store(repo_mod.Repo(tmp_path))


def _details(tmp_path: Path) -> list[dict[str, object]]:
    return [dict(e.detail) for e in event_chain.load(repo_mod.Repo(tmp_path).events) if e.event == "run_measured"]


def test_both_producers_write_the_same_vocabulary(tmp_path: Path) -> None:
    """The build's shape and the review's differ in which optional columns they can fill, and in
    nothing else. They used to differ in the mandatory ones: the build carried no `outcome` at
    all, so a failed build and a finished one were indistinguishable in the log that measured
    them."""
    store = _store(tmp_path)
    launch = usage_mod.Usage(available=True, launches=1, input_tokens=10)

    run_record.record(
        store,
        kind="build",
        cycle="c1",
        run_id="r-build",
        outcome="done",
        by_role={"implementer": {"launches": 1, "prompt_bytes": 100}},
        billed={"implementer": launch},
    )
    run_record.record(
        store,
        kind="review",
        cycle="c1",
        run_id="r-review",
        outcome="generated",
        plan={"stages": []},
        billed={"actual_extractor": launch},
        reused={"comparator": launch},
    )

    details = _details(tmp_path)
    assert [d["kind"] for d in details] == ["build", "review"]
    for detail in details:
        assert ALWAYS <= set(detail) <= FIELDS, f"unknown or missing keys: {sorted(set(detail) ^ FIELDS)}"


def test_a_run_that_did_nothing_measured_nothing_rather_than_zero(tmp_path: Path) -> None:
    """A build that took the lock and found no frontier, or a review refused at its budget before
    a single launch, did not measure zero — an event saying "0" would be a different claim, and
    the failure events are already the record of it."""
    run_record.record(_store(tmp_path), kind="review", cycle="c1", run_id="r", outcome="failed")
    assert _details(tmp_path) == []


def test_a_launch_that_reported_nothing_is_recorded_as_unmeasured(tmp_path: Path) -> None:
    """Summing this over a cycle must never read a silent adapter as a free one."""
    run_record.record(
        _store(tmp_path),
        kind="build",
        cycle="c1",
        run_id="r",
        outcome="retry-later",
        billed={"implementer": usage_mod.Usage.unavailable()},
    )
    billed = _details(tmp_path)[0]["billed_by_role"]
    assert isinstance(billed, dict)
    assert billed["implementer"] == {"launches": 1, "measured": False}


def test_a_bookkeeping_failure_never_replaces_the_run_s_own_result(tmp_path: Path) -> None:
    """It is the last thing a run does and it is a measurement: losing the number is a better
    outcome than a finished run reporting a traceback from its own accounting."""

    class Broken:
        def transaction(self) -> object:
            raise OSError("the store is gone")

    run_record.record(
        Broken(),  # type: ignore[arg-type]
        kind="review",
        cycle="c1",
        run_id="r",
        outcome="generated",
        plan={"stages": []},
    )


def test_neither_producer_appends_the_event_itself() -> None:
    """One writer is the whole fix: a second `tx.append("run_measured", ...)` anywhere is how the
    two shapes drifted apart in the first place."""
    for module in (build_loop, review):
        source = inspect.getsource(module)
        assert 'append("run_measured"' not in source, f"{module.__name__} appends the event directly"
