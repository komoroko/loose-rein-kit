"""The gate ④ orientation brief: derived from the SSOT, and honest about what it does not know.

Two properties carry the whole module, and both are pinned here rather than left to review:

- **Nothing is authored.** Every value traces to a document — a task id and its frozen title, a
  changed path, a configured command, an image reference, a statement id. A section that would have
  to invent a sentence to exist does not exist.
- **Absence is distinguishable from unmeasured.** A section with nothing to report is dropped, not
  emitted empty, so "no migrations changed" never reads the same as "migrations were not looked at".

The residual findings get their own pin: they were made against one task's tree at one moment, and
carrying them to gate ④ without that stamp would turn a per-task observation into a claim about the
merged review.
"""

from __future__ import annotations

from typing import Any

from rein import brief, models
from tests._support import SANDBOXED_PROFILES, make_config, make_plan, make_state, make_task


def _plan(**kwargs: Any) -> models.Plan:
    return models.Plan(make_plan(**kwargs))


def _state(**kwargs: Any) -> models.State:
    return models.State(make_state(**kwargs))


def _config(**kwargs: Any) -> models.Config:
    return models.Config(make_config(**kwargs))


# --- delivered ----------------------------------------------------------------


def test_delivered_lists_landed_tasks_in_plan_order() -> None:
    plan = _plan(tasks=[make_task("T-002", claim_ids=["C-001"]), make_task("T-001", claim_ids=["C-001"])])
    state = _state(tasks={"T-001": "done", "T-002": "done"})
    delivered = brief.derive(plan=plan, state=state, config=None)["delivered"]
    # Plan order, not completion order: the plan is what a human froze at gate ③.
    assert [row["task_id"] for row in delivered] == ["T-002", "T-001"]
    assert delivered[0]["claim_ids"] == ["C-001"]


def test_awaiting_evidence_counts_as_delivered_and_as_residual() -> None:
    """Its code merged and passed everything; only the task is parked off the frontier."""
    plan = _plan(tasks=[make_task("T-001")])
    state = _state(tasks={"T-001": "awaiting-evidence"})
    sections = brief.derive(plan=plan, state=state, config=None)
    assert [row["task_id"] for row in sections["delivered"]] == ["T-001"]
    assert sections["residuals"]["awaiting_evidence"] == ["T-001"]


def test_an_unfinished_task_is_not_delivered() -> None:
    plan = _plan(tasks=[make_task("T-001")])
    state = _state(tasks={"T-001": "blocked"})
    sections = brief.derive(plan=plan, state=state, config=None)
    assert "delivered" not in sections
    assert sections["residuals"]["blocked"] == ["T-001"]


# --- execution boundary -------------------------------------------------------


def test_the_network_line_reports_what_the_sandbox_enforced() -> None:
    sections = brief.derive(plan=None, state=None, config=_config(profiles=SANDBOXED_PROFILES))
    rows = {row["step"]: row for row in sections["execution_boundary"]}
    assert rows["test"]["sandbox"] == "oci"
    assert rows["test"]["network"] == "none"
    assert rows["test"]["image"].startswith("localhost/rein-quality@sha256:")
    assert rows["test"]["command"] == ["make", "test"]


def test_a_host_profile_is_reported_as_unconfined_not_as_none() -> None:
    """A host step has no network boundary to report, and "none" about it would be a claim the
    runtime never made — the executor only ever enforces `--network none` on an oci profile."""
    sections = brief.derive(plan=None, state=None, config=_config())  # default profiles are host
    assert {row["network"] for row in sections["execution_boundary"]} == {"unconfined"}


def test_a_step_naming_a_profile_that_does_not_exist_claims_no_boundary() -> None:
    """Reporting `network: none` for a profile nobody could resolve would be a claim about a
    sandbox that was never configured, let alone enforced."""
    config = models.Config({"quality_gate": [{"name": "test", "executor_profile": "ghost"}], "executor_profiles": {}})
    row = brief.derive(plan=None, state=None, config=config)["execution_boundary"][0]
    assert row["step"] == "test" and "network" not in row and "sandbox" not in row


def test_the_sandbox_moving_since_gate_three_is_reported() -> None:
    """Gate ③ freezes config.yaml without its image pins, so a rebuilt sandbox blocks nothing. The
    approver at gate ④ signs over evidence produced in the later one, which is a fact about that
    evidence rather than something they should have to go and look for."""
    config = _config(profiles=SANDBOXED_PROFILES)
    state = models.State({**make_state(), "plan": {"status": "frozen", "environment_digest": "sha256:" + "e" * 64}})
    drift = brief.derive(plan=None, state=state, config=config)["environment_drift"]
    assert drift["approved_at_gate_three"] == "sha256:" + "e" * 64
    assert drift["evidence_produced_in"] == config.environment_digest()


def test_an_unchanged_sandbox_reports_nothing() -> None:
    """A section that says "the environment is the approved one" on every review is a section
    people stop reading, and this one only matters when it is there."""
    config = _config(profiles=SANDBOXED_PROFILES)
    state = models.State(
        {**make_state(), "plan": {"status": "frozen", "environment_digest": config.environment_digest()}}
    )
    assert "environment_drift" not in brief.derive(plan=None, state=state, config=config)


def test_a_freeze_that_recorded_no_environment_claims_no_drift() -> None:
    """Nothing to compare against is not the same as "it moved", and it is not "it did not" either."""
    state = models.State({**make_state(), "plan": {"status": "frozen"}})
    assert "environment_drift" not in brief.derive(plan=None, state=state, config=_config())


# --- what moved underneath the code -------------------------------------------


def test_dependency_generated_and_migration_paths_are_split() -> None:
    changed = [
        "uv.lock",
        "src/app/api.py",
        "db/migrations/0003_add_index.sql",
        "web/generated/client.ts",
        "tests/test_api.py",
    ]
    sections = brief.derive(plan=None, state=None, config=None, changed_paths=changed)
    assert sections["stack"]["dependency_files"] == ["uv.lock"]
    assert sections["stack"]["generated_files"] == ["web/generated/client.ts"]
    assert sections["data"]["migrations"] == ["db/migrations/0003_add_index.sql"]


def test_a_section_with_nothing_to_report_is_absent_not_empty() -> None:
    """Emitting `{"migrations": []}` would say "we looked and found none" in a document where the
    distinction between that and "we did not look" is the whole point."""
    sections = brief.derive(plan=None, state=None, config=None, changed_paths=["src/app/api.py"])
    assert "data" not in sections and "stack" not in sections


# --- behaviour ----------------------------------------------------------------


def _statement(sid: str, category: str, path: str) -> dict[str, Any]:
    return {
        "id": sid,
        "statement": "the handler retries on 503",
        "category": category,
        "confidence": "high",
        "code_anchors": [{"path": path, "start_line": 1, "end_line": 2}],
    }


def test_behaviour_points_by_id_and_never_copies_the_prose() -> None:
    """The text carries a confidence and code anchors in `actual_extraction`; a copy here would be
    the same sentence with its epistemic status left behind."""
    statements = [
        _statement("AST-001", "public_interface", "src/app/api.py"),
        _statement("AST-002", "control_flow", "src/app/api.py"),
        _statement("AST-003", "security_boundary", "src/app/auth.py"),
    ]
    rows = brief.derive(plan=None, state=None, config=None, actual_statements=statements)["behaviour"]
    by_category = {row["category"]: row for row in rows}
    assert set(by_category) == {"public_interface", "security_boundary"}  # control_flow is the comparison's job
    assert by_category["public_interface"]["statement_ids"] == ["AST-001"]
    assert by_category["public_interface"]["paths"] == ["src/app/api.py"]
    assert not any("statement" in row for row in rows)


def test_a_statement_with_no_id_is_dropped_rather_than_pointed_at_by_an_empty_string() -> None:
    """The section is a set of pointers into `actual_extraction`; one the reader cannot follow says
    something was read out while giving no way to see what."""
    statements = [
        {"category": "public_interface", "code_anchors": [{"path": "src/a.py"}]},  # no id
        _statement("AST-002", "public_interface", "src/b.py"),
    ]
    rows = brief.derive(plan=None, state=None, config=None, actual_statements=statements)["behaviour"]
    assert rows[0]["statement_ids"] == ["AST-002"]


# --- verification and operations -----------------------------------------------


def test_verification_counts_the_tasks_a_step_was_established_for() -> None:
    plan = _plan(tasks=[make_task("T-001"), make_task("T-002")])
    state = models.State(
        {
            **make_state(tasks={"T-001": "done", "T-002": "done"}),
            "tasks": {
                "T-001": {"status": "done", "evidence": {"steps": [{"name": "test"}, {"name": "check"}]}},
                "T-002": {"status": "done", "evidence": {"steps": [{"name": "test"}]}},
            },
        }
    )
    rows = brief.derive(plan=plan, state=state, config=_config())["verification"]
    assert {row["step"]: row["established_for"] for row in rows} == {"test": 2, "check": 1}


def test_a_configured_step_established_for_nothing_still_appears() -> None:
    """The interesting row: every task's diff missed its `paths:`, or the run never got that far."""
    rows = brief.derive(plan=None, state=None, config=_config())["verification"]
    assert {row["established_for"] for row in rows} == {0}


def test_operations_reports_the_smoke_command_and_whether_it_is_required() -> None:
    gate = [
        {"name": "test", "kind": "command", "command": ["make", "test"], "executor_profile": "quality"},
        {"name": "smoke", "kind": "command", "command": [], "executor_profile": "quality", "required": False},
    ]
    sections = brief.derive(plan=None, state=None, config=_config(quality_gate=gate))
    assert sections["operations"] == {"command": [], "required": False}


# --- residual findings ---------------------------------------------------------


def _state_with_findings(findings: list[dict[str, Any]], *, status: str = "done") -> models.State:
    return models.State(
        {
            **make_state(),
            "tasks": {
                "T-001": {
                    "status": status,
                    "completed_commit": "a" * 40,
                    "evidence": {"tree": "sha256:" + "b" * 64},
                    "handoff": {"review": {"findings": findings}},
                }
            },
        }
    )


def test_a_consider_finding_reaches_gate_four_stamped_with_the_tree_it_was_made_against() -> None:
    """`consider` stops nothing by design, so nothing in the build loop ever acts on it. Both the
    reviewer's prompt and the state schema promised it would reach a human here; this is what
    makes that true — and the stamp is what stops it being read as a claim about the merged tree."""
    state = _state_with_findings(
        [{"severity": "consider", "statement": "the retry key could be threaded through", "anchor": "src/a.py:42"}]
    )
    findings = brief.residual_findings(state)
    assert len(findings) == 1
    assert findings[0]["task_id"] == "T-001"
    assert findings[0]["severity"] == "consider"
    assert findings[0]["anchor"] == "src/a.py:42"
    assert findings[0]["observed_commit"] == "a" * 40
    assert findings[0]["observed_tree"] == "sha256:" + "b" * 64


def test_a_must_fix_finding_on_a_blocked_task_is_carried_at_its_own_severity() -> None:
    """A task that blocked with its findings unresolved is exactly the case worth surfacing, and
    downgrading it to `consider` on the way here would hide why the task never landed."""
    state = _state_with_findings(
        [{"severity": "must_fix", "statement": "the token is logged in cleartext"}], status="blocked"
    )
    assert [f["severity"] for f in brief.residual_findings(state)] == ["must_fix"]


def test_a_finding_with_no_statement_is_dropped() -> None:
    state = _state_with_findings([{"severity": "consider", "statement": ""}])
    assert brief.residual_findings(state) == []


def test_no_state_yields_no_findings_rather_than_an_error() -> None:
    assert brief.residual_findings(None) == []
    assert brief.derive(plan=None, state=None, config=None) == {}
