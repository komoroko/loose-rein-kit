"""The scenario the whole environment-fault distinction exists for, end to end, over real git.

An agent session limit on a build of any length is close to certain, and the way people run
into it is unattended: something re-runs `rein build` from another terminal afterwards. So what
matters is not only that the loop reports the stop honestly — it is that the *next* process
picks the work up where the last one left it.

That recovery machinery (`build_git._salvage_leftovers` / `_restore_salvaged`) was already
complete and already tested. What it had never been able to do is *run*, because a leaf stopped
by a launch failure was marked `blocked`, `blocked` leaves the frontier, and the salvage path is
only ever reached for a task the frontier hands back. This file is the join: one stopped run,
one re-run, and the implementer continuing rather than starting over.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, make_task, seed_repo

SESSION_LIMIT = (1, "You've hit your session limit · resets 3:30am (Asia/Tokyo)")
WORK_BRANCH = "build/demo"

#: A host profile and a command that always passes: what is under test is the orchestration, not
#: anyone's test runner.
GATE = [{"name": "test", "kind": "command", "command": ["true"], "executor_profile": "quality", "retries": 2}]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    """A real git checkout on the work branch, with two independent leaves ready to build."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, max_parallel=2, launch_retries=0),
        plan=make_plan(
            tasks=[
                make_task("T-001", kind="parallel", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", claim_ids=["C-001"]),
            ]
        ),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def implementer_writing(root: Path, *, stop_on: str = "") -> object:
    """A fake agent CLI that writes a file for whichever task's worktree it was launched in.

    `stop_on` names the task whose launch reports an exhausted session limit — *after* writing,
    the way a real session dies partway through work rather than before it.
    """

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        task = Path(cwd or root).name
        (Path(cwd or root) / f"{task}.py").write_text(f"# {task} implementation\n", encoding="utf-8")
        return SESSION_LIMIT if task == stop_on else (0, agent_envelope(""))

    return _run


def status_of(repo: repo_mod.Repo, task_id: str) -> dict[str, Any]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw["tasks"].get(task_id, {}))


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def test_a_session_limit_stops_the_run_without_losing_the_batch_or_the_work(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(build_loop, "_run", implementer_writing(repo.root, stop_on="T-002"))

    assert build(repo) == common.EXIT_RETRY_LATER

    # The leaf that passed its gate earned its merge; throwing that away because a *different*
    # leaf hit a limit would be its own kind of dishonesty.
    assert status_of(repo, "T-001")["status"] == "done"
    assert (repo.root / "T-001.py").exists()

    # The leaf the machine stopped: no verdict, so no `blocked` — and its tree is left standing,
    # because that is what the next run finalizes and salvages.
    stopped = status_of(repo, "T-002")
    assert stopped["status"] == "todo"
    # No retry budget was spent, so nothing that is a *verdict* about this task was written down —
    # while the reason the machine stopped is kept, in the keys that are diagnostics.
    handoff = stopped.get("handoff", {})
    assert not {"retries_left", "failed_step", "failure_summary"} & set(handoff)
    assert "session limit" in handoff["last_fault"]["output_tail"]
    assert (repo.root / ".worktrees" / "T-002" / "T-002.py").exists()

    events = [e.event for e in store_mod.Store(repo).read_events()]
    assert "run_aborted" in events
    assert "knowledge_gap" not in events  # nothing here is a gap in anyone's knowledge


def test_the_re_run_continues_the_stopped_leaf_instead_of_restarting_it(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The join this file exists for. Under the old behaviour T-002 would be `blocked`, off the
    frontier, and this second run would report "no runnable tasks" with the work stranded."""
    monkeypatch.setattr(build_loop, "_run", implementer_writing(repo.root, stop_on="T-002"))
    assert build(repo) == common.EXIT_RETRY_LATER
    interrupted = (repo.root / ".worktrees" / "T-002" / "T-002.py").read_text(encoding="utf-8")

    # A new terminal, capacity back.
    monkeypatch.setattr(build_loop, "_run", implementer_writing(repo.root))
    assert build(repo) == common.EXIT_DONE

    assert status_of(repo, "T-002")["status"] == "done"
    assert (repo.root / "T-002.py").read_text(encoding="utf-8") == interrupted
    # The interrupted attempt's work reached the work branch through the salvage branch, not by
    # being written a second time: it was committed as WIP at restart, then merged.
    log = git(repo.root, "log", "--oneline", "--all")
    assert "WIP (salvaged at restart)" in log


def test_a_missing_agent_cli_refuses_rather_than_asking_to_be_re_run(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the exit-code contract: a supervisor looping on 3 must not loop here."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if cmd and cmd[0] == "claude":
            return 127, "could not run 'claude': [Errno 2] No such file or directory: 'claude'"
        return common.run(cmd, cwd, timeout)

    monkeypatch.setattr(build_loop, "_run", _run)
    assert build(repo) == common.EXIT_CANNOT_PROCEED
    assert status_of(repo, "T-001")["status"] == "todo"
    assert status_of(repo, "T-002")["status"] == "todo"


def test_a_serial_task_interrupted_after_committing_is_judged_on_its_commit_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A serial implementer commits straight onto the work branch. When the session dies after that
    commit, the next run used to re-take HEAD as the task's base — the diff from there was empty,
    and the task was blocked as `no_implementation` with its work one commit back."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(tasks=[make_task("T-001", kind="foundation", claim_ids=["C-001"])]),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    repo = repo_mod.Repo(root)
    started_on = git(root, "rev-parse", "HEAD")

    def committing_then_stopping(
        cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        (root / "T-001.py").write_text("# T-001 implementation\n", encoding="utf-8")
        git(root, "add", "T-001.py")
        git(root, "commit", "-q", "-m", "T-001: implement")
        return SESSION_LIMIT

    monkeypatch.setattr(build_loop, "_run", committing_then_stopping)
    assert build(repo) == common.EXIT_RETRY_LATER
    stopped = status_of(repo, "T-001")
    assert stopped["status"] == "todo"
    assert stopped["base"] == started_on
    assert git(root, "rev-parse", "HEAD") != started_on

    def idle(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        return 0, agent_envelope("")

    monkeypatch.setattr(build_loop, "_run", idle)
    assert build(repo) == common.EXIT_DONE
    landed = status_of(repo, "T-001")
    assert landed["status"] == "done"
    assert "base" not in landed


def test_a_serial_task_whose_pinned_base_left_history_stops_rather_than_re_pinning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(tasks=[make_task("T-001", kind="foundation", claim_ids=["C-001"])]),
        state={**make_state(plan_status="frozen"), "tasks": {"T-001": {"status": "todo", "base": "0" * 40}}},
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    repo = repo_mod.Repo(root)
    monkeypatch.setattr(build_loop, "_run", implementer_writing(root))

    assert build(repo) == 1
    assert status_of(repo, "T-001")["base"] == "0" * 40


def _serial_then_leaf(tmp_path: Path) -> tuple[repo_mod.Repo, str]:
    """A foundation task and a leaf that does not depend on it, on a work branch; returns the head."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(
            tasks=[
                make_task("T-001", kind="foundation", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", claim_ids=["C-001"]),
            ]
        ),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root), git(root, "rev-parse", "HEAD")


def test_nothing_lands_above_a_blocked_serial_task_s_unlanded_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leaf merged above T-001's commits would stand on work no gate passed, and T-001's next
    attempt would then be charged with the leaf's change by every check asking what it did."""
    repo, started_on = _serial_then_leaf(tmp_path)
    build_loop.set_task_status(repo, "T-001", "in-progress", base=started_on)
    (repo.root / "partial.py").write_text("# half of T-001\n", encoding="utf-8")
    git(repo.root, "add", "partial.py")
    git(repo.root, "commit", "-q", "-m", "T-001: partial")
    build_loop.set_task_status(repo, "T-001", "blocked")
    held_at = git(repo.root, "rev-parse", "HEAD")
    monkeypatch.setattr(build_loop, "_run", implementer_writing(repo.root))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-002").get("status", "todo") == "todo"
    assert git(repo.root, "rev-parse", "HEAD") == held_at

    from rein import task_cmd

    task_cmd.reset(repo, "T-001", status="todo", reason="one more attempt")
    assert build(repo) == common.EXIT_DONE
    completed = [e.subject_ids[0] for e in store_mod.Store(repo).read_events() if e.event == "task_completed"]
    assert completed == ["T-001", "T-002"]


def test_a_serial_attempt_that_left_nothing_holds_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stopped before writing anything: no base to keep, so neither a stop for the other tasks nor
    a stale base to charge their work to this one later."""
    repo, _ = _serial_then_leaf(tmp_path)

    def stopping(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        return SESSION_LIMIT

    monkeypatch.setattr(build_loop, "_run", stopping)
    assert build(repo) == common.EXIT_RETRY_LATER
    assert status_of(repo, "T-001")["status"] == "todo"
    assert "base" not in status_of(repo, "T-001")


def test_a_held_base_whose_work_was_reverted_is_released(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, started_on = _serial_then_leaf(tmp_path)
    build_loop.set_task_status(repo, "T-001", "in-progress", base=started_on)
    (repo.root / "partial.py").write_text("# half of T-001\n", encoding="utf-8")
    git(repo.root, "add", "partial.py")
    git(repo.root, "commit", "-q", "-m", "T-001: partial")
    git(repo.root, "revert", "--no-edit", "HEAD")
    build_loop.set_task_status(repo, "T-001", "blocked")
    monkeypatch.setattr(build_loop, "_run", implementer_writing(repo.root))

    assert build(repo) == common.EXIT_HUMAN_NEEDED  # T-001 is still blocked; T-002 was free to run
    assert "base" not in status_of(repo, "T-001")
    assert status_of(repo, "T-002")["status"] == "done"
