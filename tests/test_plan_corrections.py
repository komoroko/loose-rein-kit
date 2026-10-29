"""A correction to a frozen plan is priced by what it changes (CR-40, #93).

Every correction used to go through one door: roll back the mandate, redo design and tasks, pay an
adversarial review, re-approve the whole plan. The order the work runs in is not part of what the
mandate authorizes, so adding an edge needs nobody; a change to what is authorized still needs a
person, who is shown what changed rather than the plan again.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rein import approve, brief, build_loop, dag, models, revise, task_cmd
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, make_task, seed_repo


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
        {
            "what": "criterion",
            "id": "T-001/A-1",
            "change": "changed: statement",
            "before": "under 2s",
            "after": "under 3s",
        },
    ]


def _approved_with_documents(tmp_path: Path, design: str) -> repo_mod.Repo:
    """A mandate approved through `record_approval` over a committed design document."""
    seed_repo(
        tmp_path,
        plan=make_plan(tasks=_tasks()),
        state=make_state(gates={"mandate": "pending"}, plan_status="draft"),
        git=True,
    )
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "docs" / "20-design.md").write_text(design, encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "the mandate as approved")
    repo = repo_mod.Repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    return repo


def test_a_re_approval_is_shown_the_documents_that_changed_line_by_line(tmp_path: Path) -> None:
    """The plan's delta named fields; the prose beside it was rendered as it stands, so what was new
    since the last yes in the design was whatever the reader remembered (CR-55)."""
    design = "# Design\n\nThe store is the chain.\n\nRetries are bounded.\n"
    repo = _approved_with_documents(tmp_path, design)
    [approved] = [e for e in store_mod.Store(repo).read_events() if e.event == "gate_approved"]
    assert "docs/20-design.md" in approved.detail["sources"]
    revise.apply(repo, revise.plan_revision(repo, "mandate", []), "the store changes")
    (tmp_path / "docs" / "20-design.md").write_text(design.replace("the chain", "a table"), encoding="utf-8")

    [row] = approve.naming(repo, "mandate")["documents"]
    assert (row["id"], row["change"]) == ("docs/20-design.md", "changed")
    changed = [line for line in row["diff"].splitlines() if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert changed == ["-The store is the chain.", "+The store is a table."]
    printed = approve.render_documents([row])
    assert "-The store is the chain." in printed and "+The store is a table." in printed


def test_a_document_version_git_never_saw_is_said_so(tmp_path: Path) -> None:
    repo = _approved_with_documents(tmp_path, "# Design\n\nv1\n")
    (tmp_path / "docs" / "20-design.md").write_text("# Design\n\nv2, never committed\n", encoding="utf-8")
    _git(tmp_path, "commit", "-qam", "v2")
    raw = [e for e in store_mod.Store(repo).read_events() if e.event == "gate_approved"][-1]
    assert raw.detail["sources"]["docs/20-design.md"]
    # The approved bytes are in history; bytes that never were cannot be diffed against.
    with store_mod.Store(repo).transaction() as tx:
        tx.append(
            "gate_approved",
            cycle_id="demo-cycle",
            subject_ids=["mandate", "GA-MANDATE-2"],
            detail={"plan_digest": raw.detail["plan_digest"], "sources": {"docs/20-design.md": "sha256:" + "e" * 64}},
        )
    [row] = approve.naming(repo, "mandate")["documents"]
    assert "not in git's history" in row["change"] and row["diff"] == ""


def test_an_approval_that_recorded_no_documents_says_what_it_cannot_show(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks())
    with store_mod.Store(repo).transaction() as tx:
        tx.append(
            "gate_approved",
            cycle_id="demo-cycle",
            subject_ids=["mandate", "GA-MANDATE-1"],
            detail={"plan_digest": "sha256:" + "f" * 64},
        )
    [row] = approve.naming(repo, "mandate")["documents"]
    assert "recorded no document digests" in row["change"]


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
    assert approve.naming(repo, "mandate")["documents"] == []
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


# --- widening a frozen task's scope where the config allowed it ahead of time -----------------


def _scoped_repo(tmp_path: Path, allowed: list[str] | None, statuses: dict[str, object] | None = None) -> repo_mod.Repo:
    config = make_config()
    if allowed is not None:
        config["guard"]["scope_additions"] = allowed
    tasks = [
        make_task("T-001", claim_ids=["C-001"], scope_include=["src/prompts.py", "tests/test_prompts.py"]),
        make_task("T-002", kind="parallel", claim_ids=["C-001"], scope_include=["tests/test_cli.py"]),
    ]
    state = make_state(plan_status="frozen")
    if statuses:
        state["tasks"] = statuses
    seed_repo(tmp_path, config=config, plan=make_plan(tasks=tasks), state=state)
    return repo_mod.Repo(tmp_path)


def test_a_test_the_change_drags_along_is_added_without_a_roll_back(tmp_path: Path) -> None:
    """The field case: a task changed a constant and a test elsewhere pinned the old value."""
    repo = _scoped_repo(tmp_path, ["tests/"])
    plan_before = (repo.root / ".rein" / "plan.yaml").read_bytes()

    task_cmd.scope_add(repo, "T-001", path="tests/ports/test_protocols.py", reason="asserts the old model id")

    assert (repo.root / ".rein" / "plan.yaml").read_bytes() == plan_before
    assert "tests/ports/test_protocols.py" in dag.load(repo).get("T-001").scope_include
    recorded = store_mod.Store(repo).read_events()[-1]
    assert recorded.detail["kind"] == "scope_added" and recorded.detail["reason"] == "asserts the old model id"
    residuals = brief.derive(plan=None, state=store_mod.Store(repo).read_state(), config=None)["residuals"]
    assert residuals["scope_added_after_mandate"] == [{"task_id": "T-001", "paths": ["tests/ports/test_protocols.py"]}]


def test_nothing_is_added_where_the_config_allows_nothing(tmp_path: Path) -> None:
    repo = _scoped_repo(tmp_path, None)
    with pytest.raises(ValueError, match="none configured"):
        task_cmd.scope_add(repo, "T-001", path="tests/x.py", reason="r")


def test_product_code_outside_the_allowance_still_needs_a_roll_back(tmp_path: Path) -> None:
    repo = _scoped_repo(tmp_path, ["tests/"])
    with pytest.raises(ValueError, match="rein revise --to mandate"):
        task_cmd.scope_add(repo, "T-001", path="src/other.py", reason="r")


def test_another_tasks_path_is_that_tasks_work(tmp_path: Path) -> None:
    repo = _scoped_repo(tmp_path, ["tests/"])
    with pytest.raises(ValueError, match="scope of T-002"):
        task_cmd.scope_add(repo, "T-001", path="tests/test_cli.py", reason="r")


def test_a_finished_task_is_not_widened(tmp_path: Path) -> None:
    repo = _scoped_repo(tmp_path, ["tests/"], {"T-001": {"status": "done"}})
    with pytest.raises(ValueError, match="landed against"):
        task_cmd.scope_add(repo, "T-001", path="tests/x.py", reason="r")


def test_a_path_the_task_excludes_is_not_added(tmp_path: Path) -> None:
    """`exclude` wins over `include`: the addition was recorded and listed while the guard went on
    refusing the path."""
    config = make_config()
    config["guard"]["scope_additions"] = ["tests/"]
    task = make_task("T-001", claim_ids=["C-001"], scope_include=["src/prompts.py"])
    task["scope"]["exclude"] = ["tests/golden/"]
    seed_repo(tmp_path, config=config, plan=make_plan(tasks=[task]), state=make_state(plan_status="frozen"))
    with pytest.raises(ValueError, match="own `scope.exclude`"):
        task_cmd.scope_add(repo_mod.Repo(tmp_path), "T-001", path="tests/golden/case.json", reason="r")


def test_a_roll_back_hands_added_scope_back_to_the_planner(tmp_path: Path) -> None:
    repo = _scoped_repo(tmp_path, ["tests/"])
    task_cmd.scope_add(repo, "T-001", path="tests/x.py", reason="r")
    revision = revise.plan_revision(repo, "mandate", [])
    assert revision["returned_scope"] == ["T-001: tests/x.py"]
    revise.apply(repo, revision, "re-cut")
    state = store_mod.Store(repo).read_state()
    assert state is not None and state.task_scope_added == {}


# --- keeping a done task a roll back marked (`rein task keep`, #112) ---------------------------


def _done_then_marked(tmp_path: Path, *, extra: tuple[str, ...] = ("docs/shots/01.png",)) -> repo_mod.Repo:
    """A task done under an approved mandate, then marked by `rein revise --to mandate --impacted`.

    Its criterion names `docs/run.md`; `extra` are files committed beside it that the criterion
    does not name yet — the screenshots #106 left unread.
    """
    criterion = {
        "id": "A-1",
        "statement": "the run is recorded",
        "evidence": {"kind": "artifact", "paths": ["docs/run.md"]},
    }
    task = make_task("T-001", claim_ids=["C-001"], acceptance=[criterion], scope_include=["docs/"])
    seed_repo(
        tmp_path,
        plan=make_plan(tasks=[task]),
        state=make_state(gates={"mandate": "pending"}, plan_status="draft"),
        git=True,
    )
    for rel in ("docs/run.md", *extra):
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes(b"recorded")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "the mandate as approved, and the task's work")
    repo = repo_mod.Repo(tmp_path)
    approve.record_approval(repo, "mandate", approve.approval_subject(repo, "mandate"))
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, capture_output=True, text=True)
    build_loop.set_task_status(repo, "T-001", "done", commit=head.stdout.strip())
    revise.apply(repo, revise.plan_revision(repo, "mandate", ["T-001"]), "name the screenshots")
    return repo


def _edit_task(tmp_path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    path = tmp_path / ".rein" / "plan.yaml"
    raw = store_mod.Store(repo_mod.Repo(tmp_path)).read_raw("plan")
    assert raw is not None
    edit(raw["issues"][0]["sub_issues"][0])
    path.write_bytes(store_mod.dump_yaml(raw))


def _name_the_screenshot(task: dict[str, Any]) -> None:
    task["acceptance"][0]["evidence"]["paths"].append("docs/shots/01.png")


def test_a_done_task_whose_criterion_only_gained_paths_is_kept(tmp_path: Path) -> None:
    """The case #112 had no exit for: reset cannot reach `done`, and a re-run changes nothing."""
    repo = _done_then_marked(tmp_path)
    marked = store_mod.Store(repo).read_raw("state")["tasks"]["T-001"]  # type: ignore[index]
    assert marked["status"] == "needs-revision"
    _edit_task(tmp_path, _name_the_screenshot)

    task_cmd.keep(repo, "T-001", reason="only the screenshots are named; the work is what it was")

    kept = store_mod.Store(repo).read_raw("state")["tasks"]["T-001"]  # type: ignore[index]
    assert kept == {**marked, "status": "done"}
    recorded = store_mod.Store(repo).read_events()[-1]
    assert recorded.event == "decision_declared" and recorded.subject_ids == ("T-001",)
    assert recorded.detail == {
        "kind": "task_kept",
        "from": "needs-revision",
        "to": "done",
        "commit": marked["completed_commit"],
        "reason": "only the screenshots are named; the work is what it was",
    }


def test_a_criterion_changed_beyond_its_artifact_paths_is_not_a_keep(tmp_path: Path) -> None:
    repo = _done_then_marked(tmp_path)

    def tighten(task: dict[str, Any]) -> None:
        _name_the_screenshot(task)
        task["acceptance"][0]["statement"] = "the run is recorded, twice"

    _edit_task(tmp_path, tighten)
    with pytest.raises(ValueError, match="acceptance"):
        task_cmd.keep(repo, "T-001", reason="keep")
    assert store_mod.Store(repo).read_raw("state")["tasks"]["T-001"]["status"] == "needs-revision"  # type: ignore[index]


def test_a_scope_change_is_not_a_keep(tmp_path: Path) -> None:
    repo = _done_then_marked(tmp_path)
    _edit_task(tmp_path, lambda task: task["scope"]["include"].append("src/"))
    with pytest.raises(ValueError, match="scope"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_a_path_taken_off_an_artifact_criterion_is_not_a_keep(tmp_path: Path) -> None:
    """Only added paths are checked here; a path taken away loosens what the task was held to."""
    repo = _done_then_marked(tmp_path)

    def swap(task: dict[str, Any]) -> None:
        task["acceptance"][0]["evidence"]["paths"] = ["docs/shots/01.png"]

    _edit_task(tmp_path, swap)
    with pytest.raises(ValueError, match="docs/run.md"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_an_artifact_path_head_does_not_have_is_not_a_keep(tmp_path: Path) -> None:
    repo = _done_then_marked(tmp_path, extra=())
    _edit_task(tmp_path, _name_the_screenshot)
    with pytest.raises(ValueError, match="docs/shots/01.png"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_a_task_that_was_not_done_when_marked_is_sent_to_reset(tmp_path: Path) -> None:
    repo = _repo(tmp_path, _tasks(), {"T-001": {"status": "todo"}})
    revise.apply(repo, revise.plan_revision(repo, "mandate", ["T-001"]), "re-cut")
    with pytest.raises(ValueError, match="rein task reset"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_a_task_the_build_sent_back_is_not_kept(tmp_path: Path) -> None:
    """A build's needs-revision drops `completed_commit`; only a roll-back mark keeps it."""
    repo = _repo(tmp_path, _tasks())
    build_loop.set_task_status(repo, "T-001", "done", commit="a" * 40)
    build_loop.set_task_status(repo, "T-001", "needs-revision")
    with pytest.raises(ValueError, match="rein task reset"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_a_completed_commit_head_does_not_contain_is_not_a_keep(tmp_path: Path) -> None:
    repo = _done_then_marked(tmp_path)
    _edit_task(tmp_path, _name_the_screenshot)
    _git(tmp_path, "checkout", "-q", "--orphan", "elsewhere")
    _git(tmp_path, "commit", "-q", "-m", "a history without the task's commit")
    with pytest.raises(ValueError, match="not in HEAD's history"):
        task_cmd.keep(repo, "T-001", reason="keep")


def test_keep_is_reachable_from_the_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _done_then_marked(tmp_path)
    _edit_task(tmp_path, _name_the_screenshot)
    assert task_cmd.main(["keep", "T-001", "--reason", "paths only", "--repo", str(tmp_path)]) == 0
    assert "T-001: needs-revision → done" in capsys.readouterr().out
    assert task_cmd.main(["keep", "T-001", "--reason", " ", "--repo", str(tmp_path)]) == 2


# --- an attempt budget is raised at the crossing it prices (CR-78, #118) ---------------------------


def _crossing_repo(
    tmp_path: Path, *, used: int = 2, plan_status: str = "frozen", status: str = "blocked"
) -> repo_mod.Repo:
    crossing = make_task(
        "T-001",
        claim_ids=["C-001"],
        operator_surface=[
            {"kind": "persistence", "name": "the users table", "paths": ["db/x.sql"], "reversible": False}
        ],
    )
    crossing["attempts"] = {"max": 2, "cost": "a day of quota"}
    state = make_state(
        plan_status=plan_status, gates={"mandate": "approved", "T-001": "approved", "acceptance": "pending"}
    )
    state["tasks"] = {"T-001": {"status": status, "attempts": used}}
    seed_repo(tmp_path, plan=make_plan(tasks=[crossing]), state=state)
    return repo_mod.Repo(tmp_path)


def _at_a_terminal(monkeypatch: pytest.MonkeyPatch, answer: bool = True) -> None:
    from rein import common

    monkeypatch.setattr(common, "stdin_is_terminal", lambda: True)
    monkeypatch.setattr(common, "ask_yes_no", lambda prompt: answer)


def test_a_budget_is_raised_without_touching_the_plan_or_any_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#118: every raise was a roll back to the mandate — nine of them for one task."""
    repo = _crossing_repo(tmp_path)
    store = store_mod.Store(repo)
    plan_before = repo.plan.read_bytes()
    gates_before = store.read_state().raw["gates"]  # type: ignore[union-attr]
    _at_a_terminal(monkeypatch)

    task_cmd.attempts(repo, "T-001", max_runs=4, reason="")

    assert repo.plan.read_bytes() == plan_before
    assert store.read_state().raw["gates"] == gates_before  # type: ignore[union-attr]
    assert dag.load(repo).get("T-001").attempt_max == 4
    recorded = store.read_events()[-1]
    assert recorded.event == "decision_declared"
    assert recorded.detail == {"kind": "attempts_raised", "from": 2, "to": 4, "used": 2}
    residuals = brief.derive(plan=None, state=store.read_state(), config=None)["residuals"]
    assert residuals["attempts_raised_after_mandate"] == [{"task_id": "T-001", "max": 4}]


def test_a_raise_needs_a_terminal_and_a_yes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It spends what a person priced, so an agent's captured subprocess cannot do it."""
    from rein import common

    repo = _crossing_repo(tmp_path)
    monkeypatch.setattr(common, "stdin_is_terminal", lambda: False)
    with pytest.raises(ValueError, match="needs a confirmation typed at a terminal"):
        task_cmd.attempts(repo, "T-001", max_runs=4, reason="")
    _at_a_terminal(monkeypatch, answer=False)
    with pytest.raises(ValueError, match="nothing was raised"):
        task_cmd.attempts(repo, "T-001", max_runs=4, reason="")
    assert dag.load(repo).get("T-001").attempt_max == 2


def test_a_raise_is_refused_where_it_is_not_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _at_a_terminal(monkeypatch)
    repo = _crossing_repo(tmp_path)
    with pytest.raises(ValueError, match="has to exceed it"):
        task_cmd.attempts(repo, "T-001", max_runs=2, reason="")
    with pytest.raises(ValueError, match="at most 20 runs"):
        task_cmd.attempts(repo, "T-001", max_runs=21, reason="")
    draft = _crossing_repo(tmp_path / "draft", plan_status="draft")
    with pytest.raises(ValueError, match="the plan is a draft"):
        task_cmd.attempts(draft, "T-001", max_runs=4, reason="")
    plain = _repo(tmp_path / "plain", _tasks())
    with pytest.raises(ValueError, match="crosses no irreversible point"):
        task_cmd.attempts(plain, "T-001", max_runs=4, reason="")
    done = _crossing_repo(tmp_path / "done", status="done")
    with pytest.raises(ValueError, match="no run is left"):
        task_cmd.attempts(done, "T-001", max_runs=4, reason="")


def test_a_raise_over_a_task_the_spent_budget_blocked_says_it_still_needs_a_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The loop blocks a task whose run budget ran out; the raise alone leaves it blocked."""
    _at_a_terminal(monkeypatch)
    _crossing_repo(tmp_path)
    assert task_cmd.main(["attempts", "T-001", "--max", "4", "--repo", str(tmp_path)]) == 0
    assert "rein task reset T-001" in capsys.readouterr().out


def test_a_raised_budget_returns_to_the_plan_on_a_roll_back_to_the_mandate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _crossing_repo(tmp_path)
    _at_a_terminal(monkeypatch)
    task_cmd.attempts(repo, "T-001", max_runs=4, reason="the quota stop was ours")

    revision = revise.plan_revision(repo, "mandate", [])
    assert revision["returned_attempts"] == ["T-001: 4"]
    revise.apply(repo, revision, "re-cut")

    state = store_mod.Store(repo).read_state()
    assert state is not None and "attempts_max" not in state.raw["tasks"]["T-001"]
