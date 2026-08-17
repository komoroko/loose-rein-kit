"""Tests for approve.py — readiness, the human confirmation, and recording an approval.

The single most important assertion in this file is that **only a human confirmation opens a
gate**. `readiness` never opens one — it only says whether one *could* be opened; `confirm_locally`
requires an interactive terminal and an explicit yes (the default is no); `record_approval` is the
one Central Store transaction that actually flips a gate, reachable only through a confirmation —
this one, or the dashboard's, whose write session comes from the launch link `rein ui` prints to
its own terminal. Two channels of the same kind, one recording path, and the receipt says which.
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


def test_a_yes_at_the_terminal_opens_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 0

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.gate_status("requirements") == "approved"
    assert state.current_phase == "design"
    receipt = state.gate_receipt("requirements")
    assert receipt is not None and receipt["approval_id"].startswith("GA-REQUIREMENTS-")
    # The prompt has to say what it is worth, every time — and what it is worth is narrower than
    # "a human approved", which nothing here can establish. Both halves are asserted: what it
    # does not claim, and the property that actually holds.
    printed = " ".join(capsys.readouterr().out.split())
    assert "not which human, and not, provably, a human at all" in printed
    assert "cannot happen by accident, by default, or by a configuration anyone pre-authorized" in printed


def test_the_receipt_binds_the_plan_digest_and_the_chain_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])

    receipt = (store_mod.Store(repo).read_state() or models.State({})).gate_receipt("requirements") or {}
    assert receipt["plan_digest"] and receipt["attested_chain_root"]
    # The root the approval *lands* on, not the one it was confirmed against — this very
    # transaction appends `gate_approved`, so the chain necessarily moves.
    assert receipt["attested_chain_root"] != receipt["result_chain_root"]


def test_recording_pins_the_event_that_opened_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])

    store = store_mod.Store(repo)
    events = store.read_events()
    assert [e.event for e in events] == ["gate_approved"]
    assert events[0].actor == "local-confirmation"
    receipt = (store.read_state() or models.State({})).gate_receipt("requirements") or {}
    assert receipt["approval_id"] in events[0].subject_ids
    assert "requirements" in events[0].subject_ids


@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n", "requirements\n", "  \n"])
def test_anything_but_yes_cancels(answer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare Enter is the case that matters: the default must be no, so a stray keystroke in a
    terminal that has been sitting open cannot open a gate."""
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty(answer))
    assert approve.main(["requirements", "--repo", str(tmp_path)]) == 1

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("requirements") == "pending"


def test_declining_points_at_the_way_to_record_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ "No" used to be a dead end: the reason lived in the human's head, or in a chat message
    that the next session never saw."""
    local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])
    assert "rein changes add requirements" in caplog.text


def test_the_terminal_path_says_which_channel_confirmed_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["requirements", "--repo", str(tmp_path)])
    receipt = (store_mod.Store(repo).read_state() or models.State({})).gate_receipt("requirements") or {}
    assert receipt["confirmed_via"] == "terminal"


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


# --- gate ③ freezes the plan ---------------------------------------------------
#
# This is the half that was missing entirely. Three documents said gate ③ freezes the plan,
# `gate_guard` rule 2 keyed off `plan.status == "frozen"`, and `rein build` refused to start
# against a draft — while no code anywhere ever wrote "frozen". A correctly approved repository
# could not build, and rule 2 never once engaged.


def _tasks_gate_repo(tmp_path: Path) -> repo_mod.Repo:
    """A repo standing at gate ③, with a plan whose claims all have a task."""
    return repo_at(
        tmp_path,
        state=make_state(
            gates={"tasks": "pending", "build": "pending", "release": "pending"},
            phase="tasks",
            plan_status="draft",
        ),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[make_task("T-001", claim_ids=["C-001"])]),
    )


def test_approving_gate_three_freezes_the_plan(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    store = store_mod.Store(repo)
    plan, config = store.read_plan(), store.read_config()
    assert plan is not None and config is not None

    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))

    state = store.read_state()
    assert state is not None
    assert state.plan_status == "frozen"
    assert state.plan_digest == plan.digest()
    assert state.plan_config_digest == config.digest()
    frozen = state.raw["plan"]
    assert frozen["toolchain_digest"] == config.toolchain_digest()
    assert frozen["frozen_at"]


def test_the_freeze_keys_are_exactly_the_ones_a_roll_back_clears(tmp_path: Path) -> None:
    # revise.apply pops exactly this set — it imports the constant rather than repeating it, so
    # the two can no longer drift by someone forgetting one. What is still worth asserting is the
    # round trip: everything the freeze writes is gone again after the roll back, because a key
    # that survived an un-freeze would let a later check "verify" against a freeze that no longer
    # holds.
    from rein import revise

    repo = _tasks_gate_repo(tmp_path)
    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))
    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    assert set(state.raw["plan"]) == {"status", *approve.FROZEN_PLAN_KEYS}

    revision = revise.plan_revision(repo, "tasks", [])
    assert revision["unfreezes_plan"] is True
    revise.apply(repo, revision, "a defect in the task breakdown")

    after = store.read_state()
    assert after is not None
    assert after.raw["plan"] == {"status": "draft"}  # every frozen key cleared, none left behind


def test_the_freeze_is_recorded_in_the_audit_chain(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    approval_id = approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))

    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["gate_approved", "plan_frozen"]
    frozen = events[1]
    assert approval_id in frozen.subject_ids
    # The digests the freeze covers travel in the event, so the chain says what was frozen and
    # not merely that something was.
    assert set(frozen.detail) == set(approve.FROZEN_PLAN_KEYS)


def test_a_plan_that_moved_while_the_prompt_waited_is_not_frozen(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    subject = approve.approval_subject(repo, "tasks")
    # The human is reading the digest table; meanwhile the plan gains a claim. The chain-root
    # guard does not cover plan.yaml, so without this check the approval would freeze bytes
    # nobody was shown.
    seed_repo(
        tmp_path,
        plan=make_plan(
            claims=[make_claim("C-001"), make_claim("C-002", requirement_ids=["R-2"])],
            tasks=[make_task("T-001", claim_ids=["C-001", "C-002"])],
        ),
        state=None,
        review=None,
        config=None,
    )
    with pytest.raises(approve.ApprovalError, match="plan.yaml changed while the confirmation"):
        approve.record_approval(repo, "tasks", subject)


def test_a_config_that_moved_while_the_prompt_waited_is_not_frozen(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    subject = approve.approval_subject(repo, "tasks")
    seed_repo(tmp_path, config=make_config(max_parallel=7), state=None, plan=None, review=None)
    with pytest.raises(approve.ApprovalError, match="config.yaml changed while the confirmation"):
        approve.record_approval(repo, "tasks", subject)


def test_the_other_gates_do_not_touch_the_plan_block(tmp_path: Path) -> None:
    # Only gate ③ freezes. Gate ① approving a draft plan and leaving it draft is what lets
    # /design and /tasks keep writing to it.
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, phase="requirements", plan_status="draft"))
    approve.record_approval(repo, "requirements", approve.approval_subject(repo, "requirements"))
    state = store_mod.Store(repo).read_state()
    assert state is not None and state.plan_status == "draft"


# --- the prose the build reads, pinned at the freeze ------------------------------


def test_the_freeze_pins_the_documents_the_build_will_read(tmp_path: Path) -> None:
    """`plan.yaml` was bound by a digest. The tickets an implementer is *sent to read* were not.

    That asymmetry is the whole defect: a ticket edited after gate ③ changed what got built, and
    nothing anywhere recorded that the thing built was not the thing approved.
    """
    repo = _tasks_gate_repo(tmp_path)
    ticket = repo.path("docs/tasks/T-001.md")
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("# T-001\n\n## Acceptance criteria\n- [ ] it holds\n", encoding="utf-8")

    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.frozen_sources["docs/tasks/T-001.md"].startswith("sha256:")


def test_a_document_that_does_not_exist_is_simply_not_a_source(tmp_path: Path) -> None:
    """A repository without a baseline has one fewer source, not a missing one."""
    repo = _tasks_gate_repo(tmp_path)
    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert "docs/05-current-state.md" not in state.frozen_sources


def test_a_roll_back_releases_the_pinned_sources_too(tmp_path: Path) -> None:
    """They describe a plan that is editable again — the same reason the digests go."""
    from rein import revise

    repo = _tasks_gate_repo(tmp_path)
    repo.path("docs/tasks").mkdir(parents=True, exist_ok=True)
    repo.path("docs/tasks/T-001.md").write_text("# T-001\n", encoding="utf-8")
    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))

    revise.apply(repo, revise.plan_revision(repo, "tasks", []), "the ticket was wrong")

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.frozen_sources == {}
