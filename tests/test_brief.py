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


# --- what this change requires of a person ------------------------------------


def _statement(sid: str, category: str, path: str) -> dict[str, Any]:
    return {
        "id": sid,
        "statement": "the handler retries on 503",
        "category": category,
        "confidence": "high",
        "code_anchors": [{"path": path, "start_line": 1, "end_line": 2}],
    }


def _surface(kind: str, name: str, paths: list[str], adr: str | None = None) -> dict[str, Any]:
    entry: dict[str, Any] = {"kind": kind, "name": name, "paths": paths}
    if adr:
        entry["adr"] = adr
    return entry


def test_a_reading_no_task_declared_is_the_first_thing_the_section_says() -> None:
    """Nobody decided this would be somebody's job, and the approver is signing over it anyway."""
    statements = [_statement("AST-001", "persistence", "db/schema/users.sql")]
    section = brief.derive(plan=None, state=None, config=None, actual_statements=statements)["requirements_on_people"]
    assert [row["statement_id"] for row in section["undeclared"]] == ["AST-001"]
    # With its status attached: a sentence separated from its confidence and its anchor is not
    # evidence of anything, which is the whole reason this may carry the text at all.
    assert section["undeclared"][0]["statement"] == "the handler retries on 503"
    assert section["undeclared"][0]["confidence"] == "high"
    assert section["undeclared"][0]["paths"] == ["db/schema/users.sql"]


def test_a_declared_surface_the_extractor_confirms_becomes_a_count_not_a_row() -> None:
    plan = _plan(tasks=[make_task("T-001", operator_surface=[_surface("persistence", "users", ["db/schema"])])])
    statements = [_statement("AST-001", "persistence", "db/schema/users.sql")]
    section = brief.derive(plan=plan, state=None, config=None, actual_statements=statements)["requirements_on_people"]
    assert "undeclared" not in section
    assert section["as_declared"]["count"] == 1
    assert section["as_declared"]["entries"][0]["statement_ids"] == ["AST-001"]


def test_the_category_has_to_match_as_well_as_the_path() -> None:
    """A declaration about the schema does not foresee a public interface that lives in the same file."""
    plan = _plan(tasks=[make_task("T-001", operator_surface=[_surface("persistence", "users", ["db/schema"])])])
    statements = [_statement("AST-001", "public_interface", "db/schema/users.sql")]
    section = brief.derive(plan=plan, state=None, config=None, actual_statements=statements)["requirements_on_people"]
    assert [row["statement_id"] for row in section["undeclared"]] == ["AST-001"]
    assert [row["name"] for row in section["unobserved"]] == ["users"]


def test_a_declaration_nothing_was_read_out_about_is_reported_as_unobserved() -> None:
    """Not built, or not readable — this says only that the reading is absent."""
    plan = _plan(
        tasks=[make_task("T-001", operator_surface=[_surface("dependency", "redis", ["deploy/"], adr="ADR-004")])]
    )
    section = brief.derive(plan=plan, state=None, config=None)["requirements_on_people"]
    assert section["unobserved"] == [
        {"task_id": "T-001", "kind": "dependency", "name": "redis", "paths": ["deploy/"], "adr": "ADR-004"}
    ]
    assert "as_declared" not in section


def test_the_trailing_slash_in_a_declared_path_changes_nothing() -> None:
    """The same rule as a task's `scope`, through the same helper — not a second spelling."""
    statements = [_statement("AST-001", "persistence", "db/schema/users.sql")]
    matched = []
    for pattern in ("db/schema", "db/schema/"):
        plan = _plan(tasks=[make_task("T-001", operator_surface=[_surface("persistence", "users", [pattern])])])
        section = brief.derive(plan=plan, state=None, config=None, actual_statements=statements)[
            "requirements_on_people"
        ]
        matched.append(section.get("as_declared", {}).get("count"))
    assert matched == [1, 1]


def test_an_internal_category_is_not_something_anybody_operates() -> None:
    """`control_flow` and `state_propagation` describe the inside of the code; nobody runs them."""
    statements = [_statement("AST-001", "control_flow", "src/app/api.py")]
    sections = brief.derive(plan=None, state=None, config=None, actual_statements=statements)
    assert "requirements_on_people" not in sections


def test_a_statement_with_no_id_is_dropped_rather_than_reported_without_a_way_to_reach_it() -> None:
    statements = [
        {"category": "persistence", "code_anchors": [{"path": "db/a.sql"}]},  # no id
        _statement("AST-002", "persistence", "db/b.sql"),
    ]
    section = brief.derive(plan=None, state=None, config=None, actual_statements=statements)["requirements_on_people"]
    assert [row["statement_id"] for row in section["undeclared"]] == ["AST-002"]


def test_the_as_built_view_names_the_blob_rather_than_copying_the_file() -> None:
    """review.yaml is not a second copy of the repository; the body is fetched on demand."""
    plan = _plan(tasks=[make_task("T-001", operator_surface=[_surface("persistence", "users", ["db/users.sql"])])])
    section = brief.derive(
        plan=plan,
        state=None,
        config=None,
        blob_facts=lambda path: {"blob": "a" * 40, "bytes": 120} if path == "db/users.sql" else None,
    )["requirements_on_people"]
    assert section["unobserved"][0]["as_built"] == [{"path": "db/users.sql", "blob": "a" * 40, "bytes": 120}]


def test_a_declared_path_that_does_not_exist_at_the_reviewed_commit_is_simply_absent() -> None:
    plan = _plan(tasks=[make_task("T-001", operator_surface=[_surface("persistence", "users", ["db/users.sql"])])])
    section = brief.derive(plan=plan, state=None, config=None, blob_facts=lambda path: None)["requirements_on_people"]
    assert "as_built" not in section["unobserved"][0]


# --- verification and operations -----------------------------------------------


def test_verification_reports_a_step_established_for_nothing_and_counts_the_rest() -> None:
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
    section = brief.derive(plan=plan, state=state, config=_config())["verification"]
    # The steps that ran for something say what everyone already assumed, so they are a number, and
    # with no exception left there is no list at all.
    assert section == {"steps": len(_config().quality_gate)}


def test_every_step_established_for_nothing_leaves_only_the_exceptions() -> None:
    """The interesting row: every task's diff missed its `paths:`, or the run never got that far."""
    section = brief.derive(plan=None, state=None, config=_config())["verification"]
    assert section["established_for_nothing"] == [step.name for step in _config().quality_gate]


def _operations_for(*steps: dict[str, Any]) -> dict[str, Any] | None:
    sections = brief.derive(plan=None, state=None, config=_config(quality_gate=list(steps)))
    value = sections.get("operations")
    return value if isinstance(value, dict) else None


def _step(name: str, command: list[str], *, required: bool = False) -> dict[str, Any]:
    return {"name": name, "kind": "command", "command": command, "executor_profile": "quality", "required": required}


def test_operations_reports_the_smoke_command_and_whether_it_is_required() -> None:
    assert _operations_for(_step("test", ["make", "test"]), _step("smoke", [])) == {
        "command": [],
        "required": False,
        "placeholder": False,
    }


def test_the_shipped_placeholder_is_reported_as_one() -> None:
    """`["true"]` has a command and cannot fail. The panel used to say nothing at all about it."""
    assert _operations_for(_step("smoke", ["true"], required=True)) == {
        "command": ["true"],
        "required": True,
        "placeholder": True,
    }


def test_a_real_launch_command_is_not_a_placeholder() -> None:
    assert _operations_for(_step("smoke", ["./app", "--version"])) == {
        "command": ["./app", "--version"],
        "required": False,
        "placeholder": False,
    }


def test_no_step_named_smoke_reports_nothing_rather_than_an_empty_command() -> None:
    """ "No step declares the launch" is a different fact from "the launch step ran nothing"."""
    assert _operations_for(_step("launch", ["./app"])) is None


# --- the account of a task that did not land -----------------------------------


def _with_report(status: str, outcome: str, summary: str) -> models.State:
    return models.State(
        {
            **make_state(),
            "tasks": {"T-001": {"status": status, "handoff": {"report": {"outcome": outcome, "summary": summary}}}},
        }
    )


def test_an_unfinished_task_carries_what_the_implementer_said_about_it() -> None:
    """`rein report` has always written this and nobody approving a gate has ever read it — and the
    task it is about is one the approver is being asked to sign around."""
    residuals = brief.derive(
        plan=None, state=_with_report("blocked", "blocked", "the API key is not in the image"), config=None
    )["residuals"]
    assert residuals["accounts"] == [
        {"task_id": "T-001", "outcome": "blocked", "summary": "the API key is not in the image"}
    ]


def test_a_task_that_landed_does_not_hand_the_approver_the_implementer_explanation() -> None:
    """It is the one input the blind extractor may not see; showing it at the moment of decision
    moves that priming onto the person the arrangement exists to protect."""
    sections = brief.derive(plan=None, state=_with_report("done", "implemented", "wrote the adapter"), config=None)
    # Nothing is open, so there is no residual section at all — and no account inside one.
    assert "residuals" not in sections


def test_an_unfinished_task_that_reported_nothing_produces_no_row() -> None:
    state = models.State({**make_state(), "tasks": {"T-001": {"status": "blocked"}}})
    assert "accounts" not in brief.derive(plan=None, state=state, config=None)["residuals"]


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


def test_a_reading_two_tasks_both_declared_is_credited_to_both() -> None:
    """Crediting only the first would report the second as "nothing was read out about this" while
    the reading sits in the very same list."""
    plan = _plan(
        tasks=[
            make_task("T-001", operator_surface=[_surface("persistence", "users", ["db/"])]),
            make_task("T-002", operator_surface=[_surface("persistence", "schema", ["db/schema"])]),
        ]
    )
    statements = [_statement("AST-001", "persistence", "db/schema/users.sql")]
    section = brief.derive(plan=plan, state=None, config=None, actual_statements=statements)["requirements_on_people"]
    assert "unobserved" not in section
    assert section["as_declared"]["count"] == 2


# --- the negative control -----------------------------------------------------
#
# The record `build_loop` writes beside each task's status, and which nothing read until the brief
# did. The pin that matters is the asymmetry: a control that answered is a number, one that could
# not be taken is a row naming the task, because the second is the one an approver has to know.


def _controlled(**by_task: dict[str, Any]) -> dict[str, Any]:
    state = models.State(
        {
            **make_state(tasks={task_id: "done" for task_id in by_task}),
            "tasks": {
                task_id: {"status": "done", "evidence": {"negative_control": control}}
                for task_id, control in by_task.items()
            },
        }
    )
    section = brief.derive(plan=None, state=state, config=_config()).get("control", {})
    return section if isinstance(section, dict) else {}


def test_a_control_that_answered_is_counted_not_listed() -> None:
    section = _controlled(
        **{
            "T-001": {"result": "discriminating", "step": "test"},
            "T-002": {"result": "discriminating", "step": "test"},
        }
    )
    assert section == {"discriminating": 2}


def test_a_task_that_changed_no_test_file_is_named_with_its_reason() -> None:
    """The whole point of the record: this task's green rests on tests nobody wrote for it."""
    section = _controlled(
        **{
            "T-001": {"result": "discriminating", "step": "test"},
            "T-002": {"result": "no_tests_changed", "detail": "the change touched no test path"},
        }
    )
    assert section == {
        "discriminating": 1,
        "no_tests_changed": [{"task_id": "T-002", "detail": "the change touched no test path"}],
    }


def test_a_control_that_could_not_be_set_up_is_its_own_row() -> None:
    section = _controlled(**{"T-001": {"result": "undetermined", "detail": "the worktree would not create"}})
    assert section == {"undetermined": [{"task_id": "T-001", "detail": "the worktree would not create"}]}


def test_a_task_that_did_not_land_carries_no_control_row() -> None:
    """A blocked task has no `done` for evidence to justify, so its control is not evidence here."""
    state = models.State(
        {
            **make_state(tasks={"T-001": "blocked"}),
            "tasks": {"T-001": {"status": "blocked", "evidence": {"negative_control": {"result": "no_tests_changed"}}}},
        }
    )
    assert "control" not in brief.derive(plan=None, state=state, config=_config())


def test_a_run_that_recorded_no_control_reports_nothing_rather_than_zero() -> None:
    """Absence stays distinguishable from unmeasured: no section, not `discriminating: 0`."""
    state = models.State({**make_state(tasks={"T-001": "done"}), "tasks": {"T-001": {"status": "done"}}})
    assert "control" not in brief.derive(plan=None, state=state, config=_config())
