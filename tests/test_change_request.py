"""`rein changes` — the answer between "yes" and "roll back a yes".

The one assertion this file exists for: **an open change request holds the gate shut.** Before it,
"not yet, fix R-3" was a chat message. The gate stayed ready, `rein next` kept recommending the
approval, and a fresh session had no idea a human had already declined. Everything else here —
the anchor, the note, who may move which status — is in service of that.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import approve, change_request, models, status_api
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import SANDBOXED_PROFILES, make_claim, make_config, make_plan, make_state, seed_repo

PENDING_ALL = dict.fromkeys(models.GATE_ORDER, "pending")


def repo_at(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]),
        # Sandboxed, so the board's sandbox row (which rightly precedes the phase rows) does not
        # mask what these tests are about.
        config=make_config(profiles=SANDBOXED_PROFILES),
    )
    return repo_mod.Repo(tmp_path)


def state_of(repo: repo_mod.Repo) -> models.State:
    state = store_mod.Store(repo).read_state()
    assert state is not None
    return state


# --- the blocking property ---------------------------------------------------------


def test_an_open_request_holds_the_gate_shut(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    assert approve.readiness(repo, "requirements") == []

    change_request.add(repo, "requirements", "docs/10-requirements.md#R-3", "the acceptance criterion is unmeasurable")
    blockers = approve.readiness(repo, "requirements")
    assert len(blockers) == 1
    assert "open change request against docs/10-requirements.md#R-3" in blockers[0]
    assert "unmeasurable" in blockers[0]


def test_addressing_a_request_lets_the_gate_open_again(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    change_request.address(repo, request_id, "R-3 now names a p95 latency threshold")
    assert approve.readiness(repo, "requirements") == []


def test_an_approval_closes_what_it_covered(tmp_path: Path) -> None:
    """The approval is what resolves them: the human read each note beside the digests and
    decided they were answered."""
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    change_request.address(repo, request_id, "added a threshold")
    approve.record_approval(repo, "requirements", approve.approval_subject(repo, "requirements"))

    entry = state_of(repo).change_requests[0]
    assert entry["status"] == "resolved"


def test_another_gates_requests_are_left_alone(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    mine = change_request.add(repo, "design", "docs/20-design.md#R-1", "no failure mode named")
    change_request.address(repo, mine, "added one")
    approve.record_approval(repo, "requirements", approve.approval_subject(repo, "requirements"))
    assert state_of(repo).change_requests[0]["status"] == "addressed"


def test_the_approval_screen_lists_what_it_would_close(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    change_request.address(repo, request_id, "added a p95 threshold")
    shown = approve.addressed_requests(repo, "requirements")
    assert [cr["id"] for cr in shown] == [request_id]
    assert "added a p95 threshold" in change_request.render(shown)


# --- what a request has to say -----------------------------------------------------


def test_a_request_must_anchor_somewhere(tmp_path: Path) -> None:
    """The anchor is the point. Without one, answering it means re-reading the whole
    deliverable — which is the phase re-run this exists to avoid."""
    repo = repo_at(tmp_path)
    with pytest.raises(change_request.ChangeRequestError, match="needs a --target"):
        change_request.add(repo, "requirements", "  ", "something is off")


def test_a_request_must_say_what_is_wrong(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(change_request.ChangeRequestError, match="needs a --reason"):
        change_request.add(repo, "requirements", "R-3", "")


def test_addressing_must_name_what_changed(tmp_path: Path) -> None:
    """"Addressed" with nothing behind it is a status field cleared to make a board green — and
    this one stops a gate being blocked."""
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    with pytest.raises(change_request.ChangeRequestError, match="--note is required"):
        change_request.address(repo, request_id, "   ")


def test_an_unknown_gate_is_refused(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(change_request.ChangeRequestError, match="unknown gate"):
        change_request.add(repo, "nope", "R-3", "x")


def test_a_request_cannot_be_addressed_twice(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    change_request.address(repo, request_id, "fixed")
    with pytest.raises(change_request.ChangeRequestError, match="already addressed"):
        change_request.address(repo, request_id, "fixed again")


def test_an_unknown_request_is_reported(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(change_request.ChangeRequestError, match="no change request"):
        change_request.address(repo, "CR-REQUIREMENTS-DEADBEEF", "fixed")


# --- the audit record --------------------------------------------------------------


def test_both_moves_land_in_the_audit_chain(tmp_path: Path) -> None:
    """A change request that leaves no trace is a chat message with extra steps."""
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "docs/10-requirements.md#R-3", "unmeasurable")
    change_request.address(repo, request_id, "added a threshold")

    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["changes_requested", "changes_addressed"]
    assert "docs/10-requirements.md#R-3" in events[0].subject_ids
    assert request_id in events[0].subject_ids and request_id in events[1].subject_ids
    assert events[1].detail["note"] == "added a threshold"


def test_the_record_survives_a_reread(tmp_path: Path) -> None:
    """The whole reason it lives in state.yaml rather than in the conversation."""
    repo = repo_at(tmp_path)
    change_request.add(repo, "requirements", "R-3", "unmeasurable")
    fresh = models.State(store_mod.Store(repo_mod.Repo(tmp_path)).read_state().raw)  # type: ignore[union-attr]
    assert [cr["target"] for cr in fresh.change_requests_for("requirements", "open")] == ["R-3"]


# --- the board ---------------------------------------------------------------------


def test_the_board_names_the_requests_rather_than_the_gate(tmp_path: Path) -> None:
    rec = status_api.next_action(
        current_phase="requirements",
        gates=dict(PENDING_ALL),
        counts=None,
        attention_count=0,
        chain_defects=0,
        template_mode=False,
        placeholders=False,
        gate_chain_broken=False,
        plan_missing=False,
        unsandboxed_profiles=[],
        unsandboxed_build_targets=[],
        gate_ready=False,
        open_change_requests=2,
    )
    assert rec.kind == "reconcile"
    assert "2 change request(s)" in rec.reason
    assert "rein changes list --gate requirements" in rec.also


def test_the_board_reads_them_out_of_the_repository(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    change_request.add(repo, "requirements", "R-3", "unmeasurable")
    status = status_api.collect_status(repo)
    assert status["next"]["kind"] == "reconcile"  # type: ignore[index]
    blocking = [row for row in status["pending"] if row["severity"] == "blocking"]  # type: ignore[union-attr]
    assert any("open change request" in row["headline"] for row in blocking)


# --- CLI -----------------------------------------------------------------------------


def test_the_cli_records_lists_and_addresses(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_at(tmp_path)
    at = ["--repo", str(tmp_path)]
    assert change_request.main(["add", "requirements", "--target", "R-3", "--reason", "unmeasurable", *at]) == 0
    request_id = capsys.readouterr().out.split()[0]

    assert change_request.main(["list", *at]) == 0
    listed = capsys.readouterr().out
    assert request_id in listed and "R-3" in listed and "[open]" in listed

    assert change_request.main(["address", request_id, "--note", "added a threshold", *at]) == 0
    capsys.readouterr()
    assert change_request.main(["list", "--json", *at]) == 0
    assert '"status": "addressed"' in capsys.readouterr().out


def test_resolved_requests_are_out_of_the_way_unless_asked_for(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_at(tmp_path)
    request_id = change_request.add(repo, "requirements", "R-3", "unmeasurable")
    change_request.address(repo, request_id, "fixed")
    approve.record_approval(repo, "requirements", approve.approval_subject(repo, "requirements"))

    at = ["--repo", str(tmp_path)]
    assert change_request.main(["list", *at]) == 0
    assert "no change requests" in capsys.readouterr().out
    assert change_request.main(["list", "--all", *at]) == 0
    assert request_id in capsys.readouterr().out
