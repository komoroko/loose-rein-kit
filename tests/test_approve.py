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
import re
from pathlib import Path

import pytest
import yaml

from rein import approve, change_request, digests, models, review_reading
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import (
    chain,
    make_claim,
    make_config,
    make_decision,
    make_plan,
    make_review,
    make_state,
    make_task,
    seed_repo,
)

PENDING_ALL = dict.fromkeys(models.GATE_ENDS, "pending")


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
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("requirements\n"))  # readable, but not a terminal
    assert approve.main(["mandate", "--repo", str(tmp_path)]) == 1

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("mandate") == "pending"


class _Tty(io.StringIO):
    """A readable stdin that claims to be a terminal — what `confirm_locally` insists on having."""

    def isatty(self) -> bool:
        return True


def local_repo(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    kwargs.setdefault("state", make_state(gates=PENDING_ALL, plan_status="draft"))
    kwargs.setdefault("plan", make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])]))
    return repo_at(tmp_path, **kwargs)


def test_a_yes_at_the_terminal_opens_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    assert approve.main(["mandate", "--repo", str(tmp_path)]) == 0

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.gate_status("mandate") == "approved"
    assert state.stage == "building"
    receipt = state.gate_receipt("mandate")
    assert receipt is not None and receipt["approval_id"].startswith("GA-MANDATE-")
    # The prompt has to say what it is worth, every time — and what it is worth is narrower than
    # "a human approved", which nothing here can establish. Both halves are asserted: what it
    # does not claim, and the property that actually holds.
    printed = " ".join(capsys.readouterr().out.split())
    assert "not which human, and not, provably, a human at all" in printed
    assert "cannot happen by accident, by default, or by a configuration anyone pre-authorized" in printed


def test_the_receipt_binds_the_plan_digest_and_the_chain_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["mandate", "--repo", str(tmp_path)])

    receipt = (store_mod.Store(repo).read_state() or models.State({})).gate_receipt("mandate") or {}
    assert receipt["plan_digest"] and receipt["attested_chain_root"]
    # The root the approval *lands* on, not the one it was confirmed against — this very
    # transaction appends `gate_approved`, so the chain necessarily moves.
    assert receipt["attested_chain_root"] != receipt["result_chain_root"]


def test_recording_pins_the_event_that_opened_the_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["mandate", "--repo", str(tmp_path)])

    store = store_mod.Store(repo)
    events = store.read_events()
    # Two events, because approving the mandate is also what freezes the plan it authorizes.
    assert [e.event for e in events] == ["gate_approved", "plan_frozen"]
    assert events[0].actor == "local-confirmation"
    receipt = (store.read_state() or models.State({})).gate_receipt("mandate") or {}
    assert receipt["approval_id"] in events[0].subject_ids
    assert "mandate" in events[0].subject_ids


@pytest.mark.parametrize("answer", ["\n", "n\n", "no\n", "mandate\n", "  \n"])
def test_anything_but_yes_cancels(answer: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare Enter is the case that matters: the default must be no, so a stray keystroke in a
    terminal that has been sitting open cannot open a gate."""
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty(answer))
    assert approve.main(["mandate", "--repo", str(tmp_path)]) == 1

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.gate_status("mandate") == "pending"


def test_declining_points_at_the_way_to_record_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ "No" used to be a dead end: the reason lived in the human's head, or in a chat message
    that the next session never saw."""
    local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("n\n"))
    approve.main(["mandate", "--repo", str(tmp_path)])
    assert "rein changes add mandate" in caplog.text


def test_the_terminal_path_says_which_channel_confirmed_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = local_repo(tmp_path)
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))
    approve.main(["mandate", "--repo", str(tmp_path)])
    receipt = (store_mod.Store(repo).read_state() or models.State({})).gate_receipt("mandate") or {}
    assert receipt["confirmed_via"] == "terminal"


# --- readiness ------------------------------------------------------------------


def test_an_empty_plan_has_nothing_to_approve(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"), plan=make_plan(claims=[], tasks=[])
    )
    blockers = approve.readiness(repo, "mandate")
    assert any("states no claims" in b for b in blockers)


def test_a_claim_with_no_requirement_id_makes_the_thread_unknown(tmp_path: Path) -> None:
    """The false green this replaced: an empty/unlinked plan used to read as a whole thread."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=[])], tasks=[]),
    )
    assert any("unknown, not whole" in b for b in approve.readiness(repo, "mandate"))


def test_gates_open_in_order(tmp_path: Path) -> None:
    """Acceptance cannot be taken on a change nobody authorized."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    blockers = approve.readiness(repo, "acceptance")
    assert any("gate 'mandate' is still pending" in b for b in blockers)


def test_an_already_approved_gate_is_a_blocker(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)  # approved through tasks
    assert any("already approved" in b for b in approve.readiness(repo, "mandate"))


def test_already_approved_blocks_can_be_dropped_for_a_status_board(tmp_path: Path) -> None:
    """A board asking "what stands in this gate's way" must not read a healthy gate as its own
    blocker — that is the one caller `already_approved_blocks=False` exists for."""
    repo = repo_at(tmp_path)  # approved through tasks
    assert approve.readiness(repo, "mandate", already_approved_blocks=False) == []


def test_readiness_reports_every_blocker_not_just_the_first(tmp_path: Path) -> None:
    """Being handed one blocker, fixing it, and being handed the next is the review friction
    the whole release budgets against."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(claims=[], tasks=[]),
        events=chain("cycle_initialized"),
    )
    log = repo.events
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")
    blockers = approve.readiness(repo, "mandate")
    assert any("states no claims" in b for b in blockers)
    assert any("audit chain has" in b for b in blockers)


def test_a_damaged_audit_chain_blocks_every_gate(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        events=chain("cycle_initialized", "task_completed"),
    )
    log = repo.events
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")
    assert any("audit chain has" in b for b in approve.readiness(repo, "mandate"))


def test_a_tool_behind_the_repository_may_not_write_a_receipt(tmp_path: Path) -> None:
    """A receipt binds digests the recording process computed, and nothing in it says which build
    did. `confirmed_via` names the channel, not the release — so a receipt written by a tool that
    cannot parse the repository's current documents is indistinguishable afterwards from a sound
    one, and the check has to be a precondition rather than a caveat.

    Here rather than in either pane a human confirms: one check covers `rein approve` at a
    terminal, the dashboard's approval footer, and the pending queue that says a gate is ready.
    """
    from rein import lock as lock_mod

    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    lock_mod.write(repo.lock, lock_mod.new("99.0.0", "git+https://github.com/o/r@v99.0.0"))
    # The documents have to actually fail under this tool's schemas, because that *is* the
    # symptom: a newer release widens one and uses the new key. Checked after the reads, this
    # function raised `DocumentError` out of `store.read_config()` and the check never ran — which
    # is what the dashboard then rendered as "config.yaml is invalid".
    config = repo.root / ".rein" / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8") + "\nsecurity:\n  future_key: 1\n", encoding="utf-8")

    for gate in models.GATE_ENDS:
        blockers = approve.readiness(repo, gate, already_approved_blocks=False)
        assert any("written by rein 99.0.0" in b for b in blockers), gate
        assert not [b for b in blockers if "Additional properties" in b], gate

    lock_mod.write(repo.lock, lock_mod.new("0.1.0", ""))
    assert not [b for b in approve.readiness(repo, "mandate") if "written by rein" in b]


def test_an_unknown_gate_is_refused(tmp_path: Path) -> None:
    repo = repo_at(tmp_path)
    with pytest.raises(approve.ApprovalError, match="unknown gate"):
        approve.readiness(repo, "nonexistent")


# --- the mandate: the plan has to be buildable --------------------------------------


def test_gate_three_needs_a_task_for_every_claim(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "pending", "acceptance": "pending"}),
        plan=make_plan(
            claims=[make_claim("C-001"), make_claim("C-002")], tasks=[make_task("T-001", claim_ids=["C-001"])]
        ),
    )
    assert any("C-002: no task is answerable" in b for b in approve.readiness(repo, "mandate"))


def test_gate_three_needs_at_least_one_task(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "pending", "acceptance": "pending"}),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[]),
    )
    assert any("declares no tasks" in b for b in approve.readiness(repo, "mandate"))


# --- acceptance: a review, not a green test run ------------------------------------


def test_gate_four_needs_a_generated_review(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(tasks={"T-001": "done"}), review=make_review(generated=False))
    blockers = approve.readiness(repo, "acceptance")
    assert any("not on a green test run" in b for b in blockers)


def test_gate_four_blocks_on_an_insufficient_coverage_manifest(tmp_path: Path) -> None:
    """A high-risk change with something unread cannot report "Extra Behavior: 0" (plan §13.4)."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(
            generated=True, coverage_status="insufficient", human_status="frozen", effective_risk="high"
        ),
    )
    assert any("coverage is insufficient" in b for b in approve.readiness(repo, "acceptance"))


def test_a_review_that_does_not_say_what_it_weighed_gets_the_strict_path(tmp_path: Path) -> None:
    """No `effective_risk` reads as high, never as low — silence is not a safety claim."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, coverage_status="insufficient", human_status="frozen"),
    )
    assert any("coverage is insufficient" in b for b in approve.readiness(repo, "acceptance"))


def test_an_unread_file_that_bears_no_risk_does_not_hold_gate_four_shut(tmp_path: Path) -> None:
    """A low-risk gap is recorded, not blocking — splitting the scope never removes the file."""
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, coverage_status="insufficient", human_status="frozen", effective_risk="low"),
    )
    assert not [b for b in approve.readiness(repo, "acceptance") if "coverage" in b]


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
    assert any("GAP-001" in b for b in approve.readiness(repo, "acceptance"))


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
    assert any("SEC-001" in b for b in approve.readiness(repo, "acceptance"))


def test_gate_four_blocks_until_the_human_review_is_frozen(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="in_progress"),
    )
    assert any("not 'frozen'" in b for b in approve.readiness(repo, "acceptance"))


def test_gate_four_blocks_while_tasks_are_unfinished(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path, state=make_state(tasks={"T-001": "todo"}), review=make_review(generated=True, human_status="frozen")
    )
    assert any("tasks not done: T-001" in b for b in approve.readiness(repo, "acceptance"))


def _reviewed_repo(tmp_path: Path) -> tuple[repo_mod.Repo, str, str]:
    """A committed repo, and the (head, change_digest) a review taken over it would bind."""
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
    (tmp_path / "product.py").write_text("x = 1\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "reviewed")
    repo = repo_mod.Repo(tmp_path)
    head = git("rev-parse", "HEAD")
    state = store_mod.Store(repo).read_state()
    return repo, head, review_reading.change_digest(repo, head, review_reading.not_the_product(repo, state))


def _commit(tmp_path: Path, *paths: str) -> None:
    import subprocess

    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", " ".join(paths) or "later"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )


def test_gate_four_refuses_a_review_of_code_that_has_since_moved(tmp_path: Path) -> None:
    """Only the UI pane used to check this: none of the digests re-verified when code did, so
    generate → commit → approve could open acceptance over code no reviewer had seen."""
    repo, head, change = _reviewed_repo(tmp_path)

    fresh = make_review(generated=True, human_status="frozen", effective_risk="low")
    fresh["machine"]["binding"]["subject_head_sha"] = head
    fresh["machine"]["binding"]["change_digest"] = change
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=fresh)
    assert not [b for b in approve.readiness(repo, "acceptance") if "stale" in b or "says nothing" in b]

    (tmp_path / "product.py").write_text("x = 2\n", encoding="utf-8")
    _commit(tmp_path, "the product moved")
    assert any("says nothing about the code as it now stands" in b for b in approve.readiness(repo, "acceptance"))


def test_recording_a_review_does_not_make_it_stale(tmp_path: Path) -> None:
    """The circle this closed. The workflow commits each phase's deliverables at its gate, so the
    act of committing `review.yaml` moved HEAD — and a staleness test on the commit id read that
    as "the review says nothing about the commits since", about the very commit that recorded it.
    Generate, commit, and the gate could not be approved.

    `.rein/` is not the product (`review_reading.not_the_product`), and the review already binds
    the product's digest. So this commit moves nothing the review is about, and neither does one
    that touches only the frozen prose or an installed agent surface.
    """
    repo, head, change = _reviewed_repo(tmp_path)
    review = make_review(generated=True, human_status="frozen", effective_risk="low")
    review["machine"]["binding"]["subject_head_sha"] = head
    review["machine"]["binding"]["change_digest"] = change
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=review)

    _commit(tmp_path, "record the review")  # .rein/review.yaml and .rein/state.yaml
    assert repo._git_rc("rev-parse", "HEAD")[1].strip() != head, "HEAD did move"
    assert not [b for b in approve.readiness(repo, "acceptance") if "says nothing" in b]


# --- what acceptance carries rather than re-reads -------------------------------------


def test_gate_five_carries_gate_fours_security_review_and_refuses_a_stale_one(tmp_path: Path) -> None:
    """`/verify` no longer commissions a second security reading, so these two checks are what
    the release gate's security answer now rests on.

    Re-running the reviewer at acceptance asked the same reviewer about the same commit and wrote the
    answer into a table cell nothing anchors — and the whole-codebase scope it asked for is a
    different question from "is this change safe", one the cycle never asked. Carrying acceptance's
    review instead is sound only because of these: a blocking finding holds this gate shut too, and
    a review taken against an older commit is refused rather than trusted. If either stops holding,
    acceptance has no security evidence at all, so they are pinned here and not only at acceptance.
    """
    finding = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "the reviewer container reaches a host credential",
        "blocking": True,
    }
    repo, head, change = _reviewed_repo(tmp_path)

    blocking = make_review(generated=True, human_status="frozen", security_findings=[finding])
    blocking["machine"]["binding"]["subject_head_sha"] = head
    blocking["machine"]["binding"]["change_digest"] = change
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=blocking)
    assert any("SEC-001" in b for b in approve.readiness(repo, "acceptance")), (
        "a blocking finding holds acceptance shut"
    )

    clean = make_review(generated=True, human_status="frozen", effective_risk="low")
    clean["machine"]["binding"]["subject_head_sha"] = head
    clean["machine"]["binding"]["change_digest"] = change
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=clean)
    assert not [b for b in approve.readiness(repo, "acceptance") if "says nothing" in b]

    (tmp_path / "product.py").write_text("x = 2\n", encoding="utf-8")
    _commit(tmp_path, "the product moved")
    assert any("says nothing about the code as it now stands" in b for b in approve.readiness(repo, "acceptance")), (
        "a review about earlier code is not this release's security evidence"
    )


# --- what an approval covers ----------------------------------------------------


def test_the_subject_binds_the_plan_config_and_chain_root(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        config=make_config(),
        events=chain("cycle_initialized"),
    )
    subject = approve.approval_subject(repo, "mandate")
    assert digests.is_digest(subject["plan_digest"])
    assert digests.is_digest(subject["config_digest"])
    assert subject["attested_chain_root"] == store_mod.Store(repo).chain_root()
    assert subject["cycle_id"] == "demo-cycle"


def test_the_subject_includes_the_review_digests_once_generated(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(tasks={"T-001": "done"}), review=make_review(generated=True))
    subject = approve.approval_subject(repo, "acceptance")
    assert digests.is_digest(subject["machine_digest"])
    assert digests.is_digest(subject["human_digest"])


def test_the_subject_includes_the_artifact_digest_when_the_deliverable_exists(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"), docs=True)
    subject = approve.approval_subject(repo, "mandate")
    assert digests.is_digest(subject["artifact_digest"])


# --- recording an approval directly (bypassing the terminal prompt) ------------


def test_recording_refuses_when_the_chain_moved_since_the_subject_was_read(tmp_path: Path) -> None:
    """The subject was shown to a human at one chain root; if the chain moved before the
    confirmation was recorded, the approval covers a log that no longer exists."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    subject = approve.approval_subject(repo, "mandate")

    with store_mod.Store(repo).transaction() as tx:
        tx.append("knowledge_gap", cycle_id="demo-cycle")

    with pytest.raises(approve.ApprovalError, match="chain moved"):
        approve.record_approval(repo, "mandate", subject)


def test_recording_refuses_a_damaged_chain(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"), events=chain("cycle_initialized")
    )
    subject = approve.approval_subject(repo, "mandate")
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    with pytest.raises(approve.ApprovalError, match="damaged audit chain"):
        approve.record_approval(repo, "mandate", subject)


def test_recording_an_approval_writes_a_receipt_and_the_stage_follows(tmp_path: Path) -> None:
    """The approval writes the gate and nothing else. Where the cycle stands is read off it.

    It used to write `current_phase` in the same transaction, which made one write the author of
    two facts — what has been permitted and how far the work has got — that could then disagree.
    """
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    subject = approve.approval_subject(repo, "mandate")
    approval_id = approve.record_approval(repo, "mandate", subject)

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    assert state.gate_status("mandate") == "approved"
    assert state.stage == "building"
    assert "current_phase" not in state.raw
    receipt = state.gate_receipt("mandate")
    assert receipt is not None and receipt["approval_id"] == approval_id


# --- the mandate freezes the plan ---------------------------------------------------
#
# This is the half that was missing entirely. Three documents said the mandate freezes the plan,
# `gate_guard` rule 2 keyed off `plan.status == "frozen"`, and `rein build` refused to start
# against a draft — while no code anywhere ever wrote "frozen". A correctly approved repository
# could not build, and rule 2 never once engaged.


def _tasks_gate_repo(tmp_path: Path) -> repo_mod.Repo:
    """A repo standing at the mandate, with a plan whose claims all have a task."""
    return repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "pending", "acceptance": "pending"}, plan_status="draft"),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[make_task("T-001", claim_ids=["C-001"])]),
    )


def test_approving_gate_three_freezes_the_plan(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    store = store_mod.Store(repo)
    plan, config = store.read_plan(), store.read_config()
    assert plan is not None and config is not None

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = store.read_state()
    assert state is not None
    assert state.plan_status == "frozen"
    assert state.plan_digest == plan.digest()
    assert state.plan_config_digest == config.frozen_digest()
    frozen = state.raw["plan"]
    assert frozen["environment_digest"] == config.environment_digest()
    assert frozen["frozen_at"]


def test_the_freeze_keys_are_exactly_the_ones_a_roll_back_clears(tmp_path: Path) -> None:
    # revise.apply pops exactly this set — it imports the constant rather than repeating it, so
    # the two can no longer drift by someone forgetting one. What is still worth asserting is the
    # round trip: everything the freeze writes is gone again after the roll back, because a key
    # that survived an un-freeze would let a later check "verify" against a freeze that no longer
    # holds.
    from rein import revise

    repo = _tasks_gate_repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    assert set(state.raw["plan"]) == {"status", *approve.FROZEN_PLAN_KEYS}

    revision = revise.plan_revision(repo, "mandate", [])
    assert revision["unfreezes_plan"] is True
    revise.apply(repo, revision, "a defect in the task breakdown")

    after = store.read_state()
    assert after is not None
    assert after.raw["plan"] == {"status": "draft"}  # every frozen key cleared, none left behind


def test_the_freeze_is_recorded_in_the_audit_chain(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    approval_id = approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["gate_approved", "plan_frozen"]
    frozen = events[1]
    assert approval_id in frozen.subject_ids
    # The digests the freeze covers travel in the event, so the chain says what was frozen and
    # not merely that something was.
    assert set(frozen.detail) == set(approve.FROZEN_PLAN_KEYS)


def test_a_plan_that_moved_while_the_prompt_waited_is_not_frozen(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    subject = approve.approval_subject(repo, "mandate")
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
        approve.record_approval(repo, "mandate", subject)


def test_a_config_that_moved_while_the_prompt_waited_is_not_frozen(tmp_path: Path) -> None:
    repo = _tasks_gate_repo(tmp_path)
    subject = approve.approval_subject(repo, "mandate")
    seed_repo(tmp_path, config=make_config(max_parallel=7), state=None, plan=None, review=None)
    with pytest.raises(approve.ApprovalError, match="config.yaml changed while the confirmation"):
        approve.record_approval(repo, "mandate", subject)


def test_acceptance_does_not_touch_the_plan_block(tmp_path: Path) -> None:
    """Only the mandate freezes. Acceptance rests on that freeze rather than taking another."""
    repo = _tasks_gate_repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    before = store_mod.Store(repo).read_state()
    assert before is not None and before.plan_status == "frozen"
    frozen = dict(before.raw["plan"])

    approve.record_approval(repo, "acceptance", approve.approval_subject(repo, "acceptance"))
    after = store_mod.Store(repo).read_state()
    assert after is not None and after.raw["plan"] == frozen


# --- the prose the build reads, pinned at the freeze ------------------------------


def test_the_freeze_pins_the_documents_the_build_will_read(tmp_path: Path) -> None:
    """`plan.yaml` was bound by a digest. The tickets an implementer is *sent to read* were not.

    That asymmetry is the whole defect: a ticket edited after the mandate changed what got built, and
    nothing anywhere recorded that the thing built was not the thing approved.
    """
    repo = _tasks_gate_repo(tmp_path)
    ticket = repo.path("docs/tasks/T-001.md")
    ticket.parent.mkdir(parents=True, exist_ok=True)
    ticket.write_text("# T-001\n\n## Acceptance criteria\n- [ ] it holds\n", encoding="utf-8")

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.frozen_sources["docs/tasks/T-001.md"].startswith("sha256:")


def test_a_document_that_does_not_exist_is_simply_not_a_source(tmp_path: Path) -> None:
    """A repository without a baseline has one fewer source, not a missing one."""
    repo = _tasks_gate_repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert "docs/05-current-state.md" not in state.frozen_sources


def test_a_roll_back_releases_the_pinned_sources_too(tmp_path: Path) -> None:
    """They describe a plan that is editable again — the same reason the digests go."""
    from rein import revise

    repo = _tasks_gate_repo(tmp_path)
    repo.path("docs/tasks").mkdir(parents=True, exist_ok=True)
    repo.path("docs/tasks/T-001.md").write_text("# T-001\n", encoding="utf-8")
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    revise.apply(repo, revise.plan_revision(repo, "mandate", []), "the ticket was wrong")

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.frozen_sources == {}


# --- unresolved clarification markers -------------------------------------------
#
# Three documents said `rein approve` machine-checked these. Nothing did, so a marker left standing
# opened the gate and the question it named was answered by whatever default the draft assumed.


def _requirements(repo: repo_mod.Repo, body: str) -> None:
    path = repo.path("docs/10-requirements.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def test_a_marker_left_in_the_prose_holds_gate_one_shut(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    _requirements(
        repo,
        "# Requirements\n\n### R-1: export\n- retention: [NEEDS CLARIFICATION: how long?]\n\n"
        "### R-2: import\n- format: [NEEDS CLARIFICATION: csv or json?]\n",
    )
    blockers = [b for b in approve.readiness(repo, "mandate") if "NEEDS CLARIFICATION" in b]
    assert len(blockers) == 1
    # The lines, so the human is not left grepping their own deliverable.
    assert "2 unresolved" in blockers[0]
    assert "line 4, 7" in blockers[0]


def test_the_scaffolds_own_guidance_does_not_hold_the_gate_shut(tmp_path: Path) -> None:
    """The convention is explained *using* the marker. A check that cannot tell guidance from an
    open question is one nobody can leave switched on — and it would have blocked this very repo."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    _requirements(
        repo,
        "# Requirements\n<!-- While drafting, mark anything undecided as `[NEEDS CLARIFICATION: <what>]`\n"
        "     and resolve every marker before gate 1. -->\n\n### R-1: export\n- retention: 30 days\n",
    )
    assert not [b for b in approve.readiness(repo, "mandate") if "NEEDS CLARIFICATION" in b]


def test_resolving_the_marker_clears_the_blocker(tmp_path: Path) -> None:
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    _requirements(repo, "# Requirements\n\n### R-1: export\n- retention: [NEEDS CLARIFICATION: how long?]\n")
    assert [b for b in approve.readiness(repo, "mandate") if "NEEDS CLARIFICATION" in b]
    _requirements(repo, "# Requirements\n\n### R-1: export\n- retention: 30 days\n")
    assert not [b for b in approve.readiness(repo, "mandate") if "NEEDS CLARIFICATION" in b]


def test_every_document_the_mandate_is_written_from_is_swept(tmp_path: Path) -> None:
    """One gate, so one sweep — over both documents rather than one document per gate.

    A marker in the design used to be checked only at the design gate; with the phases gone there
    is nowhere later for it to be caught, so the mandate reads all of its own material.
    """
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    path = repo.path("docs/20-design.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# Design\n\n### R-1 -> design\n- store: [NEEDS CLARIFICATION: sqlite or postgres?]\n", encoding="utf-8"
    )
    assert any("docs/20-design.md still carries 1" in b for b in approve.readiness(repo, "mandate"))
    # ...and acceptance is not judged on it: the mandate that cites it was approved with it whole.
    assert not [b for b in approve.readiness(repo, "acceptance") if "NEEDS CLARIFICATION" in b]


def test_a_missing_document_is_not_this_check_to_report(tmp_path: Path) -> None:
    """Absence is a different failure — `_plan_blockers` refuses a gate with nothing behind it."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))
    assert not repo.path("docs/10-requirements.md").exists()
    assert not [b for b in approve.readiness(repo, "mandate") if "NEEDS CLARIFICATION" in b]


def test_a_freshness_nobody_could_measure_holds_the_gate_shut(tmp_path: Path) -> None:
    """ "We could not tell" is not "it is current". `freshness` reports the unmeasurable case with
    `fresh=False`, and reading only its `reason` meant `approve` added no blocker at all and
    `doctor` reported nothing — so a review that could not be shown to speak for the code opened
    acceptance on silence. An unreadable gate fails closed.
    """
    repo, head, change = _reviewed_repo(tmp_path)
    review = make_review(generated=True, human_status="frozen", effective_risk="low")
    review["machine"]["binding"]["subject_head_sha"] = head
    review["machine"]["binding"]["change_digest"] = change
    seed_repo(tmp_path, state=make_state(tasks={"T-001": "done"}), review=review)
    assert not [b for b in approve.readiness(repo, "acceptance") if "could not be measured" in b]

    # The one input every measurement here rests on, gone.
    import shutil

    shutil.rmtree(tmp_path / ".git")
    assert any("could not be measured" in b for b in approve.readiness(repo, "acceptance"))


# --- decisions the mandate rests on (plan §14, reach) --------------------------


def test_an_unknown_decision_the_mandate_rests_on_holds_the_gate_shut(tmp_path: Path) -> None:
    """The one state a mandate cannot be opened over: the loop would be told what it may change
    while what it may change is the undecided thing."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[
                make_decision("D-001", reach="mandate", status="unknown", settled_by=None, answer=None, rationale=None)
            ]
        ),
    )
    blockers = [b for b in approve.readiness(repo, "mandate") if "still `unknown`" in b]

    assert len(blockers) == 1
    assert "D-001" in blockers[0]


def test_an_unknown_decision_the_loop_owns_does_not_hold_the_gate_shut(tmp_path: Path) -> None:
    """Not a count. Any number of open questions the loop can answer later is fine — recorded
    rather than guessed at, which is the whole point of writing them down."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[
                make_decision(f"D-{n:03d}", reach="local", status="unknown", settled_by=None, answer=None)
                for n in range(1, 8)
            ]
        ),
    )
    assert not [b for b in approve.readiness(repo, "mandate") if "still `unknown`" in b]


def test_the_decision_check_is_the_mandates_alone(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[
                make_decision("D-001", reach="mandate", status="unknown", settled_by=None, answer=None, rationale=None)
            ]
        ),
    )
    assert not [b for b in approve.readiness(repo, "acceptance") if "still `unknown`" in b]


def test_a_mandate_decision_the_loop_settled_itself_holds_the_gate_shut(tmp_path: Path) -> None:
    """The same defect as `unknown`, read from the other side: the record classified this one as a
    human's to make and then made it anyway. Left to the gate screen it would be ratified by not
    being objected to, which is the shape of approval this record exists to replace."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[make_decision("D-001", reach="mandate", settled_by="loop", rationale=None)],
        ),
    )
    blockers = [b for b in approve.readiness(repo, "mandate") if "settled them itself" in b]

    assert len(blockers) == 1
    assert "D-001" in blockers[0]


def test_a_mandate_decision_a_human_settled_opens_the_gate(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[make_decision("D-001", reach="mandate", settled_by="human", rationale=None)],
        ),
    )
    assert not [b for b in approve.readiness(repo, "mandate") if "settled them itself" in b]


def test_a_settled_decision_must_say_who_settled_it(tmp_path: Path) -> None:
    """The field the gate screen is built out of. Nothing in `plan.yaml` is written by hand — an
    LLM writes it from a prose instruction — so a field that only a paragraph requires is one that
    goes missing, and the list of decisions nobody was asked about goes silently empty with it."""
    with pytest.raises(models.DocumentError) as exc:
        models.Plan.parse(
            yaml.safe_dump(make_plan(decisions=[make_decision("D-001", settled_by=None)])),
            cross_reference=False,
        )
    assert "settled_by" in str(exc.value)


def test_an_unknown_decision_may_not_claim_somebody_settled_it(tmp_path: Path) -> None:
    with pytest.raises(models.DocumentError) as exc:
        models.Plan.parse(
            yaml.safe_dump(make_plan(decisions=[make_decision("D-001", status="unknown", settled_by="loop")])),
            cross_reference=False,
        )
    assert "settled_by" in str(exc.value)


def test_a_local_decision_must_carry_the_reasoning_a_human_would_overrule(tmp_path: Path) -> None:
    """`reach: local` is a claim that reversing this later costs one task. The rationale is the
    only material a human has for disagreeing with that claim at the gate."""
    with pytest.raises(models.DocumentError) as exc:
        models.Plan.parse(
            yaml.safe_dump(make_plan(decisions=[make_decision("D-001", reach="local", rationale=None)])),
            cross_reference=False,
        )
    assert "rationale" in str(exc.value)


def test_a_settled_decision_with_no_recorded_settler_is_shown_rather_than_hidden() -> None:
    """Fail-closed, and the direction matters. Showing a human one decision they did make costs a
    line; hiding one the loop made costs the whole point of the record."""
    decision = models.Decision({"id": "D-001", "subject": "x", "reach": "local", "status": "settled"})
    assert decision.unasked is True


def test_the_confirmation_lists_what_the_loop_settled_without_asking(tmp_path: Path) -> None:
    """An approval silently ratifies every default the loop took unless they are put on screen.

    The mandate is the last moment disagreeing costs an edit rather than a `/revise`, so it is the
    only gate that shows them.
    """
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001"), make_decision("D-002", settled_by="human")]),
    )
    unasked = approve.naming(repo, "mandate")["unasked"]

    assert [d["id"] for d in unasked] == ["D-001"]  # D-002 is one the human already saw
    rendered = approve.render_unasked(unasked)
    assert "D-001" in rendered and "local because" in rendered
    assert approve._unasked_decisions(repo, "acceptance") == []


# --- the naming layer reaches every route that can open the gate ------------------


def test_the_naming_layer_carries_the_same_selection_the_terminal_prints(tmp_path: Path) -> None:
    """Whatever a gate requires on screen belongs on every route that can open that gate. The
    dashboard grew a second approval route and this did not follow it — the material was always in
    `plan.yaml`, which its mandate pane serves whole; what only the terminal had was the selection.
    """
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001"), make_decision("D-002", settled_by="human")]),
    )

    naming = approve.naming(repo, "mandate")

    assert [d["id"] for d in naming["unasked"]] == [d.id for d in approve._unasked_decisions(repo, "mandate")]
    assert naming["unasked"][0]["rationale"]
    assert naming["overrule_cost"] == approve.OVERRULE_COST


def test_the_cost_of_overruling_is_one_string_both_screens_say(tmp_path: Path) -> None:
    """Two screens saying it in their own words would be two claims about the same mechanism."""
    assert "`/revise`" in approve.OVERRULE_COST
    assert "costs a task" in approve.OVERRULE_COST


def test_the_naming_layer_is_the_mandate_s_alone(tmp_path: Path) -> None:
    """The mandate is the last moment disagreeing with a reach call costs an edit rather than a
    `/revise`, so it is the only gate with anything to name."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001")]),
    )

    empty: approve.Naming = {"unasked": [], "overrule_cost": approve.OVERRULE_COST, "lenses": [], "crossing": []}
    assert approve.naming(repo, "acceptance") == empty


def test_the_naming_layer_carries_the_whole_lens_selection_not_one_task_s(tmp_path: Path) -> None:
    """At the moment of approving nothing has been narrowed yet — `lenses.for_task` runs at the
    hand-off to a reviewer — and what the approval can overrule is the selection itself."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[make_decision("D-001")],
            lenses=[{"id": "L-CODE-SCHEMA-DRIFT", "stage": "code", "status": "applied"}],
        ),
    )

    listed = approve.naming(repo, "mandate")["lenses"]

    assert [entry["id"] for entry in listed] == ["L-CODE-SCHEMA-DRIFT"]
    assert listed[0]["applies_when"]


def test_a_frozen_lens_the_library_no_longer_holds_is_named_rather_than_dropped(tmp_path: Path) -> None:
    """Exactly the machine-local drift the freeze exists to expose, and the gate is where it can
    still be acted on."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[make_decision("D-001")],
            lenses=[{"id": "L-GONE", "stage": "code", "status": "applied"}],
        ),
    )

    listed = approve.naming(repo, "mandate")["lenses"]

    assert listed[0]["applies_when"] == "(no longer in the library)"


def test_the_library_text_is_not_handed_to_a_reader_without_a_session(tmp_path: Path) -> None:
    """`attack` and `applies_when` are read out of the user-global library, which belongs to the
    person and not to this repository — other projects' failures are written in it. The dashboard
    serves a gate's readiness to any reader by design, so that text goes only to the reader who
    could do the approving. The ids are plan content and stay."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            decisions=[make_decision("D-001")],
            lenses=[{"id": "L-CODE-SCHEMA-DRIFT", "stage": "code", "status": "applied"}],
        ),
    )

    withheld = approve.naming(repo, "mandate", include_library=False)["lenses"][0]
    given = approve.naming(repo, "mandate", include_library=True)["lenses"][0]

    assert withheld["id"] == given["id"] == "L-CODE-SCHEMA-DRIFT"
    assert withheld["attack"] == "" and withheld["applies_when"] == ""
    assert given["attack"] and given["applies_when"]


def test_the_terminal_route_prints_the_lens_selection_the_dashboard_shows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`naming` built this list and only the dashboard rendered it — the same rule CR-2 restored,
    broken in the other direction by the change that restored it."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(lenses=[{"id": "L-CODE-CONCURRENCY", "stage": "code", "status": "proposed"}]),
    )
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))

    approve.confirm_locally(repo, "mandate", {"plan": "sha256:0"})

    printed = " ".join(capsys.readouterr().out.split())
    assert "L-CODE-CONCURRENCY" in printed
    # The half being asked about says so, and says what it costs to be wrong about it.
    assert "yours to keep or drop" in printed
    assert "1 of them yours to keep or drop" in printed


def test_every_list_the_naming_layer_carries_reaches_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The rule, held as a check rather than as something to remember. A list `naming` assembles
    and no route renders is the defect this fixes, and the next list added is the one that would
    repeat it."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            tasks=[_crossing_task("T-001")],
            decisions=[make_decision("D-001")],
            lenses=[{"id": "L-CODE-CONCURRENCY", "stage": "code", "status": "proposed"}],
        ),
    )
    monkeypatch.setattr("sys.stdin", _Tty("y\n"))

    approve.confirm_locally(repo, "mandate", {"plan": "sha256:0"})

    printed = " ".join(capsys.readouterr().out.split())
    named = approve.naming(repo, "mandate")
    rows_by_list = {key: value for key, value in named.items() if isinstance(value, list)}
    # Without this the loop below passes by having nothing to iterate over.
    assert rows_by_list and all(rows_by_list.values())
    for key, rows in rows_by_list.items():
        for row in rows:
            token = row.get("task_id") or row.get("id") or ""
            assert token and token in printed, f"`naming` carries {key} and the terminal never prints it"


def test_every_list_the_naming_layer_carries_reaches_the_dashboard() -> None:
    """The mirror image, and the direction the defect actually ran in. The list that went missing
    was built here and rendered by one route only — so a check that reads the terminal alone would
    have passed while the bug was live, and passes again the next time it is the other screen's
    turn. `Gate.jsx` reaches each list by the key `naming` carries it under, so the keys are what
    the source has to mention; `tests/ui/decide.test.mjs` is where they are rendered and read back.
    """
    panel = (Path(__file__).resolve().parent.parent / "ui" / "gate" / "Gate.jsx").read_text(encoding="utf-8")

    for key in approve.Naming.__annotations__:
        # Word-bounded: a key renamed on one side only would otherwise pass as a prefix of the
        # other side's new name, which is the same silent drift this is here to catch.
        assert re.search(rf"\b{re.escape(key)}\b", panel), f"`naming` carries {key} and ui/gate/Gate.jsx never reads it"


# --- the other side of a misjudged reach ------------------------------------------


def _derived(repo: repo_mod.Repo, reaches: dict[str, str]) -> None:
    """Stand in for the pre-freeze pass `rein lens --select` makes over every draft."""
    from rein import store as store_mod

    with store_mod.Store(repo).transaction() as tx:
        tx.append("decisions_derived", cycle_id="demo-cycle", subject_ids=sorted(reaches), detail={"reaches": reaches})


def test_a_reach_the_human_walked_back_is_recorded_against_the_criterion(tmp_path: Path) -> None:
    """The mirror of the change request `change_request` files. `rein approve mandate` names this
    move itself — answer it, or change the reach and say why undoing it stays local — and the
    schema requires that rationale, so it cannot be made silently."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001", reach="local")]),
    )
    _derived(repo, {"D-001": "mandate"})

    assert approve._reach_movement(repo)[0] == ["D-001"]


def test_a_reach_that_never_moved_is_not_a_reading(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001", reach="local")]),
    )
    _derived(repo, {"D-001": "local"})

    assert approve._reach_movement(repo)[0] == []


def test_a_promotion_is_not_counted_against_the_criterion(tmp_path: Path) -> None:
    """`local` → `mandate` is a human saying the loop was too loose, and `change_request` already
    files that from the gesture it is actually made with. Counting it here too would double it."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001", reach="mandate", settled_by="human")]),
    )
    _derived(repo, {"D-001": "local"})

    assert approve._reach_movement(repo)[0] == []


def test_the_freeze_advances_the_baseline_it_just_read(tmp_path: Path) -> None:
    """A comparison whose baseline is only ever written by *drafting* counts the same demotion again
    at every re-approval — `/revise` puts this gate back to `pending` — so the figure would grow
    with the number of revisions rather than with the number of misjudged reaches. The gate that
    consumes the baseline advances it."""
    from rein import event_chain, observations, revise

    repo = repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "pending", "acceptance": "pending"}, plan_status="draft"),
        plan=make_plan(
            claims=[make_claim("C-001")],
            tasks=[make_task("T-001", claim_ids=["C-001"])],
            decisions=[make_decision("D-001", reach="local")],
        ),
    )
    _derived(repo, {"D-001": "mandate"})

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    first = [e for e in observations.read() if e.kind == "reach_overruled"]

    events, _ = event_chain.scan(repo.events)
    assert event_chain.derived_reaches(events) == {"D-001": "local"}
    assert [e.arm for e in first] == [observations.ARM_TOO_MANDATE]

    revise.apply(repo, revise.plan_revision(repo, "mandate", []), "the task breakdown was wrong")
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    assert len([e for e in observations.read() if e.kind == "reach_overruled"]) == 1


def test_a_decision_with_no_snapshot_behind_it_is_not_guessed_at(tmp_path: Path) -> None:
    """One that first appeared after the last pre-freeze pass has no "before" to have moved from,
    and a demotion inferred from a missing snapshot is a reading with no measurement behind it."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(decisions=[make_decision("D-001", reach="local")]),
    )

    assert approve._reach_movement(repo)[0] == []  # nothing derived at all


# --- how many times this cycle stops ----------------------------------------------


#: A task whose work leaves the repository. Anything not exactly `False` is not a declaration that
#: something is irreversible, so the fixtures spell the flag out both ways.
def _state_of(repo: repo_mod.Repo) -> models.State:
    state = store_mod.Store(repo).read_state()
    assert state is not None
    return state


def _crossing_task(task_id: str = "T-001", *, reversible: bool = False) -> dict[str, object]:
    return make_task(
        task_id,
        claim_ids=["C-001"],
        operator_surface=[
            {
                "kind": "persistence",
                "name": f"{task_id}: the users table",
                "paths": ["db/schema.sql"],
                "reversible": reversible,
                "adr": "ADR-001",
            }
        ],
    )


def test_a_cycle_that_declares_nothing_irreversible_keeps_two_gates(tmp_path: Path) -> None:
    """Two is what the criterion gives a change that is only code — never a ceiling the tool holds."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(tasks=[_crossing_task("T-001", reversible=True)]),
    )

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    assert _state_of(repo).gate_ids == ("mandate", "acceptance")


def test_freezing_a_mandate_gives_every_irreversible_task_a_gate_of_its_own(tmp_path: Path) -> None:
    """The act that fixes what will be built is the act that fixes how many more times this stops."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(
            tasks=[_crossing_task("T-001"), _crossing_task("T-002", reversible=True), _crossing_task("T-003")]
        ),
    )

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = _state_of(repo)
    assert state.gate_ids == ("mandate", "T-001", "T-003", "acceptance")
    assert [state.gate_status(g) for g in ("T-001", "T-003")] == ["pending", "pending"]


def test_a_crossing_stands_on_the_mandate_alone_and_never_on_another_crossing(tmp_path: Path) -> None:
    """Ordering two crossings against each other would be authorizing execution order, which
    `00-concept.md` puts inside the delegation. Each is downstream of the mandate, and acceptance
    is downstream of all of them."""
    state = models.State(
        make_state(gates={"mandate": "approved", "T-001": "pending", "T-003": "pending", "acceptance": "pending"})
    )

    assert state.upstream_of("T-001") == ("mandate",)
    assert state.upstream_of("T-003") == ("mandate",)
    assert state.upstream_of("acceptance") == ("mandate", "T-001", "T-003")
    assert state.pending_upstream("T-003") is None  # the other crossing does not hold it shut
    assert state.pending_upstream("acceptance") == "T-001"


def test_acceptance_is_not_ready_while_a_crossing_is_pending(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "approved", "T-001": "pending", "acceptance": "pending"}),
        plan=make_plan(tasks=[_crossing_task("T-001")]),
    )

    assert any("T-001" in b for b in approve.readiness(repo, "acceptance"))


def test_a_gate_this_cycle_does_not_have_is_refused_rather_than_read_as_pending(tmp_path: Path) -> None:
    """`gate_status` answers `pending` for a name it does not hold, so a membership test that was
    not there would have made every well-formed task id an approvable gate."""
    repo = repo_at(tmp_path, state=make_state(gates=PENDING_ALL, plan_status="draft"))

    with pytest.raises(approve.ApprovalError, match="has no gate 'T-009'"):
        approve.readiness(repo, "T-009")


def test_re_approving_a_mandate_replaces_the_crossings_rather_than_adding_to_them(tmp_path: Path) -> None:
    """A roll back un-freezes the plan and the next mandate may cut different tasks. A crossing
    gate left behind with no task to reach it is a gate acceptance waits on forever."""
    from rein import revise

    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(tasks=[_crossing_task("T-001")]),
    )
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    assert _state_of(repo).crossing_gates == ("T-001",)

    revise.apply(repo, revise.plan_revision(repo, "mandate", []), "the breakdown was wrong")
    (repo.root / ".rein" / "plan.yaml").write_text(
        yaml.safe_dump(make_plan(tasks=[_crossing_task("T-002")]), sort_keys=False), encoding="utf-8"
    )
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    assert _state_of(repo).crossing_gates == ("T-002",)


def test_the_mandate_screen_names_every_stop_it_is_about_to_create(tmp_path: Path) -> None:
    """A count that follows from the change, seen while approving the change — not one discovered
    later, one stop at a time."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates=PENDING_ALL, plan_status="draft"),
        plan=make_plan(tasks=[_crossing_task("T-001"), _crossing_task("T-002", reversible=True)]),
    )

    named = approve.naming(repo, "mandate")["crossing"]

    assert [row["task_id"] for row in named] == ["T-001"]
    assert named[0]["adr"] == "ADR-001"
    assert "cannot be undone" in approve.render_crossing(named)


def test_a_crossing_screen_names_only_the_task_about_to_run(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"mandate": "approved", "T-001": "pending", "T-003": "pending", "acceptance": "pending"}
        ),
        plan=make_plan(tasks=[_crossing_task("T-001"), _crossing_task("T-003")]),
    )

    assert [row["task_id"] for row in approve.naming(repo, "T-003")["crossing"]] == ["T-003"]
    assert approve.naming(repo, "acceptance")["crossing"] == []


# --- a gate that stops existing takes its change requests with it ------------------


def _recut(tmp_path: Path, *, reversible: bool) -> None:
    """Re-cut the draft plan so T-001 is, or is no longer, an irreversible point."""
    path = tmp_path / ".rein" / "plan.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    for task in document["tasks"]:
        for surface in task.get("operator_surface", []):
            surface["reversible"] = reversible
    path.write_text(yaml.safe_dump(document, sort_keys=False, allow_unicode=True), encoding="utf-8")


def test_an_open_request_against_a_crossing_refuses_the_freeze_that_would_delete_it(tmp_path: Path) -> None:
    """The mandate is the one approval that can end another gate's existence. A request standing
    against a gate this cut deletes would survive as a record holding nothing shut, which is the
    state `changes add` already refuses to create."""
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"mandate": "approved", "T-001": "pending", "acceptance": "pending"},
            plan_status="draft",
        ),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[_crossing_task("T-001")]),
    )
    request_id = change_request.add(repo, "T-001", target="T-001", reason="the schema is wrong")
    _recut(tmp_path, reversible=True)

    blockers = approve.readiness(repo, "mandate", already_approved_blocks=False)

    assert any(request_id in b and "no longer declares irreversible" in b for b in blockers)


def test_addressing_it_lets_the_freeze_through_and_the_approval_closes_it(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"mandate": "approved", "T-001": "pending", "acceptance": "pending"},
            plan_status="draft",
        ),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[_crossing_task("T-001")]),
    )
    request_id = change_request.add(repo, "T-001", target="T-001", reason="the schema is wrong")
    change_request.address(repo, request_id, "the task no longer writes the table")
    _recut(tmp_path, reversible=True)

    assert not any(request_id in b for b in approve.readiness(repo, "mandate", already_approved_blocks=False))

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = _state_of(repo)
    assert state.gate_ids == ("mandate", "acceptance")
    closed = next(cr for cr in state.change_requests if cr["id"] == request_id)
    assert closed["status"] == "resolved", "a request whose gate was deleted is closed, never orphaned"


def test_a_request_against_a_crossing_the_cut_keeps_is_untouched(tmp_path: Path) -> None:
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"mandate": "approved", "T-001": "pending", "acceptance": "pending"},
            plan_status="draft",
        ),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[_crossing_task("T-001")]),
    )
    request_id = change_request.add(repo, "T-001", target="T-001", reason="the schema is wrong")

    assert not any(request_id in b for b in approve.readiness(repo, "mandate", already_approved_blocks=False))

    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    state = _state_of(repo)
    assert "T-001" in state.gate_ids
    still_open = next(cr for cr in state.change_requests if cr["id"] == request_id)
    assert still_open["status"] == "open"
    assert any(request_id in b for b in approve.readiness(repo, "T-001"))


def test_no_plan_drops_no_gate(tmp_path: Path) -> None:
    """`Plan.crossing_task_ids` is `()` both for a plan that declares nothing irreversible and for
    a plan that is not there, and subtracting the second from this cycle's gates says every
    crossing is about to be deleted. The question is asked of a plan or not asked."""
    repo = repo_at(
        tmp_path,
        state=make_state(
            gates={"mandate": "approved", "T-001": "pending", "acceptance": "pending"},
            plan_status="draft",
        ),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[_crossing_task("T-001")]),
    )
    request_id = change_request.add(repo, "T-001", target="T-001", reason="the schema is wrong")
    (tmp_path / ".rein" / "plan.yaml").unlink()

    blockers = approve.readiness(repo, "mandate", already_approved_blocks=False)

    assert not any(request_id in b for b in blockers)
    assert any("plan.yaml" in b for b in blockers), "the absent plan is still reported"


def test_approving_a_crossing_closes_no_other_gates_requests(tmp_path: Path) -> None:
    """Only the mandate re-derives the gate set, so only the mandate can delete one. Reading the
    deleted set off an empty `crossings` at any other gate would name every crossing there is."""
    repo = repo_at(
        tmp_path,
        state=make_state(gates={"mandate": "approved", "T-001": "pending", "T-002": "pending"}),
        plan=make_plan(claims=[make_claim("C-001")], tasks=[_crossing_task("T-001"), _crossing_task("T-002")]),
    )
    other = change_request.add(repo, "T-002", target="T-002", reason="not yet")
    change_request.address(repo, other, "changed the migration")

    approve.record_approval(repo, "T-001", approve.approval_subject(repo, "T-001"))

    still = next(cr for cr in _state_of(repo).change_requests if cr["id"] == other)
    assert still["status"] == "addressed", "T-002's gate still exists, so its request is still its own"
