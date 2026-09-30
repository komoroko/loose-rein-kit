"""reviews.yaml: which reviews run, outside the mandate's freeze and written only by a human (CR-50).

What the freeze used to guarantee about the reviewer is kept by other means, and each of those
means is pinned here: the guard refuses an editor's write, every write is recorded with the
document it wrote, nothing runs on a file that differs from that record (CR-56), the verb insists
on a terminal and a reason, and acceptance says what each task was actually read for.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from rein import approve, brief, build_loop, doctor, event_chain, gate_guard, models, reviews_cmd, store
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


def _expect(repo: repo_mod.Repo) -> str:
    """The digest a person reading reviews.yaml now would apply a change against."""
    record = reviews_cmd.recorded(repo)
    assert record is not None
    return record.digest


def _apply(repo: repo_mod.Repo, document: dict[str, object], reason: str = "r") -> list[str]:
    return reviews_cmd.apply(repo, document, reason, actor="test", expect=_expect(repo))


def _without_simplification() -> dict[str, object]:
    return make_reviews(steps=[{**REVIEW_STEP, "reviews": ["correctness"]}])


def test_changing_the_reviews_after_the_freeze_rewinds_nothing(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    before = store.Store(repo).read_state()
    assert before is not None and before.gate_status("mandate") == "approved"

    changes = _apply(repo, _without_simplification(), "the team runs its own linter")

    assert changes == ["step review: no longer reads for simplification"]
    after = store.Store(repo).read_state()
    assert after is not None and after.gate_status("mandate") == "approved"
    assert after.plan_status == "frozen"
    # The freeze still holds over everything it covers: nothing the mandate approved moved.
    assert gate_guard._frozen_artifact_failures(repo) == []
    changed = [e for e in store.Store(repo).read_events() if e.event == "reviews_changed"][-1]
    assert changed.detail["reason"] == "the team runs its own linter"
    assert changed.detail["changes"] == changes
    assert reviews_cmd.binding_problem(repo) == ""


def test_an_agent_editing_reviews_yaml_is_refused_by_the_guard(tmp_path: Path) -> None:
    """Outside the freeze, so rule 2 never covers it; rule 1 does, at every stage of a cycle."""
    repo = _frozen(tmp_path)
    allowed, why = gate_guard.evaluate(str(tmp_path / ".rein/reviews.yaml"), repo)
    assert not allowed and "reviews.yaml" in why


def test_a_shell_write_of_reviews_yaml_is_run_on_by_nothing_until_it_is_restored(tmp_path: Path) -> None:
    """The edit hook never sees a shell redirect. Before CR-56 nothing compared the file with
    anything: the build ran with the reviewer switched off, the commit went through, and doctor
    said PASS."""
    repo = _frozen(tmp_path)
    repo.reviews.write_bytes(store.dump_yaml(make_reviews(steps=[])))

    with pytest.raises(ValueError, match="not what the audit chain records"):
        build_loop.Config.load(repo)
    assert gate_guard._reviews_failures(repo) != []
    findings, _ = doctor.check_documents(repo)
    assert any(f.level == "FAIL" and "reviews.yaml" in f.message for f in findings)
    # A person's change is not applied over it either: it would be measured from a version nobody chose.
    with pytest.raises(reviews_cmd.ReviewsError, match="restore"):
        _apply(repo, _without_simplification())

    assert reviews_cmd.restore(repo) is True
    assert [s.name for s in build_loop.Config.load(repo).steps if s.kind == "agent"] == ["review"]
    assert store.Store(repo).read_events()[-1].event == "reviews_restored"
    assert reviews_cmd.restore(repo) is False


def test_the_commit_stage_holds_a_repository_that_runs_a_cycle_not_the_template(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    repo.reviews.write_bytes(store.dump_yaml(make_reviews(steps=[])))
    assert gate_guard._reviews_failures(repo) != []
    repo.events.unlink()
    assert gate_guard._reviews_failures(repo) == []
    with pytest.raises(ValueError, match="recorded nowhere"):
        build_loop.Config.load(repo)


def test_a_deleted_reviews_yaml_is_restored_never_reseeded(tmp_path: Path) -> None:
    """Seeding over a record would reset a person's choice to the default for anyone who deletes the file."""
    repo = _frozen(tmp_path)
    _apply(repo, _without_simplification())
    repo.reviews.unlink()

    assert reviews_cmd.seed(repo, actor="test") is False
    assert "rein reviews restore" in reviews_cmd.binding_problem(repo)
    reviews_cmd.restore(repo)
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.steps[0].reviews == ("correctness",)


def test_a_write_of_reviews_yaml_without_its_record_is_refused_by_the_store(tmp_path: Path) -> None:
    """Held at the store, not at each writer: a writer that forgot would pass its own tests."""
    repo = _frozen(tmp_path)
    with pytest.raises(store.StoreError, match="reviews_changed"):
        with store.Store(repo).transaction() as tx:
            tx.write("reviews", make_reviews(steps=[]))
            tx.append("reviews_applied", cycle_id="demo-cycle", detail={})
    assert reviews_cmd.binding_problem(repo) == ""


def test_a_change_needs_a_reason(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    with pytest.raises(reviews_cmd.ReviewsError, match="needs a reason"):
        _apply(repo, _without_simplification(), "  ")


def test_a_change_made_against_an_older_version_is_refused_not_written_over(tmp_path: Path) -> None:
    """The dashboard and the terminal each read a version, then write. Before, the write was checked
    against whatever sat on disk at that moment, so the second writer undid the first in silence."""
    repo = _frozen(tmp_path)
    seen = _expect(repo)
    _apply(repo, _without_simplification(), "the terminal")

    security = make_reviews(steps=[{**REVIEW_STEP, "reviews": ["correctness", "simplification", "security"]}])
    with pytest.raises(store.StaleWriteError):
        reviews_cmd.apply(repo, security, "the dashboard", actor="test", expect=seen)
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.steps[0].reviews == ("correctness",)


def test_reordering_is_a_change(tmp_path: Path) -> None:
    """Steps run in their order and a reviewer is asked in its step's order. Before, a reorder was
    "nothing changes" and was never written, while the dashboard offered it."""
    repo = _frozen(tmp_path)
    swapped = make_reviews(steps=[{**REVIEW_STEP, "reviews": ["simplification", "correctness"]}])
    assert _apply(repo, swapped) == [
        "step review: asks in the order simplification, correctness (was correctness, simplification)"
    ]

    review = {**REVIEW_STEP, "reviews": ["simplification", "correctness"]}
    security = {"name": "sec", "reviews": ["security"]}
    _apply(repo, make_reviews(steps=[review, security]))
    assert _apply(repo, make_reviews(steps=[security, review])) == [
        "steps run in the order sec, review (was review, sec)"
    ]
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and [s.name for s in reviews.steps] == ["sec", "review"]


def test_a_default_written_out_is_not_a_change(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    explicit = make_reviews(steps=[{**REVIEW_STEP, "retries": 1, "stage": "both"}])
    assert _apply(repo, explicit) == []


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


def test_what_a_custom_review_asks_is_part_of_the_record(tmp_path: Path) -> None:
    """The question used to live in a repository file of its own: an agent could rewrite it, and
    neither reviews.yaml, the chain nor acceptance changed. It is in the document now, so a new
    question is a change like any other."""
    repo = _frozen(tmp_path)
    document = make_reviews(steps=[{"name": "review", "reviews": ["correctness", "performance"]}])
    document["custom"] = [{"name": "performance", "question": "Does every query use an index?"}]
    assert _apply(repo, document) == [
        "step review: no longer reads for simplification",
        "step review: now reads for performance",
        "custom review performance: added, asks: Does every query use an index?",
    ]
    document["custom"] = [{"name": "performance", "question": "Is every loop over a table bounded?"}]
    assert _apply(repo, document) == [
        "custom review performance: now asks: Is every loop over a table bounded? (was: Does every query use an index?)"
    ]
    assert build_loop.Config.load(repo).questions == {"performance": "Is every loop over a table bounded?"}

    by_file = {**document, "custom": [{"name": "performance", "prompt": "docs/reviews/performance.md"}]}
    with pytest.raises(reviews_cmd.ReviewsError, match="prompt"):
        _apply(repo, by_file)


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


def _drafting(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, state=make_state(gates={"mandate": "pending"}, plan_status="draft"), reviews=make_reviews())
    return repo_mod.Repo(tmp_path)


def _design(repo: repo_mod.Repo, on: bool, reason: str) -> None:
    document = make_reviews()
    document["adversarial"]["design"] = on
    _apply(repo, document, reason)


def test_a_stage_switched_off_and_back_on_while_drafting_is_still_named(tmp_path: Path) -> None:
    """The mandate screen read reviews.yaml as it stands. Switched off while the design was drafted
    and on again before the approval, the design went without its review and the screen said nothing."""
    repo = _drafting(tmp_path)
    _design(repo, False, "a one-line fix")
    _design(repo, True, "done")

    [row] = approve.naming(repo, "mandate")["adversarial_off"]
    assert row["id"] == "design"
    assert "switched off" in row["change"] and "a one-line fix" in row["change"]
    assert "switched back on" in row["change"]


def test_a_stage_switched_off_in_an_earlier_round_is_not_named_again(tmp_path: Path) -> None:
    """The other half: the screen said "no adversarial review ran here" about a round that had one."""
    repo = _drafting(tmp_path)
    _design(repo, False, "a one-line fix")
    _design(repo, True, "done")
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    assert approve.naming(repo, "mandate")["adversarial_off"] == []
    _design(repo, False, "after the approval")
    [row] = approve.naming(repo, "mandate")["adversarial_off"]
    assert "after the approval" in row["change"] and "a one-line fix" not in row["change"]


def test_a_stage_already_off_when_the_round_began_says_so(tmp_path: Path) -> None:
    repo = _drafting(tmp_path)
    _design(repo, False, "we never review design here")
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))

    [row] = approve.naming(repo, "mandate")["adversarial_off"]
    assert row["change"].startswith("off when this round began") and "we never review design here" in row["change"]
