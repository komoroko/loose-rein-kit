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

from rein import build_loop, common
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import (
    REVIEW_STEP,
    agent_envelope,
    make_config,
    make_plan,
    make_reviews,
    make_state,
    make_task,
    seed_repo,
)

WORK_BRANCH = "build/demo"
GATE = [
    {
        "name": "test",
        "command": [sys.executable, "-c", "pass"],
        "executor_profile": "quality",
        "retries": 1,
    },
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
        reviews=make_reviews(steps=[REVIEW_STEP]),
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
        #: task id → what the diff command the reviewer was handed printed, per reading
        self.diffs: dict[str, list[str]] = {}

    def __call__(
        self, cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        if "(the quality gate's agent step)" in cmd[-1]:
            ids = re.findall(r"^- \*\*(T-\d+)\*\*", cmd[-1], flags=re.MULTILINE)
            self.reviews.append(ids)
            for tid, diff_cmd in re.findall(r"^- \*\*(T-\d+)\*\*.*change `([^`]+)`", cmd[-1], flags=re.MULTILINE):
                shown = subprocess.run(diff_cmd.split(), cwd=where, capture_output=True, text=True).stdout
                self.diffs.setdefault(tid, []).append(shown)
            entries: dict[str, Any] = {}
            for tid in ids:
                found = []
                if self.must_fix.get(tid, 0) > 0:
                    self.must_fix[tid] -= 1
                    found = [
                        {"severity": "must_fix", "statement": f"{tid} drops the guard", "anchor": f"src/{tid}.py:1"}
                    ]
                entries[tid] = {"findings": found}
            written = re.search(r"Write your findings to `([^`]+)`", cmd[-1])
            assert written, "the reviewer was not told where to write"
            target = where / written.group(1)
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
    # What each task was read for is in the chain, for acceptance to list (CR-50).
    [applied] = [e for e in store_mod.Store(repo).read_events() if e.event == "reviews_applied"]
    assert list(applied.subject_ids) == list(LEAVES)
    assert applied.detail == {"step": "review", "stage": "task", "reviews": ["correctness", "simplification"]}


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


def test_the_reviewer_is_shown_work_the_implementer_did_not_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake implementer never commits, as real ones sometimes do not. The loop finalized only
    at the merge, so the diff the reviewer was handed — the branch — was empty while the gate had
    tested the worktree."""
    repo = seeded(tmp_path)
    agents = Agents({})
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    for tid in LEAVES:
        [shown] = agents.diffs[tid]
        assert f"src/{tid}.py" in shown, f"the reviewer was handed an empty change for {tid}"


def _operating(tmp_path: Path, *, attempts: dict[str, Any] | None, gate_red_once: bool) -> tuple[repo_mod.Repo, Path]:
    """One task that operates: its run appends a line to `runs`, outside the product."""
    runs = tmp_path / "runs"
    reds = tmp_path / "reds"
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    task = make_task("T-002", kind="parallel", claim_ids=["C-001"], scope_include=["src/T-002.py", "out.txt"])
    task["operate"] = [{"name": "full-run", "command": ["sh", "-c", f"echo run >> {runs}; echo measured > out.txt"]}]
    if attempts is not None:
        task["attempts"] = attempts
    red_once = (
        f"import pathlib, sys; p = pathlib.Path({str(reds)!r}); n = len(p.read_text()) if p.exists() else 0; "
        "p.write_text('x' * (n + 1)); sys.exit(1 if n == 0 else 0)"
    )
    gate = [{**GATE[0], "command": [sys.executable, "-c", red_once if gate_red_once else "pass"]}]
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=gate, launch_retries=0),
        reviews=make_reviews(steps=[REVIEW_STEP]),
        plan=make_plan(tasks=[task]),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root), runs


def _runs(counter: Path) -> int:
    return counter.read_text().count("run") if counter.exists() else 0


def test_a_task_that_operates_is_read_before_its_run_so_a_finding_does_not_cost_a_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read after the run, every `must_fix` sent the task round again — the run included."""
    repo, runs = _operating(tmp_path, attempts=None, gate_red_once=False)
    agents = Agents({"T-002": 1})
    monkeypatch.setattr(build_loop, "_run", agents)

    assert build(repo) == common.EXIT_DONE

    assert _runs(runs) == 1, "the finding was answered before the run, so the run happened once"
    assert agents.reviews == [["T-002"], ["T-002"]], "read, sent back, read again — then never with the batch"
    first, fix = agents.implementers["T-002"]
    assert fix[fix.index("--resume") + 1] == first[first.index("--session-id") + 1]


def test_every_run_after_the_first_is_an_attempt_and_one_past_the_budget_is_not_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`attempts.max` priced the runs, and was counted once per `in-progress`: a red step after the
    run started it again inside the same attempt, as often as the step's retries allowed."""
    repo, runs = _operating(tmp_path, attempts={"max": 1, "cost": "one full run"}, gate_red_once=True)
    monkeypatch.setattr(build_loop, "_run", Agents({}))

    assert build(repo) == common.EXIT_HUMAN_NEEDED

    assert _runs(runs) == 1, "the second run was not covered by the approval"
    assert status_of(repo, "T-002") == "blocked"
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    assert raw["tasks"]["T-002"]["handoff"]["escalation"]["kind"] == "attempt_budget_spent"


def test_a_run_the_budget_covers_is_counted_as_an_attempt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, runs = _operating(tmp_path, attempts={"max": 2, "cost": "one full run"}, gate_red_once=True)
    monkeypatch.setattr(build_loop, "_run", Agents({}))

    assert build(repo) == common.EXIT_DONE

    assert _runs(runs) == 2
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    assert raw["tasks"]["T-002"]["attempts"] == 2


def test_a_chain_reading_repairs_only_the_task_whose_work_is_the_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A chain is read when its last task lands. A finding about the first task, repaired on top of
    the branch, moved the *last* task's `completed_commit` onto it — so the stack cut along those
    commits put the first task's fix in the last task's pull request."""
    from rein import actual_extraction, review_reading, security_review

    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    tasks = [
        make_task("T-001", claim_ids=["C-001"], scope_include=["src/T-001.py"]),
        make_task("T-002", claim_ids=["C-001"], blocked_by=["T-001"], scope_include=["src/T-002.py"]),
    ]
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=[GATE[0]], launch_retries=0),
        plan=make_plan(tasks=tasks),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    monkeypatch.setattr(build_loop, "_run", Agents({}))

    def finding(fid: str, path: str) -> dict[str, Any]:
        return {"id": fid, "blocking": True, "code_anchors": [{"path": path}]}

    def warm(self: build_loop.Orchestrator, task: Any) -> list[review_reading.ReadOut]:
        if task.id != "T-002":
            return []
        return [
            review_reading.ReadOut(
                reading=review_reading.Reading(
                    unit="T-001+T-002", include=("src/T-001.py", "src/T-002.py"), members=("T-001", "T-002")
                ),
                extraction=actual_extraction.ExtractionResult(actual_statements=(), coverage={}, actual_digest=""),
                security=security_review.SecurityResult(
                    findings=(finding("SEC-001", "src/T-001.py"), finding("SEC-002", "src/T-002.py"))
                ),
            )
        ]

    repaired: list[str] = []

    def repair(self: build_loop.Orchestrator, task: Any, item: Any, *, where: str) -> None:
        repaired.append(task.id)
        (root / "src" / f"{task.id}.py").write_text("# repaired\n", encoding="utf-8")
        git(root, "commit", "-q", "-am", f"{task.id}: repair")

    monkeypatch.setattr(build_loop.Orchestrator, "_warm_reading", warm)
    monkeypatch.setattr(build_loop.Orchestrator, "_repair", repair)

    assert build(repo_mod.Repo(root)) == common.EXIT_DONE

    assert repaired == ["T-002"], "T-001's finding waits for acceptance, where it is repaired on T-001's slice"
    raw = store_mod.Store(repo_mod.Repo(root)).read_raw("state")
    assert raw is not None
    assert git(root, "log", "-1", "--format=%s", raw["tasks"]["T-002"]["completed_commit"]) == "T-002: repair"


def test_two_tasks_that_operate_are_read_at_once_without_reading_each_other_s_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each is read from its own thread before its run. One findings file at the root for every
    reading meant the second reader overwrote the first one's answer before it was read."""
    import threading

    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    tasks = []
    for tid in ("T-002", "T-003"):
        task = make_task(tid, kind="parallel", claim_ids=["C-001"], scope_include=[f"src/{tid}.py", f"{tid}.out"])
        task["operate"] = [{"name": "full-run", "command": ["sh", "-c", f"echo measured > {tid}.out"]}]
        tasks.append(task)
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        reviews=make_reviews(steps=[REVIEW_STEP]),
        plan=make_plan(tasks=tasks),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    agents = Agents({})
    together = threading.Barrier(2, timeout=20)

    def overlapping(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **kw: object) -> Any:
        answer = agents(cmd, cwd, timeout, **kw)
        if cmd and cmd[0] == "claude" and "(the quality gate's agent step)" in cmd[-1]:
            together.wait()  # both answers are on disk before either is read
        return answer

    monkeypatch.setattr(build_loop, "_run", overlapping)

    assert build(repo_mod.Repo(root)) == common.EXIT_DONE
    assert sorted(ids for [ids] in agents.reviews) == ["T-002", "T-003"]
