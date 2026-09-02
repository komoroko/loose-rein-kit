"""Tests for build_loop.py — the deterministic half of the implementation phase.

The line this file defends is the one in the module docstring: **scheduling is decided in
code, not in a prompt.** Which tasks run, at what parallelism, in what order they merge, and
when the loop stops are all pure functions of the graph, so two runs of the same plan schedule
identically. The LLM writes the code; it does not decide what happens next.

The other half is what the loop refuses to claim. When the tasks finish it says what it
established (the gate passed) and what it did *not* (that the code does what the plan says),
and hands over to the review pipeline.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rein import (
    adapters,
    build_loop,
    common,
    conflict,
    dag,
    digests,
    dossier,
    evidence,
    executors,
    faults,
    models,
    pr_stack,
    review_policy,
    review_reading,
)
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod
from tests._support import (
    agent_envelope,
    agent_output,
    fake_git,
    make_config,
    make_plan,
    make_state,
    make_task,
    seed_repo,
)


def graph_of(done: tuple[str, ...] = ()) -> dag.Graph:
    def status(tid: str) -> str:
        return "done" if tid in done else "todo"

    return dag.Graph.from_tasks(
        [
            dag.Task(id="T-001", title="base", kind="foundation", status=status("T-001")),
            dag.Task(id="T-002", title="leaf A", kind="parallel", blocked_by=("T-001",), status=status("T-002")),
            dag.Task(id="T-003", title="leaf B", kind="parallel", blocked_by=("T-001",), status=status("T-003")),
            dag.Task(id="T-004", title="leaf C", kind="parallel", blocked_by=("T-001",), status=status("T-004")),
            dag.Task(id="T-005", title="leaf D", kind="parallel", blocked_by=("T-001",), status=status("T-005")),
        ]
    )


def build_repo(tmp_path: Path, **kwargs: object) -> Path:
    """A repo ready to build: gate 3 approved, plan frozen, four tasks."""
    kwargs.setdefault(
        "plan",
        make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
    )
    kwargs.setdefault("state", make_state(phase="build", plan_status="frozen"))
    seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
    return tmp_path


# --- scheduling (pure, deterministic) -----------------------------------------


def test_a_foundation_task_is_finalized_serially() -> None:
    batch = build_loop.plan_batch(graph_of(), max_parallel=3)
    assert batch == ("serial", [graph_of().get("T-001")])


def test_leaves_run_in_parallel_capped_at_max() -> None:
    mode, tasks = build_loop.plan_batch(graph_of(done=("T-001",)), max_parallel=3)  # type: ignore[misc]
    assert mode == "parallel"
    assert [t.id for t in tasks] == ["T-002", "T-003", "T-004"]  # T-005 waits for the next iteration


def test_an_empty_frontier_returns_none() -> None:
    assert build_loop.plan_batch(graph_of(done=("T-001", "T-002", "T-003", "T-004", "T-005")), 3) is None


def test_the_batch_is_the_same_every_time() -> None:
    """Two runs of the same plan must schedule identically, or the loop is something you watch
    rather than something you can predict."""
    graph = graph_of(done=("T-001",))
    first = build_loop.plan_batch(graph, 2)
    for _ in range(5):
        assert build_loop.plan_batch(graph, 2) == first


def deep_graph(done: tuple[str, ...] = ()) -> dag.Graph:
    """Five layers, fanning out and back in, so a batch has plenty of room to get it wrong."""
    edges = {
        "T-001": [],
        "T-002": ["T-001"], "T-003": ["T-001"], "T-004": ["T-001"],
        "T-005": ["T-002"], "T-006": ["T-002", "T-003"], "T-007": ["T-004"],
        "T-008": ["T-005", "T-006", "T-007"],
        "T-009": ["T-008"], "T-010": ["T-008"],
    }  # fmt: skip
    return dag.Graph.from_tasks(
        [
            dag.Task(
                tid,
                f"task {tid}",
                "foundation" if tid == "T-001" else "parallel",
                tuple(deps),
                "done" if tid in done else "todo",
            )
            for tid, deps in edges.items()
        ]
    )


@pytest.mark.parametrize("max_parallel", [1, 3, 100])
def test_a_batch_never_starts_a_task_whose_upstream_is_unfinished(max_parallel: int) -> None:
    """The ordering guarantee, at any parallelism: raising max_parallel widens a batch, never
    the set it may draw from. Consuming the graph layer by layer must never hand out a task with
    an unfinished dependency, nor two tasks from the same batch that depend on each other."""
    done: tuple[str, ...] = ()
    graph = deep_graph()
    for _ in range(len(graph.tasks) + 1):
        batch = build_loop.plan_batch(deep_graph(done), max_parallel)
        if batch is None:
            break
        _, tasks = batch
        assert len(tasks) <= max_parallel
        ids = {t.id for t in tasks}
        for task in tasks:
            assert set(task.blocked_by) <= set(done), f"{task.id} started before {task.blocked_by}"
            assert not (set(task.blocked_by) & ids), f"{task.id} shares a batch with its own dependency"
        done += tuple(ids)  # a batch is only ever marked done after it has passed its gate
    assert set(done) == {t.id for t in graph.tasks}, "every task was reachable"


# --- config ------------------------------------------------------------------


def test_config_normalizes_the_quality_gate(tmp_path: Path) -> None:
    config = build_loop.Config.from_models(models.Config(make_config()))
    assert [s.name for s in config.steps] == ["test", "check"]
    assert config.steps[0].command == ("make", "test")
    assert config.gate_cmds == ["make test", "make check"]


def test_an_unknown_adapter_is_refused_up_front() -> None:
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "mystery"  # type: ignore[index]
    with pytest.raises(adapters.LaunchRefused, match="does not know how to launch"):
        build_loop.Config.from_models(models.Config(config))


def test_worktree_isolation_is_not_optional() -> None:
    """Parallel leaves writing one tree is how two tasks' changes end up attributed to one
    review, so there is no knob that turns it off."""
    config = build_loop.Config.from_models(models.Config(make_config()))
    assert config.worktree_enabled is True


def test_the_integration_gate_is_not_a_knob() -> None:
    """Each leaf was green only in isolation, so a batch that merged two or more has never
    been verified as one tree — there is nothing to opt out of."""
    assert not hasattr(build_loop.Config, "integration_gate")
    assert "integration_gate" not in json.dumps(make_config())


def test_a_step_command_is_an_argv_list_not_a_shell_string() -> None:
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    assert step.display == "make test"
    assert step.runnable
    assert not build_loop.GateStep(name="smoke", kind="command").runnable


# --- an agent step launches the role it declares -------------------------------
#
# The config schema requires `agent_role` on an agent step, and nothing read it:
# the step launched `agents.implementer`'s adapter while calling itself `code_reviewer`. Two
# roles an operator had configured separately were one process — the reviewer asked for a second
# opinion was the same model that had just written the code. The template's own config sets both
# roles to `claude`, so nothing observable changed and the defect survived; these tests pin the
# roles to *different* adapters, which is the only way the difference shows up at all.

_AGENT_GATE = [
    {"name": "review", "kind": "agent", "agent_role": "code_reviewer", "retries": 1, "required": True},
]


def _config_with_split_adapters() -> build_loop.Config:
    raw = make_config(quality_gate=list(_AGENT_GATE))
    raw["agents"]["implementer"] = {"adapter": "claude"}
    raw["agents"]["code_reviewer"] = {"adapter": "codex"}
    return build_loop.Config.from_models(models.Config(raw))


def test_an_agent_step_resolves_its_own_role_not_the_implementers() -> None:
    config = _config_with_split_adapters()
    step = config.steps[0]
    assert step.agent_role == "code_reviewer"
    assert step.agent_argv == adapters.ADAPTER_TABLE["codex"].launch_argv()
    # The implementer's own adapter is untouched — the two are resolved independently.
    assert config.adapter_argv == adapters.ADAPTER_TABLE["claude"].launch_argv()


def reviewing(root: Path, findings: list[dict[str, str]], launched: list[list[str]]) -> object:
    """A fake reviewer that writes the findings file the step now reads its verdict from."""

    def fake_run(cmd: list[str], cwd: str | None = None, **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        target = dossier.findings_path(cwd or str(root), "T-001")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"findings": findings}), encoding="utf-8")
        return 0, agent_envelope("")

    return fake_run


def test_an_agent_step_launches_with_its_roles_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_repo(tmp_path)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo_mod.Repo(root))
    monkeypatch.setattr(orch, "_fingerprint", lambda cwd: "sha256:" + "0" * 64)
    launched: list[list[str]] = []
    monkeypatch.setattr(build_loop, "_run", reviewing(root, [], launched))

    orch._run_agent_step(orch.config.steps[0], dag.Task(id="T-001", title="base", kind="foundation"), str(root), "")

    assert launched, "the agent step never launched anything"
    assert tuple(launched[0][:2]) == adapters.ADAPTER_TABLE["codex"].launch_argv(), (
        f"the agent step launched {launched[0][:2]} — it must use agents.code_reviewer, not agents.implementer"
    )


def test_the_reviewer_is_launched_without_write_access(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It reports; it does not repair.

    `codex exec` is read-only unless told otherwise, and the reviewer is the one launch that
    should stay that way. Handing it `--sandbox workspace-write` let one participant both judge a
    change and edit the judgement away — and moved the tree underneath the gate, so every
    already-passed step had to be re-run behind it.
    """
    root = build_repo(tmp_path)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo_mod.Repo(root))
    monkeypatch.setattr(orch, "_fingerprint", lambda cwd: "sha256:" + "0" * 64)
    launched: list[list[str]] = []
    monkeypatch.setattr(build_loop, "_run", reviewing(root, [], launched))

    orch._run_agent_step(orch.config.steps[0], dag.Task(id="T-001", title="base", kind="foundation"), str(root), "")

    assert "--sandbox" not in launched[0], "the reviewer was launched able to change the code it is judging"


def test_a_must_fix_finding_goes_to_the_implementer_and_the_reviewer_looks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The round the separation buys: somebody other than the reviewer has to act on a finding."""
    root = build_repo(tmp_path)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo_mod.Repo(root))
    monkeypatch.setattr(orch, "_fingerprint", lambda cwd: "sha256:" + "0" * 64)
    launched: list[list[str]] = []
    rounds = iter([[{"severity": "must_fix", "statement": "the guard is gone", "anchor": "src/x.py:4"}], []])

    def fake_run(cmd: list[str], cwd: str | None = None, **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        if cmd[0] == "codex":  # the reviewer's adapter in this config
            target = dossier.findings_path(cwd or str(root), "T-001")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"findings": next(rounds)}), encoding="utf-8")
        return 0, agent_output(cmd)

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch._run_agent_step(orch.config.steps[0], dag.Task(id="T-001", title="base", kind="foundation"), str(root), "")

    adapters = [cmd[0] for cmd in launched]
    assert adapters == ["codex", "claude", "codex"], (
        "expected review → implementer fix → review again, got " + " → ".join(adapters)
    )
    assert "the guard is gone" in launched[1][-1], "the fixer was not told what the reviewer found"


def test_an_unreadable_review_is_not_a_review_that_found_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode a silent pass would hide: a reviewer that said nothing readable."""
    root = build_repo(tmp_path)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo_mod.Repo(root))
    monkeypatch.setattr(orch, "_fingerprint", lambda cwd: "sha256:" + "0" * 64)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, **kwargs: (0, "I had a good look, honestly"))

    with pytest.raises(common.StopLoop, match="wrote no findings file"):
        orch._run_agent_step(orch.config.steps[0], dag.Task(id="T-001", title="base", kind="foundation"), str(root), "")


def test_a_claude_launch_gains_no_sandbox_flags() -> None:
    """The flags are one CLI's own vocabulary, not a portable concept — nothing else grows them."""
    assert adapters.write_flags(adapters.ADAPTER_TABLE["claude"].launch_argv()) == ()
    assert adapters.write_flags(adapters.ADAPTER_TABLE["gemini"].launch_argv()) == ()
    assert adapters.write_flags(()) == ()


def test_the_review_transport_is_not_given_write_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A reviewer that cannot write is the point. The transport launches the bare adapter argv, so
    the read-only default is the one the reviewer wants — assert it stays that way."""
    from rein import common, review_transport

    raw = make_config()
    raw["agents"]["code_reviewer"] = {"adapter": "codex"}
    repo = repo_mod.Repo(seed_repo(tmp_path, config=raw))
    launched: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        return 0, agent_output(cmd, "{}")

    # The transport resolves `common.run` at call time, so patching the module both share is what
    # actually intercepts the launch — reaching through `review_transport.common` is the same
    # object by accident of import, and mypy is right that it is not part of its interface.
    monkeypatch.setattr(common, "run", fake_run)
    ledger = usage_mod.Ledger()
    review_transport._adapter_reviewer(repo, "code_reviewer", ledger=ledger)({"request": "x"})
    assert launched[0] == list(adapters.ADAPTER_TABLE["codex"].launch_argv())


def test_an_unlaunchable_role_adapter_stops_the_build_before_it_starts() -> None:
    """Refused up front, not at the first step that needed it — halfway through a task."""
    raw = make_config(quality_gate=list(_AGENT_GATE))
    raw["agents"]["code_reviewer"] = {"adapter": "nonesuch"}
    with pytest.raises(adapters.LaunchRefused, match="agents.code_reviewer.adapter"):
        build_loop.Config.from_models(models.Config(raw))


# --- task status goes through the Central Store -------------------------------


def test_a_status_change_lands_with_the_event_that_explains_it(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    build_loop.set_task_status(repo, "T-001", "done")

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None and state.task_status["T-001"] == "done"
    assert [e.event for e in store.read_events()] == ["task_completed"]


def test_the_commit_that_completed_a_task_is_recorded_once(tmp_path: Path) -> None:
    """`completed_commit` was in the schema and written by nobody, and the commit lived in a
    *second* `task_completed` event — so everything that counts events counted every finished task
    twice (`rein events --summary`, the resume packet's "tasks completed: N")."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "done", commit="a" * 40)

    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-001"]["completed_commit"] == "a" * 40
    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["task_completed"]
    assert events[0].detail["commit"] == "a" * 40


def test_a_task_that_leaves_done_leaves_its_commit_behind(tmp_path: Path) -> None:
    """The field says which commit *completed* the task. Sent back for revision, it has none —
    and a stale hash beside `needs-revision` would name work the board no longer accepts."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "done", commit="b" * 40)
    build_loop.set_task_status(repo, "T-001", "needs-revision")
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and "completed_commit" not in raw["tasks"]["T-001"]


@pytest.mark.parametrize("commit", ["", "dry-run", "HEAD", "Z" * 40, "abc"])
def test_only_something_shaped_like_a_commit_is_written(tmp_path: Path, commit: str) -> None:
    """`ws.head()` returns "" when git is unavailable. Writing that through would put a value in
    state.yaml that `$defs/commit` rejects, and the next read of the SSOT would fail validation."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "done", commit=commit)
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and "completed_commit" not in raw["tasks"]["T-001"]


def test_each_merged_leaf_records_its_own_merge_commit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A batch's leaves merge one after another, so a hash read once at the end of the batch names
    the last merge for every member of it — and an integration gate that commits a fix moves it
    further still. Each leaf's commit is read at its own merge."""
    loop = orchestrator(tmp_path)
    tasks = [dag.Task(id=f"T-00{n}", title=f"leaf {n}", kind="parallel") for n in (1, 2, 3)]
    heads = iter(["a" * 40, "b" * 40, "c" * 40, "d" * 40])
    recorded: dict[str, str] = {}

    monkeypatch.setattr(loop, "_set_status", lambda tid, status, commit="": recorded.update({tid: commit}))
    monkeypatch.setattr(loop.ws, "add_worktree", lambda task_id, restore_from="": f"build/x-{task_id}")
    monkeypatch.setattr(loop, "_safe_run_task", lambda task, cwd: build_loop.LeafOutcome(ok=True))
    monkeypatch.setattr(loop.ws, "finalize_commit", lambda cwd, message: True)
    monkeypatch.setattr(loop, "_gate_violations", lambda paths: [])
    monkeypatch.setattr(loop.ws, "branch_changed_paths", lambda task_id, cwd="": [])
    monkeypatch.setattr(loop, "merge_leaf", lambda task, branch: True)
    monkeypatch.setattr(loop, "_integration_gate", lambda merged: (True, ""))
    monkeypatch.setattr(loop.ws, "landed", lambda task_id: next(heads))

    loop._consume_parallel(tasks)

    assert recorded == {"T-001": "a" * 40, "T-002": "b" * 40, "T-003": "c" * 40}


def test_a_leaf_gate_violation_blocks_without_merging(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A worktree that edited a gate-guarded path is blocked and never merged — caught by the
    time `_safe_run_task` returns, not only at the old merge-time check."""
    loop = orchestrator(tmp_path)
    task = dag.Task(id="T-002", title="leaf", kind="parallel")
    violations = [("docs/10-requirements.md", "gate 'requirements' is pending")]

    monkeypatch.setattr(loop.ws, "add_worktree", lambda task_id, restore_from="": f"build/x-{task_id}")
    monkeypatch.setattr(
        loop, "_safe_run_task", lambda t, cwd: build_loop.LeafOutcome(ok=False, log="x", violations=violations)
    )
    monkeypatch.setattr(loop, "merge_leaf", lambda t, branch: pytest.fail("must not merge a violating leaf"))  # noqa: ARG005
    cleaned: list[str] = []
    monkeypatch.setattr(loop, "_cleanup_worktree", lambda t: cleaned.append(t.id))
    escalated: list[tuple[object, ...]] = []
    monkeypatch.setattr(loop, "_escalate_gate_violation", lambda *a: escalated.append(a))

    with pytest.raises(build_loop.StopLoop):
        loop._consume_parallel([task])

    raw = store_mod.Store(loop.repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-002"]["status"] == "blocked"
    assert cleaned == ["T-002"]
    assert escalated == [("T-002", "its worktree changes (caught before merge)", violations)]


def test_starting_a_task_counts_an_attempt(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "in-progress")
    build_loop.set_task_status(repo, "T-001", "in-progress")
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-001"]["attempts"] == 2


def test_a_block_records_which_step_went_red_and_what_was_left(tmp_path: Path) -> None:
    """The terminal `task_failed` used to carry a prose note and nothing a reader could sort or
    count by — the step name and the budget lived only on the per-attempt records, and a task that
    blocked on its first round produced no per-attempt record at all. Nothing new is discovered
    here: the facts are in the handoff written by this same transaction."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.record_attempt_failure(
        repo, "T-001", failed_step="test", failure_summary="2 failed", retries_left={"test": 0, "check": 2}
    )
    build_loop.set_task_status(repo, "T-001", "blocked", note="out of retries")

    failures = [e for e in store_mod.Store(repo).read_events() if e.event == "task_failed"]
    assert failures[-1].detail["step"] == "test"
    assert failures[-1].detail["retries_left"] == 0
    assert failures[-1].detail["note"] == "out of retries"


def test_a_block_with_nothing_recorded_says_nothing_it_does_not_know(tmp_path: Path) -> None:
    """No handoff, no step — an event that invented one would be worse than one that is silent."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "blocked", note="the implementer reported blocked")
    failed = [e for e in store_mod.Store(repo).read_events() if e.event == "task_failed"][-1]
    assert "step" not in failed.detail and "retries_left" not in failed.detail


def test_a_completion_carries_no_failure_detail(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.record_attempt_failure(repo, "T-001", failed_step="test", failure_summary="x", retries_left={"test": 1})
    build_loop.set_task_status(repo, "T-001", "done", commit="a" * 40)
    done = [e for e in store_mod.Store(repo).read_events() if e.event == "task_completed"][-1]
    assert "step" not in done.detail


def test_an_off_vocabulary_status_is_refused(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    with pytest.raises(ValueError, match="unknown task status"):
        build_loop.set_task_status(repo, "T-001", "nearly")


# --- preconditions ------------------------------------------------------------


def test_the_loop_refuses_while_gate_three_is_pending(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path, state=make_state(gates={"tasks": "pending"}, phase="tasks", plan_status="draft"))
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 2
    assert "no frozen plan to build against" in capsys.readouterr().err


def test_the_loop_refuses_to_build_against_a_draft_plan(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Gate 3's approval is what freezes the plan; building against a draft would implement a
    plan nobody signed for."""
    root = build_repo(tmp_path, state=make_state(phase="build", plan_status="draft"))
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 2
    assert "not 'frozen'" in capsys.readouterr().err


def test_approving_gate_three_is_what_lets_the_loop_start(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The two halves of the precondition above, joined.

    Regression for the gap where nothing in the codebase ever wrote `plan.status: frozen`: a
    repository whose gate ③ was properly approved still could not build, because the freeze
    existed only in prose. Asserting the refusal (the test above) passed happily while the
    approval that clears it did not exist — so the pair is what pins the behaviour.
    """
    from rein import approve
    from rein import repo as repo_mod

    root = build_repo(
        tmp_path,
        state=make_state(gates={"tasks": "pending", "build": "pending"}, phase="tasks", plan_status="draft"),
    )
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 2

    repo = repo_mod.Repo(root)
    approve.record_approval(repo, "tasks", approve.approval_subject(repo, "tasks"))
    capsys.readouterr()  # drop the refusal above

    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 0
    assert "not 'frozen'" not in capsys.readouterr().err


def test_a_command_step_with_no_command_cannot_be_expressed() -> None:
    """An empty `run` would have to fail fast at build time. The schema refuses the shape
    outright instead, so the contradictory DoD never reaches the loop — the scaffold ships an
    explicit placeholder command rather than a silent skip."""
    config = make_config(quality_gate=[{"name": "smoke", "kind": "command", "executor_profile": "quality"}])
    assert any("'command' is a required property" in e for e in models.schema_errors(config, "config"))


def test_a_leaf_worktree_may_not_drive_a_build(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    repo._cache["git_common_dir"] = tmp_path / "elsewhere" / ".git"
    monkey = repo_mod.get
    try:
        repo_mod.get = lambda *_a, **_k: repo  # type: ignore[assignment]
        assert build_loop.main(["--repo", str(root)]) == 2
    finally:
        repo_mod.get = monkey  # type: ignore[assignment]
    assert "linked worktree" in capsys.readouterr().err


# --- the dry run is strictly read-only ----------------------------------------


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    store = store_mod.Store(repo)
    before = store.document_digest("state")

    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 0

    assert store.document_digest("state") == before
    assert store.read_events() == []
    assert not store.build_lock.exists()


def test_a_dry_run_walks_the_whole_graph(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 0
    out = capsys.readouterr().out
    assert "T-001" in out and "T-002" in out
    assert "all tasks done" in out


# --- what the loop hands over -------------------------------------------------


def test_the_handover_says_what_was_not_established(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Green tests plus an AI's summary is not evidence that the code does what the plan says,
    and the loop must not let the phrasing imply otherwise."""
    root = build_repo(tmp_path)
    build_loop.main(["--dry-run", "--repo", str(root)])
    out = capsys.readouterr().out
    assert "did NOT establish" in out
    assert "rein review generate" in out
    assert "cannot open gate 4" in out


def test_the_handover_does_not_offer_to_approve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    build_loop.main(["--dry-run", "--repo", str(root)])
    out = capsys.readouterr().out
    assert "security review" not in out.lower()  # not this step's gate-4 evidence
    assert "interactive terminal" in out


# --- the build lock -----------------------------------------------------------


def test_two_runs_cannot_overlap(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """…and says so as retry-later, not as a refusal.

    Nothing is broken when another run holds the repository, and a supervisor restarting the
    build races the previous process's shutdown often enough that reading it as fatal would end
    the loop for good.
    """
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    store_mod.ensure_private_dir(store_mod.Store(repo).runtime)
    with build_loop.build_lock(repo):
        config = build_loop.Config.load(repo)
        assert build_loop.Orchestrator(config, dry_run=False, repo=repo).run() == common.EXIT_RETRY_LATER
    assert "holds the lock" in capsys.readouterr().err


def test_the_lock_lives_outside_the_worktree(tmp_path: Path) -> None:
    """A per-worktree lock inode meant two leaves could each hold "the" lock (plan §11.1)."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    assert not str(store_mod.Store(repo).build_lock).startswith(str(tmp_path / ".rein"))


# --- the quality-gate pipeline ------------------------------------------------


def orchestrator(tmp_path: Path, **kwargs: object) -> build_loop.Orchestrator:
    root = build_repo(tmp_path, **kwargs)
    repo = repo_mod.Repo(root)
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)


def test_a_command_step_passes_on_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git())
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    assert loop._run_cmd_step(step, cwd=str(tmp_path)) == ""


def test_a_command_step_already_green_on_this_tree_is_not_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of the ledger, at the one place every gate command goes through.

    A retry that failed only in `check` re-ran `test` from the top, and the integration gate
    re-ran a DoD a leaf had already verified on the identical tree. Both are the same command
    against the same content in the same image — one fact, established once.
    """
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "a" * 64)
    ran: list[tuple[str, ...]] = []

    def counting(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        ran.append(tuple(cmd))
        return 0, ""

    monkeypatch.setattr(common, "run", counting)
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))

    assert loop._run_cmd_step(step, cwd=str(tmp_path)) == ""
    assert loop._run_cmd_step(step, cwd=str(tmp_path)) == ""
    assert ran.count(("make", "test")) == 1


def test_a_command_step_is_re_run_when_the_tree_moved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The subject is content. A tree that moved by a byte has never been checked."""
    loop = orchestrator(tmp_path)
    trees = iter(["sha256:" + "a" * 64, "sha256:" + "b" * 64])
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: next(trees))
    ran: list[tuple[str, ...]] = []

    def counting(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        ran.append(tuple(cmd))
        return 0, ""

    monkeypatch.setattr(common, "run", counting)
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))

    loop._run_cmd_step(step, cwd=str(tmp_path))
    loop._run_cmd_step(step, cwd=str(tmp_path))
    assert ran.count(("make", "test")) == 2


def test_a_red_command_step_is_never_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Only greens are recorded: a cached red would let one broken afternoon stand as a verdict."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "c" * 64)
    monkeypatch.setattr(common, "run", fake_git({("make", "test"): (1, "tests/x.py::t FAILED")}))
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))

    assert loop._run_cmd_step(step, cwd=str(tmp_path)) != ""
    assert not loop.ledger.hit(
        evidence.KIND_GATE_STEP, "sha256:" + "c" * 64, loop._step_tool(step, loop._profile_for(step))
    )


def test_a_command_step_summarizes_its_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git({("make", "test"): (1, "tests/x.py::t FAILED")}))
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    summary = loop._run_cmd_step(step, cwd=str(tmp_path))
    assert "make test (rc=1)" in summary and "FAILED" in summary


def test_a_step_runs_its_argv_verbatim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No shell, and no shlex-splitting of user text: an argument with a space stays one
    argument, and a pipe cannot appear by accident."""
    record: list[list[str]] = []
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git(record=record))
    step = build_loop.GateStep(name="test", kind="command", command=("pytest", "-k", "a b"))
    loop._run_cmd_step(step, cwd=str(tmp_path))
    assert record[-1] == ["pytest", "-k", "a b"]


def test_the_task_pipeline_is_the_configured_dod(tmp_path: Path) -> None:
    """The ticket's own `test:` command is deliberately not prepended. A task's extra judgement
    boundary is the shared DoD, not a command the implementer could have chosen."""
    loop = orchestrator(tmp_path)
    task = dag.Task(id="T-001", title="t", kind="foundation")
    assert [s.name for s in loop._steps_for(task)] == ["test", "check"]


# --- path-scoped quality-gate steps (frozen at gate 3, never an implementer's choice) ----------


def _paths_scoped_config() -> dict[str, object]:
    return make_config(
        quality_gate=[
            {
                "name": "test",
                "kind": "command",
                "command": ["make", "test"],
                "executor_profile": "quality",
                "retries": 2,
                "required": True,
            },
            {
                "name": "web",
                "kind": "command",
                "command": ["npm", "test"],
                "executor_profile": "quality",
                "retries": 2,
                "paths": ["web/*"],
            },
        ]
    )


def test_a_paths_scoped_step_is_skipped_when_the_diff_does_not_touch_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = orchestrator(tmp_path, config=_paths_scoped_config())
    monkeypatch.setattr(loop, "_review_scope", lambda task, cwd, base: (["backend/app.py"], ""))
    task = dag.Task(id="T-001", title="t", kind="parallel")
    assert [s.name for s in loop._steps_for(task, cwd="/tmp/leaf", base="")] == ["test"]


def test_a_paths_scoped_step_runs_when_the_diff_touches_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path, config=_paths_scoped_config())
    monkeypatch.setattr(loop, "_review_scope", lambda task, cwd, base: (["web/app.tsx"], ""))
    task = dag.Task(id="T-001", title="t", kind="parallel")
    assert [s.name for s in loop._steps_for(task, cwd="/tmp/leaf", base="")] == ["test", "web"]


def test_paths_scoping_never_activates_without_a_cwd(tmp_path: Path) -> None:
    """No `cwd` (the pre-existing call shape) means no diff was computed — degrade to the full
    DoD rather than silently narrowing on nothing."""
    loop = orchestrator(tmp_path, config=_paths_scoped_config())
    task = dag.Task(id="T-001", title="t", kind="parallel")
    assert [s.name for s in loop._steps_for(task)] == ["test", "web"]


def test_paths_scoping_runs_everything_when_the_diff_is_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty diff (a fresh worktree, dry-run) must not read as an empty scope — that would
    silently skip a step nobody decided to skip."""
    loop = orchestrator(tmp_path, config=_paths_scoped_config())
    monkeypatch.setattr(loop, "_review_scope", lambda task, cwd, base: ([], ""))
    task = dag.Task(id="T-001", title="t", kind="parallel")
    assert [s.name for s in loop._steps_for(task, cwd="/tmp/leaf", base="")] == ["test", "web"]


def test_gate_step_matches_paths_is_fnmatch_style(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path, config=_paths_scoped_config())
    web_step = next(s for s in loop.config.steps if s.name == "web")
    assert web_step.matches_paths(["web/app.tsx"])
    assert not web_step.matches_paths(["backend/app.py"])
    assert web_step.matches_paths([])  # empty diff is unresolved, not "resolved to nothing" — fail open


def test_a_gate_violation_is_caught_right_after_the_implementer_spending_no_retry_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worktree edit to a gate-guarded path must not wait for a cmd step to fail (spending its
    retry budget) or for merge — it is checked, and raised, before the pipeline even runs."""
    loop = orchestrator(tmp_path)
    violations = [("docs/10-requirements.md", "gate 'requirements' is pending")]
    monkeypatch.setattr(loop, "_invoke_implementer", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_review_scope", lambda task, cwd, base: (["docs/10-requirements.md"], ""))
    monkeypatch.setattr(loop, "_gate_violations", lambda paths: violations)
    monkeypatch.setattr(loop, "_run_pipeline", lambda *a, **k: pytest.fail("must not reach the pipeline"))  # noqa: ARG005

    task = dag.Task(id="T-001", title="base", kind="foundation")
    with pytest.raises(build_loop.GateViolationFault) as caught:
        loop._run_task_to_done(task, cwd=str(tmp_path), base="a" * 40)
    assert caught.value.violations == violations


# --- the implementer prompt ---------------------------------------------------


def test_the_prompt_names_the_claims_the_task_answers_for(tmp_path: Path) -> None:
    from rein import build_prompts

    task = dag.Task(id="T-002", title="retry", kind="parallel", claim_ids=("C-002",))
    prompt = build_prompts.implementer_prompt(task, "", gate_cmds=["make test"], has_baseline=False)
    assert "C-002" in prompt
    assert "answerable for" in prompt


def test_the_prompt_falls_back_to_the_whole_design_without_claims() -> None:
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.implementer_prompt(task, "", gate_cmds=["make test"], has_baseline=False)
    assert "docs/20-design.md" in prompt


def test_a_previous_failure_is_passed_through_already_summarized() -> None:
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.implementer_prompt(
        task, "$ make test (rc=1)\nE  assert 1 == 2", gate_cmds=["make test"], has_baseline=False
    )
    assert "assert 1 == 2" in prompt


# --- handoff: what the next attempt inherits ----------------------------------


def test_a_failed_attempt_leaves_the_next_one_something_to_go_on(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    build_loop.record_attempt_failure(
        loop.repo, "T-001", failed_step="check", failure_summary="E  ruff: unused import", retries_left={"check": 1}
    )
    handoff = build_loop.read_task_handoff(store_mod.Store(loop.repo).read_state(), "T-001")
    assert handoff["failed_step"] == "check"
    assert handoff["failure_summary"] == "E  ruff: unused import"
    assert handoff["retries_left"] == {"check": 1}
    assert [e.event for e in store_mod.Store(loop.repo).read_events()] == ["task_failed"]


def test_a_restarted_attempt_does_not_get_its_retry_budget_back(tmp_path: Path) -> None:
    """The point of persisting the budget: a run killed mid-task and restarted came back with a
    full allowance every time, so a task that can never pass could burn retries forever."""
    loop = orchestrator(tmp_path)
    build_loop.record_attempt_failure(
        loop.repo, "T-001", failed_step="check", failure_summary="still red", retries_left={"check": 0}
    )
    inherited = build_loop.read_task_handoff(store_mod.Store(loop.repo).read_state(), "T-001")["retries_left"]
    assert inherited == {"check": 0}


def test_a_handoff_does_not_outlive_the_task_it_describes(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    build_loop.record_attempt_failure(
        loop.repo, "T-001", failed_step="test", failure_summary="red", retries_left={"test": 2}
    )
    build_loop.set_task_status(loop.repo, "T-001", "done")
    assert build_loop.read_task_handoff(store_mod.Store(loop.repo).read_state(), "T-001") == {}


def test_a_handoff_survives_an_ordinary_status_change(tmp_path: Path) -> None:
    """`in-progress` is written on every attempt, including the one recovering from a crash."""
    loop = orchestrator(tmp_path)
    build_loop.record_attempt_failure(
        loop.repo, "T-001", failed_step="test", failure_summary="red", retries_left={"test": 2}
    )
    build_loop.set_task_status(loop.repo, "T-001", "in-progress")
    assert build_loop.read_task_handoff(store_mod.Store(loop.repo).read_state(), "T-001")["failed_step"] == "test"


def test_salvaged_work_is_recorded_where_the_next_attempt_looks(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    build_loop.record_salvage(loop.repo, "T-001", branch="build/x-T-001-salvage-1", salvage_state="pending")
    handoff = build_loop.read_task_handoff(store_mod.Store(loop.repo).read_state(), "T-001")
    assert handoff["salvage_branch"] == "build/x-T-001-salvage-1"
    assert handoff["salvage_state"] == "pending"


def test_the_implementer_is_told_where_the_previous_attempt_went(tmp_path: Path) -> None:
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    restored = build_prompts.implementer_prompt(
        task,
        "",
        gate_cmds=["make test"],
        has_baseline=False,
        handoff={"salvage_branch": "b-T-001-salvage-1", "salvage_state": "restored"},
    )
    assert "already been merged" in restored and "b-T-001-salvage-1" in restored
    conflicted = build_prompts.implementer_prompt(
        task,
        "",
        gate_cmds=["make test"],
        has_baseline=False,
        handoff={"salvage_branch": "b-T-001-salvage-1", "salvage_state": "conflict"},
    )
    assert "conflicted" in conflicted
    assert build_prompts.handoff_note({}) == ""  # a first attempt says nothing about a previous one


# --- the reviewer prompt ------------------------------------------------------
#
# The reviewer is the only reader that judges the TESTS. The negative control can show that the
# test half is not inert against the base; nothing else in the loop asks whether a test would go
# red if the behaviour were wrong, so the question has to be in the prompt or it is asked nowhere.


def test_the_reviewer_is_asked_whether_a_test_would_go_red() -> None:
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.review_prompt(task, gate_cmds=["make test"], dossier_path=".rein/work/T-001.json")
    assert "would go red" in prompt
    assert "not inert" in prompt
    # And the reason it is here rather than left to the control.
    assert "only place that judgement is made" in prompt


def test_the_reviewer_is_pointed_at_the_criteria_nothing_else_establishes() -> None:
    """`command` and `artifact` criteria are established by the caller; a prose one is judged here."""
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.review_prompt(
        task, gate_cmds=["make test"], dossier_path=".rein/work/T-001.json", diff_cmd="git diff HEAD~1"
    )
    assert "`evidence.kind` is `prose`" in prompt
    assert "already established by the caller" in prompt


def test_the_integration_reviewer_is_not_asked_the_test_question() -> None:
    """The merged tree's suite is the union of the leaves', and every test in it was already read."""
    from rein import build_prompts

    prompt = build_prompts.integration_review_prompt(
        "T-001, T-002", gate_cmds=["make test"], diff_cmd="git diff main", findings_path="/tmp/f.json"
    )
    assert "would go red" not in prompt


# --- the host's own review disciplines ----------------------------------------
#
# `/code-review` and `/simplify` were named in rein's prompts as bare prose for several releases:
# real Claude Code commands, written into text that also runs under `codex` and `gemini`, where
# they mean nothing. They are a host capability now, declared per adapter, and the question is
# written out beside them so that a host without them asks the same thing.


def _claude_disciplines() -> dict[str, str]:
    return dict(adapters.ADAPTER_TABLE["claude"].disciplines)


def test_only_a_host_that_has_them_is_told_to_use_them() -> None:
    assert set(_claude_disciplines()) == {adapters.CORRECTNESS, adapters.SIMPLIFICATION, adapters.SECURITY}
    assert adapters.ADAPTER_TABLE["codex"].disciplines == {}
    assert adapters.ADAPTER_TABLE["gemini"].disciplines == {}
    assert adapters.disciplines_for(["nothing-this-release-knows"]) == {}


def test_a_host_without_them_is_never_pointed_at_a_command_that_is_not_there() -> None:
    """The defect this replaced: the prompt named `/code-review` to a CLI that has no such thing."""
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    codex = adapters.ADAPTER_TABLE["codex"].disciplines
    prompt = build_prompts.review_prompt(task, gate_cmds=["make test"], disciplines=codex)
    assert "/code-review" not in prompt and "/simplify" not in prompt
    # The questions themselves are still asked — that is the floor, and it is what a codex reviewer
    # has always had.
    assert "correctness bugs" in prompt and "YAGNI" in prompt


def test_a_claude_reviewer_is_pointed_at_both_and_told_what_they_must_not_do() -> None:
    """`/simplify` ends by applying its fixes, and `/code-review ultra` is billed and user-triggered."""
    from rein import build_prompts

    task = dag.Task(id="T-001", title="base", kind="foundation")
    prompt = build_prompts.review_prompt(task, gate_cmds=["make test"], disciplines=_claude_disciplines())
    assert "/code-review" in prompt and "/simplify" in prompt
    assert "Run its review phase only" in prompt  # whoever judges does not repair
    # `--fix` is the same collapse by another route: the reviewer's own fix, read by nobody.
    assert "Never `/code-review --fix`" in prompt
    assert "never `/code-review ultra`" in prompt
    assert "ReportFindings" in prompt  # the answer comes back through the findings file
    # And a host where the command is missing or disabled is not a reason to stop.
    assert "ask the questions above yourself" in prompt


def test_the_join_says_to_keep_only_what_the_join_shows() -> None:
    """The disciplines read the whole branch, and each task inside it was already reviewed alone."""
    from rein import build_prompts

    prompt = build_prompts.integration_review_prompt(
        "T-001, T-002",
        gate_cmds=["make test"],
        diff_cmd="git diff main",
        findings_path="/tmp/f.json",
        disciplines=_claude_disciplines(),
    )
    assert "Keep what only the join shows" in prompt
    assert "already reviewed on its own" in prompt


def test_the_security_discipline_is_offered_in_the_contract_and_never_replaces_it() -> None:
    """Its own output is a markdown report; the answer here is the JSON, every finding anchored."""
    from rein import security_review as sec

    offered = sec.contract("/security-review")
    assert "/security-review" in offered
    assert "not the answer here" in offered
    assert "one JSON object and no other text" in offered  # the contract is unchanged underneath
    assert "/security-review" not in sec.contract()


def test_the_joins_two_send_backs_are_not_framed_as_the_same_work() -> None:
    """A reviewer's findings are not a red command step, and an implementer is told which it has.

    Both used to go through `integration_fix_prompt`, whose subject is "typically a cross-file
    lint/format/type error" — so an implementer handed a paragraph about a contract two tasks read
    differently had to work out that the framing was wrong before it could start.
    """
    from rein import build_prompts

    deterministic = build_prompts.integration_fix_prompt(
        "T-001,T-002", "$ make check (rc=1)\nE  unused import", gate_cmds=["make check"]
    )
    from_review = build_prompts.integration_review_fix_prompt(
        "T-001,T-002", "- must_fix: two tasks read `Config.timeout` differently", gate_cmds=["make check"]
    )
    assert "fails the deterministic gate" in deterministic
    assert "fails the deterministic gate" not in from_review
    # The review send-back names the reader who will look again, which is what makes disputing a
    # finding a real option rather than a silence.
    assert "reviewer looks again" in from_review
    assert "reviewer looks again" not in deterministic
    # And it says where the fix belongs, which is the thing only the join's framing carries.
    assert "belongs between the tasks" in from_review


def test_each_join_send_back_reaches_the_implementer_with_its_own_framing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The routing, not just the wording: the loop must hand each caller's prompt through."""
    from rein import build_prompts

    loop = orchestrator(tmp_path)
    sent: list[str] = []

    def capture(argv: list[str], **kwargs: object) -> str:
        sent.append(argv[-1])
        return ""

    monkeypatch.setattr(loop, "_launch", capture)

    loop._invoke_integration_fixer("T-001", loop._integration_fix_prompt("T-001", "rc=1"))
    loop._invoke_integration_fixer(
        "T-001", build_prompts.integration_review_fix_prompt("T-001", "- must_fix: x", gate_cmds=["make check"])
    )
    assert "fails the deterministic gate" in sent[0]
    assert "reviewer looks again" in sent[1]


# --- events -------------------------------------------------------------------


def test_the_loop_records_what_it_did(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    loop._event("task_started", "T-001", {"why": "test"})
    events = store_mod.Store(loop.repo).read_events()
    assert [e.event for e in events] == ["task_started"]
    assert events[0].subject_ids == ("T-001",)


def test_a_dry_run_records_nothing(tmp_path: Path) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=True, repo=repo)
    loop._event("task_started", "T-001", {})
    assert store_mod.Store(repo).read_events() == []


def test_an_escalation_is_recorded_and_announced(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    loop = orchestrator(tmp_path)
    loop._escalate("blocked", "everything is on fire", task="T-001")
    assert "everything is on fire" in capsys.readouterr().err
    events = store_mod.Store(loop.repo).read_events()
    assert [e.event for e in events] == ["knowledge_gap"]
    assert events[0].detail["kind"] == "blocked"


@pytest.mark.parametrize("kind", ["gate_violation", "no_runnable", "blocked", "integration_red"])
def test_every_escalation_kind_reaches_the_chain(tmp_path: Path, kind: str) -> None:
    """The kind is the escalation's vocabulary, not the chain's.

    Passing it through as the event type made `event_chain.make` raise on all four, so the loop
    died with a traceback at exactly the moment it had something to tell a human.
    """
    loop = orchestrator(tmp_path)
    loop._escalate(kind, f"{kind} happened", task="T-001")
    events = store_mod.Store(loop.repo).read_events()
    assert [e.event for e in events] == ["knowledge_gap"]
    assert events[0].event in events_mod.ATTENTION_EVENTS  # `rein events --summary` lists it as open
    assert events[0].detail["kind"] == kind


def test_a_batch_escalation_records_one_subject_per_task(tmp_path: Path) -> None:
    """A comma-joined batch of ids overruns the schema's 64-character subject at eleven leaves."""
    loop = orchestrator(tmp_path)
    tasks = [dag.Task(f"T-{n:03d}", f"task {n}", "parallel") for n in range(1, 13)]
    loop._escalate_batch("integration_red", "the merged tree is red", tasks)
    events = store_mod.Store(loop.repo).read_events()
    assert events[0].subject_ids == tuple(t.id for t in tasks)


def test_there_is_no_resolve_verb() -> None:
    """An escalation is closed by a signed disposition in the review, not by a flag somebody
    flips in a log."""
    source = Path(build_loop.__file__).read_text(encoding="utf-8")
    assert "log_escalation" not in source
    assert "rotate_if_large" not in source


# --- crash recovery -----------------------------------------------------------


def test_a_task_left_in_progress_is_reset_to_todo(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The frontier only picks up `todo`, so an interrupted task would deadlock the loop."""
    root = build_repo(tmp_path, state=make_state(phase="build", tasks={"T-001": "in-progress"}))
    repo = repo_mod.Repo(root)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    loop._recover_in_progress()

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.task_status["T-001"] == "todo"
    assert "reset in-progress" in capsys.readouterr().out


# --- the CLI ------------------------------------------------------------------


def test_an_invalid_config_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_repo(tmp_path)
    (root / ".rein" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    assert build_loop.main(["--dry-run", "--repo", str(root)]) == 1
    assert "cannot load .rein/config.yaml" in capsys.readouterr().err


def test_the_integration_gate_actually_runs_the_command_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It compared `step.kind` against "cmd" — a value the enum does not contain, so the loop
    body was unreachable and the gate returned green having run nothing. Four documents call
    this the backstop against a green report that is not true."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git({("make", "test"): (1, "tests/x.py::t FAILED")}))
    monkeypatch.setattr(build_loop, "_run", fake_git())  # the fixer the gate hands the failure to
    task = dag.Task(id="T-001", title="t", kind="parallel")
    ok, failure = loop._integration_gate([task])
    assert not ok
    assert "FAILED" in failure


def test_a_gate_step_carries_the_executor_profile_the_schema_requires(tmp_path: Path) -> None:
    """The config schema makes `executor_profile` required for a command step; normalization
    dropped it, so "repository code runs in the sandbox, never on the host" was true of the
    the sandbox and of nothing else — `make test` runs agent-authored files."""
    config = build_loop.Config.from_models(models.Config(make_config()))
    test_step = next(s for s in config.steps if s.name == "test")
    assert test_step.executor_profile == "quality"


def test_a_sandboxed_step_mounts_the_tree_it_is_testing(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    profile = models.ExecutorProfile(
        "quality", {"kind": "oci", "image": "localhost/x@sha256:" + "a" * 64, "network_profile": "none"}
    )
    mounts = loop._mounts_for(profile, cwd="/repo/worktree")
    assert mounts == ((Path("/repo/worktree"), "/work", "rw"),)


def test_mount_repo_read_only_is_honoured(tmp_path: Path) -> None:
    """A schema key nothing read. `read_only` is a real answer for a gate that only inspects."""
    loop = orchestrator(tmp_path)
    profile = models.ExecutorProfile(
        "quality",
        {
            "kind": "oci",
            "image": "localhost/x@sha256:" + "a" * 64,
            "network_profile": "none",
            "mount_repo": "read_only",
        },
    )
    assert loop._mounts_for(profile, cwd="/repo")[0][2] == "ro"


def sandbox_profile(**extra: object) -> models.ExecutorProfile:
    base = {"kind": "oci", "image": "localhost/x@sha256:" + "a" * 64, "network_profile": "none"}
    return models.ExecutorProfile("quality", {**base, **extra})


def make_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A worktree's shape on disk: `.git` is a *file* redirecting to the main repo's `.git`."""
    main_git = tmp_path / "main" / ".git"
    git_dir = main_git / "worktrees" / "T-001"
    git_dir.mkdir(parents=True)
    (git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    checkout = tmp_path / "main" / ".worktrees" / "T-001"
    checkout.mkdir(parents=True)
    (checkout / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    return checkout, main_git


def test_a_sandboxed_leaf_can_resolve_its_worktree_git(tmp_path: Path) -> None:
    """A worktree's `.git` file names the main repo's `.git` by absolute host path.

    Mounting only the checkout left that redirect pointing at nothing inside the container, so
    every gate step that shells out to git failed identically on every retry — for every leaf,
    and never for a foundation task. The shared `.git` has to be there under its own path.
    """
    checkout, main_git = make_linked_worktree(tmp_path)
    loop = orchestrator(tmp_path / "repo")
    mounts = loop._mounts_for(sandbox_profile(), cwd=str(checkout))
    assert mounts == ((checkout, "/work", "rw"), (main_git, str(main_git), "rw"))


def test_an_ordinary_checkout_mounts_only_itself(tmp_path: Path) -> None:
    """`.git` is a real directory there — already inside the mount, so nothing is added."""
    root = tmp_path / "plain"
    (root / ".git").mkdir(parents=True)
    loop = orchestrator(tmp_path / "repo")
    assert loop._mounts_for(sandbox_profile(), cwd=str(root)) == ((root, "/work", "rw"),)


def test_the_shared_git_follows_the_profile_mount_mode(tmp_path: Path) -> None:
    checkout, main_git = make_linked_worktree(tmp_path)
    loop = orchestrator(tmp_path / "repo")
    mounts = loop._mounts_for(sandbox_profile(mount_repo="read_only"), cwd=str(checkout))
    assert [m[2] for m in mounts] == ["ro", "ro"]
    assert loop._mounts_for(sandbox_profile(mount_repo="none"), cwd=str(checkout)) == ()


def test_a_malformed_worktree_marker_degrades_to_the_old_mount(tmp_path: Path) -> None:
    """Unreadable metadata reads as "not a worktree" rather than crashing the gate step."""
    checkout = tmp_path / "broken"
    checkout.mkdir()
    (checkout / ".git").write_text("this is not a gitdir pointer\n", encoding="utf-8")
    loop = orchestrator(tmp_path / "repo")
    assert loop._mounts_for(sandbox_profile(), cwd=str(checkout)) == ((checkout, "/work", "rw"),)


def test_a_host_profile_still_runs_where_it_was_told_to(tmp_path: Path) -> None:
    """A host profile means the host — what changed is that a sandboxed one is now honoured."""
    record: list[list[str]] = []
    loop = orchestrator(tmp_path)
    seen: dict[str, object] = {}

    def fake_run(cmd: list[str], cwd: str | None = None, **kwargs: object) -> tuple[int, str]:
        record.append(cmd)
        seen["cwd"] = cwd
        return 0, ""

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(common, "run", fake_run)
    try:
        step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
        assert loop._run_cmd_step(step, cwd=str(tmp_path)) == ""
    finally:
        monkeypatch.undo()
    assert record == [["make", "test"]]
    assert seen["cwd"] == str(tmp_path)


# --- environment faults: the machine's failures are not the code's ------------
#
# The loop records verdicts it earned. These pin the consequence of getting that wrong: a task
# `blocked` for a rate limit leaves the frontier, and a task off the frontier never reaches the
# salvage/restore path that exists to continue it — so an auto-restarted build never picks it up.

SESSION_LIMIT = (1, "You've hit your session limit · resets 3:30am (Asia/Tokyo)")
NO_SUCH_CLI = (127, "could not run 'claude': [Errno 2] No such file or directory: 'claude'")


def launch_failing(result: tuple[int, str], record: list[list[str]] | None = None) -> object:
    """A `build_loop._run` that fails every agent-CLI launch and lets git through."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if record is not None:
            record.append(cmd)
        return (0, "") if cmd and cmd[0] == "git" else result

    return _run


def test_a_serial_gate_violation_blocks_before_finalize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A foundation task that edited a gate-guarded path is blocked and stops the run — caught
    right after its implementer ran, not only at the old finalize-time check."""
    loop = orchestrator(tmp_path)
    violations = [("docs/10-requirements.md", "gate 'requirements' is pending")]

    monkeypatch.setattr(loop.ws, "head", lambda cwd=None: "a" * 40)

    def _raise(*_a: object, **_k: object) -> tuple[bool, str]:
        raise build_loop.GateViolationFault(violations)

    monkeypatch.setattr(loop, "_run_task_to_done", _raise)
    escalated: list[tuple[object, ...]] = []
    monkeypatch.setattr(loop, "_escalate_gate_violation", lambda *a: escalated.append(a))

    with pytest.raises(build_loop.StopLoop):
        loop._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])

    raw = store_mod.Store(loop.repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-001"]["status"] == "blocked"
    assert escalated == [("T-001", "its work-branch changes (caught before finalize)", violations)]


def test_a_launch_the_machine_failed_leaves_the_task_where_it_found_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not `blocked`: nothing judged this task's code, so nothing may say it was judged.

    The status is the load-bearing half. `blocked` takes the task off the frontier, and the
    salvage/restore machinery in build_git only ever runs for a task the frontier hands back.
    """
    loop = orchestrator(tmp_path, config=make_config(launch_retries=0))
    monkeypatch.setattr(build_loop, "_run", launch_failing(SESSION_LIMIT))

    with pytest.raises(build_loop.EnvironmentFault):
        loop._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])

    raw = store_mod.Store(loop.repo).read_raw("state")
    assert raw is not None
    entry = raw["tasks"]["T-001"]
    assert entry["status"] == "todo"
    handoff = entry.get("handoff", {})
    # No retry budget was spent, so none was written down — the fields that *are* a verdict about
    # this task stay absent.
    assert "retries_left" not in handoff
    assert "failed_step" not in handoff
    assert "failure_summary" not in handoff


def test_a_launch_the_machine_failed_still_says_why(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason has to outlive the terminal, without becoming a verdict about the task.

    "No verdict was reached" was being enforced by recording nothing at all, which is how an
    implementer that stopped for a nameable reason reached the operator as a task that had simply
    not moved. Diagnostics and verdicts are different things and now live in different keys.
    """
    loop = orchestrator(tmp_path, config=make_config(launch_retries=0))
    monkeypatch.setattr(build_loop, "_run", launch_failing(SESSION_LIMIT))

    with pytest.raises(build_loop.EnvironmentFault):
        loop._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])

    raw = store_mod.Store(loop.repo).read_raw("state")
    assert raw is not None
    handoff = raw["tasks"]["T-001"]["handoff"]
    assert SESSION_LIMIT[1] in handoff["last_agent"]["output_tail"]
    assert handoff["last_agent"]["role"] == "implementer"
    assert SESSION_LIMIT[1] in handoff["last_fault"]["output_tail"]


def test_a_launch_failure_writes_no_verdict_into_the_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`task_failed` and `knowledge_gap` are both `ATTENTION_EVENTS`, and the chain is
    append-only: a machine's bad afternoon would sit on gate ⑤'s screen as an unresolved
    escalation for the life of the repository."""
    loop = orchestrator(tmp_path, config=make_config(launch_retries=0))
    monkeypatch.setattr(build_loop, "_run", launch_failing(SESSION_LIMIT))

    with pytest.raises(build_loop.EnvironmentFault) as caught:
        loop._consume_serial([dag.Task(id="T-001", title="base", kind="foundation")])
    loop._record_abort(caught.value)

    recorded = [e.event for e in store_mod.Store(loop.repo).read_events()]
    assert "run_aborted" in recorded
    assert not [e for e in recorded if e in events_mod.ATTENTION_EVENTS]


def test_capacity_exhaustion_asks_to_be_re_run_and_a_missing_cli_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exit code is what an unattended supervisor decides on, so the two must differ."""
    for result, expected in ((SESSION_LIMIT, common.EXIT_RETRY_LATER), (NO_SUCH_CLI, common.EXIT_CANNOT_PROCEED)):
        loop = orchestrator(tmp_path / str(expected), config=make_config(launch_retries=0))
        monkeypatch.setattr(build_loop, "_run", launch_failing(result))
        assert loop._run_loop() == expected


# --- --supervise: the documented while-loop recipe, carried in-process -------


def test_supervise_retries_on_exit_retry_later_until_something_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only `EXIT_RETRY_LATER` is retried; each attempt is a fresh `Orchestrator(...)` call, so
    this changes nothing about what one run does — only whether something is still watching
    after it returns 3."""
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    config = build_loop.Config.load(repo)
    codes = iter([common.EXIT_RETRY_LATER, common.EXIT_RETRY_LATER, common.EXIT_DONE])
    seen_dry_run: list[bool] = []

    class FakeOrchestrator:
        def __init__(self, config: object, dry_run: bool, repo: object) -> None:
            seen_dry_run.append(dry_run)

        def run(self) -> int:
            return next(codes)

    monkeypatch.setattr(build_loop, "Orchestrator", FakeOrchestrator)
    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", slept.append)

    assert build_loop._supervise(config, repo, interval_sec=7) == common.EXIT_DONE
    assert slept == [7, 7]
    assert seen_dry_run == [False, False, False]  # --supervise never runs the loop read-only


@pytest.mark.parametrize("rc", [common.EXIT_DONE, common.EXIT_HUMAN_NEEDED, common.EXIT_CANNOT_PROCEED])
def test_supervise_returns_immediately_on_anything_but_retry_later(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rc: int
) -> None:
    """A verdict a human has to act on, or a finished run, is not something to sit on — only
    `EXIT_RETRY_LATER` is the machine's own "nothing to see, try later"."""
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    config = build_loop.Config.load(repo)

    class FakeOrchestrator:
        def __init__(self, config: object, dry_run: bool, repo: object) -> None:
            pass

        def run(self) -> int:
            return rc

    monkeypatch.setattr(build_loop, "Orchestrator", FakeOrchestrator)
    monkeypatch.setattr(time, "sleep", lambda _: pytest.fail("must not sleep on a non-retry-later exit"))

    assert build_loop._supervise(config, repo, interval_sec=1) == rc


def test_main_rejects_supervise_with_dry_run() -> None:
    """A supervised run has to call the real loop; the two are a contradiction, not a fast
    no-op dry check repeated forever."""
    assert build_loop.main(["--supervise", "--dry-run"]) == 2


def test_main_rejects_a_non_positive_supervise_interval() -> None:
    assert build_loop.main(["--supervise", "--supervise-interval-sec", "0"]) == 2


def test_an_exhausted_session_is_not_retried_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A limit that lifts in hours is not something to sit on holding the build lock: it exits
    at once so whatever re-runs `rein build` can do the waiting."""
    record: list[list[str]] = []
    loop = orchestrator(tmp_path, config=make_config(launch_retries=3))
    monkeypatch.setattr(build_loop, "_run", launch_failing(SESSION_LIMIT, record))
    monkeypatch.setattr(
        build_loop,
        "_wait_out_the_machine",
        lambda *_: pytest.fail("a capacity limit must not be slept on"),
    )

    assert loop._run_loop() == common.EXIT_RETRY_LATER
    assert len([c for c in record if c[0] == "claude"]) == 1


def test_a_blip_is_retried_from_the_runs_own_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The budget belongs to the run, not to a task: an environment fault is a property of the
    machine, so a per-task allowance would let one broken machine be re-discovered per task."""
    record: list[list[str]] = []
    slept: list[float] = []
    loop = orchestrator(tmp_path, config=make_config(launch_retries=2))
    monkeypatch.setattr(build_loop, "_run", launch_failing((143, ""), record))
    # The backoff itself, not every sleep in the process: `subprocess.Popen.wait(timeout=...)`
    # polls with `time.sleep`, so patching the global counted every subprocess wait this run made
    # and failed whenever the machine was busy enough for one to poll.
    monkeypatch.setattr(build_loop, "_wait_out_the_machine", lambda where, rc, attempt: slept.append(attempt))

    assert loop._run_loop() == common.EXIT_RETRY_LATER
    assert len([c for c in record if c[0] == "claude"]) == 3  # the launch plus its two retries
    assert len(slept) == 2
    assert loop._launch_retries_left == 0


def test_a_step_that_could_not_run_does_not_charge_the_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same category error, on the other side of the pipeline: `ExecutorError` means no
    container runtime / no pinned image, and was being summarized as a failed quality gate."""
    loop = orchestrator(tmp_path)

    class Boom:
        def run(self, spec: object) -> object:
            raise executors.ExecutorError("no container runtime (docker/podman) on PATH")

    monkeypatch.setattr(executors, "for_profile", lambda profile: Boom())
    step = build_loop.GateStep(name="test", kind="command", command=("make", "test"))
    with pytest.raises(build_loop.EnvironmentFault) as caught:
        loop._run_cmd_step(step, cwd=str(tmp_path))
    assert not caught.value.retryable


def test_a_stopped_leaf_keeps_its_worktree_while_its_batchmates_still_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two facts in one batch: the leaf that passed earned its merge, and the leaf the machine
    stopped goes back to `todo` with its worktree intact — which is exactly what the next run's
    `add_worktree` finalizes and salvages so the implementer continues instead of restarting."""
    loop = orchestrator(tmp_path)
    tasks = [
        dag.Task(id="T-001", title="leaf A", kind="parallel"),
        dag.Task(id="T-002", title="leaf B", kind="parallel"),
    ]
    fault = build_loop.EnvironmentFault(
        faults.Fault.ENV_TRANSIENT, where="T-002: implementer", rc=1, output=SESSION_LIMIT[1]
    )
    outcomes = {"T-001": build_loop.LeafOutcome(ok=True), "T-002": build_loop.LeafOutcome(ok=False, fault=fault)}
    statuses: list[tuple[str, str]] = []
    cleaned: list[str] = []
    merged: list[str] = []

    monkeypatch.setattr(loop, "_set_status", lambda tid, status, commit="": statuses.append((tid, status)))
    monkeypatch.setattr(loop.ws, "add_worktree", lambda task_id, restore_from="": f"build/x-{task_id}")
    monkeypatch.setattr(loop, "_safe_run_task", lambda task, cwd: outcomes[task.id])
    monkeypatch.setattr(loop.ws, "finalize_commit", lambda cwd, message: True)
    monkeypatch.setattr(loop, "_gate_violations", lambda paths: [])
    monkeypatch.setattr(loop.ws, "branch_changed_paths", lambda task_id, cwd="": [])
    monkeypatch.setattr(loop, "_cleanup_worktree", lambda task: cleaned.append(task.id))

    def record_merge(task: dag.Task, branch: str) -> bool:
        merged.append(task.id)
        return True

    monkeypatch.setattr(loop, "merge_leaf", record_merge)
    monkeypatch.setattr(loop.ws, "head", lambda cwd=None: "a" * 40)

    with pytest.raises(build_loop.EnvironmentFault):
        loop._consume_parallel(tasks)

    assert merged == ["T-001"]
    assert ("T-001", "done") in statuses
    assert ("T-002", "todo") in statuses
    assert not [s for s in statuses if s == ("T-002", "blocked")]
    assert cleaned == []  # the stopped leaf's tree is the next run's salvage source


def test_a_real_verdict_outranks_a_machine_fault_in_the_same_batch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running clears the fault but never the blocked task, so the human has to look either
    way — and the fault is still recorded, because it explains the leaf that came back `todo`."""
    loop = orchestrator(tmp_path)
    tasks = [
        dag.Task(id="T-001", title="leaf A", kind="parallel"),
        dag.Task(id="T-002", title="leaf B", kind="parallel"),
    ]
    fault = build_loop.EnvironmentFault(
        faults.Fault.ENV_TRANSIENT, where="T-002: implementer", rc=1, output=SESSION_LIMIT[1]
    )
    outcomes = {
        "T-001": build_loop.LeafOutcome(ok=False, log="gate red"),
        "T-002": build_loop.LeafOutcome(ok=False, fault=fault),
    }
    monkeypatch.setattr(loop.ws, "add_worktree", lambda task_id, restore_from="": f"build/x-{task_id}")
    monkeypatch.setattr(loop, "_safe_run_task", lambda task, cwd: outcomes[task.id])
    monkeypatch.setattr(loop, "_cleanup_worktree", lambda task: None)

    with pytest.raises(build_loop.StopLoop) as caught:
        loop._consume_parallel(tasks)
    assert caught.value.code == common.EXIT_HUMAN_NEEDED
    recorded = [e.event for e in store_mod.Store(loop.repo).read_events()]
    assert "run_aborted" in recorded


def test_a_stale_session_still_falls_back_but_an_exhausted_one_does_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resume fallback exists because session files expire. It used to fire on any nonzero
    rc, spending a second doomed launch on the one failure where nothing was going to launch."""
    loop = orchestrator(tmp_path, config=make_config(launch_retries=0))
    task = dag.Task(id="T-001", title="base", kind="foundation")

    stale: list[list[str]] = []
    monkeypatch.setattr(build_loop, "_run", launch_failing((1, "session not found"), stale))
    with pytest.raises(build_loop.EnvironmentFault):
        loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="", session="s-1", resume=True)
    assert len([c for c in stale if c[0] == "claude"]) == 2  # the resume, then one fresh launch

    exhausted: list[list[str]] = []
    monkeypatch.setattr(build_loop, "_run", launch_failing(SESSION_LIMIT, exhausted))
    with pytest.raises(build_loop.EnvironmentFault):
        loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="", session="s-2", resume=True)
    assert len([c for c in exhausted if c[0] == "claude"]) == 1


# --- the adapter capability record ------------------------------------------------


def test_every_adapter_declares_what_it_can_do() -> None:
    """The table replaced two dicts and one `== "claude"` test, each load-bearing and invisible.

    What the loop needs from an adapter is not "which one is it" but what it can do: change the
    tree, continue a session, isolate itself. Asserting on the declarations is asserting on the
    only thing any caller reads.
    """
    codex = adapters.ADAPTER_TABLE["codex"]
    assert codex.write_flags == ("--sandbox", "workspace-write")
    assert codex.own_sandbox, "codex sandboxes itself — the fact the nested-sandbox check needs"
    assert not codex.resumable, "codex resumes the *last* session, which parallel leaves cannot name"

    claude = adapters.ADAPTER_TABLE["claude"]
    assert claude.resumable and not claude.own_sandbox and not claude.write_flags


def test_a_resumable_implementer_stamps_then_resumes_its_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flags come off the record, so adding an adapter is data rather than a new branch."""
    loop = orchestrator(tmp_path)
    launched: list[list[str]] = []

    def capture(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        return 0, agent_output(cmd)

    monkeypatch.setattr(build_loop, "_run", capture)
    task = dag.Task(id="T-001", title="base", kind="foundation")

    loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="", session="S-1", resume=False)
    loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="", session="S-1", resume=True)

    assert "--session-id" in launched[0] and "--resume" not in launched[0]
    assert "--resume" in launched[1] and "--session-id" not in launched[1]


# --- where a DoD step runs (`stage:`) ------------------------------------------


def _staged_config() -> build_loop.Config:
    """A DoD split the way `stage:` exists for: a fast check per task, the whole suite over the join."""
    return build_loop.Config.from_models(
        models.Config(
            make_config(
                quality_gate=[
                    {
                        "name": "focused",
                        "kind": "command",
                        "command": ["a"],
                        "executor_profile": "quality",
                        "stage": "task",
                    },
                    {
                        "name": "suite",
                        "kind": "command",
                        "command": ["b"],
                        "executor_profile": "quality",
                        "stage": "integration",
                    },
                    {"name": "check", "kind": "command", "command": ["c"], "executor_profile": "quality"},
                ]
            )
        )
    )


def test_a_stage_moves_when_a_step_runs_not_whether(tmp_path: Path) -> None:
    """Every configured step still runs. The question `stage:` answers is how often the same
    confidence gets bought — a whole suite re-established on each attempt of each task, and again
    over the join, is the same thing paid for several times."""
    loop = build_loop.Orchestrator(_staged_config(), dry_run=True, repo=repo_mod.Repo(build_repo(tmp_path)))

    assert [s.name for s in loop._steps_at("task")] == ["focused", "check"]
    assert [s.name for s in loop._steps_at("integration")] == ["suite", "check"]
    # Nothing is dropped: every step belongs to at least one stage.
    assert {s.name for s in loop._steps_at("task")} | {s.name for s in loop._steps_at("integration")} == {
        s.name for s in loop.config.steps
    }


def test_a_step_with_no_stage_runs_everywhere_as_it_always_did(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    assert loop._steps_at("task") == loop._steps_at("integration") == loop.config.steps


# --- what this run put in front of a model -------------------------------------


def test_the_run_measures_its_own_prompt_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loop composes every prompt, so this is the one number it can know exactly.

    Gate ④'s review budget is measured rather than declared for the same reason; the build side
    of the run had no number at all, which is why "we are re-sending too much" could only ever be
    an impression.
    """
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, **kwargs: (0, agent_output(cmd)))
    task = dag.Task(id="T-001", title="base", kind="foundation")

    loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="")
    loop._invoke_implementer(task, cwd=str(tmp_path), failure_log="")

    summary = loop.spend_summary()
    assert "2 launches" in summary
    assert "implementer" in summary


def test_a_run_that_launched_nothing_reports_nothing(tmp_path: Path) -> None:
    """An empty measurement is not a measurement of zero — it is silence, and it prints as silence."""
    assert orchestrator(tmp_path).spend_summary() == ""


# --- the run's own input budget (measured, not estimated) ----------------------
#
# "Every launch's input is measured and reported at the end of the run — a budget nobody counts is
# a statement of intent." The counter used to see only the argv this process composes, which is the
# small half: what a launch is *told to read* is the dossier plus the ticket, the design slice and
# the baseline, and that is where a build's input goes. It also lived in the process, so the
# `EXIT_RETRY_LATER` a long build is nearly certain to hit took the number with it.


def test_a_launch_counts_what_it_was_told_to_read_not_only_what_was_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git())
    task = dag.Task(id="T-001", title="base", kind="foundation")
    (tmp_path / "docs" / "tasks").mkdir(parents=True, exist_ok=True)
    ticket = tmp_path / "docs" / "tasks" / "T-001.md"
    ticket.write_text("x" * 4096, encoding="utf-8")

    loop._write_dossier(task, str(tmp_path), base="", role="implementer")
    handed = loop.spend_totals()["implementer"]["handed_bytes"]
    # The dossier itself plus the ticket it names — the ticket alone is already larger than any
    # prompt this loop composes, which is the asymmetry the second counter exists to show.
    assert handed > ticket.stat().st_size


def test_a_retried_launch_is_counted_by_both_measures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A retry is another launch: the same argv goes to the provider again and is paid for again.

    Counting once per `_launch` under-reported every retried task, and once the billed counter
    landed beside it, one `run_measured` event carried two fields called `launches` disagreeing —
    the byte one saying 1 where the billed one said 3.
    """
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_LAUNCH_BACKOFF_SEC", (0.0,))
    attempts = iter([(1, "connection reset by peer"), (1, "connection reset by peer")])

    def flaky(cmd: list[str], *a: object, **k: object) -> tuple[int, str]:
        return next(attempts, (0, agent_output(cmd)))

    monkeypatch.setattr(build_loop, "_run", flaky)
    loop._launch(["claude", "-p", "go"], cwd=str(tmp_path), where="w", role="implementer")

    assert loop.spend_totals()["implementer"]["launches"] == 3
    assert loop.usage_totals()["implementer"].launches == 3


def test_a_resumed_launch_is_not_counted_as_a_cold_one(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A cold launch re-reads its ticket, design slice and code from scratch. Whether that is the
    largest avoidable cost in a long build is a claim the run can now answer about itself."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, *a, **k: (0, agent_output(cmd)))
    loop._launch(["claude", "-p", "go"], cwd=str(tmp_path), where="w", role="implementer")
    loop._launch(["claude", "-p", "go"], cwd=str(tmp_path), where="w", role="implementer", resumed=True)
    row = loop.spend_totals()["implementer"]
    assert row["launches"] == 2 and row["cold_launches"] == 1


def test_the_summary_reports_both_numbers_and_the_cold_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, *a, **k: (0, agent_output(cmd)))
    loop._launch(["claude", "-p", "go"], cwd=str(tmp_path), where="w", role="implementer")
    loop._spend_handover("implementer", 200_000)
    summary = loop.spend_summary()
    assert "sent" in summary and "handed to read" in summary and "1 cold" in summary


def test_a_run_that_launched_nothing_records_no_measurement(tmp_path: Path) -> None:
    """A run that took the lock and found no frontier did not measure zero — it measured nothing,
    and an event saying "0 bytes" would be a different claim."""
    loop = orchestrator(tmp_path)
    loop._record_spend("done")
    assert [e for e in loop.store.read_events() if e.event == "run_measured"] == []


def test_the_measurement_lands_in_the_audit_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """In the chain rather than in state.yaml: the chain never rotates, so summing `run_measured`
    over a cycle is the cycle's total while each run stays separately readable."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(build_loop, "_run", lambda cmd, *a, **k: (0, agent_output(cmd)))
    loop._launch(["claude", "-p", "go"], cwd=str(tmp_path), where="w", role="implementer")
    loop._spend_handover("code_reviewer", 1024)
    loop._record_spend("retry-later")

    measured = [e for e in loop.store.read_events() if e.event == "run_measured"]
    assert len(measured) == 1
    detail = measured[0].detail
    # The per-role columns and nothing derived from them: the run-wide totals this used to carry
    # beside `by_role` were the sums of these, and a sum recorded next to its own addends is a
    # field that can disagree with itself.
    assert detail["kind"] == "build" and detail["outcome"] == "retry-later"
    assert set(detail["by_role"]) == {"implementer", "code_reviewer"}
    assert detail["by_role"]["implementer"]["launches"] == 1
    assert detail["by_role"]["implementer"]["cold_launches"] == 1
    assert detail["by_role"]["code_reviewer"]["handed_bytes"] == 1024
    assert detail["by_role"]["implementer"]["prompt_bytes"] > 0


# --- what is not worth another round ------------------------------------------
#
# Two observations, neither of which parses the failure's text. `faults` refuses to interpret
# build-tool output on principle, so the reported cases — a lockfile mismatch, a missing browser
# binary, an absent CDK context — are caught by the shape of the retry instead: the step was
# already red before the task ran, or nothing moved between two identical failures.


def _task() -> dag.Task:
    return dag.Task(id="T-001", title="demo", kind="foundation", status="in-progress")


def test_a_step_already_red_on_the_work_branch_is_not_sent_back(tmp_path: Path) -> None:
    """The reported run: task after task spent its whole send-back budget on a `check` a dependency
    drift had broken weeks earlier, and the chain recorded each as a verdict about that task's code."""
    loop = orchestrator(tmp_path)
    loop._baseline_red["check"] = "ruff: 3 pre-existing errors"
    reason = loop._futile(_task(), "check", "ruff: 3 pre-existing errors", "sha256:" + "a" * 64, ("", "", ""))
    assert "already red" in reason and "ruff: 3 pre-existing errors" in reason


def test_a_step_red_only_now_is_still_worth_a_round(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    loop._baseline_red["check"] = "pre-existing"
    assert loop._futile(_task(), "test", "1 failed", "sha256:" + "a" * 64, ("", "", "")) == ""


def test_an_identical_failure_over_an_unmoved_tree_is_not_retried(tmp_path: Path) -> None:
    """The implementer ran and changed nothing, so the next round has the same inputs."""
    loop = orchestrator(tmp_path)
    tree = "sha256:" + "a" * 64
    log = "error: the lockfile is out of date"
    seen = ("test", digests.of_bytes(log.encode("utf-8")), tree)
    assert "unchanged tree" in loop._futile(_task(), "test", log, tree, seen)


def test_a_moved_tree_earns_another_round(tmp_path: Path) -> None:
    """Even on an identical failure: the implementer changed something, so the inputs are not the
    same and the retry is not provably pointless."""
    loop = orchestrator(tmp_path)
    log = "error: the lockfile is out of date"
    seen = ("test", digests.of_bytes(log.encode("utf-8")), "sha256:" + "a" * 64)
    assert loop._futile(_task(), "test", log, "sha256:" + "b" * 64, seen) == ""


def test_a_different_failure_over_an_unmoved_tree_earns_another_round(tmp_path: Path) -> None:
    """A flaky suite failing differently each time is not the case this catches."""
    loop = orchestrator(tmp_path)
    tree = "sha256:" + "a" * 64
    seen = ("test", digests.of_bytes(b"first failure"), tree)
    assert loop._futile(_task(), "test", "a different failure", tree, seen) == ""


def test_an_unknown_fingerprint_never_matches(tmp_path: Path) -> None:
    """Fail open towards retrying: spending a retry is recoverable, refusing one on a tree nothing
    could read is not."""
    loop = orchestrator(tmp_path)
    log = "the same failure"
    seen = ("test", digests.of_bytes(log.encode("utf-8")), "")
    assert loop._futile(_task(), "test", log, "", seen) == ""


def test_the_futile_reason_reaches_the_audit_chain(tmp_path: Path) -> None:
    """ "the budget ran out" and "the budget was abandoned as pointless" are different things, and
    only the second names something to go and repair."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.record_attempt_failure(
        repo,
        "T-001",
        failed_step="check",
        failure_summary="ruff: 3 errors",
        retries_left={"check": 0},
        futile="T-001: 'check' was already red on work before this task ran",
    )
    failed = [e for e in store_mod.Store(repo).read_events() if e.event == "task_failed"][-1]
    assert "already red" in failed.detail["futile"]


# --- the same question, asked twice over the same tree ------------------------
#
# `_futile` covers a gate step that failed identically over an unmoved tree. An attempt
# `_check_implementer_output` stopped returns before any step runs, so its repeat is always a later
# `rein build` — and the only thing that survives one is the handoff.


def _escalated(loop: build_loop.Orchestrator, tree: str, kind: str = "no_implementation") -> None:
    build_loop.record_escalation(
        loop.repo,
        "T-001",
        kind=kind,
        message="T-001: the implementer produced no change at all (git diff is empty).",
        tree=tree,
    )


def test_a_verdict_already_reached_over_this_tree_is_not_bought_a_second_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported run: a task whose work had already landed via a salvage merge was handed a
    fresh implementer once per `rein build`, three times, each one correctly reporting that there
    was nothing to do."""
    loop = orchestrator(tmp_path)
    tree = "sha256:" + "a" * 64
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: tree)
    _escalated(loop, tree)
    launched: list[object] = []
    monkeypatch.setattr(loop, "_invoke_implementer", lambda *a, **k: launched.append(a))

    ok, message = loop._run_task_to_done(_task(), str(tmp_path))

    assert launched == [], "the launch this whole record exists to avoid was spent anyway"
    assert ok is False
    assert "not re-launched" in message
    assert "the implementer produced no change at all" in message, "the original verdict must survive"
    assert loop._stops["T-001"] == "blocked"


def test_the_repeat_is_marked_futile_where_a_step_failure_would_be(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "Determined once and re-raised" and "determined again" must not read the same in the log."""
    loop = orchestrator(tmp_path)
    tree = "sha256:" + "a" * 64
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: tree)
    _escalated(loop, tree)
    monkeypatch.setattr(loop, "_invoke_implementer", lambda *a, **k: None)

    loop._run_task_to_done(_task(), str(tmp_path))

    build_loop.set_task_status(loop.repo, "T-001", "blocked", note="stopped")
    failed = [e for e in store_mod.Store(loop.repo).read_events() if e.event == "task_failed"][-1]
    assert "not re-launched" in failed.detail["futile"]
    assert failed.detail["escalation"] == "no_implementation"


def test_an_implementer_that_said_needs_revision_parks_the_task_there_on_the_repeat_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An implementer that found the *design* wrong said so; re-raising it as `blocked` would file
    a defect in the plan as a defect in the code."""
    loop = orchestrator(tmp_path)
    tree = "sha256:" + "a" * 64
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: tree)
    _escalated(loop, tree, kind="agent_needs_revision")
    monkeypatch.setattr(loop, "_invoke_implementer", lambda *a, **k: None)

    loop._run_task_to_done(_task(), str(tmp_path))
    assert loop._stops["T-001"] == "needs-revision"


def test_a_moved_tree_earns_the_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Something changed since that verdict — another task merged, a human edited — so the question
    is not the one that was answered."""
    loop = orchestrator(tmp_path)
    _escalated(loop, "sha256:" + "a" * 64)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "b" * 64)
    assert loop._already_answered(_task(), str(tmp_path)) is None


def test_an_unread_tree_earns_the_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The same fail-open `_futile` takes: spending a launch is recoverable, refusing one over a
    tree nothing could read is not."""
    loop = orchestrator(tmp_path)
    _escalated(loop, "sha256:" + "a" * 64)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "")
    assert loop._already_answered(_task(), str(tmp_path)) is None


def test_a_task_that_never_escalated_earns_the_launch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "a" * 64)
    assert loop._already_answered(_task(), str(tmp_path)) is None


def test_recording_an_escalation_appends_exactly_one_event(tmp_path: Path) -> None:
    """The handoff and the `knowledge_gap` are one write: a terminal killed between them would
    otherwise leave an escalation in the chain with nothing saying what the next run already knows.
    And one event, not two — this replaces `_escalate` on this path rather than joining it."""
    repo = repo_mod.Repo(build_repo(tmp_path))
    before = len(store_mod.Store(repo).read_events())
    build_loop.record_escalation(
        repo,
        "T-001",
        kind="report_mismatch",
        message="T-001: named paths the diff does not contain",
        tree="sha256:" + "c" * 64,
    )
    events = store_mod.Store(repo).read_events()
    assert len(events) == before + 1
    assert events[-1].event == "knowledge_gap"
    assert events[-1].detail["kind"] == "report_mismatch"
    handoff = build_loop.read_task_handoff(store_mod.Store(repo).read_state(), "T-001")
    assert handoff["escalation"] == {
        "kind": "report_mismatch",
        "message": "T-001: named paths the diff does not contain",
        "tree": "sha256:" + "c" * 64,
    }


def test_a_baseline_is_not_established_in_a_dry_run(tmp_path: Path) -> None:
    """A dry run enters no sandbox and runs no command — its job is to print the control flow."""
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=True, repo=repo)
    loop._establish_baseline()
    assert loop._baseline_red == {}


def test_the_baseline_is_taken_once_per_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One batch per layer, and the tree under it moves as tasks land — re-running the whole DoD at
    the root before each batch would buy a number nothing reads."""
    loop = orchestrator(tmp_path)
    ran: list[str] = []

    def record(step: build_loop.GateStep, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(loop, "_run_cmd_step", record)
    loop._establish_baseline()
    loop._establish_baseline()
    assert ran == ["test", "check"]


def test_a_baseline_that_cannot_run_marks_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No container runtime, no pinned image — the machine failed, so nothing was learned about any
    step. The batch about to start hits the same fault and aborts the run without marking a task."""
    loop = orchestrator(tmp_path)

    def unrunnable(step: build_loop.GateStep, cwd: str) -> str:
        raise faults.EnvironmentFault(faults.Fault.ENV_PERMANENT, where="test", rc=127, output="could not run make")

    monkeypatch.setattr(loop, "_run_cmd_step", unrunnable)
    loop._establish_baseline()
    assert loop._baseline_red == {}


# --- where a task's work lands ------------------------------------------------


def test_a_task_with_no_open_pull_request_lands_on_the_work_branch(tmp_path: Path) -> None:
    """Every first build. Nothing about this path changes when there is no stack."""
    loop = orchestrator(tmp_path)

    assert loop.ws.target_branch("T-001") == loop.branch
    assert loop.ws.landing == {}


def test_a_task_whose_pull_request_is_open_lands_on_that_branch(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-002": "build/x-pr-02-T-002"}

    assert loop.ws.target_branch("T-002") == "build/x-pr-02-T-002"
    assert loop.ws.target_branch("T-001") == loop.branch


def test_a_leaf_is_branched_from_its_target_not_from_the_work_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fix has to start from the pull request it belongs to, or it would not contain it."""
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-002": "slice-branch"}
    calls: list[list[str]] = []
    monkeypatch.setattr(loop.ws, "_salvage_leftovers", lambda task_id, branch, path: "")
    monkeypatch.setattr(loop.ws, "git", lambda args, cwd=None: calls.append(args))

    loop.ws.add_worktree("T-002")

    assert calls[0][:3] == ["worktree", "add", "-b"]
    assert calls[0][-1] == "slice-branch"


def test_the_landing_map_comes_from_the_audit_log(tmp_path: Path) -> None:
    loop = orchestrator(tmp_path)
    slice_ = pr_stack.Slice(
        index=2,
        task_id="T-002",
        title="t",
        branch="b-pr-02-T-002",
        base_ref="b-pr-01-T-001",
        base_sha="0" * 40,
        head_sha="1" * 40,
    )
    with store_mod.Store(loop.repo).transaction() as tx:
        tx.append(
            pr_stack.LEDGER_EVENT,
            cycle_id=loop.cycle_id,
            detail=pr_stack.opened_event_detail(slice_, "https://example.invalid/pr/2"),
        )

    loop._load_landing()

    assert loop.ws.landing == {"T-002": "b-pr-02-T-002"}


def test_a_slice_already_ready_is_not_landed_on(tmp_path: Path) -> None:
    """Past gate ④ a change is a human's call, not something a re-run puts there quietly."""
    loop = orchestrator(tmp_path)
    slice_ = pr_stack.Slice(
        index=2,
        task_id="T-002",
        title="t",
        branch="b-pr-02-T-002",
        base_ref="main",
        base_sha="0" * 40,
        head_sha="1" * 40,
    )
    with store_mod.Store(loop.repo).transaction() as tx:
        for action in (pr_stack.LEDGER_OPENED, pr_stack.LEDGER_READY):
            tx.append(
                pr_stack.LEDGER_EVENT,
                cycle_id=loop.cycle_id,
                detail=pr_stack.opened_event_detail(slice_, "https://example.invalid/pr/2", action),
            )

    loop._load_landing()

    assert loop.ws.landing == {}


def test_a_leaf_that_landed_elsewhere_is_left_out_of_the_integration_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The combined tree is not on the work branch yet — `--restack` is what makes it."""
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-002": "slice-branch"}
    tasks = [dag.Task(id=f"T-00{n}", title=f"leaf {n}", kind="parallel") for n in (1, 2, 3)]
    gated: list[list[str]] = []

    monkeypatch.setattr(loop, "_set_status", lambda tid, status, commit="": None)
    monkeypatch.setattr(loop.ws, "add_worktree", lambda task_id, restore_from="": f"build/x-{task_id}")
    monkeypatch.setattr(loop, "_safe_run_task", lambda task, cwd: build_loop.LeafOutcome(ok=True))
    monkeypatch.setattr(loop.ws, "finalize_commit", lambda cwd, message: True)
    monkeypatch.setattr(loop, "_gate_violations", lambda paths: [])
    monkeypatch.setattr(loop.ws, "branch_changed_paths", lambda task_id, cwd="": [])
    monkeypatch.setattr(loop, "merge_leaf", lambda task, branch: True)
    monkeypatch.setattr(loop.ws, "landed", lambda task_id: "a" * 40)

    def record_gate(merged: list[dag.Task]) -> tuple[bool, str]:
        gated.append([t.id for t in merged])
        return True, ""

    monkeypatch.setattr(loop, "_integration_gate", record_gate)

    loop._consume_parallel(tasks)

    assert gated == [["T-001", "T-003"]]


def test_a_leaf_is_diffed_against_its_target_branch_not_the_work_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The base and the branch used to be separate arguments and the base was always the work branch.

    A leaf landing on a slice branch was then diffed against a branch it never forked from, so its
    changed-path set carried every commit the slices below had added — and `_gate_violations`
    judged paths the task had not touched.
    """
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-002": "build/x-pr-c1-02-T-002"}
    calls: list[list[str]] = []

    def record(cmd: list[str], cwd: str | None = None, **_: object) -> tuple[int, str]:
        calls.append(cmd)
        return 0, ""

    monkeypatch.setattr(loop.ws, "_run", record)

    loop.ws.branch_changed_paths("T-002")
    loop.ws.branch_changed_paths("T-001")

    ranges = [cmd[-1] for cmd in calls if cmd[:3] == ["git", "diff", "--name-only"]]
    assert ranges[0].startswith("build/x-pr-c1-02-T-002...")
    assert ranges[1].startswith(f"{loop.branch}...")


def test_the_review_step_is_told_the_scope_it_actually_has(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-002": "build/x-pr-c1-02-T-002"}
    monkeypatch.setattr(loop.ws, "branch_changed_paths", lambda task_id, cwd="": [])
    task = dag.Task(id="T-002", title="leaf", kind="parallel")

    _, command = loop._review_scope(task, cwd=str(tmp_path / "leaf"), base="")

    assert command == "git diff build/x-pr-c1-02-T-002...HEAD"


# --- the wiring from a merge conflict to an implementer ------------------------


def test_a_merge_conflict_goes_through_the_classifier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`merge_leaf` used to abort and block, full stop. Its conflict path had no test at all."""
    loop = orchestrator(tmp_path)
    task = dag.Task(id="T-001", title="leaf", kind="parallel")
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(loop.ws, "merge_cwd", lambda task_id: loop.root)
    monkeypatch.setattr(loop.ws, "merge_leaf", lambda task_id, branch, cwd: False)
    monkeypatch.setattr(loop.ws, "git", lambda args, cwd=None: None)

    def resolved(plan: Any, **kw: Any) -> conflict.Resolution:
        seen.append((kw["ours_task"], kw["theirs_task"]))
        return conflict.Resolution(kind="mechanical")

    monkeypatch.setattr(conflict, "merge_with_resolution", resolved)

    assert loop.merge_leaf(task, "build/x-T-001") is True
    assert seen == [("", "T-001")]


def test_an_unresolved_conflict_escalates_and_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    loop.ws.landing = {"T-001": "build/x-pr-c1-01-T-001"}
    task = dag.Task(id="T-001", title="leaf", kind="parallel")
    escalated: list[conflict.Resolution] = []
    monkeypatch.setattr(loop.ws, "merge_cwd", lambda task_id: loop.root)
    monkeypatch.setattr(loop.ws, "merge_leaf", lambda task_id, branch, cwd: False)

    def refused(plan: Any, **kw: Any) -> conflict.Resolution:
        return conflict.Resolution(
            kind="semantic",
            conflict=conflict.Conflict(kw["ours_task"], kw["theirs_task"], ("src/a.py",)),
            escalation="they contradict",
        )

    def record(repo: Any, resolution: conflict.Resolution) -> list[str]:
        escalated.append(resolution)
        return []

    monkeypatch.setattr(conflict, "merge_with_resolution", refused)
    monkeypatch.setattr(conflict, "escalate", record)

    assert loop.merge_leaf(task, "build/x-T-001") is False
    assert [r.kind for r in escalated] == ["semantic"]
    # A task landing on its own slice is both sides of the merge, so its scope is checkable.
    collision = escalated[0].conflict
    assert collision is not None and collision.ours_task == "T-001"


def test_resolving_hands_out_an_orchestrator_with_a_live_control_plane(tmp_path: Path) -> None:
    """The lock and the socket, in the order `run()` takes them. Nothing had ever entered this."""
    root = build_repo(tmp_path)
    with build_loop.resolving(repo_mod.Repo(root)) as orchestrator_:
        assert orchestrator_.control is not None
        assert orchestrator_.control.socket_path.exists()
        assert callable(orchestrator_.resolve_conflict)
        assert callable(orchestrator_.task_gate)


def test_the_task_gate_runs_only_the_deterministic_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    ran: list[str] = []

    def note(step: Any, cwd: str) -> str:
        ran.append(step.name)
        return ""

    monkeypatch.setattr(loop, "_run_cmd_step", note)

    assert loop.task_gate(str(tmp_path)) == (0, "")
    assert ran and all(s.kind == "command" for s in loop._steps_at("task") if s.name in ran)


def test_the_task_gate_reports_the_step_that_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_run_cmd_step", lambda step, cwd: "2 tests failed")

    status, log = loop.task_gate(str(tmp_path))

    assert status == 1 and "2 tests failed" in log


# --- the merged tree is read too ----------------------------------------------


def test_an_agent_step_declared_at_the_integration_stage_runs_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`stage:` moves *when* a step runs, never whether. An agent step at the integration stage was
    skipped outright, which made the join the one tree no reviewer ever read."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git())
    step = build_loop.GateStep(name="review", kind="agent", agent_role="code_reviewer", stage="integration")
    monkeypatch.setattr(loop, "_steps_at", lambda stage: (step,) if stage == "integration" else ())

    seen: list[str] = []
    monkeypatch.setattr(loop, "_run_integration_agent_step", lambda s, tasks: seen.append(s.name))
    ok, _ = loop._integration_gate([dag.Task(id="T-001", title="t", kind="parallel")])
    assert ok and seen == ["review"], "the agent step declared at this stage has to run at it"


def test_the_integration_reviewers_findings_go_to_the_task_whose_scope_owns_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A finding about the join is carried to gate ④ beside the task that owns the code it names —
    the same derivation gate ④ uses — and one nobody owns is said out loud rather than filed
    against a task that does not own it."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git())
    tasks = [
        dag.Task(id="T-001", title="a", kind="parallel", scope_include=("alpha/",)),
        dag.Task(id="T-002", title="b", kind="parallel", scope_include=("beta/",)),
    ]
    loop._file_integration_findings(
        [
            {"severity": "consider", "statement": "duplicated helper", "anchor": "beta/mod.py:4"},
            {"severity": "consider", "statement": "about the join itself"},
        ],
        tasks,
    )
    assert loop._take_diagnostics("T-002")["review"]["findings"][0]["statement"] == "duplicated helper"
    assert loop._take_diagnostics("T-001") == {}, "a finding nobody owns is not filed against a task"


def test_a_second_review_of_a_task_does_not_discard_the_first(tmp_path: Path) -> None:
    """A task can be read twice — in its worktree and again after it merges — and the two are
    different observations. Replacing the key dropped whichever arrived first."""
    loop = orchestrator(tmp_path)
    loop._add_review_findings("T-001", [{"severity": "consider", "statement": "from its own reviewer"}])
    loop._add_review_findings("T-001", [{"severity": "consider", "statement": "from the join"}])
    kept = loop._take_diagnostics("T-001")["review"]["findings"]
    assert [f["statement"] for f in kept] == ["from its own reviewer", "from the join"]


def test_a_task_with_no_declared_scope_is_not_warmed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An undeclared scope means unbounded, so its reading would be the whole change — neither one
    task wide nor the question gate ④ will ask."""
    loop = orchestrator(tmp_path)
    warmed: list[str] = []
    monkeypatch.setattr(review_reading, "warm", lambda *a, **k: warmed.append("yes"))
    loop._warm_reading(dag.Task(id="T-001", title="t", kind="foundation"))
    assert warmed == []


def test_a_warm_up_that_cannot_be_taken_does_not_fail_the_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate takes the reading either way; retrying it once per task would spend a session limit
    on an optimization."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(common, "run", fake_git())

    def refuse(*_a: object, **_k: object) -> None:
        raise review_policy.AdapterFailure("the provider said no", rc=429, output="")

    monkeypatch.setattr(review_reading, "warm", refuse)
    task = dag.Task(id="T-001", title="t", kind="foundation", scope_include=("alpha/",))
    loop._warm_reading(task)
    assert loop._warming_off, "one refusal stops the warming rather than being retried per task"
    loop._warm_reading(task)  # and the second one does not even try


# --- the negative control -----------------------------------------------------
#
# The DoD is the only automated evidence a task's `done` rests on, and until this existed
# nothing ever asked whether it could go red. The tests it runs were written by the implementer
# in the same launch as the code; the blind extractor is never shown them, the security reviewer
# reads them only as an attack surface, and the per-task reviewer is asked about the source. So
# the Expected/Actual split was applied to the code and never to the tests, and a test that
# asserts nothing produces a green that re-running reproduces exactly. These tests pin the
# experiment that closes it and — as much — the three answers that are *not* passes.


def _controlled(
    loop: build_loop.Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
    *,
    changed: list[str],
    reds: dict[str, str] | None = None,
    apply_rc: int = 0,
) -> list[str]:
    """Wire a control run: `changed` is the task's diff, `reds` names the steps that go red."""
    import contextlib as _contextlib

    from rein import build_git as _build_git

    ran: list[str] = []
    monkeypatch.setattr(loop, "_review_scope", lambda task, cwd, base: (changed, ""))
    monkeypatch.setattr(loop.ws, "fork_point", lambda ref, cwd: "b" * 40)
    monkeypatch.setattr(loop.ws, "diff_from", lambda base, cwd, paths: "diff --git a/t b/t\n")
    monkeypatch.setattr(build_loop, "_run", lambda cmd, cwd=None, timeout=None: (apply_rc, "does not apply"))

    @_contextlib.contextmanager
    def _scratch(*_args: object, **_kw: object) -> Iterator[str]:
        yield str(loop.root)

    monkeypatch.setattr(_build_git, "scratch_worktree", _scratch)

    def _step(step: build_loop.GateStep, cwd: str, *, note: bool = True) -> str:
        ran.append(step.name)
        assert note is False, "a control green is a fact about the control tree, not the task's"
        return (reds or {}).get(step.name, "")

    monkeypatch.setattr(loop, "_run_cmd_step", _step)
    return ran


def _cmd_steps() -> tuple[build_loop.GateStep, ...]:
    return (
        build_loop.GateStep(name="test", kind="command", command=("make", "test")),
        build_loop.GateStep(name="check", kind="command", command=("make", "check")),
    )


def test_a_dod_that_is_green_without_the_change_does_not_let_the_task_land(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. Every step still green over the base with only the tests applied means no
    test in the change exercises it, so the green that would have closed the task is a fact about
    code that was already there."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "tests/test_x.py"])

    # A leaf worktree, so the base is the commit it forked off rather than the caller's.
    failed, message = loop._negative_control(_task(), str(tmp_path / "wt"), "", _cmd_steps())

    assert failed == build_loop.NEGATIVE_CONTROL
    assert "green without your change" in message
    assert ran == ["test", "check"]
    # Deliberately not recorded as evidence: `evidence.negative_control` justifies a `done`, and
    # this is the verdict that stops there being one. It travels as a task failure instead.
    assert loop._current_control == {}


def test_a_step_that_goes_red_without_the_change_is_what_makes_the_control_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """And it stops at the first red: one discriminating step settles the question."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "tests/test_x.py"], reds={"test": "test_x.py::t FAILED"})

    assert loop._negative_control(_task(), str(loop.root), "a" * 40, _cmd_steps()) == (None, "")
    assert ran == ["test"]
    assert loop._current_control == {"result": "discriminating", "base": "a" * 40, "step": "test"}


def test_a_task_that_changed_no_test_is_recorded_rather_than_passed_or_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """There is no control to take, and that is neither a pass nor a failure — a task genuinely
    covered by tests that already existed is a real thing. What it must not be is silent: the
    record is what says this task's green rests on tests nobody wrote for it."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "README.md"])

    assert loop._negative_control(_task(), str(loop.root), "a" * 40, _cmd_steps()) == (None, "")
    assert ran == []
    assert loop._current_control["result"] == "no_tests_changed"


def test_a_control_that_could_not_be_set_up_is_undetermined_and_never_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A broken experiment is not evidence in either direction. It says why, and it does not
    borrow the vocabulary of the two answers that were actually reached."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "tests/test_x.py"], apply_rc=1)

    assert loop._negative_control(_task(), str(loop.root), "a" * 40, _cmd_steps()) == (None, "")
    assert ran == []
    assert loop._current_control["result"] == "undetermined"
    assert "did not apply" in loop._current_control["detail"]


def test_a_dod_with_no_command_step_records_that_it_could_not_be_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of the silence this record exists to end. `no_tests_changed` says "the green
    rests on tests nobody wrote for it"; a quality gate made only of agent steps said nothing at
    all — no record, and so no row on the orient brief, which skips a task that has none."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "tests/test_x.py"])
    agents_only = (build_loop.GateStep(name="review", kind="agent", agent_role="code_reviewer"),)

    assert loop._negative_control(_task(), str(loop.root), "a" * 40, agents_only) == (None, "")
    assert ran == []
    assert loop._current_control["result"] == "undetermined"
    assert "no command step" in loop._current_control["detail"]


def test_a_diff_git_would_not_give_up_is_not_reported_as_an_empty_test_half(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`diff_from` returned "" for a failed `git diff`, which reads here as "the change touched no
    test bytes" — a sentence about a diff that was never obtained."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["src/x.py", "tests/test_x.py"])
    monkeypatch.setattr(loop.ws, "diff_from", lambda *a, **k: None)

    assert loop._negative_control(_task(), str(loop.root), "a" * 40, _cmd_steps()) == (None, "")
    assert ran == []
    assert loop._current_control["result"] == "undetermined"
    assert "could not be read out of git" in loop._current_control["detail"]


def test_a_control_failure_spends_a_send_back_rather_than_ending_the_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It comes back through the channel a red step uses, and until this test it did not get the
    thing that channel is made of. `negative-control` is not a configured step, so
    `budgets.get(name, 0)` answered zero: the task ended on the first control failure with the
    implementer never told what was missing, while every line describing the mechanism said it
    spends an attempt like a red step does."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "a" * 64)
    monkeypatch.setattr(loop, "_check_implementer_output", lambda *a, **k: ("", ""))
    launched: list[str] = []
    monkeypatch.setattr(loop, "_invoke_implementer", lambda task, cwd, failure_log, **k: launched.append(failure_log))
    monkeypatch.setattr(
        loop, "_run_pipeline", lambda task, cwd, base="": (build_loop.NEGATIVE_CONTROL, "green without your change")
    )

    ok, _ = loop._run_task_to_done(_task(), str(tmp_path))

    assert ok is False
    assert len(launched) == 1 + build_loop.SEND_BACK_RETRIES, launched
    assert "green without your change" in launched[-1], "the send-back carries what is missing"


def test_a_verdict_with_no_send_back_budget_is_a_failure_and_not_a_silent_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shape both bugs came out of: `.get(name, 0)` turned "this loop has no send-back rule for
    that verdict" into "this verdict has no retries left", which reads identically in the log and
    ends the task either way."""
    loop = orchestrator(tmp_path)
    monkeypatch.setattr(loop, "_fingerprint", lambda cwd: "sha256:" + "a" * 64)
    monkeypatch.setattr(loop, "_check_implementer_output", lambda *a, **k: ("", ""))
    monkeypatch.setattr(loop, "_invoke_implementer", lambda *a, **k: None)
    monkeypatch.setattr(loop, "_run_pipeline", lambda task, cwd, base="": ("a-verdict-nobody-seeded", "x"))

    with pytest.raises(common.ReinError, match="no retry budget is registered"):
        loop._run_task_to_done(_task(), str(tmp_path))


def test_the_control_runs_only_the_command_steps_that_actually_passed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It negates the claim the gate just made, so its subject is exactly the steps that made it —
    an agent step judged nothing a re-run could contradict, and a step that never ran claimed
    nothing to negate."""
    loop = orchestrator(tmp_path)
    ran = _controlled(loop, monkeypatch, changed=["tests/test_x.py"])
    passed = (
        build_loop.GateStep(name="review", kind="agent", agent_role="code_reviewer"),
        build_loop.GateStep(name="smoke", kind="command"),
        build_loop.GateStep(name="test", kind="command", command=("make", "test")),
    )

    loop._negative_control(_task(), str(loop.root), "a" * 40, passed)
    assert ran == ["test"]
