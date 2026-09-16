"""What the harness measures about itself (plan §K).

Several rules here rest on a measurement nobody was taking. These pin the two properties that make
the store worth having: every figure is attached to a claim it could falsify, and nothing reads it
back — a cycle's outcome must not depend on what earlier cycles happened to record, or the same
repository answers differently on another machine.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    monkeypatch.setattr(observations.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert observations.record("reach_overruled", project="p", cycle_id="c") is False


def test_a_torn_line_is_skipped_rather_than_refusing_the_file(store: Path) -> None:
    """An append-only file written by long-lived processes eventually holds one. Refusing the whole
    file over it would lose every reading before it, and this is material for a judgement — not
    evidence anybody signs."""
    observations.record("reach_overruled", project="p", cycle_id="c-1")
    with store.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "reach_ov\n')
    observations.record("judgement_raised", project="p", cycle_id="c-1", value=3)

    kinds = [entry.kind for entry in observations.read()]
    assert kinds == ["reach_overruled", "judgement_raised"]


def test_the_store_holds_counts_and_classes_never_content(store: Path) -> None:
    """It is user-global and crosses projects, so what is in it has to be safe to keep there.
    Whatever needs the content is in that cycle's own archive, which the cycle id finds."""
    observations.record("judgement_raised", project="demo", cycle_id="c-1", value=2, subject="T-004")
    written = json.loads(store.read_text(encoding="utf-8").strip())

    assert set(written) == {"kind", "project", "cycle_id", "value", "at", "subject"}
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
        observations.record("reach_overruled", project="a", cycle_id=f"c-{n}")

    assert observations.prune(4) == 6
    kept = observations.read()
    assert [entry.cycle_id for entry in kept] == [f"c-{n}" for n in range(6, 10)]


def test_pruning_below_the_count_drops_nothing(store: Path) -> None:
    observations.record("reach_overruled", project="a", cycle_id="c-1")
    assert observations.prune(100) == 0
    assert len(observations.read()) == 1
