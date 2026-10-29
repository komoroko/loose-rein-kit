"""The sub-issues of one issue share a launch, and a launch that fails is split, not judged (CR-82, CR-83).

End to end over real git. A plan used to be cut into tasks, and each task was one launch; grouping
two of them meant re-cutting the plan, which is a roll back of the mandate. Now an issue bounds a
launch and the loop decides how its sub-issues share one. A shared launch whose gate stays red says
nothing about which sub-issue it was red for, so each goes back to run alone, and its own launch
decides it.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from rein import build_loop, common, dag, pr_stack
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import (
    agent_envelope,
    make_claim,
    make_config,
    make_issue,
    make_plan,
    make_reviews,
    make_state,
    make_task,
    seed_repo,
)

WORK_BRANCH = "build/demo"
MEMBERS = ("T-001", "T-002", "T-003")
_REVIEWS = make_reviews(actual_extraction=False, comparison=False, acceptance=())


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _gate(*, red_when: str = "") -> list[dict[str, object]]:
    """A green gate, or one that is red while any file under `src/` says `red_when`."""
    check = (
        f"import pathlib, sys; sys.exit(any({red_when!r} in p.read_text() for p in pathlib.Path('src').glob('*.py')))"
        if red_when
        else "pass"
    )
    return [{"name": "test", "command": [sys.executable, "-c", check], "executor_profile": "quality", "retries": 1}]


def seeded(tmp_path: Path, *, gate: list[dict[str, object]]) -> repo_mod.Repo:
    """One issue of three sub-issues: T-002 waits for T-001, T-003 waits for nothing."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "commit", "-q", "--allow-empty", "-m", "base")
    subs = [
        make_task("T-001", claim_ids=["C-001"], scope_include=["src/T-001.py"]),
        make_task("T-002", blocked_by=["T-001"], claim_ids=["C-001"], scope_include=["src/T-002.py"]),
        make_task("T-003", claim_ids=["C-001"], scope_include=["src/T-003.py"]),
    ]
    plan = make_plan(claims=[make_claim("C-001")], issues=[make_issue("I-001", sub_issues=subs, kind="foundation")])
    plan["cycle"]["base_commit"] = git(root, "rev-parse", "HEAD")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=gate, launch_retries=0, repair_rounds=1),
        reviews=_REVIEWS,
        plan=plan,
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


class Implementers:
    """A fake claude implementer. It writes one file per sub-issue its prompt hands it."""

    def __init__(self, *, poison_when_bundled: str = "") -> None:
        #: lead id → the sub-issues each of its launches carried
        self.launches: list[tuple[str, ...]] = []
        self.poison = poison_when_bundled

    def __call__(
        self, cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        bundled = re.search(r"Your task is the sub-issues ([T0-9, -]+) of I-", cmd[-1])
        carried = tuple(bundled.group(1).split(", ")) if bundled else (where.name,)
        self.launches.append(carried)
        (where / "src").mkdir(exist_ok=True)
        for task_id in carried:
            (where / "src" / f"{task_id}.py").write_text(f"# {task_id}\n", encoding="utf-8")
        if bundled and self.poison:
            # Inside T-003's own scope, so what stops the bundle is its gate and nothing before it.
            (where / "src" / "T-003.py").write_text(f"# T-003 {self.poison}\n", encoding="utf-8")
        return 0, agent_envelope("")


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def state_of(repo: repo_mod.Repo) -> dict[str, dict[str, object]]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    tasks: dict[str, dict[str, object]] = raw["tasks"]
    return tasks


def test_the_sub_issues_of_one_issue_land_in_one_launch_and_one_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, gate=_gate())
    agents = Implementers()
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    assert agents.launches == [MEMBERS], f"expected one launch carrying all three, got {agents.launches}"
    tasks = state_of(repo)
    assert {tasks[t]["status"] for t in MEMBERS} == {"done"}
    commits = {tasks[t]["completed_commit"] for t in MEMBERS}
    assert len(commits) == 1, "a bundle lands as one commit, which every member names"
    [landed] = [e for e in store_mod.Store(repo).read_events() if e.event == "bundle_landed"]
    assert list(landed.subject_ids) == list(MEMBERS) and landed.detail == {"commit": commits.pop()}
    assert all(git(Path(repo.root), "show", f"{WORK_BRANCH}:src/{t}.py") == f"# {t}" for t in MEMBERS)
    # The stack reads the same record: one pull request, delivering all three (CR-84).
    slices = pr_stack.derive(repo, pr_stack.Documents.read(repo))
    assert [s.delivers for s in slices if s.task_id] == [MEMBERS]


def test_a_bundle_that_stays_red_is_split_and_each_sub_issue_is_decided_by_its_own_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, gate=_gate(red_when="poison"))
    agents = Implementers(poison_when_bundled="poison")
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    # The bundle's launch and its one send-back (a resumed session, told only the failure), then
    # one launch per sub-issue.
    assert agents.launches[0] == MEMBERS
    assert sorted(agents.launches[2:]) == [("T-001",), ("T-002",), ("T-003",)]
    events = store_mod.Store(repo).read_events()
    [split] = [e for e in events if e.event == "bundle_split"]
    assert list(split.subject_ids) == list(MEMBERS)
    assert "kept" in split.detail and str(split.detail["kept"]).startswith(f"{WORK_BRANCH}-T-001-bundle-")
    tasks = state_of(repo)
    assert {tasks[t]["status"] for t in MEMBERS} == {"done"}
    assert len({tasks[t]["completed_commit"] for t in MEMBERS}) == 3, "each landed on its own"
    assert all(tasks[t].get("unbundled") for t in MEMBERS)
    # No sub-issue was blocked or sent for revision on the bundle's account.
    statuses = [e.detail.get("status") for e in events if e.event in ("task_failed", "decision_declared")]
    assert "blocked" not in statuses and "needs-revision" not in statuses
    assert git(Path(repo.root), "show", f"{WORK_BRANCH}:src/T-003.py") == "# T-003"


# --- the cut itself, without git --------------------------------------------


def _t(task_id: str, **kw: object) -> dag.Task:
    return dag.Task(id=task_id, title=task_id, kind="parallel", issue_id=str(kw.pop("issue", "I-001")), **kw)  # type: ignore[arg-type]


def test_a_bundle_is_a_contiguous_run_of_its_issue_and_stops_at_the_first_that_cannot_share() -> None:
    graph = dag.Graph.from_tasks(
        [
            _t("T-001", scope_include=("src/a",)),
            _t("T-002", blocked_by=("T-001",), scope_include=("src/b",)),
            _t("T-003", operate=({"name": "run", "command": ["x"]},)),
            _t("T-004"),
            _t("T-005", issue="I-002"),
        ]
    )
    launch = dag.bundle(graph, graph.get("T-001"))
    assert launch.members == ("T-001", "T-002")
    assert launch.scope_include == ("src/a", "src/b") and launch.blocked_by == ()
    assert dag.bundle(graph, graph.get("T-004")).members == (), "alone: nothing of its issue follows it"


def test_a_done_sibling_is_passed_over_and_an_unmet_edge_or_another_environment_ends_the_bundle() -> None:
    graph = dag.Graph.from_tasks(
        [
            _t("T-001"),
            _t("T-002", status="done"),
            _t("T-003"),
            _t("T-004", env=(("MODE", "x"),)),
            _t("T-009", issue="I-002", status="in-progress"),
            _t("T-005", blocked_by=("T-009",)),
        ]
    )
    assert dag.bundle(graph, graph.get("T-001")).members == ("T-001", "T-003")
    assert dag.bundle(graph, graph.get("T-004")).members == (), "T-005 waits on work outside the bundle"


@pytest.mark.parametrize(
    "why, field",
    [
        ("a person produces it", {"produced_by": "person"}),
        ("it crosses an irreversible point", {"irreversible": True}),
        ("the loop operates it", {"operate": ({"name": "run"},)}),
        ("it has preconditions of its own", {"requires": ({"says": "a browser"},)}),
        ("a bundle holding it was split", {"unbundled": "red"}),
    ],
)
def test_what_has_to_run_alone_is_never_bundled(why: str, field: dict[str, object]) -> None:
    lone = _t("T-002", **field)
    graph = dag.Graph.from_tasks([_t("T-001"), lone])
    assert dag.bundleable(lone) == why
    assert dag.bundle(graph, graph.get("T-001")).members == ()
    assert dag.bundle(graph, lone) is lone


def test_a_bundles_criteria_keep_each_member_apart_and_come_back_under_their_own_ids() -> None:
    graph = dag.Graph.from_tasks(
        [
            _t("T-001", acceptance=({"id": "A-1", "statement": "one"},)),
            _t("T-002", acceptance=({"id": "A-1", "statement": "two"},), scope_exclude=("src/x",)),
        ]
    )
    launch = dag.bundle(graph, graph.get("T-001"))
    assert [e["id"] for e in launch.acceptance] == ["T-001/A-1", "T-002/A-1"]
    assert dag.of_member(launch.acceptance, "T-002") == [{"id": "A-1", "statement": "two"}]
    # T-001 is unbounded, so T-002's "not src/x" is not the bundle's: T-001 may write there.
    assert launch.scope_include == () and launch.scope_exclude == ()
