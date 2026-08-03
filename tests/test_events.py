"""Tests for events.py — the read-only view over the hash-chained audit log.

The behaviour under test is mostly about what the command *refuses* to do. An audit log an
operator can append to or resolve by hand is not evidence, so there are no such verbs and their
absence is asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import event_chain, events
from tests._support import chain, seed_repo


def _seed(tmp_path: Path, *names: str) -> Path:
    seed_repo(tmp_path, events=chain(*names) if names else None)
    return tmp_path


# --- what the CLI no longer offers --------------------------------------------


def test_there_is_no_way_to_append_or_resolve_by_hand() -> None:
    # An audit log a human can write into is not an audit log. Dispositions live in
    # review.yaml and are signed; "resolve" implied a record could be ticked off.
    for gone in ("append_event", "log_escalation", "open_escalations", "rotate_if_large", "refresh_state_view"):
        assert not hasattr(events, gone), f"events.{gone} should not exist"


def test_the_cli_exposes_only_read_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        events.main(["--help"])
    helptext = capsys.readouterr().out
    for verb in ("--render", "--summary", "--verify", "--root"):
        assert verb in helptext
    for gone in ("--add", "--resolve", "--refresh-state"):
        assert gone not in helptext


# --- rendering ----------------------------------------------------------------


def test_render_of_an_empty_log() -> None:
    assert events.render([]) == "no events yet"


def test_render_lists_the_chain_in_append_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_started", "task_completed")
    assert events.main(["--repo", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.index("cycle_initialized") < out.index("task_started") < out.index("task_completed")
    assert "| 1 |" in out and "| 3 |" in out


def test_summary_counts_kinds_and_reports_the_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "task_completed", "task_completed", "task_failed")
    assert events.main(["--repo", str(root), "--summary"]) == 0
    out = capsys.readouterr().out
    assert "task_completed×2" in out
    assert "chain root: sha256:" in out


def test_summary_names_the_events_awaiting_a_human_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "task_completed", "task_failed", "knowledge_gap")
    events.main(["--repo", str(root), "--summary"])
    out = capsys.readouterr().out
    assert "needing a human decision: 2" in out
    assert "task_failed" in out and "knowledge_gap" in out


def test_root_prints_only_the_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized")
    assert events.main(["--repo", str(root), "--root"]) == 0
    assert capsys.readouterr().out.strip().startswith("sha256:")


def test_the_empty_root_is_a_real_digest_not_a_blank(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # "no events yet" and "field absent" must not look the same to a receipt that binds a root.
    root = _seed(tmp_path)
    events.main(["--repo", str(root), "--root"])
    assert capsys.readouterr().out.strip() == event_chain.EMPTY_CHAIN_ROOT


# --- verification -------------------------------------------------------------


def test_verify_passes_on_an_intact_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_completed")
    assert events.main(["--repo", str(root), "--verify"]) == 0
    assert "PASS event-chain" in capsys.readouterr().out


def test_verify_reports_every_defect_and_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_started", "task_completed")
    log = root / ".rein" / "events.ndjson"
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")  # the middle record removed

    assert events.main(["--repo", str(root), "--verify"]) == 1
    out = capsys.readouterr().out
    assert "FAIL event-chain" in out
    assert "seq_gap" in out or "broken_link" in out
    assert "Restore it from git" in out  # the repair is restore, never rewrite-to-agree


def test_a_damaged_chain_is_not_rendered_as_though_it_were_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A table drawn from a broken log looks exactly like one drawn from a good log."""
    root = _seed(tmp_path, "cycle_initialized", "task_completed")
    log = root / ".rein" / "events.ndjson"
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other-cycle", 1), encoding="utf-8")

    assert events.main(["--repo", str(root)]) == 1
    assert capsys.readouterr().out.strip() == ""


def test_attention_events_are_a_subset_of_the_vocabulary() -> None:
    from rein import models

    assert events.ATTENTION_EVENTS < models.EVENT_VALUES
