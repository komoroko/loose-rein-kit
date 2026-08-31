"""Tests for status_api.py — the status object and the deterministic "what next".

Two things are under test. The decision table must be **first-match and total**: the same
state always yields the same recommendation, which is what lets a human predict the tool
instead of interviewing it. And the status object must never report *not measured* as *fine* —
a damaged chain, a broken gate ladder, and an unanalysable review each have to say so.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rein import models, status_api
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import (
    SANDBOXED_PROFILES,
    chain,
    make_config,
    make_plan,
    make_review,
    make_state,
    make_task,
    seed_repo,
)

PENDING_ALL = dict.fromkeys(models.GATE_ORDER, "pending")
APPROVED_ALL = dict.fromkeys(models.GATE_ORDER, "approved")

BASE: dict[str, Any] = dict(
    current_phase="build",
    gates={g: "approved" for g in ("requirements", "design", "tasks")} | {"build": "pending", "release": "pending"},
    counts=None,
    attention_count=0,
    chain_defects=0,
    uninitialized=False,
    gate_chain_broken=False,
    plan_missing=False,
    unsandboxed_profiles=[],
    unsandboxed_build_targets=[],
)


def decide(**overrides: object) -> status_api.Recommendation:
    return status_api.next_action(**{**BASE, **overrides})  # type: ignore[arg-type]


# --- the decision table, in priority order ------------------------------------


def test_a_damaged_chain_outranks_everything() -> None:
    """Every receipt binds a chain root; nothing else matters until the log is intact."""
    rec = decide(chain_defects=3, uninitialized=True, gate_chain_broken=True)
    assert rec.command == "rein events --verify"
    assert rec.kind == "fix"


def test_the_raw_template_is_pointed_at_init() -> None:
    assert decide(uninitialized=True).command.startswith("rein init")


def test_a_broken_gate_ladder_is_a_repair_not_a_phase() -> None:
    rec = decide(gate_chain_broken=True)
    assert rec.command == "rein doctor"
    assert "withdrawn" in rec.reason


def test_needs_revision_parks_everything_at_tasks() -> None:
    rec = decide(counts={"needs-revision": 1, "done": 0})
    assert rec.command == "/tasks"
    assert rec.kind == "reconcile"


def test_an_unsandboxed_profile_precedes_the_phase_rows() -> None:
    """And the command finishes the job: every image, and the pins written.

    Recommending `--profile <first of N>` sandboxed one profile of three and, without
    `--write-config`, left the digest to be copied out of the terminal by hand — so the
    recommendation never actually cleared the FAIL it was answering.
    """
    rec = decide(unsandboxed_profiles=["quality", "reviewer"], unsandboxed_build_targets=["python", "reviewer"])
    assert rec.command == "rein oci build --all --write-config"


def test_the_recommended_build_names_a_containerfile_not_a_profile() -> None:
    """`--profile` is resolved against the packaged Containerfiles, and `quality` is not one of
    them — this recommendation used to be a command that exits 1 the moment you paste it."""
    from rein import executors

    rec = decide(unsandboxed_profiles=["quality"], unsandboxed_build_targets=["python"])
    argv = rec.command.split()
    assert "--write-config" in argv
    target = argv[argv.index("--profile") + 1]
    assert target in executors.containerfile_names(), f"{rec.command!r} names no packaged Containerfile"


def test_verify_must_clear_the_events_awaiting_a_decision() -> None:
    rec = decide(current_phase="verify", attention_count=2)
    assert rec.command == "rein events --summary"
    assert rec.kind == "resolve"


def test_the_brief_comes_before_the_lifecycle() -> None:
    rec = decide(current_phase="brief", gates=PENDING_ALL)
    assert rec.command == "/req"


def test_everything_approved_closes_the_cycle() -> None:
    rec = decide(current_phase="done", gates=APPROVED_ALL)
    assert rec.command.startswith("rein cycle-close")
    assert rec.kind == "close"


def test_a_phase_in_progress_points_at_its_own_command() -> None:
    rec = decide(current_phase="build")
    assert rec.command == "/build"
    assert "rein approve build --check" in rec.also


def test_an_approved_gate_advances_to_the_next_phase() -> None:
    rec = decide(current_phase="tasks", gates={**BASE["gates"], "tasks": "approved"})  # type: ignore[dict-item]
    assert rec.command == "/build"
    assert "rein build" in rec.also


# --- the gate waiting on a human ----------------------------------------------
#
# The kind existed, `pending_decision` special-cased it, and no row ever produced one: a phase
# that was finished and a gate with nothing in its way still recommended running the phase again,
# and `waiting_on_human` stayed False for the entire wait.


def test_a_ready_gate_is_the_humans_decision_not_another_phase_run() -> None:
    rec = decide(current_phase="build", gate_ready=True)
    assert rec.command == "rein approve build"
    assert rec.kind == "approve_gate"
    assert "rein ui" in rec.also


def test_a_ready_gate_says_the_agent_does_not_run_it() -> None:
    """The recommendation is about to be printed to agents that are built to run the next
    recommended command — so the sentence that stops them travels with it."""
    assert "an agent never runs it for you" in decide(current_phase="build", gate_ready=True).reason


def test_a_blocked_gate_still_points_at_the_phase() -> None:
    rec = decide(current_phase="build", gate_ready=False)
    assert rec.command == "/build"
    assert rec.kind == "run_phase"


def test_an_unprobed_gate_is_not_treated_as_ready() -> None:
    """`None` means readiness was not probed, which is not the same as probed-and-clear. A board
    that never looked must not tell a human their gate is waiting on them."""
    assert decide(current_phase="build", gate_ready=None).kind == "run_phase"


def test_a_ready_gate_makes_the_decision_wait_on_a_human() -> None:
    rec = decide(current_phase="build", gate_ready=True)
    decision = status_api.pending_decision(rec, "build")
    assert decision["waiting_on_human"] is True
    assert decision["kind"] == "approve_gate"
    # The id names the gate, so the notification does not re-fire as an unrelated row appears.
    assert decision["id"] == "approve_gate:build:rein approve build"


def test_the_queue_and_the_recommendation_agree_about_readiness() -> None:
    """Both are derived from the same `gate_blockers`, so a board showing "waiting on your
    decision" and a recommendation saying "run the phase again" cannot coexist."""
    rec = decide(current_phase="build", gate_ready=True)
    queue = status_api.pending_queue(
        probe_gate="build",
        gate_blockers=[],
        chain_defects=0,
        unsandboxed_profiles=[],
        unsandboxed_build_targets=[],
        attention=[],
        task_rows=[],
    )
    ready = [row for row in queue if row["kind"] == "gate_ready"]
    assert len(ready) == 1
    assert ready[0]["action"] == rec.command


def test_a_missing_plan_mid_lifecycle_is_a_repair() -> None:
    assert decide(plan_missing=True, current_phase="build").command == "rein doctor"


def test_an_off_vocabulary_phase_is_diagnosed_not_guessed() -> None:
    rec = decide(current_phase="somewhere")
    assert rec.command == "rein doctor"
    assert "not in the lifecycle vocabulary" in rec.reason


def test_the_table_is_total_over_the_lifecycle() -> None:
    for phase in models.PHASE_ORDER:
        assert decide(current_phase=phase, gates=PENDING_ALL).command


# --- the status object --------------------------------------------------------


def test_status_reports_gates_evidence_and_the_chain(tmp_path: Path) -> None:
    seed_repo(tmp_path, events=chain("cycle_initialized"))
    status = status_api.collect_status(repo_mod.Repo(tmp_path))

    assert status["project"] == "demo"
    assert status["plan_status"] == "frozen"
    gates = status["gates"]
    assert isinstance(gates, list) and len(gates) == 5
    assert gates[0]["approval_id"] == "GA-REQUIREMENTS-0001"

    plan = status["plan"]
    assert isinstance(plan, dict) and plan["claims"] == 1
    chain_block = status["chain"]
    assert isinstance(chain_block, dict) and chain_block["events"] == 1 and chain_block["defects"] == []


def test_an_ungenerated_review_says_so_rather_than_reporting_zero(tmp_path: Path) -> None:
    seed_repo(tmp_path, review=make_review(generated=False))
    review = status_api.collect_status(repo_mod.Repo(tmp_path))["review"]
    assert isinstance(review, dict) and review == {"status": "not_generated"}


def test_an_insufficient_coverage_manifest_makes_the_count_undeterminable(tmp_path: Path) -> None:
    """ "0 extra behaviours" next to an unanalysable diff is the most misleading thing this
    tool could print, so the count is withheld entirely (plan §2.4)."""
    seed_repo(tmp_path, review=make_review(generated=True, coverage_status="insufficient"))
    review = status_api.collect_status(repo_mod.Repo(tmp_path))["review"]
    assert isinstance(review, dict)
    assert review["coverage"] == "undeterminable"
    assert review["extra_behaviors"] is None


def test_sufficient_coverage_reports_the_count(tmp_path: Path) -> None:
    seed_repo(tmp_path, review=make_review(generated=True))
    review = status_api.collect_status(repo_mod.Repo(tmp_path))["review"]
    assert isinstance(review, dict)
    assert review["coverage"] == "sufficient" and review["extra_behaviors"] == 0


def test_a_damaged_chain_is_a_warning_and_a_recommendation(tmp_path: Path) -> None:
    seed_repo(tmp_path, events=chain("cycle_initialized", "task_completed"))
    log = tmp_path / ".rein" / "events.ndjson"
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other", 1), encoding="utf-8")

    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    warnings = status["warnings"]
    assert isinstance(warnings, list) and any("audit chain" in w for w in warnings)
    assert status["next"]["command"] == "rein events --verify"  # type: ignore[index]


def test_status_stays_up_over_a_broken_document(tmp_path: Path) -> None:
    """The dashboard has to stay up precisely when the state is odd — that is when a human
    most needs to look at it."""
    seed_repo(tmp_path)
    (tmp_path / ".rein" / "plan.yaml").write_text("claims: [\n", encoding="utf-8")
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    warnings = status["warnings"]
    assert isinstance(warnings, list) and any("plan.yaml" in w for w in warnings)
    assert status["next"]["command"]  # type: ignore[index]


def test_an_inconsistent_task_graph_is_a_warning_not_a_crash(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(tasks={"T-777": "done"}))
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    warnings = status["warnings"]
    assert isinstance(warnings, list) and any("task graph is inconsistent" in w for w in warnings)


def test_the_tasks_block_is_entirely_derived(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        plan=make_plan(
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
            ]
        ),
        state=make_state(tasks={"T-001": "done"}),
    )
    tasks = status_api.collect_status(repo_mod.Repo(tmp_path))["tasks"]
    assert isinstance(tasks, dict)
    assert tasks["counts"]["done"] == 1  # type: ignore[index]
    assert tasks["critical_path"] == ["T-001", "T-002"]
    assert [t["id"] for t in tasks["frontier"]] == ["T-002"]  # type: ignore[union-attr]


def test_a_done_task_carries_the_commit_that_landed_it(tmp_path: Path) -> None:
    """The board's "done" is otherwise a word with nothing behind it: the hash is what makes it
    something a reviewer can go and read."""
    state = make_state(tasks={"T-001": "done", "T-002": "todo"})
    state["tasks"]["T-001"]["completed_commit"] = "f" * 40
    seed_repo(
        tmp_path,
        plan=make_plan(tasks=[make_task("T-001"), make_task("T-002", kind="parallel", blocked_by=["T-001"])]),
        state=state,
    )
    rows = status_api.collect_status(repo_mod.Repo(tmp_path))["tasks"]["rows"]  # type: ignore[index]
    by_id = {r["id"]: r for r in rows}  # type: ignore[union-attr]
    assert by_id["T-001"]["commit"] == "f" * 40
    assert by_id["T-002"]["commit"] == ""  # nothing landed it, so the row says nothing


# --- attention: a since-resolved task_failed/knowledge_gap must not linger forever -------------


def _chain_with_subjects(*specs: tuple[str, tuple[str, ...]]) -> list[models.Event]:
    """Like `_support.chain`, but each entry also carries the `subject_ids` this module needs."""
    from rein import event_chain

    built: list[models.Event] = []
    previous: models.Event | None = None
    for name, subjects in specs:
        linked = event_chain.link(previous, event_chain.make(name, "demo-cycle", subject_ids=subjects))
        built.append(linked)
        previous = linked
    return built


def test_a_task_failed_for_a_now_done_task_is_not_pending_attention(tmp_path: Path) -> None:
    """T-020 in the wild: three `task_failed` events from earlier attempts, the task later
    reaches `done` — the status board must stop asking a human to look at it."""
    seed_repo(
        tmp_path,
        events=_chain_with_subjects(("task_failed", ("T-001",)), ("knowledge_gap", ("T-001",))),
        state=make_state(tasks={"T-001": "done"}),
    )
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    queue = status["pending"]
    assert isinstance(queue, list)
    assert not [item for item in queue if item["kind"] == "escalation"]


def test_a_task_failed_for_a_still_blocked_task_still_shows(tmp_path: Path) -> None:
    seed_repo(
        tmp_path,
        events=_chain_with_subjects(("task_failed", ("T-001",))),
        state=make_state(tasks={"T-001": "blocked"}),
    )
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    queue = status["pending"]
    assert isinstance(queue, list)
    assert [item for item in queue if item["kind"] == "escalation"]


def test_a_batch_task_failed_stays_until_every_named_task_is_done(tmp_path: Path) -> None:
    """One event can name several tasks (a batch escalation) — it must not be dropped just
    because *some* of them finished."""
    seed_repo(
        tmp_path,
        events=_chain_with_subjects(("task_failed", ("T-001", "T-002"))),
        state=make_state(tasks={"T-001": "done", "T-002": "blocked"}),
    )
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    queue = status["pending"]
    assert isinstance(queue, list)
    assert [item for item in queue if item["kind"] == "escalation"]


def test_plan_invalidated_stands_until_the_plan_is_frozen_again(tmp_path: Path) -> None:
    """A task finishing says nothing about it — the plan is not a task."""
    seed_repo(
        tmp_path,
        events=_chain_with_subjects(("plan_invalidated", ())),
        state=make_state(tasks={"T-001": "done"}),
    )
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    queue = status["pending"]
    assert isinstance(queue, list)
    assert [item for item in queue if item["kind"] == "escalation"]


def test_a_re_freeze_retires_the_plan_invalidated_it_answered(tmp_path: Path) -> None:
    """The roll back happened, the human re-approved gate ③, and the queue said "waiting for you"
    about it for the rest of the cycle. `plan_frozen` is the event that undoes exactly the state
    `plan_invalidated` reported, so it is what closes the row — nothing is erased from the log."""
    seed_repo(
        tmp_path,
        events=_chain_with_subjects(("plan_invalidated", ()), ("plan_frozen", ("APPROVAL-1",))),
        state=make_state(tasks={"T-001": "done"}),
    )
    status = status_api.collect_status(repo_mod.Repo(tmp_path))
    queue = status["pending"]
    assert isinstance(queue, list)
    assert not [item for item in queue if item["kind"] == "escalation"]


def test_task_status_of_answers_empty_for_an_unreadable_repository(tmp_path: Path) -> None:
    """The safe direction: no statuses retires nothing, rather than a crash or a cleared queue."""
    assert status_api.task_status_of(tmp_path) == {}


# --- the pending queue --------------------------------------------------------
#
# The recommendation is first-match, so it stops at one row and cannot answer "how much is
# waiting". These pin the queue that can: its severity ordering, and — the part that matters most
# — that "we did not probe" never renders as "there is nothing there".

QUEUE_BASE: dict[str, Any] = dict(
    probe_gate=None,
    gate_blockers=None,
    chain_defects=0,
    unsandboxed_profiles=[],
    unsandboxed_build_targets=[],
    attention=[],
    task_rows=[],
)


def test_the_queue_is_ordered_by_severity_not_by_discovery() -> None:
    queue = status_api.pending_queue(
        **{
            **QUEUE_BASE,
            "probe_gate": "build",
            "gate_blockers": ["the review is bound to a commit that is no longer HEAD"],
            "task_rows": [{"id": "T-007", "status": "needs-revision", "title": "rework the parser"}],
        }
    )
    assert [item["severity"] for item in queue] == ["blocking", "attention"]
    assert [item["kind"] for item in queue] == ["gate_blocker", "task_revision"]
    assert queue[1]["action"] == "/tasks"


def test_an_unprobed_gate_is_not_a_clean_gate() -> None:
    """`None` (not probed) and `[]` (probed, clean) must not render as the same thing."""
    unprobed = status_api.pending_queue(**{**QUEUE_BASE, "probe_gate": "build", "gate_blockers": None})
    clean = status_api.pending_queue(**{**QUEUE_BASE, "probe_gate": "build", "gate_blockers": []})
    assert unprobed == []
    assert [item["kind"] for item in clean] == ["gate_ready"]
    assert clean[0]["action"] == "rein approve build"


def test_a_queue_row_keeps_its_identity_until_what_it_says_changes() -> None:
    """The dashboard tracks rows by id; an index would relabel every row below a fixed one."""
    args = {**QUEUE_BASE, "probe_gate": "build", "gate_blockers": ["a", "b"]}
    first = status_api.pending_queue(**args)  # type: ignore[arg-type]
    assert status_api.pending_queue(**args)[0]["id"] == first[0]["id"]  # type: ignore[arg-type]
    fixed_the_first = status_api.pending_queue(**{**args, "gate_blockers": ["b"]})  # type: ignore[arg-type]
    assert fixed_the_first[0]["id"] == first[1]["id"]


def test_the_queue_asks_readiness_for_the_gate_the_current_phase_will_present(tmp_path: Path) -> None:
    """The blocking rows come from `approve.readiness` — the same list `approve --check` refuses on.

    Deriving them a second way here is how a board ends up disagreeing with the gate it describes.
    """
    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))
    asked: list[str] = []

    def probe(_repo: repo_mod.Repo, gate: str) -> list[str]:
        asked.append(gate)
        return ["the human review is not frozen"]

    status = status_api.collect_status(repo_mod.Repo(tmp_path), readiness_probe=probe)
    assert asked == ["build"]  # the seed fixture sits in the build phase
    assert status["pending_deep"] is True
    queue = status["pending"]
    assert isinstance(queue, list)
    blocking = [item for item in queue if item["kind"] == "gate_blocker"]
    assert [item["headline"] for item in blocking] == ["the human review is not frozen"]


def test_a_probe_that_cannot_answer_degrades_to_a_warning(tmp_path: Path) -> None:
    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))

    def probe(_repo: repo_mod.Repo, _gate: str) -> list[str]:
        raise OSError("the object store is unreadable")

    status = status_api.collect_status(repo_mod.Repo(tmp_path), readiness_probe=probe)
    assert status["pending_deep"] is False
    warnings = status["warnings"]
    assert isinstance(warnings, list) and any("cannot check gate readiness" in w for w in warnings)


def test_an_uninitialized_template_is_not_interrogated_for_gate_blockers(tmp_path: Path) -> None:
    """Every gate is blocked by the initialization itself, which the recommendation already says."""
    seed_repo(tmp_path, config=make_config(template_mode=True, profiles=SANDBOXED_PROFILES))

    def probe(_repo: repo_mod.Repo, _gate: str) -> list[str]:
        raise AssertionError("readiness must not be probed for a repo that is still the template")

    status = status_api.collect_status(repo_mod.Repo(tmp_path), readiness_probe=probe)
    assert status["pending_deep"] is False


def test_render_puts_the_queue_above_the_gates(tmp_path: Path) -> None:
    """A reader opens the board for what is in the way, not for a list of five gate names."""
    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))
    status = status_api.collect_status(
        repo_mod.Repo(tmp_path), readiness_probe=lambda _r, _g: ["coverage is insufficient"]
    )
    rendered = status_api.render(status)
    assert rendered.index("### Waiting on you") < rendered.index("### Gates")
    assert "1 blocking" in rendered and "coverage is insufficient" in rendered


def test_the_board_says_when_it_did_not_look(tmp_path: Path) -> None:
    seed_repo(tmp_path, config=make_config(template_mode=True))
    rendered = status_api.render(status_api.collect_status(repo_mod.Repo(tmp_path)))
    assert "gate readiness not probed" in rendered


# --- rendering and the CLI ----------------------------------------------------


def test_render_next_formats_command_reason_and_also() -> None:
    rendered = status_api.render_next({"command": "/build", "reason": "because", "also": ["rein build"]})
    assert rendered.splitlines() == ["next: /build", "  why: because", "  also: rein build"]


def test_render_names_the_three_review_axes_and_never_a_bare_verified(tmp_path: Path) -> None:
    seed_repo(tmp_path, review=make_review(generated=True))
    rendered = status_api.render(status_api.collect_status(repo_mod.Repo(tmp_path)))
    assert "### Gates" in rendered and "### Plan" in rendered and "### Review" in rendered
    assert "verified" not in rendered.lower()


def test_cli_json_is_one_recommendation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert status_api.main(["--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["command"] and parsed["kind"]  # `rein next --json` is one recommendation


def test_cli_next_prints_only_the_recommendation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert status_api.main([]) == 0
    out = capsys.readouterr().out
    assert out.startswith("next: ")
    assert "### Gates" not in out


def test_a_slash_command_is_qualified_when_no_agent_surface_is_installed(tmp_path: Path) -> None:
    """Recommending /build in a repo with no integration sends the user to a command their
    agent has never heard of."""
    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))
    reason = status_api.collect_status(repo_mod.Repo(tmp_path))["next"]["reason"]  # type: ignore[index]
    assert "No agent surface is installed" in reason


def test_surfaces_on_disk_count_even_when_the_lock_records_no_install(tmp_path: Path) -> None:
    """A repository cloned from the template carries working .claude wrappers that nothing
    installed — `rein init` installs no surface. Reading the lock alone told that user
    their working /-commands did not exist. `doctor` is where the unrecorded copies get
    diagnosed; here they simply are a surface."""
    from rein import install

    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))
    for rel, blob in install._dest_map(install.INTEGRATIONS["claude"]).items():
        dest = tmp_path / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
    reason = status_api.collect_status(repo_mod.Repo(tmp_path))["next"]["reason"]  # type: ignore[index]
    assert "No agent surface is installed" not in reason


def test_the_gate_and_phase_maps_agree_with_the_vocabulary() -> None:
    assert set(status_api.PHASE_GATE.values()) == set(models.GATE_ORDER)
    assert set(status_api.GATE_PHASE) == set(models.GATE_ORDER)
    assert set(status_api.PHASE_COMMAND) <= set(models.PHASE_ORDER)


def test_a_blocking_finding_the_plan_owns_is_recommended_before_the_phase_command() -> None:
    """Typing the ids in by hand is the clerical half of a decision the plan already made."""
    rec = status_api.next_action(
        current_phase="build",
        gates={
            "requirements": "approved",
            "design": "approved",
            "tasks": "approved",
            "build": "pending",
            "release": "pending",
        },
        counts={"todo": 0, "in-progress": 0, "done": 2, "blocked": 0, "needs-revision": 0, "awaiting-evidence": 0},
        attention_count=0,
        chain_defects=0,
        uninitialized=False,
        gate_chain_broken=False,
        plan_missing=False,
        unsandboxed_profiles=[],
        attributed_findings=2,
    )

    assert rec.command.startswith("rein revise --to build --from-review")
    assert "rein pr-stack --restack" in rec.also


def test_no_finding_leaves_the_build_recommendation_alone() -> None:
    rec = status_api.next_action(
        current_phase="build",
        gates={
            "requirements": "approved",
            "design": "approved",
            "tasks": "approved",
            "build": "pending",
            "release": "pending",
        },
        counts={"todo": 1, "in-progress": 0, "done": 0, "blocked": 0, "needs-revision": 0, "awaiting-evidence": 0},
        attention_count=0,
        chain_defects=0,
        uninitialized=False,
        gate_chain_broken=False,
        plan_missing=False,
        unsandboxed_profiles=[],
        attributed_findings=0,
    )

    assert "--from-review" not in rec.command


@pytest.mark.parametrize(
    ("template_mode", "project", "expected"),
    [
        (True, "demo", True),  # template_mode alone is enough
        (False, "<product-name>", True),  # so is an unfilled placeholder
        (False, "demo", False),
    ],
)
def test_uninitialized_is_decided_by_two_documents(
    tmp_path: Path, template_mode: bool, project: str, expected: bool
) -> None:
    """One definition of "not a product yet", so `rein start` and the recommendation agree.

    `rein start` calls this directly rather than collecting the whole status object, which would
    run git subprocesses and readiness probes for a boolean these two documents already carry.
    """
    seed_repo(tmp_path, config=make_config(template_mode=template_mode), state=make_state(project=project))
    store = store_mod.Store(repo_mod.Repo(tmp_path))

    assert status_api.is_uninitialized(store.read_config(), store.read_state()) is expected


def test_nothing_read_yet_is_treated_as_uninitialized(tmp_path: Path) -> None:
    """Fail closed: a repository whose documents cannot be read is not a product to work in."""
    assert status_api.is_uninitialized(None, None) is True
