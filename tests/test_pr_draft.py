"""Tests for pr_draft.py — the PR body assembled from what the cycle actually recorded.

One line carries most of the weight: a PR body is where a reviewer's expectations get set, so
"0 blocking" next to a diff that could not be analysed is the single most misleading thing
this tool could print. When coverage is insufficient the count is not rendered at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import models, pr_draft
from rein import repo as repo_mod
from tests._support import chain, make_plan, make_review, make_state, make_task, seed_repo

ALL_APPROVED = dict.fromkeys(models.GATE_ORDER, "approved")


def body_for(tmp_path: Path, **kwargs: object) -> str:
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return pr_draft.build_body(repo_mod.Repo(tmp_path))


# --- the coverage line --------------------------------------------------------


def test_insufficient_coverage_withholds_the_count(tmp_path: Path) -> None:
    body = body_for(tmp_path, review=make_review(generated=True, coverage_status="insufficient"))
    assert "**insufficient**" in body
    assert "**undeterminable** (not zero: we could not look)" in body
    assert "Extra behaviors: 0" not in body


def test_sufficient_coverage_reports_the_count(tmp_path: Path) -> None:
    body = body_for(tmp_path, review=make_review(generated=True))
    assert "Coverage: sufficient" in body
    assert "Extra behaviors: 0 blocking, 0 total" in body


def test_a_blocking_extra_behaviour_is_counted(tmp_path: Path) -> None:
    extra = {
        "id": "EXTRA-001",
        "statement_id": "STMT-001",
        "category": "new_default",
        "risk": "high",
        "grounded": False,
        "blocking": True,
    }
    body = body_for(tmp_path, review=make_review(generated=True, extra_behaviors=[extra]))
    assert "Extra behaviors: 1 blocking, 1 total" in body


def test_an_ungenerated_review_says_so(tmp_path: Path) -> None:
    assert "Review: not generated" in body_for(tmp_path, review=make_review(generated=False))


# --- digests and gates --------------------------------------------------------


def test_the_body_names_the_plan_digest_and_the_chain_root(tmp_path: Path) -> None:
    body = body_for(tmp_path, events=chain("cycle_initialized"))
    assert "Plan digest: sha256:" in body
    assert "Event chain root: sha256:" in body


def test_an_unrecorded_digest_says_so_rather_than_being_omitted(tmp_path: Path) -> None:
    body = body_for(tmp_path, review=make_review(generated=False))
    assert "Change digest: (not recorded)" in body


def test_every_gate_is_listed_with_its_approval(tmp_path: Path) -> None:
    body = body_for(tmp_path, state=make_state(gates=ALL_APPROVED, phase="done"))
    for gate in models.GATE_ORDER:
        assert f"- {gate}: approved (approval: GA-{gate.upper()}-0001)" in body


def test_a_pending_gate_shows_no_approval(tmp_path: Path) -> None:
    assert "- build: pending (approval: -)" in body_for(tmp_path)


def test_a_damaged_chain_says_the_pr_must_not_be_merged(tmp_path: Path) -> None:
    seed_repo(tmp_path, events=chain("cycle_initialized", "task_completed"))
    log = tmp_path / ".rein" / "events.ndjson"
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    body = pr_draft.build_body(repo_mod.Repo(tmp_path))
    assert "must not be merged as it stands" in body


# --- tasks, non-goals, and the empty case -------------------------------------


def test_task_counts_are_derived(tmp_path: Path) -> None:
    body = body_for(
        tmp_path,
        plan=make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
        state=make_state(tasks={"T-001": "done"}),
    )
    assert "done=1" in body and "todo=1" in body


def test_an_uninitialized_repo_produces_a_body_that_says_so(tmp_path: Path) -> None:
    assert "nothing to summarize" in body_for(tmp_path, state=None)


# --- the CLI ------------------------------------------------------------------


def test_the_cli_writes_the_file_and_never_runs_gh(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert pr_draft.main(["--repo", str(tmp_path)]) == 0
    assert (tmp_path / pr_draft.OUT_PATH).exists()
    out = capsys.readouterr().out
    assert "create the PR yourself" in out
    assert "gh pr create" in out  # printed for the human to run, not executed


def test_stdout_mode_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert pr_draft.main(["--stdout", "--repo", str(tmp_path)]) == 0
    assert "Grounded implementation review" in capsys.readouterr().out
    assert not (tmp_path / pr_draft.OUT_PATH).exists()


def test_the_base_branch_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    pr_draft.main(["--base", "develop", "--stdout", "--repo", str(tmp_path)])
    assert "base: `develop`" in capsys.readouterr().out


