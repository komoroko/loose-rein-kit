"""A task's own acceptance criteria, end to end — and the status that says "nobody established this".

Acceptance criteria used to be markdown checkboxes in `docs/tasks/T-NNN.md`, which nothing
parsed. So "the acceptance criteria are met" was an assertion by the agent that had just written
the code, standing beside a quality gate that only ever answered a different question: is this
code *sound*, not did it do what it was *for*.

Moving them into the frozen plan makes them checkable without making them an implementer's knob —
a human freezes the list at gate ③, and the shared DoD still runs unchanged. And it makes room
for the honest third answer: a criterion this loop cannot establish is neither passed nor failed,
and `awaiting-evidence` is the status that says so instead of rounding to `done`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common, evidence, evidence_cmd
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"
GATE = [{"name": "test", "kind": "command", "command": ["true"], "executor_profile": "quality", "retries": 1}]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def build_repo(tmp_path: Path, acceptance: list[dict[str, Any]]) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, max_parallel=2, launch_retries=0),
        plan=make_plan(tasks=[make_task("T-001", kind="parallel", claim_ids=["C-001"], acceptance=acceptance)]),
        state=make_state(phase="build", plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def status_of(repo: repo_mod.Repo, task_id: str = "T-001") -> dict[str, Any]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw["tasks"].get(task_id, {}))


def build(repo: repo_mod.Repo) -> int:
    orchestrator = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    orchestrator.ledger = evidence.Ledger(path=None, enabled=False)
    return orchestrator.run()


def writing_agent(*, extra: dict[str, str] | None = None) -> object:
    """An implementer that writes its file and whatever else the test asked for."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        (where / "impl.py").write_text("# implementation\n", encoding="utf-8")
        for rel, body in (extra or {}).items():
            target = where / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(body, encoding="utf-8")
        return 0, ""

    return _run


# --- criteria the loop can establish itself -------------------------------------


def test_a_command_criterion_that_passes_lets_the_task_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build_repo(
        tmp_path,
        [{"id": "A-1", "statement": "it holds", "evidence": {"kind": "command", "command": ["true"]}}],
    )
    monkeypatch.setattr(build_loop, "_run", writing_agent())

    assert build(repo) == common.EXIT_DONE
    entry = status_of(repo)
    assert entry["status"] == "done"
    assert entry["evidence"]["acceptance"] == [{"id": "A-1", "kind": "command", "reused": False}]


def test_a_failing_criterion_goes_back_through_the_same_retry_machinery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A criterion is the task's own bar, and failing it is a verdict about the code.

    It returns through the channel a red gate step uses, so it inherits the send-back budget
    whole rather than growing a second, subtly different one beside it.
    """
    repo = build_repo(
        tmp_path,
        [{"id": "A-1", "statement": "it holds", "evidence": {"kind": "command", "command": ["false"]}}],
    )
    monkeypatch.setattr(build_loop, "_run", writing_agent())

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo)["status"] == "blocked"


def test_an_artifact_criterion_requires_the_file_to_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = build_repo(
        tmp_path,
        [
            {
                "id": "A-1",
                "statement": "the schema is published",
                "evidence": {"kind": "artifact", "paths": ["api.json"]},
            }
        ],
    )
    monkeypatch.setattr(build_loop, "_run", writing_agent())
    assert build(repo) == common.EXIT_HUMAN_NEEDED

    # The same task, with the implementer actually producing the artifact.
    repo2 = build_repo(
        tmp_path / "second",
        [
            {
                "id": "A-1",
                "statement": "the schema is published",
                "evidence": {"kind": "artifact", "paths": ["api.json"]},
            }
        ],
    )
    monkeypatch.setattr(build_loop, "_run", writing_agent(extra={"api.json": "{}\n"}))
    assert build(repo2) == common.EXIT_DONE


def test_a_prose_criterion_establishes_nothing_and_blocks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Most criteria are judgement calls. Saying so is honest; gate ④ is where a human reads them."""
    repo = build_repo(tmp_path, [{"id": "A-1", "statement": "the error message is understandable"}])
    monkeypatch.setattr(build_loop, "_run", writing_agent())

    assert build(repo) == common.EXIT_DONE
    assert "acceptance" not in status_of(repo)["evidence"]


# --- the criterion nobody here can establish ------------------------------------


EXTERNAL = [{"id": "A-1", "statement": "it works against staging", "evidence": {"kind": "external"}}]


def test_external_evidence_leaves_the_task_awaiting_not_done(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole reason this status exists.

    The code passed everything anything here can ask it. What is missing is an observation the
    loop was never able to make — and `done` would be claiming somebody made it.
    """
    repo = build_repo(tmp_path, EXTERNAL)
    monkeypatch.setattr(build_loop, "_run", writing_agent())

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo)["status"] == "awaiting-evidence"
    kinds = [str(e.detail.get("kind", "")) for e in store_mod.Store(repo).read_events()]
    assert "awaiting_evidence" in kinds


def test_a_recorded_observation_finishes_the_task_without_rebuilding_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work has already landed and already passed. Only the observation was missing.

    Re-running an implementer over merged, verified code would be paying a model to redo work
    whose one missing piece was a person looking at a screen — so the next run promotes the task
    on the spot, having checked the tree the observation names has not moved.
    """
    repo = build_repo(tmp_path, EXTERNAL)
    monkeypatch.setattr(build_loop, "_run", writing_agent())
    build(repo)
    assert status_of(repo)["status"] == "awaiting-evidence"

    evidence_cmd.record(repo, "T-001", "A-1", "checked the staging deploy by hand")

    launches: list[list[str]] = []

    def watching(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if cmd and cmd[0] == "claude":
            launches.append(cmd)
            return 0, ""
        return common.run(cmd, cwd, timeout)

    monkeypatch.setattr(build_loop, "_run", watching)
    assert build(repo) == common.EXIT_DONE
    assert status_of(repo)["status"] == "done"
    assert launches == [], "the task was rebuilt for want of an observation somebody had already made"


def test_an_observation_does_not_outlive_the_code_it_was_about(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A tree that moved is a different claim, so the record stops matching and the task waits again."""
    repo = build_repo(tmp_path, EXTERNAL)
    monkeypatch.setattr(build_loop, "_run", writing_agent())
    build(repo)
    evidence_cmd.record(repo, "T-001", "A-1", "checked the staging deploy by hand")

    # Somebody changes the code after the observation was made. It is about other bytes now.
    (repo.root / "impl.py").write_text("# changed after the observation\n", encoding="utf-8")
    git(repo.root, "commit", "-qam", "a later change")

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo)["status"] == "awaiting-evidence"


# --- who may record one ----------------------------------------------------------


def test_recording_refuses_a_criterion_the_loop_could_have_run(tmp_path: Path) -> None:
    """Overriding a check the machine can make is not "evidence the loop cannot obtain"."""
    repo = build_repo(
        tmp_path,
        [{"id": "A-1", "statement": "it holds", "evidence": {"kind": "command", "command": ["true"]}}],
    )
    with pytest.raises(evidence_cmd.EvidenceError, match="not 'external'"):
        evidence_cmd.record(repo, "T-001", "A-1", "trust me")


def test_recording_refuses_a_criterion_the_plan_never_declared(tmp_path: Path) -> None:
    repo = build_repo(tmp_path, EXTERNAL)
    with pytest.raises(evidence_cmd.EvidenceError, match="declares no acceptance criterion"):
        evidence_cmd.record(repo, "T-001", "A-9", "about something that does not exist")


def test_recording_needs_a_note(tmp_path: Path) -> None:
    """A record with nothing in it says somebody typed, not what they saw."""
    repo = build_repo(tmp_path, EXTERNAL)
    with pytest.raises(evidence_cmd.EvidenceError, match="records that somebody clicked"):
        evidence_cmd.record(repo, "T-001", "A-1", "   ")


def test_show_lists_what_is_waiting_and_what_was_observed(tmp_path: Path) -> None:
    repo = build_repo(tmp_path, EXTERNAL)
    assert evidence_cmd.outstanding(repo) == [
        {
            "task": "T-001",
            "id": "A-1",
            "statement": "it works against staging",
            "status": "todo",
            "recorded": False,
        }
    ]

    evidence_cmd.record(repo, "T-001", "A-1", "checked it")
    assert evidence_cmd.outstanding(repo)[0]["recorded"] is True
