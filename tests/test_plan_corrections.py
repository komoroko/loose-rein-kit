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
