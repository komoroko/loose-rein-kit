"""Tests for event_chain.py — the properties that make an append-only file evidence (plan §30.3).

Each tampering test corresponds to a row of plan §18.5's detection list. The last one is the
important admission: the chain alone does not survive a wholesale re-hash, and only a signed
checkpoint closes that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import event_chain, models


def _write(path: Path, count: int = 3) -> list[models.Event]:
    events: list[models.Event] = []
    previous: models.Event | None = None
    for index in range(count):
        raw = event_chain.make("task_completed", "demo", actor="alice", subject_ids=[f"T-00{index + 1}"])
        linked = event_chain.link(previous, raw)
        events.append(linked)
        previous = linked
    event_chain.append_lines(path, events)
    return events


def test_empty_log_reads_as_no_events(tmp_path: Path) -> None:
    assert event_chain.load(tmp_path / "events.ndjson") == []
    assert event_chain.chain_root([]) == event_chain.EMPTY_CHAIN_ROOT


def test_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    written = _write(path)
    loaded = event_chain.load(path)
    assert loaded == written
    assert [e.seq for e in loaded] == [1, 2, 3]
    assert loaded[0].prev_event_digest == ""
    assert loaded[1].prev_event_digest == loaded[0].event_digest


def test_unknown_event_name_is_refused_at_construction(tmp_path: Path) -> None:
    # A typo would create a kind no aggregation knows to look for.
    with pytest.raises(ValueError, match="unknown event"):
        event_chain.make("taks_completed", "demo")


@pytest.mark.parametrize("cycle_id", ["", "-leading-dash", "яя", "Upper", "has_underscore"])
def test_a_cycle_the_log_cannot_carry_is_refused_at_construction(cycle_id: str) -> None:
    """The same reason the event name is refused here: the log is queried per cycle.

    Callers wrote `state.cycle_id if state else ""` and `cycle-close` accepted `--name` values
    `str.isalnum` allows, so appending one produced an event that no per-cycle query finds *and*
    that `event.schema.json` rejects — turning a recorded failure into a defect in the chain a
    gate receipt pins.
    """
    with pytest.raises(ValueError, match="cycle_id"):
        event_chain.make("task_completed", cycle_id)


def test_the_cycle_id_rule_is_the_schema_s(tmp_path: Path) -> None:
    """One rule, two spellings, checked against each other — it previously had four."""
    for name in ("event", "state"):
        assert models.schema(name)["properties"]["cycle_id"]["pattern"] == models.CYCLE_ID_RE.pattern


def test_deleted_record_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")
    _, defects = event_chain.scan(path)
    assert {d.kind for d in defects} >= {"seq_gap", "broken_link"}


def test_reordered_records_are_detected(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[2], lines[1]]) + "\n", encoding="utf-8")
    _, defects = event_chain.scan(path)
    assert any(d.kind in {"seq_gap", "broken_link"} for d in defects)


def test_edited_record_breaks_its_own_digest(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path)
    text = path.read_text(encoding="utf-8").replace('"alice"', '"mallory"', 1)
    path.write_text(text, encoding="utf-8")
    _, defects = event_chain.scan(path)
    assert any(d.kind == "digest_mismatch" for d in defects)


def test_truncated_tail_is_detected_by_the_root_not_the_chain(tmp_path: Path) -> None:
    # Dropping the LAST record leaves a chain that is internally consistent — this is exactly
    # why a gate receipt binds the chain root and not just the final link.
    path = tmp_path / "events.ndjson"
    full = _write(path)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    truncated, defects = event_chain.scan(path)
    assert defects == []
    assert not event_chain.verify_root(truncated, event_chain.chain_root(full))


def test_full_rewrite_produces_a_consistent_chain_with_a_different_root(tmp_path: Path) -> None:
    # The honest limitation (plan §18.5): a self-consistent forgery is only caught because a
    # past gate receipt recorded the old root.
    path = tmp_path / "events.ndjson"
    original_root = event_chain.chain_root(_write(path))
    path.unlink()
    forged = _write(path, count=3)
    assert event_chain.scan(path)[1] == []  # the forgery is internally valid…
    assert not event_chain.verify_root(forged, original_root)  # …but not against the signed root


def test_duplicate_seq_is_detected(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path, count=2)
    lines = path.read_text(encoding="utf-8").splitlines()
    path.write_text("\n".join([lines[0], lines[0]]) + "\n", encoding="utf-8")
    _, defects = event_chain.scan(path)
    assert {d.kind for d in defects} >= {"duplicate_seq", "duplicate_id"}


def test_malformed_line_is_a_defect_not_a_skip(tmp_path: Path) -> None:
    # Skipping a corrupt line would keep the log from "taking the orchestrator down"; acting
    # on an unreadable audit record is the failure, not the crash.
    path = tmp_path / "events.ndjson"
    _write(path, count=1)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")
    _, defects = event_chain.scan(path)
    assert any(d.kind == "unparseable" for d in defects)


def test_load_refuses_a_damaged_chain(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path)
    path.write_text(path.read_text(encoding="utf-8").replace('"alice"', '"mallory"', 1), encoding="utf-8")
    with pytest.raises(event_chain.ChainError, match="audit chain is damaged"):
        event_chain.load(path)


def test_line_is_canonical_so_a_reserialize_is_byte_identical(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    events = _write(path)
    assert path.read_text(encoding="utf-8") == "".join(event_chain.dumps(e) + "\n" for e in events)


def test_timestamps_carry_an_explicit_offset() -> None:
    stamp = event_chain.now_iso()
    assert stamp[-6] in "+-" or stamp.endswith("Z")


def test_summarize_counts_only_present_kinds(tmp_path: Path) -> None:
    path = tmp_path / "events.ndjson"
    _write(path, count=2)
    assert event_chain.summarize(event_chain.load(path)) == {"task_completed": 2}
