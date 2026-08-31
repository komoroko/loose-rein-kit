"""Tests for task_cmd.py — the supported write path for "try this task again".

The hole this fills: `state.yaml` is machine-written only and `rein guard` denies a hand edit,
but a human deciding a blocked task should be retried is a legitimate decision with nowhere to
go. What matters is not that the status changes — it is that it cannot change without a reason
landing in the same transaction.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import build_loop, gate_guard, task_cmd
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_plan, make_state, make_task, seed_repo


def repo_with_a_blocked_task(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(
        tmp_path,
        plan=make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]),
        state=make_state(phase="build", plan_status="frozen"),
    )
    repo = repo_mod.Repo(tmp_path)
    build_loop.set_task_status(repo, "T-001", "in-progress")
    build_loop.record_attempt_failure(
        repo, "T-001", failed_step="check", failure_summary="mypy: 3 errors", retries_left={"check": 0}
    )
    build_loop.set_task_status(repo, "T-001", "blocked")
    return repo


def entry_of(repo: repo_mod.Repo, task_id: str = "T-001") -> dict[str, object]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw["tasks"][task_id])


def test_a_reset_and_its_reason_land_in_one_transaction(tmp_path: Path) -> None:
    repo = repo_with_a_blocked_task(tmp_path)
    result = task_cmd.reset(repo, "T-001", status="todo", reason="installed the missing toolchain")

    assert result.previous == "blocked"
    assert entry_of(repo)["status"] == "todo"
    event = store_mod.Store(repo).read_events()[-1]
    assert event.event == "decision_declared"
    assert event.subject_ids == ("T-001",)
    assert event.detail["kind"] == "task_reset"
    assert event.detail["reason"] == "installed the missing toolchain"
    assert event.detail["from"] == "blocked" and event.detail["to"] == "todo"


def test_the_retry_budget_is_not_quietly_refilled(tmp_path: Path) -> None:
    """Otherwise a task that can never pass gets an unlimited allowance by being reset in a loop."""
    repo = repo_with_a_blocked_task(tmp_path)
    result = task_cmd.reset(repo, "T-001", status="todo", reason="worth one more look")

    assert result.handoff["retries_left"] == {"check": 0}
    assert entry_of(repo)["handoff"]["failed_step"] == "check"  # type: ignore[index]


def test_starting_over_is_a_separate_decision_and_is_recorded_as_one(tmp_path: Path) -> None:
    repo = repo_with_a_blocked_task(tmp_path)
    task_cmd.reset(repo, "T-001", status="todo", reason="the ticket was wrong; rewritten", fresh=True)

    assert "handoff" not in entry_of(repo)
    assert store_mod.Store(repo).read_events()[-1].detail["handoff"] == "discarded"


def test_a_task_cannot_be_declared_done_by_hand(tmp_path: Path) -> None:
    """`done` means it passed the quality gate and landed a commit — the evidence gate ④ reads."""
    repo = repo_with_a_blocked_task(tmp_path)
    with pytest.raises(ValueError, match="not 'done'"):
        task_cmd.reset(repo, "T-001", status="done", reason="looks fine to me")


def test_the_guard_still_refuses_the_hand_edit_this_verb_replaces(tmp_path: Path) -> None:
    """The verb is the write path rule 1 presumes exists — not a way around rule 1."""
    repo = repo_with_a_blocked_task(tmp_path)
    ok, reason = gate_guard.evaluate(str(repo.path(".rein/state.yaml")), repo)
    assert not ok
    assert "Central Store transaction" in reason


def test_an_empty_reason_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_with_a_blocked_task(tmp_path)
    assert task_cmd.main(["reset", "T-001", "--reason", "   ", "--repo", str(tmp_path)]) == 2
    assert "the record is the point" in capsys.readouterr().err


def test_a_task_the_plan_does_not_have_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_with_a_blocked_task(tmp_path)
    assert task_cmd.main(["reset", "T-404", "--reason", "typo", "--repo", str(tmp_path)]) == 2
    assert "not a task in .rein/plan.yaml" in capsys.readouterr().err


def test_the_cli_reports_what_it_did(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo_with_a_blocked_task(tmp_path)
    assert task_cmd.main(["reset", "T-001", "--reason", "docker is installed now", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "T-001: blocked → todo (docker is installed now)" in out
    assert "handoff kept" in out
    assert "concluded by a disposition in the review" in out  # the escalation is not closed by this


def test_a_reset_that_will_not_launch_anything_says_so(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Without this line, a reset that produces no implementer launch looks like a bug.

    The handoff is kept by default, and a kept `escalation` is what makes the next `rein build`
    re-raise the verdict instead of paying for a launch that reaches it again. The human who just
    typed a reason for trying again is exactly the person who has to be told that, and told which
    flag means "I repaired something outside the tree".
    """
    repo = repo_with_a_blocked_task(tmp_path)
    build_loop.record_escalation(
        repo,
        "T-001",
        kind="no_implementation",
        message="T-001: the implementer produced no change at all",
        tree="sha256:" + "a" * 64,
    )
    assert task_cmd.main(["reset", "T-001", "--reason", "it should have work to do", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "ended 'no_implementation' before the quality gate" in out
    assert "--fresh" in out


def test_a_fresh_reset_promises_nothing_about_a_record_it_discarded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_with_a_blocked_task(tmp_path)
    build_loop.record_escalation(
        repo, "T-001", kind="no_implementation", message="nothing to do", tree="sha256:" + "a" * 64
    )
    args = ["reset", "T-001", "--reason", "installed the tool", "--fresh", "--repo", str(tmp_path)]
    assert task_cmd.main(args) == 0
    out = capsys.readouterr().out
    assert "handoff discarded" in out
    assert "no_implementation" not in out


def test_the_verb_is_reachable_from_the_dispatcher() -> None:
    from rein import cli

    assert cli.VERBS["task"].spec == "task_cmd"
    assert "task reset" in cli._build_parser(show_all=True).format_help()
