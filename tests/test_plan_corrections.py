"""A correction to a frozen plan is priced by what it changes (CR-40, #93).

Every correction used to go through one door: roll back the mandate, redo design and tasks, pay an
adversarial review, re-approve the whole plan. The order the work runs in is not part of what the
mandate authorizes, so adding an edge needs nobody; a change to what is authorized still needs a
person, who is shown what changed rather than the plan again.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rein import approve, brief, dag, models, revise, task_cmd
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_plan, make_state, make_task, seed_repo


def _repo(tmp_path: Path, planned: list[dict[str, object]], statuses: dict[str, object] | None = None) -> repo_mod.Repo:
    state = make_state(plan_status="frozen")
    if statuses:
        state["tasks"] = statuses
    seed_repo(tmp_path, plan=make_plan(tasks=planned), state=state)
    return repo_mod.Repo(tmp_path)


def _tasks() -> list[dict[str, object]]:
    return [make_task("T-001", claim_ids=["C-001"]), make_task("T-002", kind="parallel", claim_ids=["C-001"])]


def test_an_edge_is_added_without_touching_the_plan_or_its_approval(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks())
    store = store_mod.Store(repo)
    plan_before = (repo.root / ".rein" / "plan.yaml").read_bytes()
    gates_before = store.read_state().raw["gates"]  # type: ignore[union-attr]

    task_cmd.order(repo, "T-002", after="T-001", reason="T-002 imports what T-001 creates")

    assert (repo.root / ".rein" / "plan.yaml").read_bytes() == plan_before
    state = store.read_state()
    assert state is not None and state.raw["gates"] == gates_before
    assert dag.load(repo).get("T-002").blocked_by == ("T-001",)
    assert [t.id for t in dag.load(repo).frontier()] == ["T-001"]
    recorded = store.read_events()[-1]
    assert recorded.event == "decision_declared" and recorded.detail["kind"] == "edge_added"
    assert recorded.detail["reason"] == "T-002 imports what T-001 creates"


def test_the_edge_is_shown_at_acceptance(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks())
    task_cmd.order(repo, "T-002", after="T-001", reason="order")
    residuals = brief.derive(plan=None, state=store_mod.Store(repo).read_state(), config=None)["residuals"]
    assert residuals["ordered_after_mandate"] == [{"task_id": "T-002", "after": ["T-001"]}]


def test_an_edge_that_closes_a_cycle_is_refused(tmp_path: Path) -> None:
    tasks = _tasks()
    tasks[1]["blocked_by"] = ["T-001"]
    repo = _repo(tmp_path, tasks)
    with pytest.raises(ValueError, match="cyclic"):
        task_cmd.order(repo, "T-001", after="T-002", reason="backwards")


def test_an_edge_in_front_of_work_that_ran_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks(), {"T-002": {"status": "done"}})
    with pytest.raises(ValueError, match="orders nothing"):
        task_cmd.order(repo, "T-002", after="T-001", reason="late")


def test_a_roll_back_keeps_the_work_it_did_not_touch(tmp_path: Path) -> None:
    """The scoped half: rolling the mandate back for T-002 leaves T-001 done with its evidence."""
    evidence = {"tree": "sha256:" + "a" * 64, "steps": [], "updated_at": "2026-09-25T00:00:00+00:00"}
    repo = _repo(
        tmp_path,
        _tasks(),
        {"T-001": {"status": "done", "evidence": evidence}, "T-002": {"status": "blocked"}},
    )
    revision = revise.plan_revision(repo, "mandate", ["T-002"])
    revise.apply(repo, revision, "T-002's criterion was wrong")

    tasks = store_mod.Store(repo).read_raw("state")["tasks"]  # type: ignore[index]
    assert tasks["T-001"]["status"] == "done" and tasks["T-001"]["evidence"] == evidence
    assert tasks["T-002"]["status"] == "needs-revision"


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@e.x", "-c", "user.name=T", *args], cwd=root, check=True, capture_output=True
    )


def test_a_re_approval_is_shown_what_changed_since_the_last_one(tmp_path: Path) -> None:
    before = [
        make_task("T-001", claim_ids=["C-001"], acceptance=[{"id": "A-1", "statement": "under 2s"}]),
        make_task("T-002", kind="parallel", claim_ids=["C-001"]),
    ]
    seed_repo(tmp_path, plan=make_plan(tasks=before), state=make_state(plan_status="draft"), git=True)
    repo = repo_mod.Repo(tmp_path)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "the plan as approved")
    store = store_mod.Store(repo)
    approved = store.read_plan()
    assert approved is not None
    with store.transaction() as tx:
        tx.append(
            "gate_approved",
            cycle_id="demo-cycle",
            subject_ids=["mandate", "GA-MANDATE-1"],
            detail={"plan_digest": approved.digest()},
        )

    after = [
        make_task("T-001", claim_ids=["C-001"], acceptance=[{"id": "A-1", "statement": "under 3s"}]),
        make_task("T-003", kind="parallel", claim_ids=["C-001"]),
    ]
    (tmp_path / ".rein" / "plan.yaml").write_bytes(store_mod.dump_yaml(make_plan(tasks=after)))

    assert approve.naming(repo, "mandate")["delta"] == [
        {"what": "task", "id": "T-002", "change": "removed"},
        {"what": "task", "id": "T-003", "change": "added"},
        {"what": "criterion", "id": "T-001/A-1", "change": "changed: statement"},
    ]


def test_a_last_approved_plan_git_never_saw_is_said_so(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks())
    store = store_mod.Store(repo)
    with store.transaction() as tx:
        tx.append(
            "gate_approved",
            cycle_id="demo-cycle",
            subject_ids=["mandate", "GA-MANDATE-1"],
            detail={"plan_digest": "sha256:" + "f" * 64},
        )
    [row] = approve.naming(repo, "mandate")["delta"]
    assert "not in git's history" in row["change"]


def test_a_first_approval_has_no_delta(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks())
    assert approve.naming(repo, "mandate")["delta"] == []
    assert models.Plan(make_plan(tasks=_tasks())).digest()


def test_a_mandate_roll_back_hands_the_added_order_back_to_the_planner(tmp_path: Path) -> None:
    """An edge added beside a frozen plan named tasks of that plan. Left in place across a re-cut that
    drops one of them, it made the graph unreadable, and no verb could remove it."""
    repo = _repo(tmp_path, _tasks())
    task_cmd.order(repo, "T-002", after="T-001", reason="order")
    revision = revise.plan_revision(repo, "mandate", [])
    assert revision["returned_order"] == ["T-002 after T-001"]
    assert "T-002 after T-001" in revise.render(revision)
    revise.apply(repo, revision, "re-cut")

    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and "after" not in raw["tasks"]["T-002"]
    assert store_mod.Store(repo).read_events()[-2].detail["returned_order"] == ["T-002 after T-001"]
    (tmp_path / ".rein" / "plan.yaml").write_bytes(
        store_mod.dump_yaml(make_plan(tasks=[make_task("T-002", claim_ids=["C-001"])]))
    )
    raw["tasks"].pop("T-001", None)
    (tmp_path / ".rein" / "state.yaml").write_bytes(store_mod.dump_yaml(raw))
    assert [t.id for t in dag.load(repo).tasks] == ["T-002"]


# --- taking a task out of the cycle (`rein task defer`) ------------------------------


def _draft_repo_without(tmp_path: Path, removed: str, statuses: dict[str, object]) -> repo_mod.Repo:
    """A rolled-back cycle whose human deleted `removed` from the draft plan; its status is still there."""
    state = make_state(gates={"mandate": "pending"}, plan_status="draft")
    state["tasks"] = statuses
    planned = [t for t in _tasks() if t["id"] != removed]
    seed_repo(tmp_path, plan=make_plan(tasks=planned), state=state)
    return repo_mod.Repo(tmp_path)


def test_a_task_deleted_from_the_draft_plan_leaves_a_state_no_graph_reader_accepts(tmp_path: Path) -> None:
    """The failure `defer` exists for: nothing could remove the status, and hand-editing is denied."""
    repo = _draft_repo_without(tmp_path, "T-002", {"T-001": {"status": "done"}, "T-002": {"status": "blocked"}})
    with pytest.raises(dag.DagError, match="rein task defer"):
        dag.load(repo)


def test_defer_moves_the_status_aside_and_records_why(tmp_path: Path) -> None:
    repo = _draft_repo_without(tmp_path, "T-002", {"T-001": {"status": "done"}, "T-002": {"status": "blocked"}})

    task_cmd.defer(repo, "T-002", reason="needs a fix upstream; next cycle")

    assert [t.id for t in dag.load(repo).tasks] == ["T-001"]
    state = store_mod.Store(repo).read_state()
    assert state is not None and "T-002" not in state.task_status
    assert state.deferred["T-002"]["status"] == "blocked"
    assert state.deferred["T-002"]["reason"] == "needs a fix upstream; next cycle"
    recorded = store_mod.Store(repo).read_events()[-1]
    assert recorded.event == "decision_declared" and recorded.detail["kind"] == "task_deferred"


def test_the_deferred_task_is_shown_at_acceptance(tmp_path: Path) -> None:
    repo = _draft_repo_without(tmp_path, "T-002", {"T-001": {"status": "done"}, "T-002": {"status": "blocked"}})
    task_cmd.defer(repo, "T-002", reason="next cycle")
    residuals = brief.derive(plan=None, state=store_mod.Store(repo).read_state(), config=None)["residuals"]
    assert residuals["deferred"] == [{"task_id": "T-002", "status": "blocked", "reason": "next cycle"}]


def test_defer_drops_an_order_edge_that_named_the_deferred_task(tmp_path: Path) -> None:
    repo = _draft_repo_without(
        tmp_path, "T-001", {"T-001": {"status": "blocked"}, "T-002": {"status": "todo", "after": ["T-001"]}}
    )
    task_cmd.defer(repo, "T-001", reason="next cycle")
    assert dag.load(repo).get("T-002").blocked_by == ()
    assert store_mod.Store(repo).read_events()[-1].detail["edges_dropped"] == ["T-002"]


def test_defer_refuses_a_frozen_plan(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks(), {"T-002": {"status": "blocked"}})
    with pytest.raises(ValueError, match="frozen"):
        task_cmd.defer(repo, "T-002", reason="no")


def test_defer_refuses_a_task_the_plan_still_declares(tmp_path: Path) -> None:
    """This verb never edits the plan: the deletion is the human's, and it comes first."""
    state = make_state(gates={"mandate": "pending"}, plan_status="draft")
    state["tasks"] = {"T-002": {"status": "blocked"}}
    seed_repo(tmp_path, plan=make_plan(tasks=_tasks()), state=state)
    with pytest.raises(ValueError, match="still declared"):
        task_cmd.defer(repo_mod.Repo(tmp_path), "T-002", reason="no")


def test_defer_refuses_a_task_with_no_status(tmp_path: Path) -> None:
    repo = _draft_repo_without(tmp_path, "T-002", {"T-001": {"status": "done"}})
    with pytest.raises(ValueError, match="nothing to defer"):
        task_cmd.defer(repo, "T-002", reason="no")
