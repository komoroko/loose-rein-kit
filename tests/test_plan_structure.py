"""The DAG and the scopes, derived from what the criteria say they produce and read (#89).

One cycle rolled its mandate back three times for facts the plan already held: a module under a
package another task created with no edge to it (#47), an artifact outside its task's scope (#61),
and screenshots a criterion read from an ignored directory no worktree ever has (#504). Each is
refused here, before the freeze, with the fact that was missing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import approve, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_plan, make_state, make_task, seed_repo


def _criterion(**evidence: Any) -> list[dict[str, Any]]:
    return [{"id": "A-1", "statement": "holds", "evidence": {"kind": "command", "command": ["true"], **evidence}}]


def _errors(tasks: list[dict[str, Any]]) -> list[str]:
    plan = models.Plan(make_plan(tasks=tasks))
    return models.cross_reference_errors(plan)


def test_a_module_under_a_package_another_task_creates_needs_the_edge() -> None:
    """#47: T-007 creates the package; T-001 writes a module in it and did not depend on T-007."""
    package = make_task(
        "T-007", scope_include=["src/pkg/"], claim_ids=["C-001"], acceptance=_criterion(produces=["src/pkg/"])
    )
    module = make_task(
        "T-001",
        kind="parallel",
        scope_include=["src/pkg/a.py"],
        claim_ids=["C-001"],
        acceptance=_criterion(produces=["src/pkg/a.py"]),
    )
    [error] = [e for e in _errors([package, module]) if "has to depend" in e]
    assert error.startswith("tasks/T-001:") and "src/pkg/ that T-007 produces" in error

    module["blocked_by"] = ["T-007"]
    assert not [e for e in _errors([package, module]) if "has to depend" in e]


def test_an_edge_through_another_task_is_enough() -> None:
    package = make_task(
        "T-007", scope_include=["src/pkg/"], claim_ids=["C-001"], acceptance=_criterion(produces=["src/pkg/"])
    )
    middle = make_task("T-002", kind="parallel", blocked_by=["T-007"], claim_ids=["C-001"])
    module = make_task(
        "T-001",
        kind="parallel",
        blocked_by=["T-002"],
        scope_include=["src/pkg/a.py"],
        claim_ids=["C-001"],
        acceptance=_criterion(reads=["src/pkg/"]),
    )
    assert not [e for e in _errors([package, middle, module]) if "has to depend" in e]


def test_a_produced_path_outside_the_task_s_scope_is_refused() -> None:
    """#61, for a declared `produces` as well as an `artifact`."""
    task = make_task(
        "T-007", scope_include=["src/"], claim_ids=["C-001"], acceptance=_criterion(produces=["docs/m.md"])
    )
    assert any("docs/m.md, which the task's own scope does not cover" in e for e in _errors([task]))


def test_a_path_has_one_producer() -> None:
    first = make_task(
        "T-001", scope_include=["out/"], claim_ids=["C-001"], acceptance=_criterion(produces=["out/a.json"])
    )
    second = make_task(
        "T-002",
        kind="parallel",
        scope_include=["out/"],
        claim_ids=["C-001"],
        acceptance=_criterion(produces=["out/a.json"]),
    )
    assert any("out/a.json is produced by T-001, T-002" in e for e in _errors([first, second]))


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def mandate_repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, state=make_state(plan_status="draft"), git=True)
    (tmp_path / ".gitignore").write_text("out/\n", encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    (tmp_path / "fixtures" / "tracked.json").write_text("{}\n", encoding="utf-8")
    _git(tmp_path, "add", ".gitignore", "fixtures/tracked.json")
    _git(tmp_path, "-c", "user.email=t@e.x", "-c", "user.name=T", "commit", "-q", "-m", "seed")
    return repo_mod.Repo(tmp_path)


def _write_plan(repo: repo_mod.Repo, tasks: list[dict[str, Any]]) -> models.Plan:
    (repo.root / ".rein" / "plan.yaml").write_bytes(store_mod.dump_yaml(make_plan(tasks=tasks)))
    plan = store_mod.Store(repo).read_plan()
    assert plan is not None
    return plan


def test_a_criterion_reading_from_an_ignored_directory_is_refused_at_the_mandate(mandate_repo: repo_mod.Repo) -> None:
    """#504: the screenshots lived under an ignored `out/`, which no worktree ever has."""
    shots = make_task(
        "T-021", scope_include=["out/"], claim_ids=["C-001"], acceptance=_criterion(produces=["out/shots/"])
    )
    reader = make_task(
        "T-018",
        kind="parallel",
        blocked_by=["T-021"],
        claim_ids=["C-001"],
        acceptance=_criterion(reads=["out/shots/a.png"]),
    )
    blockers = approve._worktree_blockers(mandate_repo, _write_plan(mandate_repo, [shots, reader]), "mandate")
    assert blockers == [
        "T-021 produces out/shots/, which git ignores — it can never be committed, so no task that depends "
        "on it will find it in its worktree"
    ]


def test_a_read_nothing_produces_has_to_be_tracked(mandate_repo: repo_mod.Repo) -> None:
    reader = make_task(
        "T-001", claim_ids=["C-001"], acceptance=_criterion(reads=["fixtures/tracked.json", "fixtures/gone.json"])
    )
    [blocker] = approve._worktree_blockers(mandate_repo, _write_plan(mandate_repo, [reader]), "mandate")
    assert blocker.startswith("T-001 reads fixtures/gone.json, which no task produces and git does not track")


def test_criteria_that_name_no_path_are_put_in_front_of_the_approver(mandate_repo: repo_mod.Repo) -> None:
    declared = make_task("T-001", claim_ids=["C-001"], acceptance=_criterion(reads=["fixtures/tracked.json"]))
    prose = make_task(
        "T-002", kind="parallel", claim_ids=["C-001"], acceptance=[{"id": "A-1", "statement": "it is fast"}]
    )
    _write_plan(mandate_repo, [declared, prose])
    assert approve.naming(mandate_repo, "mandate")["undeclared"] == [
        {"task_id": "T-002", "id": "A-1", "statement": "it is fast"}
    ]
