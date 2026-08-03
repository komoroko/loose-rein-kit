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
from pathlib import Path

import pytest

from rein import build_loop, common, dag, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import fake_git, make_config, make_plan, make_state, make_task, seed_repo


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


# --- config ------------------------------------------------------------------


def test_config_normalizes_the_quality_gate(tmp_path: Path) -> None:
    config = build_loop.Config.from_models(models.Config(make_config()))
    assert [s.name for s in config.steps] == ["test", "check"]
    assert config.steps[0].command == ("make", "test")
    assert config.gate_cmds == ["make test", "make check"]


def test_an_unknown_adapter_is_refused_up_front() -> None:
    config = make_config()
    config["agents"]["implementer"]["adapter"] = "mystery"  # type: ignore[index]
    with pytest.raises(ValueError, match="does not know how to launch"):
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
    assert step.agent_argv == build_loop.ADAPTERS["codex"]
    # The implementer's own adapter is untouched — the two are resolved independently.
    assert config.adapter_argv == build_loop.ADAPTERS["claude"]


def test_an_agent_step_launches_with_its_roles_adapter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo)
    monkeypatch.setattr(orch, "_tree_state", lambda cwd: ("", ""))

    launched: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    task = dag.Task(id="T-001", title="base", kind="foundation")
    orch._run_agent_step(orch.config.steps[0], task, cwd=str(root), base="")

    assert launched, "the agent step never launched anything"
    assert tuple(launched[0][:2]) == build_loop.ADAPTERS["codex"], (
        f"the agent step launched {launched[0][:2]} — it must use agents.code_reviewer, not agents.implementer"
    )


def test_the_codex_agent_step_is_launched_able_to_write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`codex exec` runs in a read-only sandbox unless told otherwise. Without this the loop
    starts, every task hands work to an agent that cannot save a file, and the diffs come back
    empty with nothing saying why."""
    root = build_repo(tmp_path)
    orch = build_loop.Orchestrator(_config_with_split_adapters(), dry_run=False, repo=repo_mod.Repo(root))
    monkeypatch.setattr(orch, "_tree_state", lambda cwd: ("", ""))
    launched: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", fake_run)
    orch._run_agent_step(orch.config.steps[0], dag.Task(id="T-001", title="base", kind="foundation"), str(root), "")
    assert launched[0][:4] == ["codex", "exec", "--sandbox", "workspace-write"]


def test_a_claude_launch_gains_no_sandbox_flags() -> None:
    """The flags are one CLI's own vocabulary, not a portable concept — nothing else grows them."""
    assert build_loop.write_flags(build_loop.ADAPTERS["claude"]) == ()
    assert build_loop.write_flags(build_loop.ADAPTERS["gemini"]) == ()
    assert build_loop.write_flags(()) == ()


def test_the_review_transport_is_not_given_write_access(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A reviewer that cannot write is the point. review.py launches the bare adapter argv, so
    the read-only default is the one the reviewer wants — assert it stays that way."""
    from rein import common, review

    raw = make_config()
    raw["agents"]["code_reviewer"] = {"adapter": "codex"}
    repo = repo_mod.Repo(seed_repo(tmp_path, config=raw))
    launched: list[list[str]] = []

    def fake_run(cmd: list[str], **kwargs: object) -> tuple[int, str]:
        launched.append(cmd)
        return 0, "{}"

    # review.py resolves `common.run` at call time, so patching the module both share is what
    # actually intercepts the launch — reaching through `review.common` is the same object by
    # accident of import, and mypy is right that it is not part of review's interface.
    monkeypatch.setattr(common, "run", fake_run)
    review._adapter_reviewer(repo, "code_reviewer")({"request": "x"})
    assert launched[0] == list(build_loop.ADAPTERS["codex"])


def test_an_unlaunchable_role_adapter_stops_the_build_before_it_starts() -> None:
    """Refused up front, not at the first step that needed it — halfway through a task."""
    raw = make_config(quality_gate=list(_AGENT_GATE))
    raw["agents"]["code_reviewer"] = {"adapter": "nonesuch"}
    with pytest.raises(ValueError, match="agents.code_reviewer.adapter"):
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


def test_starting_a_task_counts_an_attempt(tmp_path: Path) -> None:
    repo = repo_mod.Repo(build_repo(tmp_path))
    build_loop.set_task_status(repo, "T-001", "in-progress")
    build_loop.set_task_status(repo, "T-001", "in-progress")
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None and raw["tasks"]["T-001"]["attempts"] == 2


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
    root = build_repo(tmp_path)
    repo = repo_mod.Repo(root)
    store_mod.ensure_private_dir(store_mod.Store(repo).runtime)
    with build_loop.build_lock(repo):
        config = build_loop.Config.load(repo)
        assert build_loop.Orchestrator(config, dry_run=False, repo=repo).run() == 2
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
    loop._escalate("task_failed", "everything is on fire", task="T-001")
    assert "everything is on fire" in capsys.readouterr().err
    assert [e.event for e in store_mod.Store(loop.repo).read_events()] == ["task_failed"]


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
