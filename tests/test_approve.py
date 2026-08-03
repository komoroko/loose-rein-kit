"""Tests for approve.py — readiness, the human confirmation, and recording an approval.

The single most important assertion in this file is that **only a human confirmation opens a
gate**. `readiness` never opens one — it only says whether one *could* be opened; `confirm_locally`
requires an interactive terminal and the gate name typed back; `record_approval` is the one
Central Store transaction that actually flips a gate, and it is reachable only through
`approve_locally`, which runs both of the above first.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from rein import approve, digests, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import chain, make_claim, make_config, make_plan, make_review, make_state, make_task, seed_repo

PENDING_ALL = dict.fromkeys(models.GATE_ORDER, "pending")


def repo_at(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


# --- only a human confirmation opens a gate ------------------------------------


def test_there_is_no_force_and_no_by(capsys: pytest.CaptureFixture[str]) -> None:
    # `--force` skipped readiness; `--by` let you type an identity. Neither is a check or an
    # identity, so neither exists.
    with pytest.raises(SystemExit):
        approve.main(["--help"])
    helptext = capsys.readouterr().out
    assert "--force" not in helptext
    assert "--by" not in helptext


def test_the_cli_refuses_without_a_terminal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A piped stdin, a CI job, or an agent's captured subprocess must not approve by accident."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("requirements\n"))  # readable, but not a terminal
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 1

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("requirements") == "pending"


class _Tty(io.StringIO):
    """A readable stdin that claims to be a terminal — what `confirm_locally` insists on having."""

    def isatty(self) -> bool:
        return True


def local_repo(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    kwargs.setdefault("state", make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    kwargs.setdefault("plan", make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]))
    return repo_at(tmp_path, **kwargs)


def test_a_typed_gate_name_opens_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("requirements\n"))
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 0

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.gate_status("requirements") == "approved"
    assert state.current_phase == "design"
    receipt = state.gate_receipt("requirements")
    assert receipt is not None and receipt["approval_id"].startswith("GA-REQUIREMENTS-")
    # The prompt has to say what it is worth, every time — a reader of state.yaml months later
    # must not mistake a typed confirmation for an identity-bound signature.
    assert "not which human" in capsys.readouterr().out


def test_the_receipt_binds_the_plan_digest_and_the_chain_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("requirements\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])

    receipt = (store_mod.Store(repo).read_state() or models.State({})).gate_receipt("requirements") or {}
    assert receipt["plan_digest"] and receipt["attested_chain_root"]
    # The root the approval *lands* on, not the one it was confirmed against — this very
    # transaction appends `gate_approved`, so the chain necessarily moves.
    assert receipt["attested_chain_root"] != receipt["result_chain_root"]


def test_recording_pins_the_event_that_opened_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("requirements\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])

    store = store_mod.Store(repo)
    events = store.read_events()
    assert [e.event for e in events] == ["gate_approved"]
    assert events[0].actor == "local-confirmation"
    receipt = (store.read_state() or models.State({})).gate_receipt("requirements") or {}
    assert receipt["approval_id"] in events[0].subject_ids
    assert "requirements" in events[0].subject_ids


def test_anything_but_the_gate_name_cancels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("yes\n"))
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 1

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("requirements") == "pending"


# --- readiness ------------------------------------------------------------------


def test_an_empty_plan_has_nothing_to_approve(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[], tasks=[]),
    )
    blockers = approve.readiness(repo, "requirements")
    assert any("states no claims" in b for b in blockers)


def test_a_claim_with_no_requirement_id_makes_the_thread_unknown(tmp_path: Path) -> None:
    """The false green this replaced: an empty/unlinked plan used to read as a whole thread."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=[])], tasks=[]),
    )
    assert any("unknown, not whole" in b for b in approve.readiness(repo, "requirements"))


def test_gates_open_in_order(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    blockers = approve.readiness(repo, "design")
    assert any("gate 'requirements' is still pending" in b for b in blockers)


def test_an_already_approved_gate_is_a_blocker(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)  # approved through tasks
    assert any("already approved" in b for b in approve.readiness(repo, "tasks"))


def test_already_approved_blocks_can_be_dropped_for_a_status_board(tmp_path: Path) -> None:
    """A board asking "what stands in this gate's way" must not read a healthy gate as its own
    blocker — that is the one caller `already_approved_blocks=False` exists for."""
    repo = repo_at(tmp_path)  # approved through tasks
    assert approve.readiness(repo, "tasks", already_approved_blocks=False) == []


def test_readiness_reports_every_blocker_not_just_the_first(tmp_path: Path) -> None:
    """Being handed one blocker, fixing it, and being handed the next is the review friction
    the whole release budgets against."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        plan=make_plan(claims=[], tasks=[]),
        events=chain("cycle_initialized"),
    )
    log = repo.events
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")
    blockers = approve.readiness(repo, "requirements")
    assert any("states no claims" in b for b in blockers)
    assert any("audit chain has" in b for b in blockers)


def test_a_damaged_audit_chain_blocks_every_gate(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        events=chain("cycle_initialized", "task_completed"),
    )
    log = repo.events
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")
    assert any("audit chain has" in b for b in approve.readiness(repo, "requirements"))


def test_an_unknown_gate_is_refused(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(approve.ApprovalError, match="unknown gate"):
        approve.readiness(repo, "nonexistent")


# --- gate 3: the plan has to be buildable --------------------------------------


def test_gate_three_needs_a_task_for_every_claim(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"tasks": "pending", "build": "pending", "release": "pending"}, phase="tasks"),
        plan=make_plan(
            claims=[make_claim("C-001"), make_claim("C-002")],
            tasks=[make_task("T-001", claim_ids=["C-001"])],
        ),
    )
    assert any("C-002: no task is answerable" in b for b in approve.readiness(repo, "tasks"))


def test_gate_three_needs_at_least_one_task(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"tasks": "pending", "build": "pending", "release": "pending"}, phase="tasks"),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[]),
    )
    assert any("declares no tasks" in b for b in approve.readiness(repo, "tasks"))


# --- gate 4: a review, not a green test run ------------------------------------


def test_gate_four_needs_a_generated_review(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(tasks={"T-001": "done"}), review=make_review(generated=False))
    blockers = approve.readiness(repo, "build")
    assert any("not a green test run" in b for b in blockers)


def test_gate_four_blocks_on_an_insufficient_coverage_manifest(tmp_path: Path) -> None:
    """A high-risk change with something unread cannot report "Extra Behavior: 0" (plan §13.4)."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(
            generated=True, coverage_status="insufficient", human_status="frozen", effective_risk="high"
        ),
    )
    assert any("coverage is insufficient" in b for b in approve.readiness(repo, "build"))


def test_a_review_that_does_not_say_what_it_weighed_gets_the_strict_path(tmp_path: Path) -> None:
    """No `effective_risk` reads as high, never as low — silence is not a safety claim."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, coverage_status="insufficient", human_status="frozen"),
    )
    assert any("coverage is insufficient" in b for b in approve.readiness(repo, "build"))


def test_an_unread_file_that_bears_no_risk_does_not_hold_gate_four_shut(tmp_path: Path) -> None:
    """A low-risk gap is recorded, not blocking — splitting the scope never removes the file."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, coverage_status="insufficient", human_status="frozen", effective_risk="low"),
    )
    assert not [b for b in approve.readiness(repo, "build") if "coverage" in b]


def test_gate_four_blocks_on_a_gap_the_comparator_marked_blocking(tmp_path: Path) -> None:
    """`machine.gaps` is written by the comparator; a gate that ignored it would open anyway."""
    gap = {
        "id": "GAP-001",
        "kind": "actual_coverage_gap",
        "statement_id": "STMT-001",
        "risk": "medium",
        "blocking": True,
    }
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="frozen", effective_risk="low", gaps=[gap]),
    )
    assert any("GAP-001" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_on_a_blocking_security_finding(tmp_path: Path) -> None:
    finding = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "the reviewer container reaches a host credential",
        "blocking": True,
    }
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="frozen", security_findings=[finding]),
    )
    assert any("SEC-001" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_until_the_human_review_is_frozen(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="in_progress"),
    )
    assert any("not 'frozen'" in b for b in approve.readiness(repo, "build"))


def test_gate_four_blocks_while_tasks_are_unfinished(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path, state=make_state(tasks={"T-001": "todo"}), review=make_review(generated=True, human_status="frozen")
    )
    assert any("tasks not done: T-001" in b for b in approve.readiness(repo, "build"))


def test_gate_four_refuses_a_review_of_an_older_commit(tmp_path: Path) -> None:
    """Only the UI pane used to check this: none of the digests re-verified when code did, so
    generate → commit → approve could open gate ④ over code no reviewer had seen."""
    import subprocess

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}))
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "reviewed")
    reviewed_head = git("rev-parse", "HEAD")
    repo = repo_mod.Repo(tmp_path)

    fresh = make_review(generated=True, human_status="frozen", effective_risk="low")
    fresh["machine"]["binding"]["subject_head_sha"] = reviewed_head
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=fresh)
    assert not [b for b in approve.readiness(repo, "build") if "stale" in b or "says nothing" in b]

    git("commit", "-q", "--allow-empty", "-m", "after the review")
    assert any("says nothing about the commits since" in b for b in approve.readiness(repo, "build"))


# --- what an approval covers ----------------------------------------------------


def test_the_subject_binds_the_plan_config_and_chain_root(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        config=make_config(),
        events=chain("cycle_initialized"),
    )
    subject = approve.approval_subject(repo, "requirements")
    assert digests.is_digest(subject["plan_digest"])
    assert digests.is_digest(subject["config_digest"])
    assert subject["attested_chain_root"] == store_mod.Store(repo).chain_root()
    assert subject["cycle_id"] == "demo-cycle"


def test_the_subject_includes_the_review_digests_once_generated(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(tasks={"T-001": "done"}), review=make_review(generated=True))
    subject = approve.approval_subject(repo, "build")
    assert digests.is_digest(subject["machine_digest"])
    assert digests.is_digest(subject["human_digest"])


def test_the_subject_includes_the_artifact_digest_when_the_deliverable_exists(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        docs=True,
    )
    subject = approve.approval_subject(repo, "requirements")
    assert digests.is_digest(subject["artifact_digest"])


# --- recording an approval directly (bypassing the terminal prompt) ------------


def test_recording_refuses_when_the_chain_moved_since_the_subject_was_read(tmp_path: Path) -> None:
    """The subject was shown to a human at one chain root; if the chain moved before the
    confirmation was recorded, the approval covers a log that no longer exists."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    subject = approve.approval_subject(repo, "requirements")

    with store_mod.Store(repo).transaction() as tx:
        tx.append("knowledge_gap", cycle_id="demo-cycle")

    with pytest.raises(approve.ApprovalError, match="chain moved"):
        approve.record_approval(repo, "requirements", subject)


def test_recording_refuses_a_damaged_chain(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"),
        events=chain("cycle_initialized"),
    )
    subject = approve.approval_subject(repo, "requirements")
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    with pytest.raises(approve.ApprovalError, match="damaged audit chain"):
        approve.record_approval(repo, "requirements", subject)


def test_recording_an_approval_writes_a_receipt_and_advances_the_phase(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    subject = approve.approval_subject(repo, "requirements")
    approval_id = approve.record_approval(repo, "requirements", subject)

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    assert state.gate_status("requirements") == "approved"
    assert state.current_phase == "design"
    receipt = state.gate_receipt("requirements")
    assert receipt is not None and receipt["approval_id"] == approval_id
