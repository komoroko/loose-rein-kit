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
    template_mode=False,
    placeholders=False,
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
    rec = decide(chain_defects=3, template_mode=True, gate_chain_broken=True)
    assert rec.command == "rein events --verify"
    assert rec.kind == "fix"


def test_the_raw_template_is_pointed_at_init() -> None:
    assert decide(template_mode=True).command.startswith("rein init")
    assert decide(placeholders=True).kind == "setup"


def test_a_broken_gate_ladder_is_a_repair_not_a_phase() -> None:
    rec = decide(gate_chain_broken=True)
    assert rec.command == "rein doctor"
    assert "withdrawn" in rec.reason


def test_needs_revision_parks_everything_at_tasks() -> None:
    rec = decide(counts={"needs-revision": 1, "done": 0})
    assert rec.command == "/tasks"
    assert rec.kind == "reconcile"


def test_an_unsandboxed_profile_precedes_the_phase_rows() -> None:
    rec = decide(unsandboxed_profiles=["quality", "reviewer"], unsandboxed_build_targets=["python", "reviewer"])
    assert rec.command == "rein oci build --profile python"


def test_the_recommended_build_names_a_containerfile_not_a_profile() -> None:
    """`--profile` is resolved against the packaged Containerfiles, and `quality` is not one of
    them — this recommendation used to be a command that exits 1 the moment you paste it."""
    from rein import executors

    rec = decide(unsandboxed_profiles=["quality"], unsandboxed_build_targets=["python"])
    target = rec.command.rsplit(" ", 1)[1]
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


def test_cli_json_is_parseable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert status_api.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "demo"


def test_cli_next_prints_only_the_recommendation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert status_api.main(["--next"]) == 0
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
