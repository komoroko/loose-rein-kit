"""Tests for revise.py — rewinding approval, in a chain.

The invariant the whole file circles: **an upstream gate returning to pending must never
leave a downstream gate approved.** Everything else here — the plan un-freezing, the receipts
clearing, the review going stale — follows from the same idea, that a decision standing on a
withdrawn decision is not a decision.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import models, revise
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_plan, make_review, make_state, make_task, seed_repo

ALL_APPROVED = dict.fromkeys(models.GATE_ORDER, "approved")


def repo_at(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


def state_of(repo: repo_mod.Repo) -> models.State:
    state = store_mod.Store(repo).read_state()
    assert state is not None
    return state


# --- the chain reset ----------------------------------------------------------


def test_the_reset_runs_forward_from_the_target(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    assert revise.gates_to_reset("design", state_of(repo)) == ["design", "tasks", "build", "release"]
    assert revise.gates_to_reset("build", state_of(repo)) == ["build", "release"]


def test_an_already_pending_chain_resets_nothing(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending"), phase="brief", plan_status="draft"),
    )
    assert revise.gates_to_reset("requirements", state_of(repo)) == []


def test_applying_a_rollback_leaves_no_downstream_approval(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    revision = revise.plan_revision(repo, "design", [])
    revise.apply(repo, revision, "the auth method was wrong")

    state = state_of(repo)
    assert state.gate_status("requirements") == "approved"
    assert [state.gate_status(g) for g in ("design", "tasks", "build", "release")] == ["pending"] * 4
    assert state.current_phase == "design"
    assert state.gate_chain_violations() == []


def test_an_unknown_target_phase_is_refused(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(revise.ReviseError, match="unknown target phase"):
        revise.plan_revision(repo, "verify", [])


def test_verify_is_not_a_rollback_target() -> None:
    # It precedes gate 5, so "rewind to verify" would reset nothing and mean nothing.
    assert "verify" not in revise.PHASE_GATE


# --- consequences of a rollback -----------------------------------------------


def test_rewinding_past_gate_three_unfreezes_the_plan(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done", plan_status="frozen"))
    revision = revise.plan_revision(repo, "tasks", [])
    assert revision["unfreezes_plan"] is True
    revise.apply(repo, revision, "the requirement was wrong")

    state = state_of(repo)
    assert state.plan_status == "draft"
    # The frozen digests described a plan that is editable again; leaving them would let a
    # later check "verify" against a freeze that no longer holds.
    assert state.plan_digest == ""


def test_rewinding_to_build_leaves_the_plan_frozen(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    revision = revise.plan_revision(repo, "build", [])
    assert revision["unfreezes_plan"] is False
    revise.apply(repo, revision, "the implementation was wrong, the plan was not")
    assert state_of(repo).plan_status == "frozen"


def test_receipts_are_cleared_but_the_audit_chain_is_untouched(tmp_path: Path) -> None:
    from tests._support import chain

    repo = repo_at(
        tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"), events=chain("cycle_initialized", "gate_approved")
    )
    before = store_mod.Store(repo).read_events()

    revision = revise.plan_revision(repo, "build", [])
    assert revision["cleared_receipts"] == ["build", "release"]
    revise.apply(repo, revision, "reason")

    assert state_of(repo).gate_receipt("build") is None
    # An audit record you can erase is not one: `revise` clears the receipt in state.yaml and
    # appends its own `gate_revised` event, but never edits or removes what was already there —
    # the original chain survives untouched as a prefix of the new one.
    after = store_mod.Store(repo).read_events()
    assert after[: len(before)] == before
    assert len(after) == len(before) + 1


def test_the_review_is_invalidated(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=ALL_APPROVED, phase="done"),
        review=make_review(generated=True, human_status="frozen"),
    )
    revise.apply(repo, revise.plan_revision(repo, "build", []), "reason")
    assert state_of(repo).review.get("status") == "stale"


# --- impact analysis ----------------------------------------------------------


def _plan_with_chain() -> dict[str, object]:
    return make_plan(
        tasks=[
            make_task("T-001", claim_ids=["C-001"]),
            make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            make_task("T-003", kind="parallel", blocked_by=["T-002"], claim_ids=["C-001"]),
            make_task("T-004", kind="parallel", claim_ids=["C-001"]),
        ]
    )


def test_the_whole_downstream_closure_is_marked(tmp_path: Path) -> None:
    """Missing an impacted task is the dangerous direction, so the closure is marked
    mechanically; "this one is actually fine" is a human reclassification at /tasks."""
    repo = repo_at(
        tmp_path,
        plan=_plan_with_chain(),
        state=make_state(gates=ALL_APPROVED, phase="done", tasks={"T-001": "done", "T-002": "done"}),
    )
    revision = revise.plan_revision(repo, "build", ["T-001"])
    assert revision["marked_tasks"] == ["T-001", "T-002", "T-003"]
    assert revision["ripple"] == ["T-002", "T-003"]
    assert "T-004" not in revision["marked_tasks"]  # type: ignore[operator]


def test_marking_records_what_was_invalidated(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        plan=_plan_with_chain(),
        state=make_state(gates=ALL_APPROVED, phase="done", tasks={"T-001": "done", "T-002": "done"}),
    )
    revision = revise.plan_revision(repo, "build", ["T-001"])
    assert revision["previous_status"]["T-001"] == "done"  # type: ignore[index]
    revise.apply(repo, revision, "reason")
    statuses = state_of(repo).task_status
    assert statuses["T-001"] == statuses["T-002"] == statuses["T-003"] == "needs-revision"


def test_an_unknown_seed_is_reported_not_silently_dropped(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, plan=_plan_with_chain(), state=make_state(gates=ALL_APPROVED, phase="done"))
    revision = revise.plan_revision(repo, "build", ["T-001", "T-999"])
    assert revision["unknown_seeds"] == ["T-999"]
    assert "unknown task id(s) ignored: T-999" in revise.render(revision)


def test_impacted_needs_a_plan(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, plan=None)
    with pytest.raises(revise.ReviseError, match="needs a plan"):
        revise.plan_revision(repo, "build", ["T-001"])


# --- CLI ----------------------------------------------------------------------


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    before = store_mod.Store(repo).document_digest("state")
    assert revise.main(["--to", "design", "--dry-run", "--repo", str(tmp_path)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert store_mod.Store(repo).document_digest("state") == before


def test_dry_run_and_the_real_run_agree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two code paths that 'do the same thing' are two code paths that eventually do not, so
    both render one computed revision."""
    seed_repo(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    revise.main(["--to", "design", "--dry-run", "--repo", str(tmp_path)])
    dry = capsys.readouterr().out.split("\n\n(dry run")[0]
    revise.main(["--to", "design", "--reason", "r", "--repo", str(tmp_path)])
    real = capsys.readouterr().out.split("\n\nRolled back")[0]
    assert dry == real


def test_a_rollback_needs_a_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The audit chain has to say why an approval was withdrawn, or the next reader cannot
    tell a correction from a mistake."""
    seed_repo(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    assert revise.main(["--to", "design", "--repo", str(tmp_path)]) == 2
    assert "refusing to roll back with no --reason" in capsys.readouterr().err


def test_the_reason_lands_in_the_audit_chain(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    assert revise.main(["--to", "tasks", "--reason", "the requirement was wrong", "--repo", str(tmp_path)]) == 0
    events = store_mod.Store(repo).read_events()
    kinds = [e.event for e in events]
    assert "gate_revised" in kinds
    assert "plan_invalidated" in kinds  # rewinding to tasks un-freezes the plan
    revised = next(e for e in events if e.event == "gate_revised")
    assert revised.detail["reason"] == "the requirement was wrong"


