"""What only a person can provide, checked before anything is launched — end to end, over real git.

The cycle behind this (#91) stopped 23 times, and the most common reason was not a code failure: an
implementer was launched at a task whose human-side precondition had never been met, and paid for
the launch to find out. T-019 alone stopped four times — a browser that had to be running, labels a
person had to make — and one of those launches produced the labels itself.

So a precondition is data the loop can evaluate, a person's deliverable is a node no implementer is
sent at, and the loop asks once, for everything the rest of the plan will need.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common, executors, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"
GATE = [{"name": "test", "command": ["true"], "executor_profile": "quality", "retries": 2}]
LABELS = "docs/test/golden-labels.yaml"


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def seeded(tmp_path: Path, tasks: list[dict[str, Any]], config: dict[str, Any] | None = None) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "person@example.com")
    git(root, "config", "user.name", "A Person")
    seed_repo(
        root,
        config=config or make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=make_plan(tasks=tasks),
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def counting_implementer(launched: list[str]) -> object:
    """An implementer that writes one file where it is launched, and remembers that it was."""

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        launched.append(where.name)
        (where / f"{where.name}.py").write_text("# done\n", encoding="utf-8")
        return 0, agent_envelope("")

    return _run


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def status_of(repo: repo_mod.Repo, task_id: str) -> dict[str, Any]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw["tasks"].get(task_id, {}))


def browser_task(probe: list[str]) -> dict[str, Any]:
    task = make_task("T-019", kind="parallel", claim_ids=["C-001"], title="read the logged-in page")
    task["requires"] = [{"says": "a logged-in browser listening on its debug port", "probe": probe}]
    return task


def labels_task() -> dict[str, Any]:
    task = make_task(
        "T-018",
        kind="parallel",
        claim_ids=["C-001"],
        title="ten golden labels",
        scope_include=["docs/test/"],
        acceptance=[
            {"id": "A-1", "statement": "the labels exist", "evidence": {"kind": "artifact", "paths": [LABELS]}}
        ],
    )
    task["produced_by"] = "person"
    return task


def test_an_unmet_precondition_launches_nothing_and_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, [browser_task(["false"])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []
    entry = status_of(repo, "T-019")
    assert entry.get("status", "todo") == "todo"
    assert "handoff" not in entry and entry.get("attempts", 0) == 0
    events = store_mod.Store(repo).read_events()
    assert "task_failed" not in {e.event for e in events}
    [asked] = [e for e in events if e.detail.get("kind") == "awaiting_operator"]
    assert "a logged-in browser listening on its debug port" in asked.detail["message"]


def test_a_precondition_that_holds_lets_the_launch_go_ahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = seeded(tmp_path, [browser_task(["true"])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_DONE
    assert launched == ["T-019"]


def test_everything_owed_is_asked_for_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Both of T-019's shapes — the browser and the labels — in one stop, not one per paid launch."""
    reader = browser_task(["false"])
    reader["blocked_by"] = ["T-018"]
    repo = seeded(tmp_path, [labels_task(), reader])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []
    [asked] = [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "awaiting_operator"]
    assert set(asked.subject_ids) == {"T-018", "T-019"}
    assert f"commit {LABELS}" in asked.detail["message"]
    assert "debug port" in asked.detail["message"]


def test_a_person_s_deliverable_is_waited_for_and_never_fabricated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    consumer = make_task("T-020", kind="parallel", claim_ids=["C-001"], blocked_by=["T-018"])
    repo = seeded(tmp_path, [labels_task(), consumer])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []

    # The file in somebody's working tree is not yet a deliverable a dependent can fork from.
    (repo.root / LABELS).parent.mkdir(parents=True)
    (repo.root / LABELS).write_text("labels: []\n", encoding="utf-8")
    assert build(repo) == common.EXIT_CANNOT_PROCEED  # the uncommitted file is refused as ever

    git(repo.root, "add", LABELS)
    git(repo.root, "commit", "-q", "-m", "labels, made by hand")
    assert build(repo) == common.EXIT_DONE
    assert launched == ["T-020"]
    labelled = status_of(repo, "T-018")
    assert labelled["status"] == "done"
    [authored] = labelled["evidence"]["authored"]
    assert authored["path"] == LABELS and authored["author"] == "A Person <person@example.com>"


def test_a_person_s_task_with_nothing_to_wait_for_is_refused_at_the_plan() -> None:
    task = labels_task()
    task["acceptance"] = []
    with pytest.raises(models.DocumentError, match="produced_by 'person' with no `artifact` criterion"):
        models.Plan.parse(store_mod.dump_yaml(make_plan(tasks=[task])).decode())


def test_a_file_precondition_is_checked_in_the_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    task = make_task("T-001", kind="parallel", claim_ids=["C-001"])
    task["requires"] = [{"says": "the API key file is in place", "file": ".secrets/key"}]
    repo = seeded(tmp_path, [task])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []


def test_the_same_list_is_what_approving_the_mandate_prints(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The other moment the whole list is worth having: the plan has just frozen, and a person can
    prepare all of it before the first launch."""
    from rein import approve

    reader = browser_task(["false"])
    reader["blocked_by"] = ["T-018"]
    repo = seeded(tmp_path, [labels_task(), reader])

    approve._print_owed_by_people(repo)

    out = capsys.readouterr().out
    assert out.index("T-018") < out.index("T-019"), "in the order the work will need them"
    assert f"commit {LABELS}" in out and "debug port" in out


def test_a_probe_that_could_not_run_is_a_machine_fault_not_an_unmet_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rein import executors

    repo = seeded(tmp_path, [browser_task(["true"])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    def refuse(_profile: object) -> object:
        raise executors.ExecutorError("no container runtime")

    monkeypatch.setattr(executors, "for_profile", refuse)
    assert build(repo) == common.EXIT_CANNOT_PROCEED
    assert launched == []
    assert not [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "awaiting_operator"]


def test_a_probe_runs_where_the_implementer_will_and_not_in_the_gate_s_sandbox(tmp_path: Path) -> None:
    """The recommended gate sandbox has no network; a browser the implementer can reach is not visible
    from there, and a probe run there waited forever for a browser that was running."""
    repo = seeded(tmp_path, [browser_task(["true"])])
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    step = build_loop.GateStep(name="T-019:requires[0]", kind="command", command=("true",))
    assert loop._probe_profile(step).kind == "host"
    assert loop._probe_profile(step).name != loop.config.raw.quality_gate_profile.name  # type: ignore[union-attr]


def test_the_stop_names_what_stopped_the_frontier_even_if_a_probe_changes_its_mind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asked twice, a flaky probe could answer "fine" the second time and leave a stop with no task to
    close it. What stopped the frontier is carried, not asked again."""
    from rein import dag

    repo = seeded(tmp_path, [browser_task(["true"])])
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    assert loop._present_owed(dag.load(repo), {"T-019": ["a browser (probe failed)"]}) == common.EXIT_HUMAN_NEEDED
    [asked] = [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "awaiting_operator"]
    assert list(asked.subject_ids) == ["T-019"]


def test_a_probe_that_ran_and_found_the_tool_missing_is_unmet_not_a_machine_fault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "command not found" and "could not resolve host" are what a probe of an installed tool or of
    the network is there to observe. Classified the way a gate step's output is, they stopped the
    run as a broken machine instead of asking the person for the tool."""
    repo = seeded(tmp_path, [browser_task(["sh", "-c", "echo 'curl: (6) Could not resolve host: x'; exit 127"])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []
    [asked] = [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "awaiting_operator"]
    assert "exited 127" in asked.detail["message"]


# --- a task's declared environment (`environment.env` / `environment.allow`) ------------------


def recording_implementer(seen: list[tuple[list[str], dict[str, str]]]) -> object:
    """An implementer that records its command line and environment, then writes one file."""

    def _run(
        cmd: list[str], cwd: str | None = None, timeout: float | None = None, env: Any = None, **_: object
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        seen.append((list(cmd), dict(env or {})))
        where = Path(cwd or ".")
        (where / f"{where.name}.py").write_text("# done\n", encoding="utf-8")
        return 0, agent_envelope("")

    return _run


def measuring_task(
    env: dict[str, str], allow: list[str] | None = None, probe: list[str] | None = None
) -> dict[str, Any]:
    task = make_task("T-047", kind="parallel", claim_ids=["C-001"], title="measure on the host")
    task["environment"] = {"env": env, **({"allow": allow} if allow else {})}
    if probe:
        task["requires"] = [{"says": "the model imports", "probe": probe}]
    return task


def test_the_declared_environment_reaches_the_implementer_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The field case: PYTHONPATH lived in the operator's shell, and a launch from a shell without
    it failed the task's own precondition."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    repo = seeded(tmp_path, [measuring_task({"PYTHONPATH": "$HOME/CosyVoice:${HOME}/extra"})])
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(build_loop, "_run", recording_implementer(seen))

    assert build(repo) == common.EXIT_DONE
    [(_, env)] = seen
    home = str(tmp_path / "home")
    assert env["PYTHONPATH"] == f"{home}/CosyVoice:{home}/extra"
    assert env[build_loop.DECLARED_ENV] == "PYTHONPATH"


def test_a_reference_to_an_unset_variable_is_an_unmet_precondition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never expanded to "": a PYTHONPATH that silently became empty is the failure this removes."""
    monkeypatch.delenv("REIN_TEST_NOT_SET", raising=False)
    repo = seeded(tmp_path, [measuring_task({"PYTHONPATH": "$REIN_TEST_NOT_SET/lib"})])
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(build_loop, "_run", recording_implementer(seen))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert seen == []
    [asked] = [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "awaiting_operator"]
    assert "PYTHONPATH" in asked.detail["message"]


def test_a_probe_asks_under_the_declared_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = ["sh", "-c", 'test "$REIN_TEST_MODEL_DIR" = /models']
    repo = seeded(tmp_path, [measuring_task({"REIN_TEST_MODEL_DIR": "/models"}, probe=probe)])
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(build_loop, "_run", recording_implementer(seen))

    assert build(repo) == common.EXIT_DONE
    assert len(seen) == 1


def test_declared_commands_are_granted_for_that_launch_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The field case widened the repository's standing `permissions.allow` with `Bash(uv run *:*)`
    so one task could run its measurement script; every later session inherited it."""
    allow = ["uv run --no-sync python tests/measurements/run.py"]
    repo = seeded(tmp_path, [measuring_task({}, allow=allow)])
    seen: list[tuple[list[str], dict[str, str]]] = []
    monkeypatch.setattr(build_loop, "_run", recording_implementer(seen))

    assert build(repo) == common.EXIT_DONE
    [(cmd, _)] = seen
    settings = json.loads(cmd[cmd.index("--settings") + 1])
    assert settings == {"permissions": {"allow": [f"Bash({allow[0]}:*)"]}}
    # The settings value sits before the prompt, which stays the last argument.
    assert cmd.index("--settings") < len(cmd) - 2
    assert not (repo.root / ".claude" / "settings.json").exists() or "uv run" not in (
        repo.root / ".claude" / "settings.json"
    ).read_text(encoding="utf-8")


# --- the long, deterministic part of a task is run by the loop (`operate`) ---------------------


def operating_task(steps: list[dict[str, Any]], env: dict[str, str] | None = None) -> dict[str, Any]:
    task = make_task("T-047", kind="parallel", claim_ids=["C-001"], title="the full run")
    task["operate"] = steps
    if env:
        task["environment"] = {"env": env}
    return task


def test_operate_runs_after_the_implementer_and_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its output lands with the task: the gate sees the file the run wrote, and so does the merge."""
    step = {"name": "full-run", "command": ["sh", "-c", 'echo "$REIN_TEST_DECK" > measured.txt; echo measured']}
    repo = seeded(tmp_path, [operating_task([step], env={"REIN_TEST_DECK": "deck-a"})])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) == common.EXIT_DONE
    assert launched == ["T-047"]
    assert git(repo.root, "show", f"{WORK_BRANCH}:measured.txt") == "deck-a"
    log = repo.root / ".rein" / "work" / "T-047" / "operate" / "01-full-run.log"
    assert log.read_text(encoding="utf-8").strip() == "measured"


def test_a_failing_operate_step_goes_back_to_the_implementer_with_its_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    step = {"name": "full-run", "command": ["sh", "-c", "echo 'RecallRejected: ungrounded_keys=175'; exit 1"]}
    repo = seeded(tmp_path, [operating_task([step])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) != common.EXIT_DONE
    # Sent back once; the stub changes nothing, so the second identical failure is not paid for again.
    assert launched == ["T-047", "T-047"]
    handoff = status_of(repo, "T-047")["handoff"]
    assert handoff["failed_step"] == "operate:full-run"
    assert "ungrounded_keys=175" in handoff["failure_summary"]


def test_an_operate_step_the_machine_could_not_run_spends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, [operating_task([{"name": "full-run", "command": ["rein-no-such-runner"]}])])
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", counting_implementer(launched))

    assert build(repo) in (common.EXIT_CANNOT_PROCEED, common.EXIT_RETRY_LATER)
    assert launched == ["T-047"]
    assert "failed_step" not in status_of(repo, "T-047").get("handoff", {})
    assert "task_failed" not in {e.event for e in store_mod.Store(repo).read_events()}


def test_operate_runs_where_a_gate_step_does_not_as_a_host_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A step runs the implementer's code. It went straight to the host with the operator's
    environment, past the `quality_gate_profile` every gate step is held to."""
    steps = [
        {"name": "build-deck", "command": ["true"]},
        {"name": "full-run", "command": ["true"], "executor_profile": "measure"},
    ]
    profiles = {
        "quality": {"kind": "host", "containerfile": "python"},
        "measure": {"kind": "host", "containerfile": "python"},
    }
    config = make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0, profiles=profiles)
    repo = seeded(tmp_path, [operating_task(steps)], config=config)
    monkeypatch.setattr(build_loop, "_run", counting_implementer([]))
    ran: list[tuple[str, tuple[str, ...]]] = []
    dispatch = executors.for_profile

    def recording(profile: models.ExecutorProfile) -> executors.Executor:
        inner = dispatch(profile)

        class Recording(executors.Executor):
            def run(self, spec: executors.ExecutionSpec) -> executors.ExecutionResult:
                ran.append((spec.profile.name, spec.command))
                return inner.run(spec)

        return Recording()

    monkeypatch.setattr(executors, "for_profile", recording)

    assert build(repo) == common.EXIT_DONE
    assert ("quality", ("true",)) in ran[:2] and ("measure", ("true",)) in ran[:2]


def test_what_an_operate_step_writes_outside_the_scope_stops_before_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The scope check read the tree before the step ran, and `finalize_commit` adds the whole tree:
    a run's output landed wherever the step put it."""
    task = operating_task([{"name": "full-run", "command": ["sh", "-c", "mkdir -p data && echo x > data/out.txt"]}])
    task["scope"] = {"include": ["T-047.py"]}
    repo = seeded(tmp_path, [task])
    monkeypatch.setattr(build_loop, "_run", counting_implementer([]))

    assert build(repo) != common.EXIT_DONE
    [stop] = [e for e in store_mod.Store(repo).read_events() if e.detail.get("kind") == "scope_violation"]
    assert "data/out.txt" in stop.detail["message"]
    assert "operate" in stop.detail["message"]


def test_a_host_reference_in_the_environment_of_a_contained_task_is_unmet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`$HOME/x` expanded here names a directory of the host; inside the sandbox it names nothing,
    and the work failed as if the code were wrong."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    profiles = {
        "quality": {"kind": "host", "containerfile": "python"},
        "agentbox": {
            "kind": "oci-agent",
            "image": "localhost/rein-agent@sha256:" + "b" * 64,
            "network_profile": "egress",
        },
    }
    config = make_config(
        branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0, profiles=profiles, agent_profile="agentbox"
    )
    task = measuring_task({"PYTHONPATH": "$HOME/CosyVoice", "MODEL": "tiny"})
    repo = seeded(tmp_path, [task], config=config)
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    graph_task = loop._load_graph().get("T-047")

    [unmet] = loop._unmet_preconditions(graph_task)
    assert "PYTHONPATH" in unmet and "agentbox" in unmet
    assert "MODEL" not in unmet
