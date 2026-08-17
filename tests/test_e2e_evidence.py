"""What `done` means, end to end, over real git.

The build loop used to decide a task by asking one question — did the quality gate pass? — of
a process it had launched and then stopped listening to. Everything else followed from that.
An implementer whose sandbox refused to let it write produced a clean tree, an exit code of
zero, and a green gate over the code that was already there, and the loop called that `done`.
An implementer that said "I am blocked" said it into output nobody kept, so a reviewer and a
full test suite ran against the attempt anyway.

These tests pin the replacement: the loop looks at what the attempt *produced*, reads what the
attempt *said* through `rein report`, and records what the verdict was reached *on*.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from rein import build_loop, common, digests, dossier, evidence, strict_yaml
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"

#: A host profile and a command that always passes. What is under test is what the loop concludes
#: from an attempt, so the gate must never be the thing that fails.
GATE = [{"name": "test", "kind": "command", "command": ["true"], "executor_profile": "quality", "retries": 1}]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    """A real git checkout on the work branch with one leaf task ready to build."""
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, max_parallel=2, launch_retries=0),
        plan=make_plan(tasks=[make_task("T-001", kind="parallel", claim_ids=["C-001"])]),
        state=make_state(phase="build", plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def scope_the_plan(repo: repo_mod.Repo, task_id: str, *, include: list[str]) -> None:
    """Give one plan task a declared scope, editing the document rather than its text."""
    path = repo.path(".rein/plan.yaml")
    plan = strict_yaml.load_mapping(path.read_text(encoding="utf-8"), what="plan.yaml")
    for entry in plan["tasks"]:
        if entry["id"] == task_id:
            entry["scope"] = {"include": include}
    path.write_text(store_mod.dump_yaml(plan).decode("utf-8"), encoding="utf-8")


def status_of(repo: repo_mod.Repo, task_id: str) -> dict[str, object]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw["tasks"].get(task_id, {}))


def events_of(repo: repo_mod.Repo) -> list[tuple[str, str]]:
    """(event name, the escalation kind it carries) for every event in the chain."""
    return [(e.event, str(e.detail.get("kind", ""))) for e in store_mod.Store(repo).read_events()]


def build(repo: repo_mod.Repo, *, cache: bool = False) -> int:
    orchestrator = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo)
    if not cache:
        # The ledger is a cache. Turning it off is how a test asks "was this step actually run?"
        # rather than "did something once establish it?".
        orchestrator.ledger = evidence.Ledger(path=None, enabled=False)
    return orchestrator.run()


def call_report(where: Path, env: dict[str, str] | None, argv: list[str]) -> None:
    """Run `rein report` the way a launched agent would: inside the worktree, with its own env.

    The capability token and control socket reach a real implementer through the environment the
    orchestrator hands its subprocess, and the report only reaches the canonical store because of
    them. An in-process fake that skipped that would be testing a path production does not have.
    """
    from rein import control_plane

    previous = dict(os.environ)
    try:
        os.environ.update(env or {})
        control_plane.report_main([*argv, "--repo", str(where)])
    finally:
        os.environ.clear()
        os.environ.update(previous)


def agent(*, writes: bool = True, reports: str = "", runs: list[list[str]] | None = None) -> object:
    """A fake agent CLI, with the two knobs that decide what a real one produces.

    `writes` is whether it changes a file at all — a sandbox that will not let the agent write is
    indistinguishable, from the loop's side, from an agent that decided to do nothing.
    `reports` is the `rein report --outcome` it ends with, "" for an agent that reports nothing.
    """

    def _run(
        cmd: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
        *,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> tuple[int, str]:
        if runs is not None:
            runs.append(list(cmd))
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        if writes:
            (where / f"{where.name}.py").write_text(f"# {where.name}\n", encoding="utf-8")
        if reports:
            call_report(where, env, ["--task", where.name, "--outcome", reports, "--summary", "the fake agent said so"])
        return 0, "the agent's closing words"

    return _run


# --- an attempt that produced nothing -----------------------------------------


def test_an_implementer_that_changes_nothing_does_not_finish_the_task(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case this whole file exists for.

    Everything the old loop looked at said yes: the process exited zero, the tree was clean so
    the finalize commit was a no-op "success", and `true` passed the gate. None of it was about
    the task, and the task was marked done anyway.
    """
    monkeypatch.setattr(build_loop, "_run", agent(writes=False))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-001")["status"] == "blocked"
    assert ("knowledge_gap", "no_implementation") in events_of(repo)
    assert "task_completed" not in [name for name, _ in events_of(repo)]


def test_an_implementer_that_changes_nothing_keeps_what_it_said(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ "The tree did not change" is a symptom. The reason lived in output the loop discarded."""
    monkeypatch.setattr(build_loop, "_run", agent(writes=False))

    build(repo)

    handoff = status_of(repo, "T-001")["handoff"]
    assert isinstance(handoff, dict)
    assert handoff["last_agent"]["output_tail"] == "the agent's closing words"
    assert handoff["last_agent"]["role"] == "implementer"


# --- an attempt that said it could not ----------------------------------------


def test_a_blocked_report_stops_the_attempt_before_the_quality_gate(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An agent that says it is stuck is not a question for a reviewer or a test suite.

    The gate command here is `true`, so this cannot be checked by whether the gate *passed* — it
    is checked by whether it ran at all. Running it, and the review agent after it, is the token
    cost of asking a model about an attempt that already answered.
    """
    runs: list[list[str]] = []
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="blocked", runs=runs))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-001")["status"] == "blocked"
    assert ["true"] not in runs, "the quality gate ran for an attempt that had already reported blocked"
    assert ("knowledge_gap", "agent_blocked") in events_of(repo)


def test_a_needs_revision_report_is_not_recorded_as_a_blocked_task(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A defect in the plan is not a defect in the code, and the status is what says which.

    `blocked` means the implementation could not pass; `needs-revision` means the ticket or the
    design is wrong. Collapsing the second into the first files the bug in the wrong place, and
    the human reads the wrong document looking for it.
    """
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="needs-revision"))

    build(repo)

    assert status_of(repo, "T-001")["status"] == "needs-revision"


def test_a_report_naming_paths_it_did_not_change_is_a_finding(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--touched` is a claim, checked against the diff. An agent wrong about its own work is a fact."""

    def _run(
        cmd: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
        *,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        (where / "real.py").write_text("# real\n", encoding="utf-8")
        call_report(where, env, ["--task", where.name, "--outcome", "implemented", "--touched", "imagined.py"])
        return 0, ""

    monkeypatch.setattr(build_loop, "_run", _run)

    build(repo)

    assert status_of(repo, "T-001")["status"] == "blocked"
    assert ("knowledge_gap", "report_mismatch") in events_of(repo)


# --- what a `done` carries ----------------------------------------------------


def test_a_finished_task_records_what_its_verdict_was_reached_on(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`done` has to say on what. Without that it is only a word a process exiting produced."""
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="implemented"))

    assert build(repo) == common.EXIT_DONE
    entry = status_of(repo, "T-001")
    assert entry["status"] == "done"
    recorded = entry["evidence"]
    assert isinstance(recorded, dict)
    assert recorded["tree"].startswith("sha256:")
    assert [step["name"] for step in recorded["steps"]] == ["test"]
    assert recorded["reported"] == "implemented"


def test_a_task_leaving_done_loses_its_evidence(repo: repo_mod.Repo) -> None:
    """The record says why this task *is* done — the same reason `completed_commit` is dropped."""
    build_loop.set_task_status(repo, "T-001", "done", commit="a" * 40, evidence={"tree": "sha256:" + "b" * 64})
    assert "evidence" in status_of(repo, "T-001")

    build_loop.set_task_status(repo, "T-001", "todo")
    assert "evidence" not in status_of(repo, "T-001")


# --- the reuse half -----------------------------------------------------------


def test_the_ledger_reuses_a_step_already_green_on_the_same_tree(tmp_path: Path) -> None:
    """A fact established about content is a fact about that content, not about a moment."""
    ledger = evidence.Ledger(path=tmp_path / "evidence.jsonl")
    tree, tool = "sha256:" + "c" * 64, ("test", "make", "test", "img@sha256:x")

    assert not ledger.hit(evidence.KIND_GATE_STEP, tree, tool)
    ledger.record(evidence.KIND_GATE_STEP, tree, tool)
    assert ledger.hit(evidence.KIND_GATE_STEP, tree, tool)

    # A tree that moved by a byte, or a different image, is a different claim.
    assert not ledger.hit(evidence.KIND_GATE_STEP, "sha256:" + "d" * 64, tool)
    assert not ledger.hit(evidence.KIND_GATE_STEP, tree, ("test", "make", "test", "img@sha256:y"))


def test_an_unknown_tree_is_never_a_hit_and_is_never_recorded(tmp_path: Path) -> None:
    """A fingerprint the git layer could not compute is "unknown", and unknown must cost a re-run."""
    ledger = evidence.Ledger(path=tmp_path / "evidence.jsonl")
    ledger.record(evidence.KIND_GATE_STEP, "", ("test",))
    assert not ledger.hit(evidence.KIND_GATE_STEP, "", ("test",))


def test_the_ledger_survives_a_damaged_line(tmp_path: Path) -> None:
    """A cache that can take a build down with it is worse than no cache."""
    path = tmp_path / "evidence.jsonl"
    ledger = evidence.Ledger(path=path)
    ledger.record(evidence.KIND_GATE_STEP, "sha256:" + "e" * 64, ("test",))
    ledger.flush()
    path.write_text("{ this is not json\n" + path.read_text(encoding="utf-8"), encoding="utf-8")

    reloaded = evidence.Ledger(path=path)
    reloaded._load()
    assert reloaded.hit(evidence.KIND_GATE_STEP, "sha256:" + "e" * 64, ("test",))


# --- the prose the build reads --------------------------------------------------


def freeze_sources(repo: repo_mod.Repo, sources: dict[str, str]) -> None:
    """Record `sources` as what gate ③ froze, the way `approve` would have."""
    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    raw = json.loads(json.dumps(state.raw))
    raw["plan"]["sources"] = sources
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=store_mod.read_digest(state))
        tx.append("plan_frozen", cycle_id=state.cycle_id, subject_ids=["T-001"], detail={})


def write_ticket(repo: repo_mod.Repo, body: str) -> str:
    path = repo.path("docs/tasks/T-001.md")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return digests.of_file(path)


def test_a_ticket_edited_after_the_freeze_stops_the_build(repo: repo_mod.Repo) -> None:
    """`plan.yaml` was digest-frozen; the ticket the implementer is sent to read was not.

    So an edit after gate ③ changed what got built, and nothing recorded that the thing built was
    not the thing approved.
    """
    freeze_sources(repo, {"docs/tasks/T-001.md": write_ticket(repo, "# T-001\n original\n")})
    write_ticket(repo, "# T-001\n edited after the approval\n")

    assert build(repo) == common.EXIT_CANNOT_PROCEED
    assert status_of(repo, "T-001").get("status", "todo") == "todo"


def test_an_uncommitted_ticket_edit_stops_the_build(repo: repo_mod.Repo) -> None:
    """The failure with no symptom at all.

    A leaf is cut from the work branch's tip, so it reads the committed text and nothing else. An
    uncommitted edit reached no task, silently, while its author read the new version on screen.
    """
    digest = write_ticket(repo, "# T-001\n edited but never committed\n")
    freeze_sources(repo, {"docs/tasks/T-001.md": digest})

    assert build(repo) == common.EXIT_CANNOT_PROCEED


def test_a_committed_and_unchanged_ticket_builds(repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch) -> None:
    digest = write_ticket(repo, "# T-001\n as approved\n")
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "ticket")
    freeze_sources(repo, {"docs/tasks/T-001.md": digest})
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="implemented"))

    assert build(repo) == common.EXIT_DONE


def test_a_plan_frozen_before_sources_existed_is_not_stopped(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repository upgrading mid-cycle has no `sources` to check, and is not held for lacking it."""
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="implemented"))
    assert build(repo) == common.EXIT_DONE


def test_a_change_outside_the_tasks_declared_scope_blocks_it(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The plan said where this task's work belongs, and nothing had ever read it.

    "Do not reach outside scope" was a sentence in a prompt. `scope.include` has been in the plan
    schema the whole time, carrying the same statement in a form something could check.
    """
    scope_the_plan(repo, "T-001", include=["src/"])
    monkeypatch.setattr(build_loop, "_run", agent(writes=True, reports="implemented"))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert status_of(repo, "T-001")["status"] == "blocked"
    assert ("knowledge_gap", "scope_violation") in events_of(repo)


def test_the_agent_is_told_what_it_is_rather_than_left_to_infer_it(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Role, task and sandbox reached the agent only through the shape of its prompt — which is
    guessing, and it guessed wrong in the case that mattered: a self-sandboxing CLI already
    running inside an OCI profile still tried to build a second sandbox around itself."""
    seen: list[dict[str, str]] = []

    def _run(
        cmd: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
        *,
        env: dict[str, str] | None = None,
        **_: object,
    ) -> tuple[int, str]:
        if cmd and cmd[0] == "claude":
            seen.append({k: v for k, v in (env or {}).items() if k.startswith("REIN_")})
            Path(cwd or ".").joinpath("x.py").write_text("# x\n", encoding="utf-8")
            return 0, ""
        return common.run(cmd, cwd, timeout)

    monkeypatch.setattr(build_loop, "_run", _run)
    build(repo)

    assert seen and seen[0]["REIN_ROLE"] == "implementer"
    assert seen[0]["REIN_TASK_ID"] == "T-001"
    assert seen[0]["REIN_SANDBOX"] == "host"


def test_the_dossier_is_written_where_the_agent_is_told_to_read_it(
    repo: repo_mod.Repo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The handover itself: the loop's own facts, on disk, before the agent starts guessing."""
    found: list[dict[str, object]] = []

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if cmd and cmd[0] == "claude":
            path = Path(cwd or ".") / dossier.RELATIVE_PATH / "T-001.json"
            found.append(json.loads(path.read_text(encoding="utf-8")))
            assert str(path.relative_to(Path(cwd or "."))) in cmd[-1], "the prompt must name the dossier"
            Path(cwd or ".").joinpath("x.py").write_text("# x\n", encoding="utf-8")
            return 0, ""
        return common.run(cmd, cwd, timeout)

    monkeypatch.setattr(build_loop, "_run", _run)
    build(repo)

    assert found and found[0]["task"]["id"] == "T-001"
    assert found[0]["env"]["role"] == "implementer"
