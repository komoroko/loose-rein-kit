"""`pr_stack` derivation: cutting the work branch into one pull request per task.

Every test here builds a *real* git history, because the thing under test is a claim about
git's first-parent chain. A fake that answers `rev-list` from a dict would pass while the
real command disagreed, which is the one failure this module cannot afford.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rein import event_chain, models, pr_stack
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import DEMO_CYCLE, make_config, make_plan, make_review, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"


# --- a real repository --------------------------------------------------------


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def init_repo(root: Path) -> str:
    """An initialised repository with one commit on `main`. Returns that commit."""
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "test")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    git(root, "add", "README.md")
    git(root, "commit", "-q", "-m", "base")
    return git(root, "rev-parse", "HEAD")


def land_task(root: Path, task_id: str, *, path: str = "") -> str:
    """Land one task the way the build loop does: a leaf branch merged `--no-ff` into the work branch.

    Returns the merge commit — what `build_loop._landed()` records as `completed_commit`.
    """
    leaf = f"{WORK_BRANCH}-{task_id}"
    git(root, "switch", "-q", "-c", leaf, WORK_BRANCH)
    target = root / (path or f"src/{task_id}.py")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"# {task_id}\n", encoding="utf-8")
    git(root, "add", "--", str(target.relative_to(root)))
    git(root, "commit", "-q", "-m", f"{task_id}: work")
    git(root, "switch", "-q", WORK_BRANCH)
    git(root, "merge", "-q", "--no-ff", "--no-edit", leaf)
    return git(root, "rev-parse", "HEAD")


def commit_on(root: Path, branch: str, message: str, *, path: str) -> str:
    """One ordinary commit on `branch` — a phase deliverable, or a review fix onto a slice."""
    current = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    git(root, "switch", "-q", branch)
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(message + "\n", encoding="utf-8")
    git(root, "add", "--", path)
    git(root, "commit", "-q", "-m", message)
    head = git(root, "rev-parse", "HEAD")
    git(root, "switch", "-q", current)
    return head


@pytest.fixture
def cycle(tmp_path: Path) -> Callable[..., dict[str, Any]]:
    """Build a repository whose work branch has landed `tasks`, and seed `.rein/` to match."""

    def build(
        task_ids: tuple[str, ...] = ("T-001", "T-002", "T-003"),
        *,
        blocked_by: dict[str, list[str]] | None = None,
        gates: dict[str, str] | None = None,
        events: list[models.Event] | None = None,
        review: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        root = tmp_path / "repo"
        root.mkdir()
        base = init_repo(root)
        git(root, "switch", "-q", "-c", WORK_BRANCH)

        landed: dict[str, str] = {}
        for task_id in task_ids:
            landed[task_id] = land_task(root, task_id)

        deps = blocked_by or {}
        plan = make_plan(
            branch=WORK_BRANCH,
            tasks=[
                make_task(
                    task_id,
                    kind="foundation" if not deps.get(task_id) else "parallel",
                    blocked_by=deps.get(task_id),
                    claim_ids=["C-001"],
                )
                for task_id in task_ids
            ],
        )
        plan["cycle"]["base_commit"] = base
        state = make_state(gates=gates, tasks={t: "done" for t in task_ids})
        for task_id, commit in landed.items():
            state["tasks"][task_id]["completed_commit"] = commit
        seed_repo(
            root,
            plan=plan,
            state=state,
            config=make_config(branch=WORK_BRANCH),
            events=events,
            review=review if review is not None else make_review(),
        )
        return {"root": root, "base": base, "landed": landed, "repo": repo_mod.Repo(root)}

    return build


def documents(bundle: dict[str, Any], events: list[models.Event] | None = None) -> pr_stack.Documents:
    """The SSOT as `pr_stack` reads it, with `events` standing in for the log when given.

    Tests pass events explicitly rather than writing them into `events.ndjson` because what is
    under test is the reading of the ledger, not the chaining that `event_chain` already covers.
    """
    docs = pr_stack.Documents.read(bundle["repo"])
    return docs if events is None else replace(docs, events=tuple(events))


def derive(bundle: dict[str, Any], events: list[models.Event] | None = None) -> list[pr_stack.Slice]:
    return pr_stack.derive(bundle["repo"], documents(bundle, events), base="main")


def ledger_event(slice_: pr_stack.Slice, url: str = "https://example.invalid/pr/1") -> models.Event:
    return event_chain.make(
        pr_stack.LEDGER_EVENT,
        DEMO_CYCLE,
        subject_ids=(slice_.task_id,) if slice_.task_id else (),
        detail=pr_stack.opened_event_detail(slice_, url),
    )


def chained(events: list[models.Event]) -> list[models.Event]:
    linked: list[models.Event] = []
    previous: models.Event | None = None
    for event in events:
        previous = event_chain.link(previous, event)
        linked.append(previous)
    return linked


# --- derivation ---------------------------------------------------------------


def test_one_slice_per_landed_task_in_landing_order(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    slices = derive(bundle)

    assert [s.task_id for s in slices] == ["T-001", "T-002", "T-003"]
    assert [s.index for s in slices] == [1, 2, 3]
    assert [s.head_sha for s in slices] == [bundle["landed"][t] for t in ("T-001", "T-002", "T-003")]


def test_each_slice_is_based_on_the_one_below_it(cycle: Callable[..., dict[str, Any]]) -> None:
    slices = derive(cycle())

    assert slices[0].base_ref == "main"
    assert [s.base_ref for s in slices[1:]] == [slices[0].branch, slices[1].branch]
    assert [s.base_sha for s in slices[1:]] == [slices[0].head_sha, slices[1].head_sha]


def test_branch_names_avoid_the_leaf_branch_namespace(cycle: Callable[..., dict[str, Any]]) -> None:
    slices = derive(cycle())

    assert [s.branch for s in slices] == [
        f"{WORK_BRANCH}-pr-01-T-001",
        f"{WORK_BRANCH}-pr-02-T-002",
        f"{WORK_BRANCH}-pr-03-T-003",
    ]
    # The leaf branches build_git leaves behind must not collide with any of them.
    assert not {s.branch for s in slices} & {f"{WORK_BRANCH}-{t}" for t in ("T-001", "T-002", "T-003")}


def test_a_slice_carries_only_its_own_commits(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    slices = derive(bundle)

    # Each task landed as one leaf commit plus the --no-ff merge that brought it in.
    for s in slices:
        assert len(s.commits) == 2, s.commits
        assert s.commits[-1] == s.head_sha
    assert len({c for s in slices for c in s.commits}) == 6


def test_commits_outside_any_task_become_a_tail_slice(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    deliverable = commit_on(bundle["root"], WORK_BRANCH, "docs at the gate", path="docs/10-requirements.md")

    slices = derive(bundle)

    tail = slices[-1]
    assert tail.is_tail and tail.task_id == ""
    assert tail.head_sha == deliverable
    assert tail.branch == f"{WORK_BRANCH}-pr-04-tail"
    assert tail.base_ref == slices[-2].branch


def test_no_tail_slice_when_the_last_task_is_the_tip(cycle: Callable[..., dict[str, Any]]) -> None:
    assert all(not s.is_tail for s in derive(cycle()))


def test_a_cycle_with_nothing_landed_derives_no_slices(tmp_path: Path) -> None:
    root = tmp_path / "empty"
    root.mkdir()
    base = init_repo(root)
    git(root, "switch", "-q", "-c", WORK_BRANCH)
    plan = make_plan(branch=WORK_BRANCH, tasks=[make_task("T-001", claim_ids=["C-001"])])
    plan["cycle"]["base_commit"] = base
    seed_repo(root, plan=plan, state=make_state(tasks={"T-001": "todo"}), config=make_config(branch=WORK_BRANCH))

    assert derive({"repo": repo_mod.Repo(root)}) == []


# --- the ledger: an opened slice freezes --------------------------------------


def test_an_opened_slice_is_restored_from_the_ledger(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    first = derive(bundle)[0]
    git(bundle["root"], "branch", first.branch, first.head_sha)
    events = chained([ledger_event(first)])

    slices = derive(bundle, events)

    assert slices[0].opened is True
    assert slices[0].branch == first.branch
    assert [s.opened for s in slices[1:]] == [False, False]


def test_a_review_fix_on_an_opened_slice_stays_in_that_slice(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    first = derive(bundle)[0]
    git(bundle["root"], "branch", first.branch, first.head_sha)
    events = chained([ledger_event(first)])
    fix = commit_on(bundle["root"], first.branch, "fix the finding", path="src/T-001.py")

    slices = derive(bundle, events)

    # The branch moved; the slice moved with it, and no new slice was invented for the fix.
    assert slices[0].head_sha == fix
    assert slices[0].opened_at == first.head_sha
    assert fix in slices[0].commits
    assert [s.task_id for s in slices] == ["T-001", "T-002", "T-003"]


def test_new_work_after_an_opened_slice_becomes_a_new_slice(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle(("T-001", "T-002"))
    slices = derive(bundle)
    git(bundle["root"], "branch", slices[0].branch, slices[0].head_sha)
    git(bundle["root"], "branch", slices[1].branch, slices[1].head_sha)
    events = chained([ledger_event(slices[0]), ledger_event(slices[1])])

    # A third task lands after both pull requests are open.
    landed = land_task(bundle["root"], "T-003")
    repo = bundle["repo"]
    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    document = dict(state.raw)
    document["tasks"] = {**document["tasks"], "T-003": {"status": "done", "completed_commit": landed}}
    plan = store.read_plan()
    assert plan is not None
    plan_document = dict(plan.raw)
    plan_document["tasks"] = [*plan_document["tasks"], make_task("T-003", kind="parallel", claim_ids=["C-001"])]
    repo.plan.write_bytes(store_mod.dump_yaml(plan_document))
    repo.state.write_bytes(store_mod.dump_yaml(document))

    grown = derive(bundle, events)

    assert [s.task_id for s in grown] == ["T-001", "T-002", "T-003"]
    assert [s.index for s in grown] == [1, 2, 3]
    assert [s.opened for s in grown] == [True, True, False]
    assert grown[2].base_ref == grown[1].branch


def test_a_ready_record_does_not_duplicate_the_opened_one(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    first = derive(bundle)[0]
    opened = ledger_event(first)
    ready = event_chain.make(
        pr_stack.LEDGER_EVENT,
        DEMO_CYCLE,
        detail=pr_stack.opened_event_detail(first, "https://example.invalid/pr/1", pr_stack.LEDGER_READY),
    )

    records = pr_stack.ledger(chained([opened, ready]))

    assert len(records) == 1
    assert records[0].ready is True
    assert records[0].branch == first.branch


def test_unrelated_decision_events_are_not_read_as_slices(cycle: Callable[..., dict[str, Any]]) -> None:
    salvage = event_chain.make(
        pr_stack.LEDGER_EVENT, DEMO_CYCLE, detail={"branch_salvaged": "b -> b-salvage", "why": "restart"}
    )

    assert pr_stack.ledger(chained([salvage])) == []


# --- failing closed -----------------------------------------------------------


def test_a_missing_slice_branch_is_refused_rather_than_guessed(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    events = chained([ledger_event(derive(bundle)[0])])  # the branch itself was never created

    with pytest.raises(pr_stack.StackError, match="not in this repository"):
        derive(bundle, events)


def test_a_done_task_whose_commit_left_the_branch_is_refused(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    repo = bundle["repo"]
    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    document = dict(state.raw)
    document["tasks"] = {**document["tasks"], "T-002": {"status": "done", "completed_commit": "0" * 40}}
    repo.state.write_bytes(store_mod.dump_yaml(document))

    with pytest.raises(pr_stack.StackError, match="not on the work branch"):
        derive(bundle)


def test_landing_a_task_before_its_dependency_is_refused(cycle: Callable[..., dict[str, Any]]) -> None:
    # T-001 landed first, but the plan says it is blocked by T-002 — the history and the DAG disagree.
    bundle = cycle(("T-001", "T-002"), blocked_by={"T-001": ["T-002"]})

    with pytest.raises(pr_stack.StackError, match="topological order"):
        derive(bundle)


def test_an_unresolvable_base_commit_is_refused(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    repo = bundle["repo"]
    store = store_mod.Store(repo)
    plan = store.read_plan()
    assert plan is not None
    document = dict(plan.raw)
    document["cycle"] = {**document["cycle"], "base_commit": "0" * 40}
    repo.plan.write_bytes(store_mod.dump_yaml(document))

    with pytest.raises(pr_stack.StackError, match="no floor to stand on"):
        derive(bundle)


def test_a_work_branch_that_does_not_exist_is_refused(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    git(bundle["root"], "switch", "-q", "main")
    git(bundle["root"], "branch", "-D", WORK_BRANCH)

    with pytest.raises(pr_stack.StackError, match="does not resolve"):
        derive(bundle)


# --- preconditions ------------------------------------------------------------


def preconditions(bundle: dict[str, Any], mode: str, *, events: list[models.Event] | None = None) -> Any:
    repo = bundle["repo"]
    docs = documents(bundle, events)
    slices = pr_stack.derive(repo, docs, base="main")
    return pr_stack.preconditions(repo, docs, slices, mode=mode, base="main")


def test_push_is_allowed_before_gate_four(cycle: Callable[..., dict[str, Any]]) -> None:
    assert preconditions(cycle(), "push").ok


def test_ready_is_refused_while_gate_four_is_pending(cycle: Callable[..., dict[str, Any]]) -> None:
    result = preconditions(cycle(), "ready")

    assert not result.ok
    assert any("gate ④" in problem for problem in result.errors)


def test_restack_is_refused_once_gate_four_is_approved(cycle: Callable[..., dict[str, Any]]) -> None:
    result = preconditions(cycle(gates={"build": "approved"}), "restack")

    assert not result.ok
    assert any("rein revise" in problem for problem in result.errors)


def test_push_after_approval_warns_instead_of_dead_ending(cycle: Callable[..., dict[str, Any]]) -> None:
    """A stack first pushed after gate ④ opened must still be publishable.

    `--ready` refuses a slice with no pull request, so a `--push` that refused an approved gate
    would leave the cycle with no way out at all.
    """
    result = preconditions(cycle(gates={"build": "approved"}), "push")

    assert result.ok
    assert any("--ready" in warning for warning in result.warnings)


def test_ready_refuses_slices_that_were_never_pushed(cycle: Callable[..., dict[str, Any]]) -> None:
    result = preconditions(cycle(gates={"build": "approved"}), "ready")

    assert any("no pull request yet" in problem for problem in result.errors)


def test_ready_refuses_when_no_review_has_been_generated(cycle: Callable[..., dict[str, Any]]) -> None:
    result = preconditions(cycle(gates={"build": "approved"}), "ready")

    assert any("nothing binding these slices" in problem for problem in result.errors)


def test_ready_refuses_a_review_generated_against_another_head(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle(gates={"build": "approved"}, review=make_review(generated=True, head_sha="a" * 40))

    result = preconditions(bundle, "ready")

    assert any("Regenerate it" in problem for problem in result.errors)


def test_ready_passes_once_the_gate_the_review_and_the_pull_requests_all_agree(
    cycle: Callable[..., dict[str, Any]],
) -> None:
    bundle = cycle(gates={"build": "approved"})
    slices = derive(bundle)
    for s in slices:
        git(bundle["root"], "branch", s.branch, s.head_sha)
    head = git(bundle["root"], "rev-parse", WORK_BRANCH)
    bundle["repo"].review.write_bytes(store_mod.dump_yaml(make_review(generated=True, head_sha=head)))
    events = chained([ledger_event(s) for s in slices])

    result = preconditions(bundle, "ready", events=events)

    assert result.ok, result.errors


def test_an_audit_chain_defect_stops_every_mode(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    repo = bundle["repo"]
    docs = documents(bundle)
    slices = pr_stack.derive(repo, docs, base="main")
    damaged = replace(docs, defects=(event_chain.ChainDefect(2, "link", "prev digest does not match"),))

    result = pr_stack.preconditions(repo, damaged, slices, mode="push", base="main")

    assert not result.ok
    assert any("audit chain" in problem for problem in result.errors)


def test_an_unknown_mode_is_a_programming_error(cycle: Callable[..., dict[str, Any]]) -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        preconditions(cycle(), "publish")


# --- rendering ----------------------------------------------------------------


def test_render_names_every_slice_and_its_base(cycle: Callable[..., dict[str, Any]]) -> None:
    slices = derive(cycle())

    text = pr_stack.render(slices, base="main")

    assert "stack of 3 slice(s)" in text
    for s in slices:
        assert s.branch in text
        assert s.base_ref in text


def test_render_says_so_when_there_is_nothing_to_show() -> None:
    assert "nothing has landed" in pr_stack.render([])


# --- pull-request bodies ------------------------------------------------------


def body(bundle: dict[str, Any], current: int = 0, events: list[models.Event] | None = None) -> str:
    docs = documents(bundle, events)
    slices = pr_stack.derive(bundle["repo"], docs, base="main")
    return pr_stack.slice_body(bundle["repo"], docs, slices, current, base="main")


def test_a_draft_body_says_it_has_not_been_reviewed(cycle: Callable[..., dict[str, Any]]) -> None:
    text = body(cycle())

    assert "**Draft.**" in text
    assert "gate ④: pending" in text
    assert "Slice 1 of 3" in text


def test_a_draft_body_prints_no_cycle_digests(cycle: Callable[..., dict[str, Any]]) -> None:
    """An unapproved review's figures would dress a draft in a finished review's authority."""
    text = body(cycle())

    assert "### Cycle facts" not in text
    assert "Change digest" not in text


def test_an_approved_body_says_the_slice_was_not_reviewed_alone(cycle: Callable[..., dict[str, Any]]) -> None:
    head = "b" * 40
    bundle = cycle(gates={"build": "approved"}, review=make_review(generated=True, head_sha=head))

    text = body(bundle)

    assert "**This slice was not reviewed on its own.**" in text
    assert "once, over the whole stack" in text
    assert head[:12] in text
    assert "### Cycle facts" in text


def test_a_verdict_is_never_collapsed_into_one_word(cycle: Callable[..., dict[str, Any]]) -> None:
    review = make_review(generated=True, head_sha="c" * 40)
    review["machine"]["claims"] = [
        {
            "claim_id": "C-001",
            "verdict": "aligned",
            "integrity": {"status": "verified"},
            "semantic_support": {"status": "supported", "assessment_basis": "machine_assessed"},
            "conformance": {"status": "unknown"},
        }
    ]
    bundle = cycle(gates={"build": "approved"}, review=review)

    text = body(bundle)

    assert "**C-001** — C-001 holds" in text
    assert "integrity verified" in text
    assert "semantic support supported" in text
    assert "conformance unknown" in text


def test_a_draft_body_states_the_claim_without_a_verdict(cycle: Callable[..., dict[str, Any]]) -> None:
    text = body(cycle())

    assert "**C-001** — C-001 holds" in text
    assert "verdict" not in text


def test_a_claim_the_review_never_reached_is_reported_as_such(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle(gates={"build": "approved"}, review=make_review(generated=True, head_sha="d" * 40))

    text = body(bundle)

    assert "not reviewed" in text


def test_an_external_criterion_with_no_observation_says_awaiting_evidence(
    cycle: Callable[..., dict[str, Any]],
) -> None:
    bundle = cycle()
    _replace_task_acceptance(
        bundle,
        "T-001",
        [{"id": "A-1", "statement": "the operator sees the banner", "evidence": {"kind": "external"}}],
    )

    text = body(bundle)

    assert "**A-1** — the operator sees the banner · `external`" in text
    assert "**awaiting evidence**" in text


def test_a_recorded_observation_names_the_tree_it_binds(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    _replace_task_acceptance(
        bundle,
        "T-001",
        [{"id": "A-1", "statement": "the operator sees the banner", "evidence": {"kind": "external"}}],
    )
    _record_observation(bundle, "T-001", {"id": "A-1", "tree": "sha256:" + "e" * 64, "note": "seen on staging"})

    text = body(bundle)

    assert "recorded against tree `sha256:e" in text
    assert "seen on staging" in text


def test_a_task_with_no_acceptance_says_the_gate_is_the_criterion(cycle: Callable[..., dict[str, Any]]) -> None:
    assert "none declared — the criterion is the gate ④ review itself" in body(cycle())


def test_the_body_lists_the_commits_the_slice_carries(cycle: Callable[..., dict[str, Any]]) -> None:
    text = body(cycle())

    assert "### Commits (2)" in text
    assert "T-001: work" in text


def test_the_stack_table_marks_the_current_slice(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()

    first, second = body(bundle, 0), body(bundle, 1)

    assert f"| 01 | T-001 | `{WORK_BRANCH}-pr-01-T-001` | `main` | not pushed ← **this one** |" in first
    assert "← **this one**" in second.split("| 02 |")[1].split("\n")[0]
    for text in (first, second):
        assert text.count("← **this one**") == 1


def test_the_body_forbids_squash_and_says_why(cycle: Callable[..., dict[str, Any]]) -> None:
    text = body(cycle())

    assert "Never squash" in text
    assert "show this diff again" in text
    assert "--delete-branch" in text


def test_a_tail_slice_has_no_claim_to_answer(cycle: Callable[..., dict[str, Any]]) -> None:
    bundle = cycle()
    commit_on(bundle["root"], WORK_BRANCH, "docs at the gate", path="docs/10-requirements.md")

    text = body(bundle, 3)

    assert "commits outside any task" in text
    assert "belong to none" in text


def _replace_task_acceptance(bundle: dict[str, Any], task_id: str, acceptance: list[dict[str, Any]]) -> None:
    repo = bundle["repo"]
    plan = store_mod.Store(repo).read_plan()
    assert plan is not None
    document = dict(plan.raw)
    document["tasks"] = [{**t, "acceptance": acceptance} if t["id"] == task_id else t for t in document["tasks"]]
    repo.plan.write_bytes(store_mod.dump_yaml(document))


def _record_observation(bundle: dict[str, Any], task_id: str, entry: dict[str, Any]) -> None:
    repo = bundle["repo"]
    state = store_mod.Store(repo).read_state()
    assert state is not None
    document = dict(state.raw)
    document["tasks"] = {**document["tasks"], task_id: {**document["tasks"][task_id], "acceptance": [entry]}}
    repo.state.write_bytes(store_mod.dump_yaml(document))
