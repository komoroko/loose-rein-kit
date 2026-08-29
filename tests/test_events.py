"""Tests for events.py — the read-only view over the hash-chained audit log.

The behaviour under test is mostly about what the command *refuses* to do. An audit log an
operator can append to or resolve by hand is not evidence, so there are no such verbs and their
absence is asserted here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import event_chain, events, models
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
    for verb in ("--render", "--summary", "--verify", "--root", "--cost"):
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


# --- what is still open -------------------------------------------------------
#
# The retirement rules, at the level they are decided. Two kinds, and the difference matters: a
# task's own later success retires a verdict about that task, while a later event in the chain
# retires a report the chain itself has since undone. Neither touches events.ndjson.


def _chain(*specs: tuple[str, tuple[str, ...]]) -> list[models.Event]:
    built: list[models.Event] = []
    previous: models.Event | None = None
    for name, subjects in specs:
        linked = event_chain.link(previous, event_chain.make(name, "demo-cycle", subject_ids=subjects))
        built.append(linked)
        previous = linked
    return built


def test_a_later_review_generated_retires_the_failure_that_preceded_it() -> None:
    """A pipeline that failed once and succeeded on the retry left a permanent "waiting for you"
    row, because these two had no retirement condition at all."""
    chain = _chain(("review_failed", ()), ("actual_extraction_failed", ()), ("review_generated", ()))
    assert events.open_attention(chain) == []


def test_a_failure_after_the_last_success_still_stands() -> None:
    """Order is the chain's, not the clock's: only a *later* answer closes anything."""
    chain = _chain(("review_generated", ()), ("review_failed", ()))
    assert [e.event for e in events.open_attention(chain)] == ["review_failed"]


def test_an_event_does_not_supersede_itself() -> None:
    chain = _chain(("plan_invalidated", ()))
    assert [e.event for e in events.open_attention(chain)] == ["plan_invalidated"]


def test_a_task_failed_is_retired_by_that_task_reaching_done() -> None:
    chain = _chain(("task_failed", ("T-001",)))
    assert events.open_attention(chain, {"T-001": "done"}) == []
    assert [e.event for e in events.open_attention(chain, {"T-001": "blocked"})] == ["task_failed"]


def test_a_batch_failure_stands_until_every_task_it_named_is_done() -> None:
    chain = _chain(("task_failed", ("T-001", "T-002")))
    assert [e.event for e in events.open_attention(chain, {"T-001": "done"})] == ["task_failed"]


def test_a_review_failure_is_not_retired_by_a_task_it_happened_to_name() -> None:
    """`review_failed` is not a verdict about a task, so a task status is not authoritative over it."""
    chain = _chain(("review_failed", ("T-001",)))
    assert [e.event for e in events.open_attention(chain, {"T-001": "done"})] == ["review_failed"]


# --- what a cycle cost --------------------------------------------------------
#
# `run_measured` carries what the provider billed each role. Nothing read it, so "where did the
# tokens go" had no answer inside the repository that had been recording it all along.


def _measured(cycle: str, role: str, tokens: int) -> models.Event:
    paid = {"launches": 1, "measured": True, "input_tokens": tokens, "output_tokens": 1, "cost_usd": 1.0}
    return event_chain.make(
        "run_measured",
        cycle,
        detail={"kind": "build", "run_id": "r", "outcome": "done", "billed_by_role": {role: paid}},
    )


def _archive(root: Path, slug: str, *evts: models.Event) -> Path:
    where = root / "docs/archive" / slug / "rein"
    where.mkdir(parents=True, exist_ok=True)
    path = where / "events.ndjson"
    event_chain.append_lines(path, [event_chain.link(None, evts[0]), *evts[1:]])
    return path


def test_cost_counts_this_cycle_and_the_archived_ones(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`cycle-close` moves the chain that answers the question into `docs/archive/`, so a report
    that read only the live chain would go blank exactly when the comparison becomes interesting.
    """
    seed_repo(tmp_path, events=[event_chain.link(None, _measured("live-cycle", "implementer", 1234))])
    _archive(tmp_path, "2026-01-01-first", _measured("old-cycle", "comparator", 5678))

    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "live-cycle — 1 run(s)" in out
    assert "old-cycle — 1 run(s)" in out
    assert "[docs/archive/2026-01-01-first/rein/events.ndjson]" in out
    assert "implementer" in out and "comparator" in out


def test_a_damaged_archive_is_named_rather_than_silently_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Leaving it out quietly would make the totals read as the whole history — the same lie as
    pricing an unmeasured role at zero. One bad archive must also not take the live cycle down."""
    seed_repo(tmp_path, events=[event_chain.link(None, _measured("live-cycle", "implementer", 1234))])
    broken = _archive(tmp_path, "2026-01-01-first", _measured("old-cycle", "comparator", 5678))
    broken.write_text(broken.read_text(encoding="utf-8").replace("5678", "9999"), encoding="utf-8")

    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "live-cycle — 1 run(s)" in out
    assert "old-cycle" not in out
    assert "docs/archive/2026-01-01-first/rein/events.ndjson: the audit chain is damaged" in out


def test_cost_is_not_narrowed_by_since(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A cycle's total computed over a window is not that cycle's total — the reason `--root`
    already ignores `--since`. Spending's axis is the cycle, not the sequence number."""
    first = event_chain.link(None, _measured("live-cycle", "implementer", 1000))
    second = event_chain.link(first, _measured("live-cycle", "implementer", 1000))
    seed_repo(tmp_path, events=[first, second])

    assert events.main(["--cost", "--since", "1", "--repo", str(tmp_path)]) == 0
    assert "live-cycle — 2 run(s)" in capsys.readouterr().out


def test_cost_on_a_repository_that_has_launched_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path, "cycle_initialized")
    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    assert "no run has recorded what it cost yet" in capsys.readouterr().out
