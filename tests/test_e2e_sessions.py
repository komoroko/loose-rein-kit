"""A task with one upstream starts from the session that finished it, rather than from cold.

End to end over real git with a fake implementer that records each command line. Every task used to
open a fresh session, so the second task of a chain re-read the ticket, the design slice and the
code its upstream had just read — the cost the session exists to avoid, paid once per task.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rein import adapters, build_loop, common, sessions
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
    }
]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def seeded(tmp_path: Path, tasks: list[dict[str, Any]], **config: Any) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0, **config),
        plan=make_plan(tasks=tasks),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def implementer(launched: dict[str, list[list[str]]]) -> object:
    """Writes `src/<task>.py` in whatever worktree it is launched in, and records the argv by task."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        launched.setdefault(where.name, []).append(cmd)
        (where / "src").mkdir(exist_ok=True)
        (where / "src" / f"{where.name}.py").write_text(f"# {where.name}\n", encoding="utf-8")
        return 0, agent_envelope("")

    return _run


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def flag(cmd: list[str], name: str) -> str:
    return cmd[cmd.index(name) + 1] if name in cmd else ""


def chain(*ids: str) -> list[dict[str, Any]]:
    return [
        make_task(tid, kind="foundation", claim_ids=["C-001"], scope_include=[f"src/{tid}.py"])
        if i == 0
        else make_task(
            tid, kind="foundation", blocked_by=[ids[i - 1]], claim_ids=["C-001"], scope_include=[f"src/{tid}.py"]
        )
        for i, tid in enumerate(ids)
    ]


def test_the_second_task_of_a_chain_forks_the_session_that_finished_the_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, chain("T-001", "T-002"))
    launched: dict[str, list[list[str]]] = {}
    monkeypatch.setattr(build_loop, "_run", implementer(launched))

    assert build(repo) == common.EXIT_DONE

    [first] = launched["T-001"]
    [second] = launched["T-002"]
    upstream = flag(first, "--session-id")
    assert upstream and "--resume" not in first, "the first task has nothing to start from"
    assert flag(second, "--resume") == upstream, "the second task started cold"
    assert "--fork-session" in second, "resuming instead of forking would move the upstream's session"
    own = flag(second, "--session-id")
    assert own and own != upstream, "a fork still gets an id of its own, so its retries can resume it"
    assert "continues from the one that implemented T-001" in second[-1]
    assert ".worktrees/T-001" in second[-1], "the prompt names the worktree that no longer exists"


def test_a_task_with_two_upstreams_starts_cold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = seeded(
        tmp_path,
        [
            make_task("T-001", kind="foundation", claim_ids=["C-001"], scope_include=["src/T-001.py"]),
            make_task("T-002", kind="foundation", claim_ids=["C-001"], scope_include=["src/T-002.py"]),
            make_task(
                "T-003",
                kind="foundation",
                blocked_by=["T-001", "T-002"],
                claim_ids=["C-001"],
                scope_include=["src/T-003.py"],
            ),
        ],
    )
    launched: dict[str, list[list[str]]] = {}
    monkeypatch.setattr(build_loop, "_run", implementer(launched))

    assert build(repo) == common.EXIT_DONE
    [third] = launched["T-003"]
    assert "--resume" not in third and "--fork-session" not in third
    assert "continues from" not in third[-1]


def test_the_session_survives_into_the_next_run_and_not_into_another_cycle_or_cli(tmp_path: Path) -> None:
    """The downstream task is usually launched by a later `rein build`; a capacity stop between two
    tasks is the normal case. Another cycle, and another CLI, never read it."""
    repo = seeded(tmp_path, chain("T-001", "T-002"))
    state = store_mod.Store(repo).read_state()
    assert state is not None
    sessions.Sessions.for_repo(repo).put(state.cycle_id, "T-001", "claude", "S-UPSTREAM")
    sessions.Sessions.for_repo(repo).put("another-cycle", "T-002", "claude", "S-ELSEWHERE")

    reread = sessions.Sessions.for_repo(repo)
    assert reread.get(state.cycle_id, "T-001", "claude") == "S-UPSTREAM"
    assert reread.get(state.cycle_id, "T-001", "codex") == ""
    assert reread.get(state.cycle_id, "T-002", "claude") == ""

    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    [second] = [t for t in loop._load_graph().tasks if t.id == "T-002"]
    assert loop._inherited_session(second) == ("T-001", "S-UPSTREAM")


def test_a_cli_that_cannot_fork_is_never_asked_to() -> None:
    """codex names its own sessions and has no fork; asking it for one is refused, not improvised."""
    codex = adapters.ADAPTER_TABLE["codex"]
    with pytest.raises(adapters.LaunchRefused):
        adapters.command(codex.launch_argv(), "P", session="S", fork_from="PARENT")
    claude = adapters.ADAPTER_TABLE["claude"]
    line = adapters.command(claude.launch_argv(), "P", session="S", fork_from="PARENT")
    assert line[-6:] == ["--session-id", "S", "--resume", "PARENT", "--fork-session", "P"]
