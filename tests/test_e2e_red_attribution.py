"""A red is charged to whoever owns the failing test, not to whoever happened to be under test.

End to end over real git, with a test runner small enough to read: it fails every test named in
`tests/owned/RED` (and, once, the one in `FLAKY`), and writes a JUnit XML report like any runner
asked for one. #90's cycle stopped three times on reds the blocked task could not fix: a flaky
concurrency test owned by a finished task, and — twice — a test another task owned.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"
REPORT = ".rein/work/junit-test.xml"
RUNNER = """\
import pathlib, sys
root = pathlib.Path('.')
red = [line.strip() for p in ('tests/owned/RED', 'src/RED') if (root / p).exists()
       for line in (root / p).read_text().splitlines() if line.strip()]
flaky, count = root / 'FLAKY', root / '.rein/work/flaky-count'
count.parent.mkdir(parents=True, exist_ok=True)
if flaky.exists():
    seen = int(count.read_text()) if count.exists() else 0
    count.write_text(str(seen + 1))
    if seen == 0:
        red.append(flaky.read_text().strip())
cases = ''.join(
    f'<testcase classname="{node.split("::")[0]}" name="{node.split("::")[1]}"><failure message="x"/></testcase>'
    for node in red
)
(root / '.rein/work/junit-test.xml').write_text(
    f'<testsuites><testsuite name="t">{cases}<testcase classname="ok" name="fine"/></testsuite></testsuites>'
)
sys.exit(1 if red else 0)
"""
GATE = [
    {
        "name": "test",
        "kind": "command",
        "command": [sys.executable, "run_tests.py"],
        "executor_profile": "quality",
        "retries": 1,
        "runs_tests": True,
        "junit": REPORT,
    }
]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def seeded(
    tmp_path: Path, tasks: list[dict[str, Any]], files: dict[str, str], done: tuple[str, ...] = ()
) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(tasks=tasks),
        state={**make_state(plan_status="frozen"), "tasks": {tid: {"status": "done"} for tid in done}},
    )
    (root / "run_tests.py").write_text(RUNNER, encoding="utf-8")
    for path, text in files.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def implementer(writes: dict[str, dict[str, str | None]]) -> object:
    """An implementer that, launched for task T, writes (or with None deletes) `writes[T]`."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        for path, text in writes.get(where.name, {}).items():
            target = where / path
            if text is None:
                target.unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8")
        return 0, agent_envelope("")

    return _run


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def status_of(repo: repo_mod.Repo, task_id: str) -> str:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return str(raw["tasks"].get(task_id, {}).get("status", "todo"))


def events(repo: repo_mod.Repo) -> list[Any]:
    return store_mod.Store(repo).read_events()


def test_a_red_another_task_owns_goes_to_it_and_the_task_under_test_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#90's T-018: the red is T-021's test, already red before T-018 changed anything."""
    repo = seeded(
        tmp_path,
        [
            make_task("T-018", kind="parallel", claim_ids=["C-001"], scope_include=["src/"]),
            make_task("T-021", kind="parallel", claim_ids=["C-001"], scope_include=["tests/owned/"]),
        ],
        {"tests/owned/RED": "tests/owned/test_shots.py::test_screenshots_exist\n"},
        done=("T-021",),
    )
    monkeypatch.setattr(
        build_loop,
        "_run",
        implementer({"T-018": {"src/T-018.py": "# T-018\n"}, "T-021": {"tests/owned/RED": None}}),
    )

    assert build(repo) == common.EXIT_DONE
    assert status_of(repo, "T-018") == "done"
    assert status_of(repo, "T-021") == "done"
    recorded = events(repo)
    assert not [e for e in recorded if e.event == "task_failed" and "T-018" in e.subject_ids]
    [routed] = [e for e in recorded if e.detail.get("kind") == "red_routed"]
    assert list(routed.subject_ids) == ["T-021"] and routed.detail["from"] == "T-018"


def test_a_test_that_passes_on_the_same_tree_the_second_time_is_flaky_and_stops_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(
        tmp_path,
        [make_task("T-009", kind="parallel", claim_ids=["C-001"], scope_include=["src/"])],
        {"FLAKY": "tests/owned/test_store.py::test_wal\n"},
    )
    monkeypatch.setattr(build_loop, "_run", implementer({"T-009": {"src/T-009.py": "# T-009\n"}}))

    assert build(repo) == common.EXIT_DONE
    [flaky] = [e for e in events(repo) if e.detail.get("kind") == "flaky"]
    assert flaky.detail["nodes"] == ["tests/owned/test_store.py::test_wal"]


def test_a_test_this_change_turned_red_is_this_change_s_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, [make_task("T-001", kind="parallel", claim_ids=["C-001"], scope_include=["src/"])], {})
    monkeypatch.setattr(
        build_loop, "_run", implementer({"T-001": {"src/T-001.py": "# T-001\n", "src/RED": "src/test_mine.py::t\n"}})
    )

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-001") == "blocked"
    failed = [e for e in events(repo) if e.event == "task_failed" and e.detail.get("step") == "test"]
    assert failed


def test_the_owner_of_a_red_that_was_already_there_is_charged_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red before its change or not, the task whose scope holds the test is the one meant to fix it."""
    repo = seeded(
        tmp_path,
        [make_task("T-021", kind="parallel", claim_ids=["C-001"], scope_include=["tests/owned/"])],
        {"tests/owned/RED": "tests/owned/test_shots.py::test_screenshots_exist\n"},
    )
    monkeypatch.setattr(build_loop, "_run", implementer({"T-021": {"tests/owned/notes.md": "tried\n"}}))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-021") == "blocked"
    assert not [e for e in events(repo) if e.detail.get("kind") == "red_routed"]


def test_parallel_leaves_read_the_forked_from_tree_in_checkouts_of_their_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two leaves of one batch can ask at the same moment; one shared scratch checkout would be
    removed by whichever finished first, under the other's test run."""
    import contextlib

    from rein import build_git

    repo = seeded(tmp_path, [make_task("T-001", kind="parallel", claim_ids=["C-001"])], {})
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    names: list[str] = []

    @contextlib.contextmanager
    def recording(_repo: object, _dir: str, name: str, _base: str, _run: object) -> Any:
        names.append(name)
        yield str(repo.root)

    monkeypatch.setattr(build_git, "scratch_worktree", recording)
    step = loop.config.steps[0]
    loop._failing_at(step, "HEAD", owner="T-001")
    loop._failing_at(step, "HEAD", owner="T-002")
    assert len(set(names)) == 2 and all(owner in name for owner, name in zip(("T-001", "T-002"), names, strict=True))


def test_a_suite_that_crashes_before_writing_its_report_is_not_read_off_the_last_one(tmp_path: Path) -> None:
    """The report an earlier run left listed only a red the change inherited. The suite then crashed
    before writing anything, and that old report was read as this run's: the crash was routed to
    the test's owner and the step passed. A report is only ever the one this run wrote."""
    repo = seeded(
        tmp_path,
        [
            make_task("T-018", kind="parallel", claim_ids=["C-001"], scope_include=["src/"]),
            make_task("T-021", kind="parallel", claim_ids=["C-001"], scope_include=["tests/owned/"]),
        ],
        {"tests/owned/RED": "tests/owned/test_shots.py::test_screenshots_exist\n"},
        done=("T-021",),
    )
    runner = repo.root / "run_tests.py"
    runner.write_text("import pathlib, sys\nif pathlib.Path('CRASH').exists(): sys.exit(2)\n" + runner.read_text())
    git(repo.root, "commit", "-qam", "the runner can crash before it reports")
    base = git(repo.root, "rev-parse", "HEAD")
    (repo.root / REPORT).parent.mkdir(parents=True, exist_ok=True)
    (repo.root / REPORT).write_text(
        '<testsuites><testsuite name="t"><testcase classname="tests/owned/test_shots.py" '
        'name="test_screenshots_exist"><failure message="x"/></testcase></testsuite></testsuites>'
    )
    (repo.root / "CRASH").write_text("")
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    from rein import dag

    step = loop.config.steps[0]
    failure = loop._run_cmd_step(step, str(repo.root))
    assert failure
    assert not (repo.root / REPORT).exists(), "the old report is gone before the run"
    assert loop._attribute_red([dag.load(repo).get("T-018")], step, str(repo.root), base, failure) != ""
