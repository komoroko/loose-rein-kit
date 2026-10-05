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

from rein import approve, brief, build_loop, data, doctor, event_chain, gate_guard, models, reviews_cmd, store
from rein import repo as repo_mod
from tests._support import make_config, make_plan, make_reviews, make_state, seed_repo

#: Two reviews at `build`, so a test can take one away or swap their order.
_TWO = ["correctness", "simplification"]


def _frozen(tmp_path: Path) -> repo_mod.Repo:
    """A repository whose mandate is approved and frozen, with two reviews at `build`."""
    seed_repo(
        tmp_path,
        plan=make_plan(),
        state=make_state(gates={"mandate": "pending"}, plan_status="draft"),
        reviews=make_reviews(build=_TWO),
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
    return make_reviews(build=["correctness"])


def test_changing_the_reviews_after_the_freeze_rewinds_nothing(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    before = store.Store(repo).read_state()
    assert before is not None and before.gate_status("mandate") == "approved"

    changes = _apply(repo, _without_simplification(), "the team runs its own linter")

    assert changes == ["build: no longer reads for simplification"]
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
    repo.reviews.write_bytes(store.dump_yaml(make_reviews()))

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
    repo.reviews.write_bytes(store.dump_yaml(make_reviews()))
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
    assert reviews is not None and reviews.reviews_at("build") == ("correctness",)


def test_a_write_of_reviews_yaml_without_its_record_is_refused_by_the_store(tmp_path: Path) -> None:
    """Held at the store, not at each writer: a writer that forgot would pass its own tests."""
    repo = _frozen(tmp_path)
    with pytest.raises(store.StoreError, match="reviews_changed"):
        with store.Store(repo).transaction() as tx:
            tx.write("reviews", make_reviews())
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

    security = make_reviews(build=["correctness", "simplification", "security"])
    with pytest.raises(store.StaleWriteError):
        reviews_cmd.apply(repo, security, "the dashboard", actor="test", expect=seen)
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.reviews_at("build") == ("correctness",)


def test_reordering_is_a_change(tmp_path: Path) -> None:
    """A reviewer is asked in its stage's order. Before, a reorder was "nothing changes" and was
    never written, while the dashboard offered it."""
    repo = _frozen(tmp_path)
    swapped = make_reviews(build=["simplification", "correctness"])
    assert _apply(repo, swapped) == [
        "build: asks in the order simplification, correctness (was correctness, simplification)"
    ]
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.reviews_at("build") == ("simplification", "correctness")


def test_the_same_document_is_not_a_change(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    assert _apply(repo, make_reviews(build=_TWO)) == []


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
    assert reviews is not None and reviews.reviews_at("build") == ("correctness", "simplification")


@pytest.mark.parametrize("name", ["comparison", "actual_extraction", "adversarial"])
def test_a_custom_review_may_not_take_the_name_of_a_packaged_review_or_switch(name: str) -> None:
    document = {**make_reviews(), "custom": [{"name": name, "question": "Is it what was asked?"}]}
    with pytest.raises(models.DocumentError, match="packaged review or switch"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_a_stage_reading_for_an_undefined_review_is_refused() -> None:
    document = make_reviews(acceptance=["performance"])
    with pytest.raises(models.DocumentError, match="acceptance/reviews: `performance`"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_a_stage_s_own_review_is_switched_not_added() -> None:
    """The adversarial review of a drafted document is the stage's own: added as well, the same
    review would run twice, and switching it off would leave it running."""
    document = make_reviews(drafting=["adversarial"])
    with pytest.raises(models.DocumentError, match="switched, not added"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_any_review_can_be_added_at_any_stage(tmp_path: Path) -> None:
    """Where a review is added is what it reads; no stage is reserved for one kind of review."""
    repo = _frozen(tmp_path)
    document = make_reviews(build=_TWO, drafting=["security"], acceptance=["adversarial", "correctness"])
    assert _apply(repo, document) == [
        "requirements: now reads for security",
        "design: now reads for security",
        "tasks: now reads for security",
        "acceptance: no longer reads for security",
        "acceptance: now reads for adversarial, correctness",
    ]
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.readings.reviews == ("adversarial", "correctness")
    assert "Security" in reviews_cmd.stage_brief(reviews, "design")


def test_what_a_custom_review_asks_is_part_of_the_record(tmp_path: Path) -> None:
    """The question used to live in a repository file of its own: an agent could rewrite it, and
    neither reviews.yaml, the chain nor acceptance changed. It is in the document now, so a new
    question is a change like any other."""
    repo = _frozen(tmp_path)
    document = make_reviews(build=["correctness", "performance"])
    document["custom"] = [{"name": "performance", "question": "Does every query use an index?"}]
    assert _apply(repo, document) == [
        "build: no longer reads for simplification",
        "build: now reads for performance",
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
    document["design"]["adversarial"] = on
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


def test_the_packaged_reviews_read_for_the_adversarial_review_alone() -> None:
    """The default adds one review, an attempt to refute each task, and keeps every stage's own
    readings on. The rest, the security review of the whole change included, are a person's to add."""
    packaged = models.Reviews.parse(data.read_text(reviews_cmd.PACKAGED))
    assert packaged.reviews_at("build") == ("adversarial",)
    assert all(packaged.adversarial(stage) and not packaged.reviews_at(stage) for stage in models.DRAFTING_STAGES)
    assert packaged.readings == models.Readings(actual_extraction=True, comparison=True, reviews=())
    assert set(models.BUILTIN_REVIEWS) == {"adversarial", "correctness", "simplification", "security"}


def test_adding_the_security_review_of_the_whole_change_is_a_recorded_change(tmp_path: Path) -> None:
    repo = _frozen(tmp_path)
    changes = _apply(repo, make_reviews(build=_TWO, acceptance=()), "no auth here")
    assert changes == ["acceptance: no longer reads for security"]
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.readings.reviews == ()
    assert "acceptance: actual extraction on; comparison on; no review added" in reviews_cmd.render(reviews)


def test_switching_the_comparison_off_is_a_recorded_change(tmp_path: Path) -> None:
    """Nothing is required: the comparison is switched off like any other reading, by a person,
    on the record."""
    repo = _frozen(tmp_path)
    changes = _apply(repo, make_reviews(build=_TWO, comparison=False), "a spike nobody will ship")
    assert changes == ["comparison at acceptance: OFF"]
    reviews = store.Store(repo).read_reviews()
    assert reviews is not None and reviews.readings.comparison is False
    assert "comparison OFF" in reviews_cmd.render(reviews)


def test_a_comparison_without_the_extraction_it_compares_is_refused() -> None:
    document = make_reviews(actual_extraction=False, comparison=True)
    with pytest.raises(models.DocumentError, match="needs `actual_extraction` on"):
        models.Reviews.parse(store.dump_yaml(document).decode())


def test_a_record_from_before_the_stages_is_replaced_by_the_document_that_adds_them() -> None:
    """A record written before this shape states none of the stages. The document that adds them
    has to be a change — normalizing the old record into values it never stated would make the two
    compare equal, and the write that repairs the file would never happen."""
    old = {"adversarial": dict.fromkeys(models.DRAFTING_STAGES, True), "steps": [{"name": "review", "reviews": _TWO}]}
    assert models.Reviews(old).normalized() == {}
    new = models.Reviews(make_reviews(build=_TWO, acceptance=())).normalized()
    assert reviews_cmd.describe(old, new) == [
        "adversarial review at requirements: on",
        "adversarial review at design: on",
        "adversarial review at tasks: on",
        "build: now reads for correctness, simplification",
        "actual extraction at acceptance: on",
        "comparison at acceptance: on",
    ]


def test_the_build_lane_is_one_reviewer_step_sent_back_on_the_repair_budget() -> None:
    """One launch reads for every review added at `build`, and its findings go back as many times
    as `review_policy.repair_rounds` allows — the budget acceptance repairs on, not one of its own."""
    config = models.Config(make_config(repair_rounds=3))
    [step] = [
        s
        for s in build_loop.Config.from_models(config, models.Reviews(make_reviews(build=_TWO))).steps
        if s.kind == "agent"
    ]
    assert (step.name, step.reviews, step.retries, step.agent_role) == ("review", tuple(_TWO), 3, "reviewer")
    none = build_loop.Config.from_models(config, models.Reviews(make_reviews()))
    assert not [s for s in none.steps if s.kind == "agent"], "nothing added, no reviewer launched"


def test_a_drafting_stage_is_told_each_question_it_asks_of_its_document() -> None:
    reviews = models.Reviews(make_reviews(drafting=["simplification", "performance"]))
    reviews = models.Reviews({**reviews.raw, "custom": [{"name": "performance", "question": "Is it bounded?"}]})
    brief_ = reviews_cmd.stage_brief(reviews, "requirements")
    assert brief_.startswith("adversarial review: on")
    assert "**Simplification**: scope no requirement asks for" in brief_ and "**performance**: Is it bounded?" in brief_
    with pytest.raises(reviews_cmd.ReviewsError, match="not a drafting stage"):
        reviews_cmd.stage_brief(reviews, "build")


def test_a_record_from_before_the_stages_names_no_stage_switched_off() -> None:
    """A record that states no stage says nothing about one: read as "off", every stage of the first
    round after an upgrade would be named as switched off by somebody who switched nothing."""
    old = event_chain.make(
        "reviews_changed",
        "demo-cycle",
        actor="t",
        detail={
            "document": {"adversarial": dict.fromkeys(models.DRAFTING_STAGES, True)},
            "digest": "sha256:" + "1" * 64,
            "reason": "old",
        },
    )
    new = event_chain.make(
        "reviews_changed",
        "demo-cycle",
        actor="t",
        detail={"document": make_reviews(), "digest": "sha256:" + "2" * 64, "reason": "upgraded"},
    )
    assert reviews_cmd.adversarial_switched_off([old, new], since=0) == []
    assert reviews_cmd.adversarial_switched_off([old, new], since=1) == []
