"""Tests for cycle.py — archiving a finished delta cycle and resetting for the next.

Two properties matter. The archive carries the cycle's **evidence** with its prose (plan,
state, review, event log), because a history of conclusions with no grounds is
not a record. And the reset carries **nothing** forward but the project identity: a gate
status or a task status surviving into a new cycle would be an approval for work that has not
happened.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rein import cycle, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import chain, make_state, seed_repo

ALL_APPROVED = dict.fromkeys(models.GATE_ORDER, "approved")


def finished_repo(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    """A repo whose release gate is approved, each gate carrying a schema-valid receipt."""
    seed_repo(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"), docs=True, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


# --- readiness ----------------------------------------------------------------


def test_an_unapproved_release_gate_blocks(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(gates={"release": "pending"}))
    blockers = cycle.readiness(repo_mod.Repo(tmp_path))
    assert any("release gate (5) is not approved" in b for b in blockers)


def test_a_damaged_chain_blocks(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path, events=chain("cycle_initialized", "task_completed"))
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    assert any("unreadable log" in b for b in cycle.readiness(repo))


def test_a_finished_cycle_is_ready(tmp_path: Path) -> None:
    assert cycle.readiness(finished_repo(tmp_path)) == []


# --- the archive plan ---------------------------------------------------------


def test_the_plan_carries_docs_and_the_machine_record(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    rows = cycle.plan_close(repo, "payment", "2026-07-23")
    archived = {src for action, src, _ in rows if action == "archive"}

    assert "docs/10-requirements.md" in archived
    # The evidence goes with the prose: archiving conclusions and dropping their grounds
    # would leave a history nobody can re-check.
    assert ".rein/plan.yaml" in archived
    assert ".rein/state.yaml" in archived
    assert ".rein/review.yaml" in archived


def test_the_product_baseline_persists_across_cycles(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    sources = {src for _, src, _ in cycle.plan_close(repo, "payment", "2026-07-23")}
    assert "docs/00-product-brief.md" not in sources
    assert "docs/05-current-state.md" not in sources


def test_an_absent_item_is_skipped_which_is_what_makes_a_rerun_idempotent(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    (tmp_path / "docs" / "retrospective.md").unlink()
    rows = dict((src, action) for action, src, _ in cycle.plan_close(repo, "p", "2026-07-23"))
    assert rows["docs/retrospective.md"] == "skip"


def test_destinations_are_dated_and_slugged(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    rows = cycle.plan_close(repo, "payment", "2026-07-23")
    assert all(dst.startswith("docs/archive/2026-07-23-payment/") for _, _, dst in rows)


# --- the reset ----------------------------------------------------------------


def test_the_next_state_carries_only_the_project_identity(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    previous = store_mod.Store(repo).read_state()
    assert previous is not None

    fresh = cycle.next_state(previous, "payment-2")
    assert fresh["project"] == previous.project
    assert fresh["cycle_id"] == "payment-2"
    assert fresh["current_phase"] == "brief"
    assert fresh["plan"] == {"status": "draft"}
    assert fresh["tasks"] == {}
    gates = fresh["gates"]
    assert isinstance(gates, dict)
    assert all(g["status"] == "pending" and g["receipt"] is None for g in gates.values())
    assert models.schema_errors(fresh, "state") == []


# --- the snapshot -------------------------------------------------------------


def test_the_snapshot_is_taken_once_and_never_overwritten(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    assert cycle.snapshot_scaffold(repo) is True
    pristine = tmp_path / ".rein" / "scaffold" / "docs" / "10-requirements.md"
    assert pristine.exists()

    (tmp_path / "docs" / "10-requirements.md").write_text("filled in by the human\n", encoding="utf-8")
    cycle.snapshot_scaffold(repo)
    assert "scaffold:" in pristine.read_text(encoding="utf-8")  # the pristine copy survived


# --- the whole close ----------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.mark.integration
def test_close_archives_resets_and_records(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path, git=True, events=chain("cycle_initialized"))
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    cycle.snapshot_scaffold(repo)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")

    assert cycle.main(["--name", "payment", "--repo", str(tmp_path)]) == 0

    archive = next((tmp_path / "docs" / "archive").iterdir())
    assert (archive / "10-requirements.md").exists()
    assert (archive / "rein" / "plan.yaml").exists()
    assert (archive / "rein" / "events.ndjson").exists()

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.cycle_id == "payment"
    assert state.current_phase == "brief"
    assert state.approved_gates == ()

    # The closing event is the last entry of the chain being archived; the new chain opens with
    # the cycle that follows it.
    archived_log = (archive / "rein" / "events.ndjson").read_text(encoding="utf-8")
    assert "cycle_closed" in archived_log
    assert [e.event for e in store_mod.Store(repo).read_events()] == ["cycle_initialized"]

    # The fresh scaffolds are back for the next cycle.
    assert (tmp_path / "docs" / "10-requirements.md").exists()


@pytest.mark.integration
def test_close_refuses_when_not_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path, state=make_state(gates={"release": "pending"}), docs=True, git=True)
    assert cycle.main(["--name", "payment", "--repo", str(tmp_path)]) == 1
    assert "cannot close this cycle" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = finished_repo(tmp_path)
    before = store_mod.Store(repo).document_digest("state")
    assert cycle.main(["--name", "payment", "--dry-run", "--repo", str(tmp_path)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert store_mod.Store(repo).document_digest("state") == before


def test_a_bad_slug_is_refused(tmp_path: Path) -> None:
    finished_repo(tmp_path)
    assert cycle.main(["--name", "Payment Refactor!", "--repo", str(tmp_path)]) == 2
