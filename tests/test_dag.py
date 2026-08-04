"""Tests for dag.py / dag_render.py / dag_trace.py — scheduling and the traceability thread.

The scheduling assertions are all about **determinism**: the same plan must always produce
the same layers, the same critical path, and the same consumption order. That is what lets a
human predict `/build` instead of interviewing it, so a test here failing means the loop
became something you have to watch rather than something you can reason about.

The traceability section covers the thread `rein dag --trace` walks: a requirement is
*declared* by a heading in `docs/10-requirements.md`, and *covered* by a claim in the plan that
cites it. Neither side alone is a pass — that is the whole point of the check.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import dag, dag_render, dag_trace, models
from rein import repo as repo_mod
from tests._support import make_claim, make_plan, make_state, make_task, seed_repo


def graph_of(*specs: tuple[str, str, list[str]]) -> dag.Graph:
    """A graph from (id, kind, blocked_by) triples — every task todo."""
    return dag.Graph.from_tasks(
        [dag.Task(id=tid, title=tid, kind=kind, blocked_by=tuple(blocked)) for tid, kind, blocked in specs]
    )


# --- validation ---------------------------------------------------------------


def test_duplicate_id_is_refused() -> None:
    with pytest.raises(dag.DagError, match="duplicate task ID"):
        graph_of(("T-001", "foundation", []), ("T-001", "parallel", []))


def test_unknown_dependency_is_refused() -> None:
    with pytest.raises(dag.DagError, match="unknown dependency 'T-999'"):
        graph_of(("T-001", "parallel", ["T-999"]))


def test_self_dependency_is_refused() -> None:
    with pytest.raises(dag.DagError, match="depends on itself"):
        graph_of(("T-001", "parallel", ["T-001"]))


def test_a_cycle_is_refused() -> None:
    with pytest.raises(dag.DagError, match="cycle"):
        graph_of(("T-001", "parallel", ["T-002"]), ("T-002", "parallel", ["T-001"]))


@pytest.mark.parametrize(
    ("field", "bad"),
    [("kind", "whatever"), ("status", "nearly-done"), ("risk", "spicy")],
)
def test_off_vocabulary_values_are_refused(field: str, bad: str) -> None:
    fields: dict[str, str] = {"kind": "parallel", "status": "todo", "risk": "low", field: bad}
    task = dag.Task(id="T-001", title="t", **fields)  # type: ignore[arg-type]
    with pytest.raises(dag.DagError, match=f"invalid {field}"):
        dag.Graph.from_tasks([task])


# --- derivation ---------------------------------------------------------------


def test_layers_and_critical_path_are_deterministic() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-001"]),
        ("T-004", "integration", ["T-002", "T-003"]),
    )
    assert graph.layers() == [["T-001"], ["T-002", "T-003"], ["T-004"]]
    assert graph.critical_path() == ["T-001", "T-002", "T-004"]
    # Same graph, tasks declared in a different order: the schedule must not move.
    shuffled = graph_of(
        ("T-004", "integration", ["T-002", "T-003"]),
        ("T-003", "parallel", ["T-001"]),
        ("T-002", "parallel", ["T-001"]),
        ("T-001", "foundation", []),
    )
    assert shuffled.layers() == graph.layers()
    assert shuffled.critical_path() == graph.critical_path()


def test_fan_out_counts_direct_dependents() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-001"]),
    )
    assert graph.fan_out() == {"T-001": 2, "T-002": 0, "T-003": 0}


def test_frontier_holds_only_startable_todo_tasks() -> None:
    tasks = [
        dag.Task(id="T-001", title="a", kind="foundation", status="done"),
        dag.Task(id="T-002", title="b", kind="parallel", blocked_by=("T-001",)),
        dag.Task(id="T-003", title="c", kind="parallel", blocked_by=("T-002",)),
        dag.Task(id="T-004", title="d", kind="parallel", status="blocked"),
    ]
    assert [t.id for t in dag.Graph.from_tasks(tasks).frontier()] == ["T-002"]


def test_order_frontier_puts_foundation_first_then_fan_out() -> None:
    tasks = [
        dag.Task(id="T-001", title="lonely", kind="parallel"),
        dag.Task(id="T-002", title="hub", kind="parallel"),
        dag.Task(id="T-003", title="base", kind="foundation"),
        dag.Task(id="T-004", title="child", kind="parallel", blocked_by=("T-002",)),
        dag.Task(id="T-005", title="child2", kind="parallel", blocked_by=("T-002",)),
    ]
    ordered = [t.id for t in dag.Graph.from_tasks(tasks).order_frontier()]
    assert ordered[0] == "T-003"  # foundation is finalized before anything forks off it
    assert ordered[1] == "T-002"  # then the highest fan-out


def test_dependents_closure_excludes_the_seeds() -> None:
    graph = graph_of(
        ("T-001", "foundation", []),
        ("T-002", "parallel", ["T-001"]),
        ("T-003", "parallel", ["T-002"]),
        ("T-004", "parallel", []),
    )
    assert graph.dependents_closure(["T-001"]) == {"T-002", "T-003"}
    assert graph.dependents_closure(["T-999"]) == set()


def test_counts_cover_every_status_in_vocabulary_order() -> None:
    graph = graph_of(("T-001", "foundation", []))
    assert list(graph.counts()) == list(models.TASK_STATUS_ORDER)


# --- joining plan structure with state status ---------------------------------


def test_join_takes_structure_from_the_plan_and_status_from_the_state() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001")],
            tasks=[make_task("T-001", claim_ids=["C-001"]), make_task("T-002", kind="parallel", blocked_by=["T-001"])],
        )
    )
    state = models.State(make_state(tasks={"T-001": "done"}))
    graph = dag.join(plan, state)
    assert graph.get("T-001").status == "done"
    assert graph.get("T-002").status == "todo"  # absent from state = not started
    assert graph.get("T-001").claim_ids == ("C-001",)


def test_status_for_a_task_the_plan_does_not_declare_is_an_error() -> None:
    """The plan was rewound and the state did not follow. Scheduling against that mismatch
    would run work nobody approved."""
    plan = models.Plan(make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]))
    state = models.State(make_state(tasks={"T-001": "done", "T-777": "done"}))
    with pytest.raises(dag.DagError, match="T-777"):
        dag.join(plan, state)


def test_join_without_a_state_treats_everything_as_todo() -> None:
    plan = models.Plan(make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]))
    assert dag.join(plan, None).get("T-001").status == "todo"


def test_claims_without_a_task_are_reported() -> None:
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001"), make_claim("C-002")],
            tasks=[make_task("T-001", claim_ids=["C-001"])],
        )
    )
    assert dag.join(plan, None).claims_without_a_task(plan) == ["C-002"]


def test_load_reads_the_repo(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert [t.id for t in dag.load(repo_mod.Repo(tmp_path)).tasks] == ["T-001"]


def test_load_without_a_plan_says_so(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None)
    with pytest.raises(dag.DagError, match="no plan"):
        dag.load(repo_mod.Repo(tmp_path))


# --- rendering ----------------------------------------------------------------


def test_render_shows_claims_and_risk_not_a_free_text_req() -> None:
    graph = dag.Graph.from_tasks(
        [dag.Task(id="T-001", title="base", kind="foundation", risk="critical", claim_ids=("C-002",))]
    )
    rendered = dag_render.render(graph)
    assert "C-002" in rendered and "critical" in rendered
    assert "req" not in rendered  # no free-text requirement column


def test_mermaid_is_deterministic_and_fenced() -> None:
    graph = graph_of(("T-001", "foundation", []), ("T-002", "parallel", ["T-001"]))
    first = dag_render.mermaid(graph)
    assert first.startswith("```mermaid") and first.rstrip().endswith("```")
    assert "T_001 --> T_002" in first
    assert dag_render.mermaid(graph) == first


def test_mermaid_of_an_empty_graph_says_so() -> None:
    assert "(no tasks)" in dag_render.mermaid(dag.Graph.from_tasks([]))


# --- the traceability thread --------------------------------------------------

REQUIREMENTS_DOC = "### R-1: title\nsome body text.\n"


def _plan_with(**kwargs: object) -> models.Plan:
    return models.Plan(make_plan(**kwargs))  # type: ignore[arg-type]


def test_declared_requirements_reads_real_headings_only() -> None:
    text = "### R-1: <title>\n### R-2: a real one\n## NFR-1: Performance\ntext mentioning R-9 inline\n"
    assert dag_trace.declared_requirements(text) == ["R-2", "NFR-1"]


def test_a_whole_thread_reports_no_errors() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"])],
        tasks=[make_task("T-001", claim_ids=["C-001"])],
    )
    report = dag_trace.trace(plan, dag.join(plan, None), declared=["R-1"], design_text="### R-1 → design\ncovered.")
    assert report.ok
    assert report.errors == [] and report.warnings == []
    assert report.requirements == ["R-1"]


def test_an_empty_plan_is_unknown_not_whole() -> None:
    """The false green this replaced: an empty plan used to report 'the thread is whole'."""
    plan = _plan_with(claims=[], tasks=[])
    report = dag_trace.trace(plan, dag.join(plan, None))
    assert not report.checked
    assert not report.ok


def test_a_declared_requirement_with_no_claim_blocks() -> None:
    plan = _plan_with(claims=[], tasks=[])
    report = dag_trace.trace(plan, declared=["R-1"])
    assert any("no claim states what it means" in e for e in report.errors)


def test_a_claim_citing_an_undeclared_requirement_is_dangling() -> None:
    plan = _plan_with(claims=[make_claim("C-001", requirement_ids=["R-9"])])
    report = dag_trace.trace(plan, declared=["R-1"])
    assert any("does not declare" in e for e in report.errors)


def test_a_task_answering_for_no_claim_blocks() -> None:
    plan = _plan_with(tasks=[make_task("T-001", claim_ids=[])])
    report = dag_trace.trace(plan, dag.join(plan, None), declared=["R-1"])
    assert any("answers for no claim" in e for e in report.errors)


def test_a_claim_with_no_task_is_a_warning_not_a_block() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"])],
        tasks=[],
    )
    report = dag_trace.trace(plan, dag.join(plan, None), declared=["R-1"])
    assert report.ok
    assert any("no task is answerable" in w for w in report.warnings)


def test_nfr_with_no_claim_is_a_warning_not_a_block() -> None:
    assert dag_trace.is_nfr("NFR-1")
    assert not dag_trace.is_nfr("R-1")
    plan = _plan_with(claims=[], tasks=[])
    report = dag_trace.trace(plan, declared=["NFR-1"])
    assert report.ok
    assert any("NFR" in w for w in report.warnings)


def test_undesigned_requirement_blocks_but_nfr_only_warns() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"]), make_claim("C-002", requirement_ids=["NFR-1"])]
    )
    report = dag_trace.trace(plan, declared=["R-1", "NFR-1"], design_text="nothing relevant here")
    assert any("R-1" in e and "no section" in e for e in report.errors)
    assert any("NFR-1" in w for w in report.warnings)


def test_test_plan_dimension_flags_a_missing_requirement() -> None:
    plan = _plan_with(claims=[make_claim("C-001", requirement_ids=["R-1"])])
    report = dag_trace.trace(plan, declared=["R-1"], test_plan_text="nothing about any requirement here")
    assert any("R-1" in e and "test plan" in e for e in report.errors)


def test_render_trace_of_an_unknown_thread_says_so() -> None:
    plan = _plan_with(claims=[], tasks=[])
    rendered = dag_trace.render_trace(dag_trace.trace(plan))
    assert "unknown" in rendered


def test_render_trace_names_the_blocking_findings() -> None:
    plan = _plan_with(claims=[make_claim("C-001", requirement_ids=["R-9"])])
    rendered = dag_trace.render_trace(dag_trace.trace(plan, declared=["R-1"]))
    assert "Blocking" in rendered and "C-001" in rendered


def test_render_trace_of_a_whole_thread_says_so() -> None:
    plan = _plan_with(
        claims=[make_claim("C-001", requirement_ids=["R-1"])], tasks=[make_task("T-001", claim_ids=["C-001"])]
    )
    report = dag_trace.trace(plan, dag.join(plan, None), declared=["R-1"], design_text="### R-1 → design\ncovered.")
    assert "The thread is whole" in dag_trace.render_trace(report)


# --- CLI ----------------------------------------------------------------------


def test_cli_validate_is_silent_on_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert dag.main(["--validate", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out == ""


def test_cli_impacted_lists_the_ripple(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
    )
    assert dag.main(["--impacted", "T-001", "--repo", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == "T-002"


def test_cli_impacted_rejects_an_unknown_task(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert dag.main(["--impacted", "T-999", "--repo", str(tmp_path)]) == 2


def test_cli_trace_exits_2_when_there_is_no_plan_yet(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=None)
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 2


def test_cli_trace_exits_2_on_an_empty_plan(tmp_path: Path) -> None:
    """The false green this replaced: an empty plan used to exit 0."""
    seed_repo(tmp_path, plan=make_plan(claims=[], tasks=[]))
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 2


def test_cli_trace_reads_declared_requirements_from_the_docs(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=make_plan(claims=[], tasks=[]), docs=True)
    (tmp_path / "docs" / "10-requirements.md").write_text(REQUIREMENTS_DOC, encoding="utf-8")
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 1  # R-1 declared, no claim covers it


def test_cli_trace_exits_1_on_a_dangling_claim_reference(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-9"])], tasks=[]),
        docs=True,
    )
    (tmp_path / "docs" / "10-requirements.md").write_text(REQUIREMENTS_DOC, encoding="utf-8")
    assert dag.main(["--trace", "--repo", str(tmp_path)]) == 1


def test_cli_trace_rejects_require_design_as_unknown_flag(tmp_path: Path) -> None:
    """`--require-design` never existed; a doc that told an agent to pass it was itself a bug."""
    seed_repo(tmp_path)
    with pytest.raises(SystemExit):
        dag.main(["--trace", "--require-design", "--repo", str(tmp_path)])


def test_cli_trace_test_plan_needs_trace(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert dag.main(["--test-plan", "docs/test/test-plan.md", "--repo", str(tmp_path)]) == 2


def test_cli_trace_test_plan_flags_a_missing_requirement(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])], tasks=[]),
        docs=True,
    )
    (tmp_path / "docs" / "10-requirements.md").write_text(REQUIREMENTS_DOC, encoding="utf-8")
    test_plan = tmp_path / "docs" / "test" / "test-plan.md"
    test_plan.write_text("no requirement id appears here\n", encoding="utf-8")
    rc = dag.main(["--trace", "--test-plan", "docs/test/test-plan.md", "--repo", str(tmp_path)])
    assert rc == 1
