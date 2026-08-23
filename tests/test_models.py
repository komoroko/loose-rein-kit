"""Tests for models.py — the vocabulary, the document views, and cross-reference validation.

The first section is a *drift canary*, in the same spirit as template_lint: every enum in a
shipped JSON Schema and every vocabulary constant in models.py are two spellings of one fact,
and a release whose schema says one thing while its code believes another is precisely the
quiet divergence this canary exists to make impossible.
"""

from __future__ import annotations

from typing import Any

import pytest

from rein import digests, models, strict_yaml
from tests._support import make_plan, make_task

SCHEMA_NAMES = ("plan", "state", "review", "event", "config")


# --- drift canary: schema enums <-> models.py vocabulary ----------------------


def _python_vocabularies() -> dict[frozenset[str], list[str]]:
    """Every module-level frozenset-of-str in models.py, indexed by its value set."""
    index: dict[frozenset[str], list[str]] = {}
    for name, value in vars(models).items():
        if name.startswith("_") or not isinstance(value, frozenset) or not value:
            continue
        if all(isinstance(item, str) for item in value):
            index.setdefault(frozenset(value), []).append(name)
    return index


def _enums(node: Any, pointer: str = "") -> list[tuple[str, frozenset[str]]]:
    """Every `enum` array in a schema, as (json pointer, value set)."""
    found: list[tuple[str, frozenset[str]]] = []
    if isinstance(node, dict):
        raw = node.get("enum")
        if isinstance(raw, list) and all(isinstance(v, str) for v in raw):
            found.append((pointer or "/", frozenset(raw)))
        for key, child in node.items():
            if key != "enum":
                found += _enums(child, f"{pointer}/{key}")
    elif isinstance(node, list):
        for index, child in enumerate(node):
            found += _enums(child, f"{pointer}/{index}")
    return found


# Enums that are deliberately narrower than the shared vocabulary they draw from, with the
# reason. Anything not listed here must match a models.py constant exactly.
LOCAL_ENUMS: dict[frozenset[str], str] = {
    frozenset({"code_reviewer"}): "quality_gate's agent step: only the code reviewer runs inside the gate",
}


@pytest.mark.parametrize("schema_name", SCHEMA_NAMES)
def test_every_schema_enum_has_a_models_constant(schema_name: str) -> None:
    known = _python_vocabularies()
    for pointer, values in _enums(models.schema(schema_name)):
        if values in LOCAL_ENUMS:
            continue
        assert values in known, (
            f"{schema_name}.schema.json{pointer}: enum {sorted(values)} matches no models.py constant. "
            "Add the constant (or record it in LOCAL_ENUMS with a reason) so the two cannot drift."
        )


# Vocabularies that deliberately have no schema enum of their own, with the reason. The header of
# models.py's vocabulary section says every one of these appears as an `enum` in a schema — an
# unchecked claim until now, and two constants had already stopped being true when it was written.
NON_DOCUMENT_VOCABULARIES: dict[str, str] = {
    "AGENT_ROLE_VALUES": "config's `agents` constrains the roles as fixed properties + additionalProperties: false",
    "CAPABILITY_VALUES": "a control-plane token's scope, never written into a document",
    "CENTRAL_ONLY_CAPABILITIES": "the same token vocabulary, split by who may exercise it",
    "MECHANIZED_EVIDENCE_KINDS": "a subset of ACCEPTANCE_EVIDENCE_KINDS (the two this loop can do itself)",
    "REVIEW_STAGE_VALUES": "the review stages are derived per request, not stored — review_api enforces them",
}


def test_every_vocabulary_appears_as_a_schema_enum() -> None:
    """The other direction, and the one that was missing.

    `test_every_schema_enum_has_a_models_constant` catches a schema enum drifting away from the
    code. Nothing caught a *constant* describing a document field the schema does not have, which is
    how `HOME_MODE_VALUES` outlived the `home` property entirely and `SCENARIO_KIND_VALUES` was
    declared for a shape no schema ever carried. Both read as vocabulary; neither constrained
    anything.
    """
    schema_enums = {values for name in SCHEMA_NAMES for _, values in _enums(models.schema(name))}
    for values, names in sorted(_python_vocabularies().items(), key=lambda kv: sorted(kv[1])):
        for constant in names:
            if constant in NON_DOCUMENT_VOCABULARIES:
                continue
            assert values in schema_enums, (
                f"models.{constant} = {sorted(values)} matches no enum in any schema. Either the field "
                "it describes is gone (delete the constant) or it never constrained one (record it in "
                "NON_DOCUMENT_VOCABULARIES with the reason)."
            )


def test_gate_and_phase_ladders_agree() -> None:
    assert set(models.PHASE_AFTER_GATE) == set(models.GATE_ORDER)
    assert set(models.PHASE_AFTER_GATE.values()) <= set(models.PHASE_ORDER)


def test_central_only_capabilities_are_capabilities() -> None:
    assert models.CENTRAL_ONLY_CAPABILITIES < models.CAPABILITY_VALUES
    # The four verbs a leaf agent legitimately needs, and nothing more.
    assert models.CAPABILITY_VALUES - models.CENTRAL_ONLY_CAPABILITIES == {
        "decision.declare",
        "knowledge_gap.create",
        "task.status",
        "event.append",
    }


def test_no_verified_value_in_the_semantic_axis() -> None:
    # `verified` belongs to integrity (a fact), never to semantic support (a judgement).
    assert "verified" in models.INTEGRITY_STATUS_VALUES
    assert "verified" not in models.SEMANTIC_SUPPORT_VALUES
    assert "verified" not in models.STATEMENT_STATUS_VALUES


def test_risk_acceptance_is_not_a_disposition() -> None:
    # A critical unknown cannot be closed by accepting it (plan §15.4).
    assert not any("accept" in action for action in models.DISPOSITION_VALUES)


def test_the_gate_four_rail_reads_before_it_asks() -> None:
    """Twelve stages used to show the same finding four times over. The surviving rail is two
    reading stages — scope (what this approval covers) then orient (what was built, and under what
    conditions) — before decision, the one screen that asks for anything, then diff and freeze."""
    assert models.REVIEW_STAGE_ORDER == ("scope", "orient", "decision", "diff", "freeze")


@pytest.mark.parametrize("name", ["plan", "state", "review", "config"])
def test_the_shipped_scaffold_validates(name: str) -> None:
    """The scaffold `rein init` seeds must satisfy its own schema.

    A canary, not a formality: the scaffold is the first thing every new repository parses,
    and a schema change that invalidates it turns `init` into an immediate hard failure.
    """
    from rein import data

    raw = strict_yaml.load_mapping(data.read_text(f"scaffold/rein/{name}.yaml"), what=f"{name}.yaml")
    errors = models.schema_errors(raw, name)
    if name == "plan" and not errors:
        errors = models.cross_reference_errors(models.Plan(raw))
    assert errors == []


def test_the_scaffold_smoke_command_is_a_string_not_a_boolean() -> None:
    # Unquoted `true` in YAML is the boolean, not /bin/true. The schema catches it; this test
    # keeps the scaffold from re-acquiring the footgun.
    from rein import data

    config = strict_yaml.load_mapping(data.read_text("scaffold/rein/config.yaml"), what="config.yaml")
    for step in config["quality_gate"]:
        assert all(isinstance(arg, str) for arg in step.get("command", []))


# --- helpers ------------------------------------------------------------------


def test_risk_ladder() -> None:
    assert models.risk_at_least("critical", "high")
    assert models.risk_at_least("high", "high")
    assert not models.risk_at_least("medium", "high")
    assert models.max_risk(["low", "critical", "medium"]) == "critical"
    assert models.max_risk([]) == "low"


@pytest.mark.parametrize("path", ["src/app.py", "docs/a.md", "a", "a.b/c-d_e/f+g@h"])
def test_repo_path_accepts_safe_paths(path: str) -> None:
    assert models.is_repo_path(path)


@pytest.mark.parametrize(
    "path",
    ["/etc/passwd", "../outside", "a/../../b", "a/..", "..", "C:\\win", "a\\b", "", "-leading-dash"],
)
def test_repo_path_rejects_escapes(path: str) -> None:
    assert not models.is_repo_path(path)


# --- plan fixture -------------------------------------------------------------
#
# The Expected Model is deliberately small: `cycle`, `claims`, and `tasks` — one claim per
# `R-N`/`NFR-N` heading, one task per unit of work, and `claim_ids` is the only edge between
# them. Everything else that used to live here (sources, searches, evidence obligations,
# technical facts, solutions, acceptance oracles) had no writer in any phase procedure.

MINIMAL_PLAN = """
cycle:
  id: demo-cycle
  base_commit: 61f3d58c4e0b1122334455667788990011223344
  branch: build/demo
claims:
  - id: C-001
    statement: the thing does not double-charge
    risk: low
"""

TASK_PLAN = """
cycle:
  id: payment-retry
  base_commit: 61f3d58c4e0b1122334455667788990011223344
  branch: build/payment-retry
claims:
  - id: C-002
    requirement_ids: [R-3]
    statement: a lost response never double-commits the same logical request
    risk: critical
    domains: [payment, idempotency]
tasks:
  - id: T-001
    title: Foundation
    kind: foundation
    risk: low
  - id: T-002
    title: Preserve idempotency across retries
    kind: parallel
    blocked_by: [T-001]
    claim_ids: [C-002]
    domains: [payment]
    risk: critical
    scope: {include: [src/payment/], exclude: [docs/]}
"""


def test_minimal_plan_parses() -> None:
    plan = models.Plan.parse(MINIMAL_PLAN)
    assert plan.cycle_id == "demo-cycle"
    assert [c.id for c in plan.claims] == ["C-001"]


def test_a_plan_with_tasks_parses_and_indexes() -> None:
    plan = models.Plan.parse(TASK_PLAN)
    assert plan.cycle_id == "payment-retry"
    assert plan.claim("C-002") is not None
    assert [t.id for t in plan.tasks] == ["T-001", "T-002"]
    assert plan.task("T-002").blocked_by == ("T-001",)  # type: ignore[union-attr]
    assert plan.task("T-002").claim_ids == ("C-002",)  # type: ignore[union-attr]


def test_plan_digest_survives_a_reflow_but_not_an_edit() -> None:
    plan = models.Plan.parse(TASK_PLAN)
    assert digests.is_digest(plan.digest())

    # Same facts, different YAML layout: a reflow must not invalidate a signed gate receipt.
    reflowed = models.Plan.parse(
        TASK_PLAN.replace(
            "    scope: {include: [src/payment/], exclude: [docs/]}",
            "    scope:\n      exclude: [docs/]\n      include: [src/payment/]",
        )
    )
    assert reflowed.digest() == plan.digest()

    # One word of one claim changed: the digest must move, or a review could be signed for
    # bytes nobody read.
    edited = models.Plan.parse(TASK_PLAN.replace("never double-commits", "sometimes double-commits"))
    assert edited.digest() != plan.digest()


def test_claim_risk_and_domains_round_trip() -> None:
    claim = models.Plan.parse(TASK_PLAN).claim("C-002")
    assert claim is not None
    assert claim.risk == "critical"
    assert claim.domains == ("payment", "idempotency")
    assert claim.requirement_ids == ("R-3",)


# --- schema rejections --------------------------------------------------------


def _plan_error(text: str) -> str:
    with pytest.raises(models.DocumentError) as excinfo:
        models.Plan.parse(text)
    return str(excinfo.value)


def test_unknown_field_rejected() -> None:
    assert "Additional properties" in _plan_error(MINIMAL_PLAN + "surprise: yes\n")


def test_absolute_scope_path_rejected() -> None:
    bad = TASK_PLAN.replace("scope: {include: [src/payment/], exclude: [docs/]}", "scope: {include: [/etc/passwd]}")
    assert "include" in _plan_error(bad)


# --- cross-reference validation -----------------------------------------------


def test_dangling_claim_reference_in_a_task_is_caught() -> None:
    bad = TASK_PLAN.replace("claim_ids: [C-002]", "claim_ids: [C-999]")
    assert "unknown claim id 'C-999'" in _plan_error(bad)


def test_duplicate_claim_id_is_caught() -> None:
    doubled = TASK_PLAN.replace(
        "tasks:",
        "  - id: C-002\n    statement: dup\n    risk: low\ntasks:",
    )
    assert "duplicate id 'C-002'" in _plan_error(doubled)


def test_task_dependency_cycle_is_caught() -> None:
    bad = TASK_PLAN.replace(
        "    title: Foundation\n    kind: foundation\n    risk: low",
        "    title: Foundation\n    kind: foundation\n    risk: low\n    blocked_by: [T-002]",
    )
    assert "dependency cycle" in _plan_error(bad)


def test_task_blocked_by_itself_is_caught() -> None:
    bad = TASK_PLAN.replace("blocked_by: [T-001]", "blocked_by: [T-002]")
    assert "blocked_by lists itself" in _plan_error(bad)


def test_all_errors_are_reported_not_just_the_first() -> None:
    bad = TASK_PLAN.replace("claim_ids: [C-002]", "claim_ids: [C-999]")
    bad = bad.replace("blocked_by: [T-001]", "blocked_by: [T-777]")
    message = _plan_error(bad)
    assert "C-999" in message and "T-777" in message


# --- state --------------------------------------------------------------------

STATE = """
project: demo
cycle_id: payment-retry
current_phase: build
updated_at: "2026-07-23T17:00:00+09:00"
gates:
  requirements:
    status: approved
    receipt:
      approval_id: GA-REQUIREMENTS-0001
      validation_digest: sha256:2222222222222222222222222222222222222222222222222222222222222222
      attested_chain_root: sha256:3333333333333333333333333333333333333333333333333333333333333333
      result_chain_root: sha256:4444444444444444444444444444444444444444444444444444444444444444
  design: {status: pending, receipt: null}
  tasks: {status: pending, receipt: null}
  build: {status: pending, receipt: null}
  release: {status: pending, receipt: null}
plan:
  status: frozen
  digest: sha256:5555555555555555555555555555555555555555555555555555555555555555
tasks:
  T-002: {status: done, attempts: 2}
"""


def test_state_parses() -> None:
    state = models.State.parse(STATE)
    assert state.current_phase == "build"
    assert state.gate_status("requirements") == "approved"
    assert state.gate_status("design") == "pending"
    assert state.approved_gates == ("requirements",)
    assert state.task_status == {"T-002": "done"}
    assert state.plan_status == "frozen"


def test_gate_status_of_an_absent_gate_reads_pending() -> None:
    # Fail closed: an unreadable gate is never "approved".
    assert models.State.parse(STATE).gate_status("nonexistent") == "pending"


def test_approved_gate_without_a_receipt_is_rejected() -> None:
    # There is no path to `approved` that is not a digest-bound receipt.
    bad = STATE.replace("  design: {status: pending, receipt: null}", "  design: {status: approved, receipt: null}")
    with pytest.raises(models.DocumentError, match="receipt"):
        models.State.parse(bad)


def test_gate_chain_violation_detected() -> None:
    approved = models.State.parse(STATE)
    assert approved.pending_upstream("build") == "design"
    assert approved.gate_chain_violations() == []  # requirements approved, nothing downstream yet

    # An approval that survived a roll back: design approved while requirements is pending.
    raw = strict_yaml.load_mapping(STATE)
    raw["gates"]["design"] = dict(raw["gates"]["requirements"])
    raw["gates"]["requirements"] = {"status": "pending", "receipt": None}
    broken = models.State(raw)
    assert broken.gate_chain_violations() == [("design", "requirements")]


# --- review -------------------------------------------------------------------

REVIEW = """
machine:
  status: generated
  binding:
    change_digest: sha256:6666666666666666666666666666666666666666666666666666666666666666
    plan_digest: sha256:7777777777777777777777777777777777777777777777777777777777777777
    environment_digest: sha256:8888888888888888888888888888888888888888888888888888888888888888
  coverage:
    - diff_digest: sha256:9999999999999999999999999999999999999999999999999999999999999999
      analyzed_files: 27
      analyzed_bytes: 12345
      truncated: false
      coverage_status: sufficient
  actual_extraction:
    - id: AST-003
      statement: the retry path passes the same idempotency key to the next attempt
      category: state_propagation
      confidence: medium
      code_anchors:
        - path: src/payment/client.py
          start_line: 81
          end_line: 114
          blob: 'git-blob:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'
  claims:
    - claim_id: C-002
      actual_statement_ids: [AST-003]
      verdict: aligned
      integrity: {status: verified}
      semantic_support: {status: supported, assessment_basis: machine_assessed}
      conformance: {status: observed}
  security:
    findings:
      - id: SEC-001
        severity: high
        category: credential_exposure
        attack_scenario: the reviewer container could reach a host credential
        blocking: true
human:
  status: not_started
"""


def test_review_parses_and_digests_the_halves_separately() -> None:
    review = models.Review.parse(REVIEW)
    assert review.human_status == "not_started"
    assert len(review.actual_statements) == 1
    assert review.coverage_sufficient
    assert review.machine_digest() != review.human_digest()


def test_blocking_security_finding_is_isolated() -> None:
    review = models.Review.parse(REVIEW)
    assert [f["id"] for f in review.blocking_security_findings] == ["SEC-001"]


def test_absent_coverage_is_not_sufficient() -> None:
    # "we did not measure" must never render as "we measured nothing missing" (plan §2.4).
    review = models.Review(strict_yaml.load_mapping("machine: {status: not_generated}\nhuman: {status: not_started}\n"))
    assert not review.coverage_sufficient
    assert not review.is_generated


def test_truncated_coverage_is_rejected_outright() -> None:
    # Reading only the head or tail of a huge diff and calling it analysed is not allowed;
    # the detector must partition instead (plan §13.4).
    with pytest.raises(models.DocumentError, match="truncated"):
        models.Review.parse(REVIEW.replace("truncated: false", "truncated: true"))


def test_the_human_half_no_longer_accepts_a_challenge_answer() -> None:
    """The unprimed-guess record is gone from the schema, not merely unused by the code.

    Leaving the field accepted would let a stale UI keep writing answers to a question nothing
    asks, and `human_digest` would move for a judgement nobody made.
    """
    bad = REVIEW.replace(
        "human:\n  status: not_started",
        "human:\n  status: in_progress\n  challenge_answers:\n"
        "    - {challenge_id: DC-001, choice: B, confidence: low, answered_before_reveal: true}",
    )
    with pytest.raises(models.DocumentError, match="challenge_answers"):
        models.Review.parse(bad)


# --- config: the two digests ----------------------------------------------------
#
# Gate ③ freezes a config.yaml with its image pins taken out, and records the whole sandbox
# picture beside it. The split is what lets a task that legitimately adds a dependency have its
# sandbox rebuilt without re-approving a plan nothing changed — and the second digest is what stops
# that permission from being a hole nobody can see through.


def _config(**profiles: dict[str, Any]) -> models.Config:
    return models.Config(
        {
            "project": {"name": "demo", "work_branch": "work"},
            "executors": {"implementer_profile": "impl", "reviewer_profile": "rev"},
            "executor_profiles": profiles,
        }
    )


_PINNED = {"kind": "oci", "image": "localhost/rein-python@sha256:" + "0" * 64, "network_profile": "none"}


def test_a_rebuilt_image_does_not_move_the_frozen_digest() -> None:
    """The whole point: `rein oci build --write-config` after a dependency lands rewrites exactly
    this one string, and it used to cost `rein revise --to tasks` — the plan un-froze and every
    gate below reset in a chain, for a decision nobody had changed."""
    before = _config(impl=_PINNED, rev=_PINNED)
    after = _config(impl={**_PINNED, "image": "localhost/rein-python@sha256:" + "1" * 64}, rev=_PINNED)
    assert before.frozen_digest() == after.frozen_digest()
    assert before.environment_digest() != after.environment_digest()


def test_opening_a_sandbox_moves_the_frozen_digest() -> None:
    """Only the pin moved out. `kind` and `network_profile` widen what may happen, and widening is
    the judgement a human made at gate ③."""
    pinned = _config(impl=_PINNED, rev=_PINNED)
    for change in ({"kind": "host"}, {"network_profile": "egress"}, {"mount_repo": "read_write"}):
        opened = _config(impl={**_PINNED, **change}, rev=_PINNED)
        assert pinned.frozen_digest() != opened.frozen_digest(), change


def test_the_environment_digest_covers_the_profile_bodies_not_only_their_names() -> None:
    """Its predecessor hashed `{"executors": ...}` — the role→profile *name* map — while claiming to
    move when the sandbox a step ran in changed. Repointing a profile at a different image, or
    flipping it to `host`, left it identical, and nothing ever compared it, so the claim was never
    contradicted."""
    pinned = _config(impl=_PINNED, rev=_PINNED)
    on_the_host = _config(impl={"kind": "host"}, rev=_PINNED)
    assert pinned.environment_digest() != on_the_host.environment_digest()


def test_a_config_with_no_profiles_still_digests() -> None:
    bare = models.Config({"project": {"name": "demo"}})
    assert digests.is_digest(bare.frozen_digest())
    assert digests.is_digest(bare.environment_digest())


# --- event ---------------------------------------------------------------------


def test_event_payload_excludes_its_own_digest() -> None:
    event = models.Event(
        seq=1,
        id="0123abcd",
        tx_id="4567ef01",
        ts="2026-07-23T18:10:00+09:00",
        event="gate_approved",
        cycle_id="demo",
        event_digest="sha256:" + "0" * 64,
    )
    assert "event_digest" not in event.payload()
    assert event.to_mapping()["event_digest"] == "sha256:" + "0" * 64


def test_event_round_trips_through_a_mapping() -> None:
    event = models.Event(
        seq=7,
        id="0123abcd",
        tx_id="4567ef01",
        ts="2026-07-23T18:10:00+09:00",
        event="task_completed",
        cycle_id="demo",
        actor="alice",
        subject_ids=("T-002",),
        prev_event_digest="sha256:" + "1" * 64,
        event_digest="sha256:" + "2" * 64,
        detail={"attempts": 2},
    )
    assert models.Event.from_mapping(event.to_mapping()) == event


# --- what a task declares it will require of a person ---------------------------


def test_a_task_reads_back_the_operator_surface_the_plan_froze() -> None:
    """The Expected side of gate ④'s "what does this now require of somebody" comparison."""
    plan = models.Plan(
        make_plan(
            tasks=[
                make_task(
                    "T-001",
                    operator_surface=[
                        {"kind": "persistence", "name": "users.email_verified", "paths": ["db/schema.sql"]}
                    ],
                )
            ]
        )
    )
    surface = plan.task("T-001").operator_surface  # type: ignore[union-attr]
    assert [entry["kind"] for entry in surface] == ["persistence"]


def test_a_task_that_declares_nothing_reads_back_an_empty_declaration() -> None:
    """Declaring nothing is allowed; it means every operator-facing reading arrives undeclared."""
    plan = models.Plan(make_plan(tasks=[make_task("T-001")]))
    assert plan.task("T-001").operator_surface == ()  # type: ignore[union-attr]


def test_the_declared_kinds_are_a_subset_of_the_actual_statement_categories() -> None:
    """Two vocabularies over one comparison would need a mapping table that can be wrong."""
    import json

    from rein import data as data_mod

    schema = json.loads(data_mod.read_text("schema/review.schema.json"))
    categories = set(
        schema["$defs"]["machine"]["properties"]["actual_extraction"]["items"]["properties"]["category"]["enum"]
    )
    assert models.OPERATOR_SURFACE_KIND_VALUES <= categories


def test_a_declared_kind_outside_the_enum_is_refused_by_the_schema() -> None:
    plan = make_plan(tasks=[make_task("T-001", operator_surface=[{"kind": "vibes", "name": "x", "paths": ["src/"]}])])
    assert any("vibes" in error or "kind" in error for error in models.schema_errors(plan, "plan"))


# --- vocabulary two modules have to agree on -----------------------------------


def test_a_stack_branch_name_carries_the_cycle() -> None:
    """Task ids restart at T-001 every cycle, so the branch alone repeats across them."""
    first = models.stack_branch("build/x", "cycle-1", 1, "T-001")
    second = models.stack_branch("build/x", "cycle-2", 1, "T-001")

    assert first == "build/x-pr-cycle-1-01-T-001"
    assert first != second


def test_a_tail_slice_is_named_tail() -> None:
    assert models.stack_branch("build/x", "c1", 4, "") == "build/x-pr-c1-04-tail"


def test_a_stack_branch_is_recognised_and_an_ordinary_one_is_not() -> None:
    assert models.is_stack_branch("build/x-pr-cycle-1-01-T-001")
    assert not models.is_stack_branch("main")
    assert not models.is_stack_branch("build/x")
    assert not models.is_stack_branch("build/x-T-001")


def test_the_placeholder_set_names_commands_that_cannot_fail() -> None:
    assert ("true",) in models.PLACEHOLDER_COMMANDS
    assert ("python", "-m", "pytest") not in models.PLACEHOLDER_COMMANDS
