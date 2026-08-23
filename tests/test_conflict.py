"""Classifying a merge conflict before resolving it.

The load-bearing test in this file is `test_a_claimed_resolution_the_gate_refuses_is_not_mechanical`.
If an implementer's own word can establish `mechanical`, the classification is self-reported and
the whole module is decoration.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rein import conflict, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, make_task, seed_repo


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def run_git(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
    """The real thing. These tests are about what git actually does on a conflict."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


# --- a repository with two branches that disagree -----------------------------


@pytest.fixture
def colliding(tmp_path: Path) -> Callable[..., dict[str, Any]]:
    """A repo where `theirs` and `ours` both changed `path`, so merging conflicts."""

    def build(
        *,
        path: str = "src/registry.py",
        scopes: dict[str, list[str]] | None = None,
        conflicting: bool = True,
    ) -> dict[str, Any]:
        root = tmp_path / "repo"
        root.mkdir()
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "test@example.invalid")
        git(root, "config", "user.name", "test")
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("VERBS = {\n}\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "base")

        git(root, "switch", "-q", "-c", "theirs")
        target.write_text('VERBS = {\n    "b": "b",\n}\n', encoding="utf-8")
        git(root, "commit", "-q", "-am", "T-002: add b")

        git(root, "switch", "-q", "main")
        git(root, "switch", "-q", "-c", "ours")
        if conflicting:
            target.write_text('VERBS = {\n    "a": "a",\n}\n', encoding="utf-8")
        else:
            (root / "src" / "other.py").write_text("# T-001\n", encoding="utf-8")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "T-001: add a")

        declared = scopes or {}
        plan = make_plan(
            tasks=[
                _task_with_scope("T-001", declared.get("T-001")),
                _task_with_scope("T-002", declared.get("T-002"), kind="parallel"),
            ]
        )
        state = make_state(tasks={"T-001": "done", "T-002": "done"})
        seed_repo(root, plan=plan, state=state, config=make_config())
        return {"root": root, "cwd": str(root), "repo": repo_mod.Repo(root), "path": path}

    return build


def _task_with_scope(task_id: str, include: list[str] | None, kind: str = "foundation") -> dict[str, Any]:
    task = make_task(task_id, kind=kind, claim_ids=["C-001"])
    if include is not None:
        task["scope"] = {"include": include}
    return task


def plan_of(bundle: dict[str, Any]) -> models.Plan:
    plan = store_mod.Store(bundle["repo"]).read_plan()
    assert plan is not None
    return plan


def merge(bundle: dict[str, Any], *, implement: Any, gate: Any, attempts: int = 2) -> conflict.Resolution:
    return conflict.merge_with_resolution(
        plan_of(bundle),
        cwd=bundle["cwd"],
        source_ref="theirs",
        ours_task="T-001",
        theirs_task="T-002",
        implement=implement,
        quality_gate=gate,
        run=run_git,
        attempts=attempts,
    )


def resolves(text: str) -> Any:
    """An implementer that writes `text` over every conflicted path and reports success."""

    def implement(c: conflict.Conflict, cwd: str) -> str:
        for path in c.paths:
            (Path(cwd) / path).write_text(text, encoding="utf-8")
            subprocess.run(["git", "-C", cwd, "add", "--", path], check=True)
        return conflict.OUTCOME_IMPLEMENTED

    return implement


def green(_cwd: str) -> tuple[int, str]:
    return 0, "all good"


def red(_cwd: str) -> tuple[int, str]:
    return 1, "2 tests failed"


# --- the merges that are not conflicts ----------------------------------------


def test_a_merge_with_nothing_to_do_is_a_noop(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding(conflicting=False)
    merge(bundle, implement=resolves("x"), gate=green)

    again = merge(bundle, implement=_never, gate=green)

    assert again.kind == "noop"
    assert again.merged


def test_a_clean_merge_commits_without_asking_anyone(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding(conflicting=False)

    result = merge(bundle, implement=_never, gate=_never_gate)

    assert result.kind == "clean"
    assert result.commit
    assert result.merged


def _never(_c: conflict.Conflict, _cwd: str) -> str:
    raise AssertionError("no implementer should be launched here")


def _never_gate(_cwd: str) -> tuple[int, str]:
    raise AssertionError("no quality gate should be run here")


# --- classification -----------------------------------------------------------


def test_a_path_neither_scope_covers_is_a_scope_violation(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding(scopes={"T-001": ["src/a/"], "T-002": ["src/b/"]})

    result = merge(bundle, implement=_never, gate=_never_gate)

    assert result.kind == "scope-violation"
    assert "src/registry.py" in result.escalation
    assert "the scope split in the plan is what has to change" in result.escalation


def test_an_undeclared_scope_is_unbounded_not_empty(colliding: Callable[..., dict[str, Any]]) -> None:
    """`dag.Task.scope_include` documents the reading; a side that declared nothing owns any path."""
    bundle = colliding(scopes={"T-001": ["src/a/"]})  # T-002 declares nothing

    result = merge(bundle, implement=resolves("resolved\n"), gate=green)

    assert result.kind == "mechanical"


def test_an_implementer_that_reports_needs_revision_is_a_semantic_conflict(
    colliding: Callable[..., dict[str, Any]],
) -> None:
    bundle = colliding()

    result = merge(bundle, implement=lambda _c, _cwd: "needs-revision", gate=_never_gate)

    assert result.kind == "semantic"
    assert "a defect in the plan" in result.escalation
    assert result.needs_a_human


def test_a_blocked_implementer_is_not_treated_as_a_resolution(colliding: Callable[..., dict[str, Any]]) -> None:
    result = merge(colliding(), implement=lambda _c, _cwd: "blocked", gate=_never_gate)

    assert result.kind == "semantic"


def test_a_resolution_the_gate_accepts_is_mechanical(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()

    result = merge(bundle, implement=resolves('VERBS = {\n    "a": "a",\n    "b": "b",\n}\n'), gate=green)

    assert result.kind == "mechanical"
    assert result.commit
    assert 'VERBS = {\n    "a": "a",\n    "b": "b",\n}\n' == (Path(bundle["root"]) / bundle["path"]).read_text()


def test_a_claimed_resolution_the_gate_refuses_is_not_mechanical(colliding: Callable[..., dict[str, Any]]) -> None:
    """The one that decides whether this module means anything.

    The implementer reports `implemented` every time and leaves a tree with no conflict markers in
    it. Only the quality gate's exit status says otherwise, and it has to be what wins.
    """
    bundle = colliding()
    attempts: list[int] = []

    def implement(c: conflict.Conflict, cwd: str) -> str:
        attempts.append(1)
        return resolves("whatever compiles\n")(c, cwd)

    result = merge(bundle, implement=implement, gate=red)

    assert result.kind == "unresolved"
    assert result.needs_a_human
    assert "not a mechanical one, whatever it was reported as" in result.escalation
    assert len(attempts) == 2, "the implementer gets one retry, and no more"
    assert result.log == "2 tests failed"


def test_conflict_markers_left_behind_are_not_a_resolution(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()

    result = merge(bundle, implement=lambda _c, _cwd: conflict.OUTCOME_IMPLEMENTED, gate=_never_gate)

    assert result.kind == "unresolved"


def test_a_git_failure_that_is_not_a_conflict_says_so(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()

    result = conflict.merge_with_resolution(
        plan_of(bundle),
        cwd=bundle["cwd"],
        source_ref="no-such-branch",
        ours_task="T-001",
        theirs_task="T-002",
        implement=_never,
        quality_gate=_never_gate,
        run=run_git,
    )

    assert result.kind == "unresolved"
    assert "git merge failed" in result.escalation


# --- the worktree is never left mid-merge -------------------------------------


@pytest.mark.parametrize(
    ("implement", "gate"),
    [
        (lambda _c, _cwd: "needs-revision", _never_gate),
        (resolves("whatever\n"), red),
        (lambda _c, _cwd: conflict.OUTCOME_IMPLEMENTED, _never_gate),
    ],
    ids=["semantic", "gate-refused", "markers-left"],
)
def test_every_unresolved_path_aborts_the_merge(
    colliding: Callable[..., dict[str, Any]], implement: Any, gate: Any
) -> None:
    """A worktree stuck mid-merge is a state the next run cannot tell from a deliberate one."""
    bundle = colliding()

    merge(bundle, implement=implement, gate=gate)

    assert not (Path(bundle["root"]) / ".git" / "MERGE_HEAD").exists()
    assert git(Path(bundle["root"]), "status", "--porcelain", "--untracked-files=no") == ""


def test_a_scope_violation_aborts_the_merge_too(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding(scopes={"T-001": ["src/a/"], "T-002": ["src/b/"]})

    merge(bundle, implement=_never, gate=_never_gate)

    assert not (Path(bundle["root"]) / ".git" / "MERGE_HEAD").exists()


# --- the commit says what was resolved ----------------------------------------


def test_the_resolution_commit_names_both_tasks_and_every_path(
    colliding: Callable[..., dict[str, Any]],
) -> None:
    """A resolution buried in merge glue is invisible to anyone asking later why this line is here."""
    bundle = colliding()

    merge(bundle, implement=resolves("resolved\n"), gate=green)

    message = git(Path(bundle["root"]), "log", "-1", "--format=%B")
    assert "T-002 into T-001" in message
    assert "src/registry.py" in message
    assert "passed the task-stage quality gate" in message


# --- escalation ---------------------------------------------------------------


def test_escalate_parks_both_sides_and_their_dependents(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()
    result = merge(bundle, implement=lambda _c, _cwd: "needs-revision", gate=_never_gate)

    marked = conflict.escalate(bundle["repo"], result)

    state = store_mod.Store(bundle["repo"]).read_state()
    assert state is not None
    assert marked == ["T-001", "T-002"]
    assert state.task_status["T-001"] == "needs-revision"
    assert state.task_status["T-002"] == "needs-revision"


def test_escalate_records_why_in_the_same_transaction(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()
    result = merge(bundle, implement=lambda _c, _cwd: "needs-revision", gate=_never_gate)

    conflict.escalate(bundle["repo"], result)

    events = event_chain.load(Path(bundle["root"]) / ".rein/events.ndjson")
    gaps = [e for e in events if e.event == "knowledge_gap"]
    assert len(gaps) == 1
    assert gaps[0].detail["classification"] == "semantic"
    assert "src/registry.py" in gaps[0].detail["anchor"]
    assert sorted(gaps[0].subject_ids) == ["T-001", "T-002"]


def test_escalate_does_nothing_for_a_merge_that_worked(colliding: Callable[..., dict[str, Any]]) -> None:
    bundle = colliding()
    result = merge(bundle, implement=resolves("resolved\n"), gate=green)

    assert conflict.escalate(bundle["repo"], result) == []
    assert not (Path(bundle["root"]) / ".rein/events.ndjson").exists()


def test_escalate_never_touches_a_gate(colliding: Callable[..., dict[str, Any]]) -> None:
    """Parking a task is not rewinding an approval — that stays a human's privilege."""
    bundle = colliding()
    before = store_mod.Store(bundle["repo"]).read_state()
    assert before is not None
    result = merge(bundle, implement=lambda _c, _cwd: "needs-revision", gate=_never_gate)

    conflict.escalate(bundle["repo"], result)

    after = store_mod.Store(bundle["repo"]).read_state()
    assert after is not None
    assert {g: after.gate_status(g) for g in models.GATE_ORDER} == {g: before.gate_status(g) for g in models.GATE_ORDER}
    assert after.current_phase == before.current_phase
