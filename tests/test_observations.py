"""What the harness measures about itself (plan §K).

Several rules here rest on a measurement nobody was taking. These pin the two properties that make
the store worth having: every figure is attached to a claim it could falsify, and nothing reads it
back — a cycle's outcome must not depend on what earlier cycles happened to record, or the same
repository answers differently on another machine.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

import pytest

from rein import observations


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "rein" / observations.STORE_NAME


# --- every figure has a claim it could falsify ----------------------------------


def test_every_kind_states_the_claim_it_tests() -> None:
    """A figure with no claim beside it is one somebody reads as a score. Measured is what would
    move if a design decision here were wrong, never what was easy to collect."""
    assert set(observations.CLAIMS) == set(observations.KINDS)
    for kind, claim in observations.CLAIMS.items():
        assert claim.strip(), kind


def test_an_unknown_kind_is_refused_rather_than_recorded(store: Path) -> None:
    """Closed for the same reason the event vocabulary is: a store anybody can add a key to is one
    nobody can aggregate."""
    assert observations.record("whatever_was_handy", project="p", cycle_id="c") is False
    assert not store.exists()


# --- it is a store, not an input -------------------------------------------------


def test_recording_never_raises_even_when_the_store_cannot_be_written(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every caller is doing something else — opening a gate, finishing a review. A store that can
    fail a gate would be an input to the thing it is measuring."""
    monkeypatch.setattr("rein.observations.Path.mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c") is False


def test_a_torn_line_is_skipped_rather_than_refusing_the_file(store: Path) -> None:
    """An append-only file written by long-lived processes eventually holds one. Refusing the whole
    file over it would lose every reading before it, and this is material for a judgement — not
    evidence anybody signs."""
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c-1")
    with store.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "reach_ov\n')
    observations.record("judgement_raised", project="p", cycle_id="c-1", value=3)

    kinds = [entry.kind for entry in observations.read()]
    assert kinds == ["reach_overruled", "judgement_raised"]


def test_a_well_formed_line_carrying_an_unaveragable_value_is_skipped_too(store: Path) -> None:
    """JSON that parses is not a reading that counts. `float(None)` raises, and it was raising
    outside the try — one such line and both `rein observe` and `--prune` fell over, against a
    docstring promising the opposite."""
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c-1")
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "waited_seconds", "project": "p", "cycle_id": "c", "value": None}) + "\n")

    assert [entry.kind for entry in observations.read()] == ["reach_overruled"]
    assert observations.prune(1) == 0


def test_recording_a_value_that_is_not_a_number_is_refused_rather_than_raised(store: Path) -> None:
    """ "Never raises" has to survive the caller that hands this a None it computed from a clock
    that was not running. The reading is lost; the gate the caller was opening is not."""
    refused = observations.record(
        "waited_seconds",
        project="p",
        cycle_id="c",
        value=None,  # type: ignore[arg-type]
        arm=observations.ARM_SILENT,
    )
    assert refused is False
    assert observations.read() == []


def test_the_store_holds_counts_and_classes_never_content(store: Path) -> None:
    """It is user-global and crosses projects, so what is in it has to be safe to keep there.
    Whatever needs the content is in that cycle's own archive, which the cycle id finds."""
    observations.record("judgement_raised", project="demo", cycle_id="c-1", value=2, subject="T-004")
    written = json.loads(store.read_text(encoding="utf-8").strip())

    assert set(written) == {"kind", "project", "cycle_id", "value", "at", "subject", "arm"}
    assert isinstance(written["value"], (int, float))


# --- reading them back ------------------------------------------------------------


def test_a_summary_totals_by_kind_and_can_be_scoped_to_one_project(store: Path) -> None:
    observations.record("judgement_raised", project="a", cycle_id="c-1", value=2)
    observations.record("judgement_raised", project="a", cycle_id="c-2", value=4)
    observations.record("judgement_raised", project="b", cycle_id="c-1", value=9)

    everything = observations.summarize(observations.read())
    just_a = observations.summarize(observations.read(), project="a")

    assert everything["judgement_raised"]["total"] == 15
    assert just_a["judgement_raised"] == {"count": 2, "total": 6, "mean": 3}


def test_the_report_carries_no_threshold(store: Path) -> None:
    """A number with a ceiling on it gets managed instead of read — which is what the acceptance
    budget demonstrated here before it was removed."""
    observations.record("acceptance_reopened", project="a", cycle_id="c-1")
    out = observations.render(observations.summarize(observations.read()))

    assert "No thresholds" in out
    assert observations.CLAIMS["acceptance_reopened"] in out


def test_pruning_keeps_the_most_recent(store: Path) -> None:
    for n in range(10):
        observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="a", cycle_id=f"c-{n}")

    assert observations.prune(4) == 6
    kept = observations.read()
    assert [entry.cycle_id for entry in kept] == [f"c-{n}" for n in range(6, 10)]


def test_pruning_below_the_count_drops_nothing(store: Path) -> None:
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="a", cycle_id="c-1")
    assert observations.prune(100) == 0
    assert len(observations.read()) == 1


# --- selection by reach, measured on both sides -----------------------------------


def test_an_armed_kind_refuses_a_reading_with_no_arm(store: Path) -> None:
    """A reading that cannot be placed in the comparison it exists for is not a reading. Filing it
    under "" would pool it with readings that never shared a condition."""
    assert observations.record("reach_overruled", project="p", cycle_id="c") is False
    assert observations.record("waited_seconds", project="p", cycle_id="c", value=1) is False
    assert observations.read() == []


def test_an_unarmed_kind_refuses_an_arm(store: Path) -> None:
    """`notified` against a count of reopened acceptances is a comparison nobody is making."""
    assert observations.record("acceptance_reopened", project="p", cycle_id="c", arm="notified") is False
    assert observations.read() == []


def test_an_arm_belonging_to_another_kind_is_refused(store: Path) -> None:
    """The vocabulary is per kind, not one shared set: a `waited_seconds` arm on a reach reading
    would put it in the notification comparison."""
    assert observations.record("reach_overruled", project="p", cycle_id="c", arm="notified") is False
    assert observations.record("waited_seconds", project="p", cycle_id="c", value=1, arm="too_local") is False
    assert observations.read() == []


def test_the_two_directions_of_a_misjudged_reach_are_never_pooled(store: Path) -> None:
    """One criterion, two ways to be wrong. A pooled figure reads as "the criterion was wrong N
    times" and says nothing about which way to move it."""
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-2", arm=observations.ARM_TOO_MANDATE)

    summary = observations.summarize(observations.read())

    assert summary["reach_overruled/too_local"]["total"] == 2
    assert summary["reach_overruled/too_mandate"]["total"] == 1
    assert "reach_overruled" not in summary


def test_a_reading_written_before_the_kind_was_armed_keeps_its_arm(store: Path) -> None:
    """0.6.0 wrote `reach_overruled` unarmed and `change_request.add` was the only thing that wrote
    it, so those readings are `too_local` — provenance, not a guess. Coercing them to "" instead
    opened a third bucket printed under a claim about arms it did not have, and the missing-arm
    warning, which looks only at real arms, stayed silent about a record that had none."""
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"kind": "reach_overruled", "project": "a", "cycle_id": "c-0", "value": 1.0, "subject": "D-001"})
        + "\n",
        encoding="utf-8",
    )

    entries = observations.read()

    assert [e.arm for e in entries] == [observations.ARM_TOO_LOCAL]
    assert set(observations.summarize(entries)) == {"reach_overruled/too_local"}


def test_a_reading_in_no_arm_of_its_kind_is_dropped_not_pooled(store: Path, caplog: Any) -> None:
    """A reading nobody can place is not a reading. Filing it under "" would pool it into a figure
    whose whole point is that its readings shared a condition."""
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"kind": "waited_seconds", "project": "a", "cycle_id": "c-0", "value": 5.0, "arm": "whenever"})
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        entries = observations.read()

    assert entries == []
    assert "belongs to no arm" in caplog.text


def test_one_sided_selection_by_reach_says_so(store: Path) -> None:
    """The whole reason for the arms: a figure with only the "too loose" side on record only ever
    reads as "ask more", and the criterion exists to ask less. The report names the gap itself
    rather than leaving somebody to remember it."""
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)

    out = observations.render(observations.summarize(observations.read()))

    assert "no readings in: too_mandate" in out
    assert "ended up `local` before the freeze" in out


def test_both_sides_on_record_raises_no_warning(store: Path) -> None:
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_MANDATE)

    assert "no readings in" not in observations.render(observations.summarize(observations.read()))


# --- how often work stopped -------------------------------------------------------


def test_the_stop_count_is_pooled_across_arms_and_carries_its_own_claim(store: Path) -> None:
    """One `waited_seconds` reading per wait, so its count is how often work stopped — which
    falsifies a different claim than the durations do. Whether a channel was configured has nothing
    to do with whether selection by reach settles the number of stops, so the count is not split."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=60, arm=observations.ARM_NOTIFIED)
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=120, arm=observations.ARM_NOTIFIED)
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=30, arm=observations.ARM_SILENT)

    out = observations.render(observations.summarize(observations.read()))

    assert observations.STOP_COUNT_CLAIM in out
    # Three waits, pooled — not two and one.
    assert re.search(r"stops \(timed, every arm\)\s+3", out)


def test_no_stop_count_line_without_waits(store: Path) -> None:
    observations.record("acceptance_reopened", project="a", cycle_id="c-1")

    assert "stops (" not in observations.render(observations.summarize(observations.read()))


def test_the_chained_stop_count_is_printed_beside_the_timed_one_never_instead(store: Path) -> None:
    """They are not the same quantity. The timed count is waits `rein ui` saw, across every project
    in this user-global store; the chained count is human interventions in one repository's audit
    chain, needing no dashboard. A reader who takes them for one number reads the gap as drift."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=60, arm=observations.ARM_NOTIFIED)

    out = observations.render(observations.summarize(observations.read()), chain_stops=4)

    assert re.search(r"stops \(timed, every arm\)\s+1", out)
    assert re.search(r"stops \(this repo, chained\)\s+4", out)
    assert "the two count different things" in out


def test_an_empty_store_still_reports_what_the_chain_counted(store: Path) -> None:
    """The case this figure exists for. A cycle driven from the terminal writes no observation and
    still stopped for a human every time it did — so returning "nothing recorded yet" here would
    withhold the count at the one moment it is the only count there is."""
    out = observations.render({}, chain_stops=5)

    assert re.search(r"stops \(this repo, chained\)\s+5", out)
    assert observations.STOP_COUNT_CLAIM in out
    assert "nothing recorded yet" not in out

    # …and with nothing anywhere, it says so about both rather than only the store.
    both_empty = observations.render({}, chain_stops=0)
    assert "nothing recorded yet" in both_empty
    assert "no stop yet either" in both_empty


def test_the_stop_count_has_no_ceiling_anywhere(store: Path) -> None:
    """Counted, never capped, and the store is where that is guaranteed rather than promised:
    nothing reads this file to decide anything, so there is no path by which the figure could
    become one."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=1, arm=observations.ARM_SILENT)

    out = observations.render(observations.summarize(observations.read()))

    assert "never a ceiling" in out
    assert "No thresholds, and none are coming" in out
