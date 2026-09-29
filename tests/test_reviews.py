"""reviews.yaml: which reviews run, outside the mandate's freeze and written only by a human (CR-50).

What the freeze used to guarantee about the reviewer is kept by other means, and each of those
means is pinned here: the guard refuses a hand edit, the verb insists on a terminal and a reason,
the change reaches the chain, and acceptance says what each task was actually read for.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from rein import approve, brief, event_chain, gate_guard, models, reviews_cmd, store
from rein import repo as repo_mod
from tests._support import REVIEW_STEP, make_plan, make_reviews, make_state, seed_repo


def _frozen(tmp_path: Path) -> repo_mod.Repo:
    """A repository whose mandate is approved and frozen, with the packaged reviewer step."""
    seed_repo(
        tmp_path,
        plan=make_plan(),
        state=make_state(gates={"mandate": "pending"}, plan_status="draft"),
        reviews=make_reviews(steps=[REVIEW_STEP]),
        git=True,
    )
    repo = repo_mod.Repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    return repo


def _without_simplification() -> dict[str, object]:
    return make_reviews(steps=[{**REVIEW_STEP, "reviews": ["correctness"]}])


def test_changing_the_reviews_after_the_freeze_rewinds_nothing(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    before = store.Store(repo).read_state()
    assert before is not None and before.gate_status("mandate") == "approved"

    changes = reviews_cmd.apply(repo, _without_simplification(), "the team runs its own linter", actor="test")

    assert changes == ["step review: no longer reads for simplification"]
    after = store.Store(repo).read_state()
    assert after is not None and after.gate_status("mandate") == "approved"
    assert after.plan_status == "frozen"
    # The freeze still holds over everything it covers: nothing the mandate approved moved.
    assert gate_guard._frozen_artifact_failures(repo) == []
    [changed] = [e for e in store.Store(repo).read_events() if e.event == "reviews_changed"]
    assert changed.detail["reason"] == "the team runs its own linter"
    assert changed.detail["changes"] == changes


def test_an_agent_editing_reviews_yaml_is_refused_by_the_guard(tmp_path: Path) -> None:
    """Outside the freeze, so rule 2 never covers it; rule 1 does, at every stage of a cycle."""
    repo = _frozen(tmp_path)
    allowed, why = gate_guard.evaluate(str(tmp_path / ".rein/reviews.yaml"), repo)
    assert not allowed and "reviews.yaml" in why


def test_a_change_needs_a_reason(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    with pytest.raises(reviews_cmd.ReviewsError, match="needs a reason"):
        reviews_cmd.apply(repo, _without_simplification(), "  ", actor="test")


def test_the_terminal_route_refuses_a_stdin_that_is_not_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A piped stdin or an agent's captured subprocess cannot switch a review off by accident."""
    repo = _frozen(tmp_path)
    draft = tmp_path / "draft.yaml"
    draft.write_bytes(store.dump_yaml(_without_simplification()))
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))

    assert reviews_cmd.main(["apply", str(draft), "--reason", "r", "--repo", str(tmp_path)]) == 2
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.steps[0].reviews == ("correctness", "simplification")


def test_comparison_cannot_be_switched_off(tmp_path: Path) -> None:
    """It is what acceptance is decided by, not a way of improving the work: there is no key for it."""
    document = {**make_reviews(), "acceptance": {"comparison": False}}
    with pytest.raises(models.DocumentError, match="acceptance"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_a_step_reading_for_an_undefined_review_is_refused() -> None:
    document = make_reviews(steps=[{"name": "review", "reviews": ["performance"]}])
    with pytest.raises(models.DocumentError, match="performance"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_a_custom_review_whose_question_file_is_missing_is_refused_before_it_is_written(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    document = make_reviews(steps=[{"name": "review", "reviews": ["correctness", "performance"]}])
    document["custom"] = [{"name": "performance", "prompt": "docs/reviews/performance.md"}]
    with pytest.raises(reviews_cmd.ReviewsError, match="performance"):
        reviews_cmd.apply(repo, document, "r", actor="test")

    (tmp_path / "docs" / "reviews").mkdir(parents=True)
    (tmp_path / "docs" / "reviews" / "performance.md").write_text("Does every query use an index?\n", encoding="utf-8")
    assert reviews_cmd.apply(repo, document, "r", actor="test") == [
        "step review: no longer reads for simplification",
        "step review: now reads for performance",
        "custom review performance: added, asks docs/reviews/performance.md",
    ]


def test_acceptance_lists_what_each_task_was_actually_read_for() -> None:
    """A change mid-cycle does not re-read the tasks already read, so the screen says who got what."""
    events = [
        event_chain.make(
            "reviews_applied",
            "demo-cycle",
            subject_ids=["T-001", "T-002"],
            detail={"step": "review", "stage": "task", "reviews": ["correctness", "simplification"]},
        ),
        event_chain.make(
            "reviews_applied",
            "demo-cycle",
            subject_ids=["T-003"],
            detail={"step": "review", "stage": "task", "reviews": ["correctness"]},
        ),
    ]
    read = brief.reviews_by_task(events)
    assert read == {
        "T-001": ["review (task): correctness, simplification"],
        "T-002": ["review (task): correctness, simplification"],
        "T-003": ["review (task): correctness"],
    }
    sections = brief.derive(plan=None, state=models.State(make_state()), config=None, reviews_applied=read)
    assert sections["residuals"]["reviews_by_task"][2] == {
        "task_id": "T-003",
        "readings": ["review (task): correctness"],
    }


def test_a_stage_without_an_adversarial_review_is_named_on_the_mandate_screen(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        state=make_state(gates={"mandate": "pending"}, plan_status="draft"),
        reviews={**make_reviews(), "adversarial": {"requirements": True, "design": False, "tasks": True}},
    )
    named = approve.naming(repo_mod.Repo(tmp_path), "mandate")["adversarial_off"]
    assert [row["id"] for row in named] == ["design"]
