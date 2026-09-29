"""A batch is read by one reviewer before it merges, and what it finds goes back to one task.

End to end over real git. Each leaf used to get a reviewer of its own, and a batch of two or more
then got one more over the join, reading the union of what the others had read: four launches for
three leaves whose code was sound. A `must_fix` finding went to a fixer launched cold, not to the
session that wrote the change.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common, dossier
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"
GATE = [
    {
        "name": "test",
        "kind": "command",
        "command": [sys.executable, "-c", "pass"],
        "executor_profile": "quality",
        "retries": 1,
    },
    {"name": "review", "kind": "agent", "agent_role": "code_reviewer", "retries": 1, "required": True},
]
LEAVES = ("T-002", "T-003", "T-004")


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def seeded(tmp_path: Path) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    tasks = [make_task("T-001", claim_ids=["C-001"], scope_include=["src/T-001.py"])] + [
        make_task(tid, kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"], scope_include=[f"src/{tid}.py"])
        for tid in LEAVES
    ]
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(tasks=tasks),
        state={**make_state(plan_status="frozen"), "tasks": {"T-001": {"status": "done"}}},
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


class Agents:
    """A fake claude: an implementer in a leaf's worktree, a reviewer at the repository root."""

    def __init__(self, must_fix: dict[str, int]) -> None:
        #: task id → how many review rounds still find a `must_fix` in it
        self.must_fix = dict(must_fix)
        self.reviews: list[list[str]] = []
        self.implementers: dict[str, list[list[str]]] = {}

    def __call__(
        self, cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        if "(the quality gate's agent step)" in cmd[-1]:
            ids = re.findall(r"^- \*\*(T-\d+)\*\*", cmd[-1], flags=re.MULTILINE)
            self.reviews.append(ids)
            entries: dict[str, Any] = {}
            for tid in ids:
                found = []
                if self.must_fix.get(tid, 0) > 0:
                    self.must_fix[tid] -= 1
                    found = [
                        {"severity": "must_fix", "statement": f"{tid} drops the guard", "anchor": f"src/{tid}.py:1"}
                    ]
                entries[tid] = {"findings": found}
            target = dossier.findings_path(str(where), "review")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"tasks": entries}), encoding="utf-8")
            return 0, agent_envelope("")
        self.implementers.setdefault(where.name, []).append(cmd)
        (where / "src").mkdir(exist_ok=True)
        rounds = len(self.implementers[where.name])
        (where / "src" / f"{where.name}.py").write_text(f"# {where.name} round {rounds}\n", encoding="utf-8")
        return 0, agent_envelope("")


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def status_of(repo: repo_mod.Repo, task_id: str) -> str:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return str(raw["tasks"].get(task_id, {}).get("status", "todo"))


def test_three_sound_leaves_are_read_by_one_reviewer_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = seeded(tmp_path)
    agents = Agents({})
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    assert agents.reviews == [list(LEAVES)], f"expected one reading of the whole batch, got {agents.reviews}"
    assert all(status_of(repo, tid) == "done" for tid in LEAVES)


def test_a_must_fix_goes_back_to_the_session_that_wrote_it_and_only_that_task_is_read_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path)
    agents = Agents({"T-003": 1})
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    assert agents.reviews == [list(LEAVES), ["T-003"]]
    first, fix = agents.implementers["T-003"]
    session = first[first.index("--session-id") + 1]
    assert fix[fix.index("--resume") + 1] == session, "the fix started cold instead of in the session that wrote it"
    assert "T-003 drops the guard" in fix[-1]
    assert [len(agents.implementers[tid]) for tid in ("T-002", "T-004")] == [1, 1], "a sound leaf was sent back"
    assert all(status_of(repo, tid) == "done" for tid in LEAVES)
    assert "round 2" in git(Path(repo.root), "show", f"{WORK_BRANCH}:src/T-003.py"), "the fix is what landed"


def test_a_finding_that_outlives_the_rounds_holds_back_its_task_and_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path)
    agents = Agents({"T-003": 5})
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_HUMAN_NEEDED

    assert status_of(repo, "T-003") == "blocked"
    assert status_of(repo, "T-002") == "done" and status_of(repo, "T-004") == "done"
    assert agents.reviews == [list(LEAVES), ["T-003"]], "retries: 1 is one send-back and one more reading"
