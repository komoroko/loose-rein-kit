"""The document model: the vocabulary, the typed views, and the validation entry point.

Four artifacts carry the cycle (plan §5.1) and each has exactly one writer:

  ``plan.yaml``    the Expected Model: one claim per requirement, and the task DAG — frozen at gate ③
  ``state.yaml``   mutable state only: phase, gate receipts, run status, task status
  ``review.yaml``  the machine review and, separately, the human review
  ``events.ndjson`` the append-only hash-chained audit log

**The parsed mapping is the truth; these classes are views over it.** A digest is taken over
that mapping (via :mod:`rein.digests`), never over a reconstructed object graph — so a
field this module does not yet know about still contributes to the digest a human approved,
and adding an accessor here can never move a receipt. That is the reason `Plan` wraps a
`raw` mapping instead of being a full dataclass tree: a lossy round-trip would be a silent
integrity bug, and there is no test that reliably catches "lossy in a way nobody wrote down".

Validation runs in three layers, all fail-closed:

  1. :mod:`rein.strict_yaml` — the document is unambiguous at all (no duplicate keys …)
  2. JSON Schema (``data/schema/*.schema.json``) — shape, ``additionalProperties: false``, enums
  3. :func:`cross_reference_errors` — every ID reference resolves (plan §23)

Layer 2's enums and this module's vocabulary constants are two spellings of one fact, so
``tests/test_models.py`` asserts they agree.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from rein import common, data, digests, strict_yaml

# --- vocabulary ---------------------------------------------------------------
#
# Ordered tuples where display/comparison order matters (risk ladders, gate chains), plain
# frozensets where only membership does. Every one of these appears as an `enum` in a schema.

RISK_ORDER: tuple[str, ...] = ("low", "medium", "high", "critical")
RISK_VALUES = frozenset(RISK_ORDER)

#: The forward gate order. A roll back resets a chain of these (plan §16).
GATE_ORDER: tuple[str, ...] = ("requirements", "design", "tasks", "build", "release")
GATE_VALUES = frozenset(GATE_ORDER)
GATE_STATUS_VALUES = frozenset({"pending", "approved"})

#: Commands that run, exit zero, and establish nothing. `["true"]` is what the scaffold ships for
#: its launch step; the others are the same gesture written differently. Shared vocabulary because
#: two places have to agree on it and neither may import the other: `doctor.check_quality_gate`,
#: which reports a DoD step that cannot fail, and `brief`, which tells a gate-④ reviewer whether
#: anything ever started the deliverable. Matched on the **argv**, never on the step's name.
PLACEHOLDER_COMMANDS: frozenset[tuple[str, ...]] = frozenset(
    {("true",), ("/bin/true",), (":",), ("echo",), ("exit", "0")}
)


#: How a stacked pull request's branch is named:
#: `<work_branch>-pr-<cycle>-NN-<task or tail>`. Here rather than in `pr_stack` because two modules
#: that never import each other have to agree on it — the one that creates these branches, and the
#: base-side CI check that has to recognise a base it must not trust (`policy_check`). `-` and not
#: `/` because git refuses a ref that is a path prefix of another, which `<branch>/pr/01` next to
#: `<branch>` would be.
#:
#: **The cycle is in the name, and it has to be.** The work branch is fixed per project and every
#: cycle's plan numbers its own tasks from `T-001`, so a name built from the branch and the index
#: alone repeats exactly. `cycle-close` archives the audit log with it, so the ledger that froze
#: cycle 1's slices is gone by then — the second cycle would find cycle 1's branch, see its commit
#: is an ancestor of the new one, fast-forward it, and push new work onto a ref GitHub already has
#: a pull request for.
STACK_BRANCH_INFIX = "-pr-"
STACK_TAIL_SUFFIX = "tail"


def stack_branch(work_branch: str, cycle_id: str, index: int, task_id: str) -> str:
    """The branch name for slice `index` of `cycle_id`'s stack on `work_branch`."""
    cycle = cycle_id or "cycle"
    return f"{work_branch}{STACK_BRANCH_INFIX}{cycle}-{index:02d}-{task_id or STACK_TAIL_SUFFIX}"


def is_stack_branch(ref: str) -> bool:
    """Whether `ref` names a slice of a stack — a base the head author created, not a trusted one.

    Membership of the namespace, not a parse of it. A false positive costs nothing where this is
    used: `policy_check` answers it by measuring against the default branch instead, which is the
    stricter reading. A false *negative* would let a base the author wrote pass as a trusted one.
    """
    return STACK_BRANCH_INFIX in ref


#: current_phase values in lifecycle order (`brief` precedes gate ①, `done` follows gate ⑤).
PHASE_ORDER: tuple[str, ...] = ("brief", "requirements", "design", "tasks", "build", "verify", "done")
PHASE_VALUES = frozenset(PHASE_ORDER)

#: The phase each gate opens the door to — `approve <gate>` advances current_phase to this.
PHASE_AFTER_GATE: Mapping[str, str] = {
    "requirements": "design",
    "design": "tasks",
    "tasks": "build",
    "build": "verify",
    "release": "done",
}

PLAN_STATUS_VALUES = frozenset({"draft", "frozen", "invalidated"})

# Task vocabulary. `kind` is the DAG role that drives build orchestration (consumption order,
# parallelism, merge) — deliberately not collapsed into a single "implementation", because
# build_loop derives layers and the critical path from it.
TASK_KIND_ORDER: tuple[str, ...] = ("foundation", "parallel", "integration")
TASK_KIND_VALUES = frozenset(TASK_KIND_ORDER)
# `awaiting-evidence` is the status that says the difference between "this failed" and "nobody
# established this". A task whose acceptance criteria include evidence the loop cannot obtain —
# a staging check, a device, a person — has not failed and is not done, and rounding it to either
# is a lie in one direction or the other. It sits off the frontier until a human records the
# observation with `rein evidence record`.
TASK_STATUS_ORDER: tuple[str, ...] = (
    "todo",
    "in-progress",
    "blocked",
    "needs-revision",
    "awaiting-evidence",
    "done",
)
TASK_STATUS_VALUES = frozenset(TASK_STATUS_ORDER)
# What became of an interrupted attempt's preserved work by the time the next one starts: still
# waiting to be picked up, merged into the fresh worktree, or left on its branch because merging
# it conflicted (see state.schema.json `handoff`).
SALVAGE_STATE_VALUES = frozenset({"pending", "restored", "conflict"})

# What an implementer says about its own attempt, through `rein report`. Deliberately *not* the
# task-status vocabulary: a status is a verdict the loop reaches, this is a claim the agent makes,
# and collapsing the two is how "the agent exited" came to mean "the task is done". `blocked` and
# `needs-revision` do map onto statuses — an agent may always narrow what happens next — while
# `implemented` earns nothing on its own and only ever opens the quality gate.
AGENT_OUTCOME_ORDER: tuple[str, ...] = ("implemented", "blocked", "needs-revision")
AGENT_OUTCOME_VALUES = frozenset(AGENT_OUTCOME_ORDER)
#: The same vocabulary as recorded beside a *verdict*, where "nothing was claimed" is itself
#: information: a task that reached done with `none` had an implementer that never reported.
REPORTED_OUTCOME_VALUES = frozenset({*AGENT_OUTCOME_ORDER, "none"})

# How a task's own acceptance criterion is established. `command` and `artifact` the loop can do
# itself; `external` it deliberately cannot — a staging check, a device, a person — which is what
# `awaiting-evidence` exists to say out loud instead of quietly rounding to `done`.
ACCEPTANCE_EVIDENCE_KINDS: tuple[str, ...] = ("command", "artifact", "external")
ACCEPTANCE_EVIDENCE_KIND_VALUES = frozenset(ACCEPTANCE_EVIDENCE_KINDS)
#: The kinds this loop can establish on its own.
MECHANIZED_EVIDENCE_KINDS = frozenset({"command", "artifact"})

EXECUTOR_VALUES = frozenset({"oci", "host"})

# Sandbox knobs (plan §10.2).
MOUNT_MODE_VALUES = frozenset({"none", "read_only", "read_write"})
QUALITY_GATE_KIND_VALUES = frozenset({"command", "agent"})
#: Where a DoD step runs. `both` is what every step has always done and stays the default; the
#: other two exist so a fast focused suite can guard each task while the whole one runs over the
#: join, rather than every task re-establishing the whole thing from scratch.
GATE_STAGE_ORDER: tuple[str, ...] = ("task", "integration", "both")
GATE_STAGE_VALUES = frozenset(GATE_STAGE_ORDER)
#: What the per-task reviewer may say about a change. `must_fix` sends it back to the implementer
#: within the review step's own budget; `consider` stops nothing and is carried to gate ④. Neither
#: passes or fails a task on its own — the reviewer reports, and the loop decides what that costs.
FINDING_SEVERITY_VALUES = frozenset({"must_fix", "consider"})
AGENT_ROLE_VALUES = frozenset({"implementer", "code_reviewer", "actual_extractor", "comparator", "security_reviewer"})

# --- review vocabulary (plan §6.7) --------------------------------------------
#
# There is no single `verified`. Three axes are reported separately because they answer three
# different questions, and one of them (`semantic_support`) is an opinion.

#: Did the Coverage Manifest manage to analyse the whole diff?
COVERAGE_STATUS_VALUES = frozenset({"sufficient", "insufficient"})
#: How a review's reading was taken: one reading of everything, or several composed — one per task
#: that landed, plus the seam between them. Recorded on every review, never inferred: a reader must
#: be able to see whether the tree in front of them was read whole (`review_reading.WHOLE`).
COMPOSITION_WHOLE = "whole"
COMPOSITION_COMPOSED = "composed"
COMPOSITION_VALUES = frozenset({COMPOSITION_WHOLE, COMPOSITION_COMPOSED})
#: What an operator may ask for at gate ④. `composed` is not among them: composing is decided by
#: whether the plan actually declares task scopes to compose along, never by a preference.
COMPOSITION_MODE_VALUES = frozenset({"auto", COMPOSITION_WHOLE})
INTEGRITY_STATUS_VALUES = frozenset({"verified", "failed", "unavailable"})
SEMANTIC_SUPPORT_VALUES = frozenset({"supported", "contradicted", "conflicted", "unknown"})
#: How the semantic judgement was reached. `machine_assessed` is an AI's opinion and must
#: never be rendered with the same weight as the other three (plan §6.7, §21.4).
ASSESSMENT_BASIS_ORDER: tuple[str, ...] = ("machine_assessed", "experimental", "expert_attested", "formal")
ASSESSMENT_BASIS_VALUES = frozenset(ASSESSMENT_BASIS_ORDER)
CONFORMANCE_STATUS_VALUES = frozenset({"observed", "partial", "unknown"})
VERDICT_VALUES = frozenset({"aligned", "diverged", "missing", "unverified", "unknown"})

#: Every human-facing sentence carries one of these (plan §6.8). `machine_inferred` is the
#: honest label for "an AI wrote this"; there is no status meaning "reads plausibly".
STATEMENT_STATUS_VALUES = frozenset(
    {
        "code_observed",
        "expert_attested",
        "machine_inferred",
        "unknown",
        "conflicted",
    }
)

EXPERTISE_LEVEL_VALUES = frozenset({"familiar", "partial", "unfamiliar"})
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})

#: What a human may do about a mismatch or a gap. Note what is *not* here: "accept the risk"
#: is not an available disposition for a critical unknown (plan §15.4). `dispute_finding` is —
#: a review that cannot be contradicted makes the reviewer infallible — but it carries a reason,
#: because "we disagree" with no why is not a disposition either.
DISPOSITION_VALUES = frozenset(
    {
        "acknowledge_corrected_model",
        "revise_requirement",
        "revise_design",
        "revise_implementation",
        "request_expert",
        "run_experiment",
        "reduce_scope",
        "dispute_finding",
    }
)

#: How far the human half of the gate ④ review has got. `frozen` is the end state: the answers
#: are sealed and digested, and `rein approve build` is what a human runs next.
HUMAN_REVIEW_STATUS_ORDER: tuple[str, ...] = ("not_started", "in_progress", "frozen")
HUMAN_REVIEW_STATUS_VALUES = frozenset(HUMAN_REVIEW_STATUS_ORDER)
#: Whether a machine review exists at all. An explicit status rather than an inference from
#: empty lists — "we did not measure" has to be a state you can name (plan §2.4).
MACHINE_REVIEW_STATUS_VALUES = frozenset({"not_generated", "generated"})

#: Source-language analysis depth reported per language in the Coverage Manifest (plan §13.3).
ANALYSIS_DEPTH_VALUES = frozenset({"ast", "ast_plus_llm", "token_only", "unsupported"})
#: Why a changed file could not be analysed. Each of these is a reason an "extra behaviours: 0"
#: line would be a lie, so each forces the count to render as "undeterminable" (plan §13.4).
UNSUPPORTED_REASON_VALUES = frozenset(
    {"binary", "generated", "unsupported_language", "parser_failure", "too_large", "vendored"}
)

#: What an Actual Statement is about — the axes the extractor is asked to sweep.
ACTUAL_CATEGORY_VALUES = frozenset(
    {
        "state_propagation",
        "control_flow",
        "side_effect",
        "default_value",
        "failure_handling",
        "concurrency",
        "persistence",
        "security_boundary",
        "observability",
        "public_interface",
        "dependency",
    }
)

#: Behaviour present in the code that no claim accounts for (plan §14.6).
EXTRA_BEHAVIOR_CATEGORY_VALUES = frozenset(
    {
        "new_default",
        "retry_timeout_fallback",
        "external_side_effect",
        "public_interface",
        "persistence",
        "exception_suppression",
        "security_boundary",
        "observability_reduction",
        "dependency_change",
        "concurrency",
    }
)

#: Why something could not be settled. Each kind names a *different* missing thing, so that
#: "we have no source" never renders the same as "the source does not support the claim".
GAP_KIND_VALUES = frozenset({"evidence_gap", "actual_coverage_gap"})

SECURITY_CATEGORY_VALUES = frozenset(
    {
        "credential_exposure",
        "injection",
        "authz_bypass",
        "authn_weakness",
        "crypto_misuse",
        "ssrf",
        "path_traversal",
        "deserialization",
        "supply_chain",
        "sandbox_escape",
        "information_disclosure",
        "denial_of_service",
        "other",
    }
)


#: The gate-④ rail, in order. Three screens and a freeze, because the same finding used to
#: appear on four of them — as a summary count, as a raw gap, as an Expected/Actual row, and
#: again as the card that actually asked for a decision. Only the last one wanted an answer.
#:
#: `scope` comes first: it says which commits, which files, and which claims this review speaks
#: for — and, more importantly, which it does not. A reviewer who does not know the boundary does
#: not know what they approved.
#: `orient` is what was actually built and under what conditions — the delivered tasks, the
#: dependency movement, the sandbox and network boundary each gate step ran in, the interfaces the
#: blind extractor read out, what the gate established and what it could not. All of it derived
#: from the SSOT (`brief.py`), none of it asked for. It exists so the reviewer stops reconstructing
#: the change from a diff before every card.
#: `decision` is the only screen that asks for anything, and each card carries its own evidence.
#: `diff` is the change itself, for a reviewer who wants to read it rather than be told about it.
REVIEW_STAGE_ORDER: tuple[str, ...] = ("scope", "orient", "decision", "diff", "freeze")
REVIEW_STAGE_VALUES = frozenset(REVIEW_STAGE_ORDER)

#: What a task may declare it will require of a person, in `plan.yaml`'s `operator_surface`.
#: Deliberately a *subset of the Actual Statement categories* (`review.schema.json`) rather than a
#: vocabulary of its own: the declaration is compared against what a blind extractor read out of
#: the code, and two vocabularies over one comparison would need a mapping table that could be
#: wrong. These six are the categories that describe a surface somebody outside the code can see —
#: `control_flow` and `state_propagation` are internal, and nobody operates them.
OPERATOR_SURFACE_KINDS: tuple[str, ...] = (
    "persistence",
    "public_interface",
    "dependency",
    "default_value",
    "observability",
    "security_boundary",
)
OPERATOR_SURFACE_KIND_VALUES = frozenset(OPERATOR_SURFACE_KINDS)

RUN_STATUS_VALUES = frozenset({"idle", "running", "waiting_for_review", "blocked", "complete"})
# There is deliberately no `state.review` status beside these. `review.yaml` carries the machine
# half's `status` and the human half's `status`, digested separately, and a second copy in
# `state.yaml` could only ever disagree with them: it was written in two places (a new cycle, a
# roll back), read in none, and its six values named states nothing could produce.

BUDGET_NAMES: tuple[str, ...] = (
    "max_critical_decisions",
    "max_human_statements",
    "max_unresolved_low_medium_unknowns",
    "max_diff_bytes",
)
BUDGET_NAME_VALUES = frozenset(BUDGET_NAMES)

# --- event vocabulary (plan §25) ----------------------------------------------

EVENT_ORDER: tuple[str, ...] = (
    "cycle_initialized",
    "knowledge_gap",
    "gate_approved",
    "gate_revised",
    "changes_requested",
    "changes_addressed",
    "plan_frozen",
    "plan_invalidated",
    "task_started",
    "task_failed",
    "task_completed",
    # The run stopped because the machine failed, not because any task did. Deliberately outside
    # `events.ATTENTION_EVENTS`: it asks nobody to judge the work, it asks for a re-run.
    "run_aborted",
    # What one build run put in front of a model, and how much of it was a cold re-read. Emitted
    # once per run because a run is the unit that ends: a per-launch event would swamp the chain,
    # and a counter living only in the process would vanish with the `EXIT_RETRY_LATER` that a
    # long build is nearly certain to hit. Also outside `ATTENTION_EVENTS` — it asks for no
    # judgement, it makes one possible.
    "run_measured",
    # A pinned sandbox image was rebuilt and re-pinned. Outside `ATTENTION_EVENTS`: it asks for no
    # judgement now — it is what makes gate ④'s "the evidence was produced in an environment the
    # gate ③ approval never saw" answerable then. Before this, `rein oci build --write-config`
    # rewrote config.yaml with `path.write_text` and the audit chain never heard about it.
    "environment_repinned",
    "decision_declared",
    "coverage_generated",
    # No `actual_extraction_started`: the vocabulary carried one and nothing ever emitted it. A
    # closed vocabulary refuses unknown names precisely so the log stays aggregatable, which makes
    # a name no code can produce a claim about the log that is not true.
    "actual_extraction_generated",
    "actual_extraction_failed",
    "comparison_generated",
    "security_review_generated",
    # A blocking finding stopped blocking. In the chain because that is a state change and the
    # chain is the only place it survives: `review.yaml` holds one generation's findings, and the
    # next generation re-derives the list from a reviewer with no memory of the last one.
    "security_finding_resolved",
    "review_generated",
    "review_failed",
    "decision_recorded",
    "expertise_declared",
    "expert_requested",
    "disposition_recorded",
    "human_review_frozen",
    "release_approved",
    "cycle_closed",
)
EVENT_VALUES = frozenset(EVENT_ORDER)

#: How a gate confirmation reached the tool. Both are the same kind of claim — something with
#: access to this machine's terminal did it — and neither is proof of a human. Recorded so a
#: reader can tell them apart rather than seeing an unqualified "approved".
CONFIRMATION_CHANNELS: tuple[str, ...] = ("terminal", "ui-session")
CONFIRMATION_CHANNEL_VALUES = frozenset(CONFIRMATION_CHANNELS)

#: A security finding's life. `open` holds gate ④ shut; `resolved` is recorded only when the code
#: the finding anchored to is no longer in the tree (`security_review.resolution_of`) — a fact
#: about the change, never the reviewer's word for it. A finding is never deleted, so the document
#: keeps the record of what closed it and against which head.
SECURITY_FINDING_STATUS_ORDER: tuple[str, ...] = ("open", "resolved")
SECURITY_FINDING_STATUS_VALUES = frozenset(SECURITY_FINDING_STATUS_ORDER)

#: A change request's life. `open` holds the gate shut; `addressed` is the agent saying it has
#: been answered and naming how, which stops it blocking but puts it on the approval screen; a
#: gate approval closes what it covered. Worst first, like the other ordered vocabularies here.
CHANGE_REQUEST_STATUS_ORDER: tuple[str, ...] = ("open", "addressed", "resolved")
CHANGE_REQUEST_STATUS_VALUES = frozenset(CHANGE_REQUEST_STATUS_ORDER)

#: Capabilities the control plane can grant. A leaf agent never receives any of
#: :data:`CENTRAL_ONLY_CAPABILITIES` (plan §11.3).
CAPABILITY_VALUES = frozenset(
    {
        "decision.declare",
        "knowledge_gap.create",
        "task.status",
        "event.append",
        "gate.approve",
        "expert.confirm",
        "human.review.complete",
        "review.machine.replace",
        "state.replace",
    }
)
CENTRAL_ONLY_CAPABILITIES = frozenset(
    {
        "gate.approve",
        "expert.confirm",
        "human.review.complete",
        "review.machine.replace",
        "state.replace",
    }
)

# --- identifiers --------------------------------------------------------------
#
# Every cross-reference is by ID, so an ID has to be a *shape* a validator can check rather
# than free text — otherwise "C-004" and "C-4" are two claims to a human and one typo
# to a reviewer.

ID_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "claim": re.compile(r"^C-\d{3,}$"),
    "task": re.compile(r"^T-\d{3,}$"),
    #: Scoped to its task, so a short number is enough — `A-1` of T-004 and `A-1` of T-005 are
    #: different criteria, and nothing ever refers to one from outside its own task.
    "acceptance": re.compile(r"^A-\d+$"),
    "statement": re.compile(r"^STMT-\d{3,}$"),
    "actual_statement": re.compile(r"^AST-\d{3,}$"),
    "decision_card": re.compile(r"^DC-\d{3,}$"),
    "finding": re.compile(r"^SEC-\d{3,}$"),
    "extra_behavior": re.compile(r"^EXTRA-\d{3,}$"),
}

#: Repo-relative POSIX paths only: no absolute path, no `..`, no backslash, no leading slash.
REPO_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._][A-Za-z0-9._/@+-]*$")

#: The cycle id, which `state.yaml` and every event carry. One spelling of a rule that had four:
#: the schema's pattern (checked against it by a test), `cycle.py`'s `--name` predicate, which
#: accepted a leading dash and any Unicode letter the schema rejects, and two `if state else ""`
#: fallbacks that put an empty one into the hash chain. Every event is queried by cycle, so an
#: event that cannot name its own is unfindable — and fails the schema `event_chain.scan` and
#: `rein doctor` validate the log against, which is how a recording failure became a chain defect.
CYCLE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def is_repo_path(value: object) -> bool:
    """True when `value` is a safe repo-relative POSIX path (no escape, no absolute form)."""
    return isinstance(value, str) and bool(REPO_PATH_RE.match(value)) and "\\" not in value


def risk_at_least(risk: str, floor: str) -> bool:
    """True when `risk` sits at or above `floor` on the risk ladder."""
    return RISK_ORDER.index(risk) >= RISK_ORDER.index(floor)


def max_risk(risks: Iterable[str]) -> str:
    """The highest risk in `risks` (`low` when empty) — the shape effective risk is built from."""
    return max(risks, key=RISK_ORDER.index, default="low")


def sandbox_setup_command(build_targets: Sequence[str]) -> str:
    """The one command that actually sandboxes `build_targets`. "" when there is nothing to do.

    Derived in one place because four surfaces print it — `rein next`, `rein doctor`, the
    dashboard, and `rein init` — so none of them can name one image out of three, or leave off
    `--write-config` and send the human to copy a digest out of the terminal. Pure, so the
    recommendation table stays testable without a repo on disk.
    """
    if len(build_targets) > 1:
        return "rein oci build --all --write-config"
    return f"rein oci build --profile {build_targets[0]} --write-config" if build_targets else ""


# --- errors -------------------------------------------------------------------


class DocumentError(common.ReinError, ValueError):
    """A document failed validation. `errors` lists every problem found, not just the first.

    Reporting all of them matters for the human loop: fixing one error, re-running, and being
    handed the next one is exactly the review friction plan §2.6 budgets against.
    """

    def __init__(self, what: str, errors: Sequence[str]) -> None:
        self.what = what
        self.errors = list(errors)
        joined = "\n".join(f"  - {e}" for e in self.errors)
        super().__init__(f"{what}: {len(self.errors)} validation error(s)\n{joined}")


# --- JSON Schema layer --------------------------------------------------------

_SCHEMA_CACHE: dict[str, Mapping[str, Any]] = {}


def schema(name: str) -> Mapping[str, Any]:
    """The packaged JSON Schema `name` (e.g. "plan"), parsed once per process."""
    if name not in _SCHEMA_CACHE:
        loaded = strict_yaml.load_json_mapping(
            data.read_text(f"schema/{name}.schema.json"),
            limits=strict_yaml.DEFAULT_LIMITS,
            what=f"{name}.schema.json",
        )
        _SCHEMA_CACHE[name] = loaded
    return _SCHEMA_CACHE[name]


def schema_errors(document: Any, name: str) -> list[str]:
    """Every JSON Schema violation in `document`, as human-readable "path: message" strings.

    jsonschema is a hard dependency, never an optional one that degrades to a WARN: a structural
    check that can silently turn itself off is not a boundary, and plan §15.4 makes schema
    conformance an absolute block.
    """
    import jsonschema  # deferred: keeps `import models` cheap for the gate-guard hook path

    validator_cls = jsonschema.validators.validator_for(schema(name))
    validator = validator_cls(schema(name))
    errors = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.absolute_path)):
        location = "/".join(str(part) for part in error.absolute_path) or "<root>"
        errors.append(f"{location}: {error.message}")
    return errors


# --- element views ------------------------------------------------------------
#
# Thin, read-only projections of one entry. Each keeps `raw` so a consumer can reach a field
# no accessor covers yet, without anyone being tempted to re-serialize the projection.


def _str(mapping: Mapping[str, Any], key: str, default: str = "") -> str:
    value = mapping.get(key, default)
    return value if isinstance(value, str) else default


def _ids(mapping: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = mapping.get(key) or []
    return tuple(v for v in value if isinstance(v, str)) if isinstance(value, list) else ()


def _maps(mapping: Mapping[str, Any], key: str) -> tuple[Mapping[str, Any], ...]:
    value = mapping.get(key) or []
    return tuple(v for v in value if isinstance(v, dict)) if isinstance(value, list) else ()


@dataclass(frozen=True)
class Element:
    """Base view: an `id` plus the raw entry it projects."""

    raw: Mapping[str, Any]

    @property
    def id(self) -> str:
        return _str(self.raw, "id")


@dataclass(frozen=True)
class Claim(Element):
    """One statement of what a requirement means — the knot the whole thread is tied at.

    `requirement_ids` points back at the `R-N` / `NFR-N` headings of `docs/10-requirements.md`;
    a task's `claim_ids` points forward at this. Neither end can be checked without it, which is
    why `/req` writes one per requirement and `approve` refuses a plan that states none.
    """

    @property
    def statement(self) -> str:
        return _str(self.raw, "statement")

    @property
    def requirement_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "requirement_ids")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.raw, "domains")


@dataclass(frozen=True)
class Task(Element):
    """One unit of implementation work and the claims it is answerable for."""

    @property
    def title(self) -> str:
        return _str(self.raw, "title")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "parallel")

    @property
    def blocked_by(self) -> tuple[str, ...]:
        return _ids(self.raw, "blocked_by")

    @property
    def claim_ids(self) -> tuple[str, ...]:
        return _ids(self.raw, "claim_ids")

    @property
    def risk(self) -> str:
        return _str(self.raw, "risk", "low")

    @property
    def domains(self) -> tuple[str, ...]:
        return _ids(self.raw, "domains")

    @property
    def scope_include(self) -> tuple[str, ...]:
        scope = self.raw.get("scope")
        return _ids(scope, "include") if isinstance(scope, dict) else ()

    @property
    def scope_exclude(self) -> tuple[str, ...]:
        scope = self.raw.get("scope")
        return _ids(scope, "exclude") if isinstance(scope, dict) else ()

    @property
    def acceptance(self) -> tuple[Mapping[str, Any], ...]:
        """This task's own acceptance criteria, as the frozen plan states them.

        Distinct from the shared DoD in two directions. The DoD is the same for every task and
        says the code is *sound*; these say this task in particular did *what it was for*. And a
        criterion here cannot loosen the gate — the DoD runs unchanged either way — because a
        human froze this list at gate ③, which is exactly what stops it being a knob an
        implementer turns down on itself.
        """
        value = self.raw.get("acceptance")
        return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()

    @property
    def operator_surface(self) -> tuple[Mapping[str, Any], ...]:
        """What this task declares it will require of a person, as the frozen plan states it.

        A config key somebody has to set, a schema somebody has to migrate, a dependency somebody
        has to provide, a signal somebody has to watch. Frozen at gate ③ with the rest of the task,
        which is what makes it an *Expected* side: at gate ④ the blind extractor's readings are
        sorted by whether one of these declarations foresaw them, and the ones nothing foresaw are
        the rows an approver has to look at.

        Declaring nothing is allowed and costs nothing to write; it just means every operator-facing
        reading in that area arrives at gate ④ as undeclared.
        """
        value = self.raw.get("operator_surface")
        return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()


_PLAN_SECTIONS: Mapping[str, type[Element]] = {"claims": Claim, "tasks": Task}


# --- documents ----------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """``plan.yaml`` — the Expected Model, frozen at gate ③ (plan §6.1).

    Everything a reviewer compares reality against lives here, and after the freeze the only
    way to change it is `rein revise --to tasks`. The views below are built once in
    `__post_init__`; `raw` stays the digest subject.
    """

    raw: Mapping[str, Any]
    _index: dict[str, dict[str, Element]] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        index: dict[str, dict[str, Element]] = {}
        for section, view in _PLAN_SECTIONS.items():
            entries = _maps(self.raw, section)
            index[section] = {_str(e, "id"): view(e) for e in entries}
        object.__setattr__(self, "_index", index)

    # -- construction ---------------------------------------------------------

    @classmethod
    def parse(cls, text: str, *, what: str = "plan.yaml", cross_reference: bool = True) -> Plan:
        """Parse and fully validate `text`. Raises :class:`DocumentError` listing every problem."""
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "plan")
        plan = cls(document)
        if not errors and cross_reference:
            errors = cross_reference_errors(plan)
        if errors:
            raise DocumentError(what, errors)
        return plan

    # -- identity -------------------------------------------------------------

    def digest(self) -> str:
        """The canonical plan digest a gate receipt binds (plan §17.1)."""
        return digests.of(self.raw, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    @property
    def cycle(self) -> Mapping[str, Any]:
        value = self.raw.get("cycle")
        return value if isinstance(value, dict) else {}

    @property
    def cycle_id(self) -> str:
        return _str(self.cycle, "id")

    @property
    def base_commit(self) -> str:
        return _str(self.cycle, "base_commit")

    @property
    def branch(self) -> str:
        return _str(self.cycle, "branch")

    # -- sections -------------------------------------------------------------

    def _section(self, name: str) -> tuple[Any, ...]:
        return tuple(self._index[name].values())

    @property
    def claims(self) -> tuple[Claim, ...]:
        return self._section("claims")

    @property
    def tasks(self) -> tuple[Task, ...]:
        return self._section("tasks")

    # -- lookup ---------------------------------------------------------------

    def get(self, section: str, element_id: str) -> Element | None:
        """The element `element_id` of `section`, or None. Never raises on an unknown ID —
        callers that must not proceed on a dangling reference use `cross_reference_errors`."""
        return self._index.get(section, {}).get(element_id)

    def claim(self, claim_id: str) -> Claim | None:
        found = self.get("claims", claim_id)
        return found if isinstance(found, Claim) else None

    def task(self, task_id: str) -> Task | None:
        found = self.get("tasks", task_id)
        return found if isinstance(found, Task) else None

    def ids(self, section: str) -> frozenset[str]:
        return frozenset(self._index.get(section, {}))


@dataclass(frozen=True)
class State:
    """``state.yaml`` — mutable state only (plan §6.5).

    Deliberately holds no task title, dependency, or claim mapping: those live in the frozen
    plan. Duplicating them here is how a state file and a plan file drift apart.
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "state.yaml") -> State:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "state")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    @property
    def project(self) -> str:
        return _str(self.raw, "project")

    @property
    def cycle_id(self) -> str:
        return _str(self.raw, "cycle_id")

    @property
    def current_phase(self) -> str:
        return _str(self.raw, "current_phase", "brief")

    @property
    def gates(self) -> Mapping[str, Mapping[str, Any]]:
        value = self.raw.get("gates")
        if not isinstance(value, dict):
            return {}
        return {k: v for k, v in value.items() if isinstance(v, dict)}

    def gate_status(self, gate: str) -> str:
        """`pending` unless the gate is explicitly approved — an unreadable gate reads as pending."""
        entry = self.gates.get(gate)
        return _str(entry, "status", "pending") if entry else "pending"

    def gate_receipt(self, gate: str) -> Mapping[str, Any] | None:
        entry = self.gates.get(gate)
        receipt = entry.get("receipt") if entry else None
        return receipt if isinstance(receipt, dict) else None

    @property
    def approved_gates(self) -> tuple[str, ...]:
        return tuple(g for g in GATE_ORDER if self.gate_status(g) == "approved")

    @property
    def plan_status(self) -> str:
        plan = self.raw.get("plan")
        return _str(plan, "status", "draft") if isinstance(plan, dict) else "draft"

    @property
    def plan_digest(self) -> str:
        plan = self.raw.get("plan")
        return _str(plan, "digest") if isinstance(plan, dict) else ""

    @property
    def plan_config_digest(self) -> str:
        """What `config.yaml` hashed to when gate ③ froze it. "" before the freeze.

        The pair of :attr:`plan_digest`, and specifically :meth:`Config.frozen_digest` — the
        sandbox *decisions* and the quality gate, image pins excluded.
        """
        plan = self.raw.get("plan")
        return _str(plan, "config_digest") if isinstance(plan, dict) else ""

    @property
    def plan_environment_digest(self) -> str:
        """The sandbox picture gate ③ saw, pins included. "" before the freeze or on an old freeze.

        Recorded rather than enforced: a rebuilt image is the same sandbox and is allowed to move
        underneath the freeze. Comparing it is how "the evidence was produced somewhere else than
        the approval saw" becomes a sentence a human reads, rather than a fact nothing states.
        """
        plan = self.raw.get("plan")
        return _str(plan, "environment_digest") if isinstance(plan, dict) else ""

    @property
    def frozen_sources(self) -> dict[str, str]:
        """The prose the build reads, as it hashed when gate ③ froze it. Empty before the freeze.

        `plan.yaml` is bound by a digest and always was; the task tickets and the design document
        an implementer is actually sent to *read* were bound to nothing. This is what makes
        "the ticket changed after it was approved" a fact something can check.
        """
        plan = self.raw.get("plan")
        sources = plan.get("sources") if isinstance(plan, dict) else None
        if not isinstance(sources, dict):
            return {}
        return {str(path): str(digest) for path, digest in sources.items() if isinstance(digest, str)}

    @property
    def change_requests(self) -> list[Mapping[str, Any]]:
        """Every change a human asked for instead of approving, oldest first."""
        value = self.raw.get("change_requests")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def change_requests_for(self, gate: str, *statuses: str) -> list[Mapping[str, Any]]:
        """This gate's change requests, filtered to `statuses` (all of them when none is given)."""
        wanted = frozenset(statuses) if statuses else CHANGE_REQUEST_STATUS_VALUES
        return [cr for cr in self.change_requests if cr.get("gate") == gate and cr.get("status") in wanted]

    @property
    def task_status(self) -> Mapping[str, str]:
        value = self.raw.get("tasks")
        if not isinstance(value, dict):
            return {}
        return {k: _str(v, "status", "todo") for k, v in value.items() if isinstance(v, dict)}

    def recorded_acceptance(self, task_id: str) -> tuple[Mapping[str, Any], ...]:
        """The observations `rein evidence record` has written for `task_id`, newest last.

        Each one binds the tree it was made against, so a reader comparing that to the current
        tree can tell a live observation from one the code has since retired.
        """
        entry = self.raw.get("tasks", {}).get(task_id)
        value = entry.get("acceptance") if isinstance(entry, dict) else None
        return tuple(item for item in value if isinstance(item, dict)) if isinstance(value, list) else ()

    def gate_chain_violations(self) -> list[tuple[str, str]]:
        """Every (approved gate, first pending gate upstream of it) pair.

        A non-empty result means an approval survived a roll back: downstream work is standing
        on a decision that has been withdrawn (AGENTS.md "Roll back").
        """
        violations: list[tuple[str, str]] = []
        first_pending: str | None = None
        for gate in GATE_ORDER:
            if self.gate_status(gate) != "approved":
                first_pending = first_pending or gate
            elif first_pending is not None:
                violations.append((gate, first_pending))
        return violations

    def pending_upstream(self, gate: str) -> str | None:
        """The first not-approved gate upstream of `gate`, or None when the chain is clear."""
        for upstream in GATE_ORDER[: GATE_ORDER.index(gate)]:
            if self.gate_status(upstream) != "approved":
                return upstream
        return None


@dataclass(frozen=True)
class Review:
    """``review.yaml`` — the machine review and the human review, digested separately (plan §6.6).

    The split is the point: regenerating the machine review resets the human section, while a
    human recording a decision must *not* make the machine review stale (plan §17.5).
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "review.yaml") -> Review:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "review")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    @property
    def machine(self) -> Mapping[str, Any]:
        value = self.raw.get("machine")
        return value if isinstance(value, dict) else {}

    @property
    def human(self) -> Mapping[str, Any]:
        value = self.raw.get("human")
        return value if isinstance(value, dict) else {}

    def machine_digest(self) -> str:
        return digests.of(self.machine, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    def human_digest(self) -> str:
        return digests.of(self.human, drop=digests.VOLATILE_TIMESTAMP_KEYS)

    @property
    def machine_status(self) -> str:
        return _str(self.machine, "status", "not_generated")

    @property
    def is_generated(self) -> bool:
        return self.machine_status == "generated"

    @property
    def binding(self) -> Mapping[str, Any]:
        value = self.machine.get("binding")
        return value if isinstance(value, dict) else {}

    @property
    def subject_head_sha(self) -> str:
        """The commit this review was generated against; "" when it does not say."""
        return _str(self.binding, "subject_head_sha")

    @property
    def effective_risk(self) -> str:
        """The risk this review was generated against (plan §13.5).

        Absent, the answer is `high`, not `low`: a review that does not say what it weighed has
        not said the change was safe, and the gate rules that read this all become permissive at
        `low`. Old and foreign reviews therefore get the strict path until they are regenerated.
        """
        recorded = _str(self.machine, "effective_risk")
        return recorded if recorded in RISK_VALUES else "high"

    @property
    def human_status(self) -> str:
        return _str(self.human, "status", "not_started")

    @property
    def actual_statements(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "actual_extraction")

    @property
    def claim_results(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "claims")

    @property
    def extra_behaviors(self) -> tuple[Mapping[str, Any], ...]:
        return _maps(self.machine, "extra_behaviors")

    @property
    def security_findings(self) -> tuple[Mapping[str, Any], ...]:
        security = self.machine.get("security")
        return _maps(security, "findings") if isinstance(security, dict) else ()

    @property
    def blocking_security_findings(self) -> tuple[Mapping[str, Any], ...]:
        """Findings that still hold gate ④ shut: `blocking`, and not closed by the change itself.

        A `resolved` finding stays in the document — that is the record of what closed it and
        against which head — but it is not a blocker any more. Filtering here rather than at each
        reader is what keeps `doctor`, `findings`, `human_review`, `pr_draft`, `review_policy` and
        `status_api` from having to agree about it separately.
        """
        return tuple(f for f in self.security_findings if f.get("blocking") is True and f.get("status") != "resolved")

    @property
    def coverage(self) -> Mapping[str, Any]:
        """The one Coverage Manifest for this review's diff, or empty when there is none.

        One, because the detector reads the whole diff or says which parts of it it could not.
        This used to be a list, for a partitioning nothing ever performed: a change too large to
        read in one sitting is refused by `review._refuse_over_budget` before a model is launched.
        """
        manifest = self.machine.get("coverage")
        return manifest if isinstance(manifest, Mapping) else {}

    @property
    def coverage_sufficient(self) -> bool:
        """True only when the coverage manifest says so.

        An absent coverage manifest is *not* sufficient, and neither is an ungenerated review:
        "we did not measure" and "we measured nothing missing" must never render the same
        (plan §2.4).
        """
        manifest = self.coverage
        return self.is_generated and bool(manifest) and _str(manifest, "coverage_status") == "sufficient"


@dataclass(frozen=True)
class ExecutorProfile:
    """One sandbox definition (plan §10.2)."""

    name: str
    raw: Mapping[str, Any]

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "host")

    @property
    def is_sandboxed(self) -> bool:
        return self.kind == "oci"

    @property
    def image(self) -> str:
        return _str(self.raw, "image")

    @property
    def containerfile(self) -> str:
        return _str(self.raw, "containerfile")

    @property
    def dockerfile(self) -> str:
        """Repo-relative path to a custom Containerfile ("" when this profile uses a packaged one)."""
        return _str(self.raw, "dockerfile")

    @property
    def build_target(self) -> str:
        """The `rein oci build --profile <x>` argument that builds this profile's image.

        A profile's name and its Containerfile's name are different things — the shipped
        `quality` profile builds from `python` — and `oci build` resolves its argument against
        the packaged Containerfiles, not against the config. Every site that guessed one name
        from the other printed a command that exits with "no packaged Containerfile named
        'quality'", which is why `containerfile:` is read here rather than left decorative.
        """
        return self.containerfile or self.name

    @property
    def image_digest(self) -> str:
        """The `sha256:…` half of a digest-pinned image reference ("" when unpinned)."""
        _, _, digest = self.image.partition("@")
        return digest

    @property
    def network_profile(self) -> str:
        return _str(self.raw, "network_profile")

    @property
    def env_allowlist(self) -> tuple[str, ...]:
        return _ids(self.raw, "env_allowlist")


@dataclass(frozen=True)
class GateStep:
    """One quality-gate step — the DoD is exactly this list, in order (plan §19)."""

    raw: Mapping[str, Any]

    @property
    def name(self) -> str:
        return _str(self.raw, "name")

    @property
    def kind(self) -> str:
        return _str(self.raw, "kind", "command")

    @property
    def command(self) -> tuple[str, ...]:
        return _ids(self.raw, "command")

    @property
    def agent_role(self) -> str:
        return _str(self.raw, "agent_role")

    @property
    def executor_profile(self) -> str:
        return _str(self.raw, "executor_profile")

    @property
    def retries(self) -> int:
        value = self.raw.get("retries")
        return value if isinstance(value, int) else 0

    @property
    def required(self) -> bool:
        """Must this step have something to run? Explicit opt-in, absent reads as False.

        It used to read as True unless the key said `false`, which contradicted both its own
        documentation ("an empty command is normally a silent skip — fine for a library") and the
        packaged config's comment on `smoke` ("…then set `required: true`"). That went unnoticed
        while nothing read the flag; now that `rein.preflight` refuses a run over it, a default of
        True would refuse every repository whose config simply never mentioned it.
        """
        return bool(self.raw.get("required"))

    @property
    def paths(self) -> tuple[str, ...]:
        """Glob patterns scoping this step to matching changed paths (empty: every task).

        Frozen at gate 3 like the rest of config.yaml — a human decision, never a knob a task's
        own ticket sets, which is what would let an implementer turn its own gate down. Matching
        happens in `build_loop.GateStep.matches_paths`, against the normalized step this parses
        into.
        """
        return _ids(self.raw, "paths")

    @property
    def stage(self) -> str:
        """Where this step runs: `task`, `integration`, or `both` (the default).

        Not a way to run less of the DoD — every step still runs — but a way to say *how often*.
        A whole test suite re-established from scratch on every attempt of every task, and again
        over the join, is the same confidence bought several times over.
        """
        value = _str(self.raw, "stage", "both")
        return value if value in GATE_STAGE_VALUES else "both"


#: The per-profile key that names a *build* of the sandbox rather than a decision about it. The
#: only thing `rein oci build --write-config` rewrites when a dependency changes, and the only
#: thing gate ③'s freeze lets move underneath it.
PIN_KEY = "image"


def _without_image_pins(raw: Mapping[str, Any]) -> dict[str, Any]:
    """`raw` with every `executor_profiles.<name>.image` removed (a copy; the input is untouched)."""
    profiles = raw.get("executor_profiles")
    if not isinstance(profiles, dict):
        return dict(raw)
    stripped = {
        name: ({k: v for k, v in body.items() if k != PIN_KEY} if isinstance(body, dict) else body)
        for name, body in profiles.items()
    }
    return {**raw, "executor_profiles": stripped}


@dataclass(frozen=True)
class Config:
    """``config.yaml`` — execution knobs, frozen at gate ③ apart from the image pins.

    Read by the guard hook, the build loop, the executors, and doctor. Note what it cannot
    answer: *who* may approve anything. There is no knob for that here — a gate opens only by a
    human typing its name at an interactive terminal, so a pull request cannot widen its own
    permissions by editing this file (plan §2.2).
    """

    raw: Mapping[str, Any]

    @classmethod
    def parse(cls, text: str, *, what: str = "config.yaml") -> Config:
        document = strict_yaml.load_mapping(text, what=what)
        errors = schema_errors(document, "config")
        if errors:
            raise DocumentError(what, errors)
        return cls(document)

    def frozen_digest(self) -> str:
        """The part of config.yaml gate ③'s approval covers: everything but the image pins.

        The pin is deliberately outside. A task that legitimately adds a dependency makes the
        pinned image wrong — the closure it needs is not baked in, and a `network: none` sandbox
        fails the same way on every retry — so the image has to be rebuilt *during* the cycle. With
        the whole file frozen, that one digest string cost a `rein revise --to tasks`: the plan
        un-froze, every gate below reset in a chain, and the human re-approved a plan nothing had
        changed. The environment moved; the decision did not.

        What stays inside is everything that is a decision: which profile each role runs under,
        `kind`, `network_profile`, `mount_repo`, the limits, the quality gate, the budgets, the
        guard. Flipping a profile from `oci` to `host`, or opening its network, still breaks the
        freeze and still needs a human — those widen what may happen, and only the pin narrows to
        "same sandbox, rebuilt".

        The pin is not thereby unbound: :meth:`environment_digest` covers it, gate ③ records that
        digest beside this one, and `rein doctor` and the gate ④ brief report when it has moved.
        """
        return digests.of(_without_image_pins(self.raw), drop=digests.VOLATILE_TIMESTAMP_KEYS)

    def environment_digest(self) -> str:
        """The sandbox picture as one digest: role→profile, and every profile body including its pin.

        This is what :meth:`frozen_digest` leaves out, plus what it keeps of the sandbox — so a
        reader comparing two of these is asking "was the evidence produced in the same environment",
        which is a different question from "is this the approved plan".

        Its predecessor hashed `{"executors": ...}` alone — the role→profile *name* map — while
        claiming in its own docstring to move when the sandbox a step ran in changed. It did not:
        `executor_profiles` is where `kind`, `image` and `network_profile` live, and repointing a
        profile at a different image left this digest identical. Nothing compared it either, so the
        claim was never contradicted by anything.
        """
        return digests.of(
            {"executors": self.raw.get("executors"), "executor_profiles": self.raw.get("executor_profiles")}
        )

    @property
    def work_branch(self) -> str:
        project = self.raw.get("project")
        return _str(project, "work_branch") if isinstance(project, dict) else ""

    @property
    def execution(self) -> Mapping[str, Any]:
        value = self.raw.get("execution")
        return value if isinstance(value, dict) else {}

    @property
    def max_parallel(self) -> int:
        return common.as_int(self.execution.get("max_parallel"), 3)

    @property
    def worktree_dir(self) -> str:
        return _str(self.execution, "worktree_dir", ".worktrees")

    @property
    def command_timeout_sec(self) -> int:
        return common.as_int(self.execution.get("command_timeout_sec"), 1800)

    @property
    def agent_timeout_sec(self) -> int:
        """How long one agent launch may run; **0 means no limit, and that is the default.**

        A wall clock cannot tell a model that is working from one that is stuck, and the two
        mistakes do not cost the same. Killing a working agent throws away the whole launch — its
        output, its quota, the session a retry would have resumed — and the retry then pays for all
        of it again from cold, which is the compounding waste this codebase keeps finding. Failing
        to kill a stuck one only stalls, and a stall is interruptible: `common.run` kills the
        process group on Ctrl-C, which until now it did not — the launch was orphaned into its own
        session and went on running, so the clock here was never what a human relied on anyway.

        A step whose runtime *is* knowable keeps its ceiling — that is `command_timeout_sec`, and
        `faults.classify_step` still reads a test suite that hangs as a fact about the code.
        """
        return common.as_int(self.execution.get("agent_timeout_sec"), 0)

    @property
    def launch_retries(self) -> int:
        """The whole run's allowance for retrying a launch the machine, not the code, failed.

        Run-scoped rather than per-task on purpose: a session limit or a missing binary is a
        property of the machine, and spending a task's budget on it would charge the code for
        something it did not do.
        """
        return common.as_int(self.execution.get("launch_retries"), 2)

    @property
    def profiles(self) -> dict[str, ExecutorProfile]:
        raw = self.raw.get("executor_profiles")
        if not isinstance(raw, dict):
            return {}
        return {name: ExecutorProfile(name, body) for name, body in raw.items() if isinstance(body, dict)}

    def profile_for(self, role: str) -> ExecutorProfile | None:
        """The sandbox a role runs in ("implementer" / "reviewer" / "quality_gate")."""
        executors = self.raw.get("executors")
        if not isinstance(executors, dict):
            return None
        return self.profiles.get(_str(executors, f"{role}_profile"))

    @property
    def quality_gate(self) -> tuple[GateStep, ...]:
        return tuple(GateStep(step) for step in _maps(self.raw, "quality_gate"))

    @property
    def agents(self) -> Mapping[str, Mapping[str, Any]]:
        raw = self.raw.get("agents")
        if not isinstance(raw, dict):
            return {}
        return {name: body for name, body in raw.items() if isinstance(body, dict)}

    def model(self, role: str) -> str:
        """Which model this role is told to run. "" means the CLI's own default."""
        return _str(self.agents.get(role, {}), "model")

    def independence_group(self, role: str) -> str:
        """`<adapter>/<model>` — **derived**, so it cannot disagree with what is launched.

        It used to be authored beside the adapter, and nothing passed a model anywhere: two roles
        could declare `claude/opus` and `claude/sonnet`, run the same model on the same CLI, and
        pass the critical-independence check on the strength of two different strings. A separation
        that is written down rather than performed is exactly what §12.4 exists to refuse, so the
        two fields are now one: you choose a model, and the group says which one you chose.

        Empty when no model is named — the CLI's default cannot be named in advance. What actually
        answered is read back from the launch (`usage.Usage.models`) and recorded on the review, and
        that is what the gate-④ check is settled on.
        """
        adapter, model = self.adapter(role), self.model(role)
        return f"{adapter}/{model}" if adapter and model else ""

    def adapter(self, role: str) -> str:
        return _str(self.agents.get(role, {}), "adapter")

    @property
    def template_mode(self) -> bool:
        guard = self.raw.get("guard")
        return bool(guard.get("template_mode")) if isinstance(guard, dict) else False

    @property
    def guard_paths(self) -> dict[str, str]:
        """Guarded path → the gate it requires. A trailing "/" makes it a prefix rule."""
        guard = self.raw.get("guard")
        entries = _maps(guard, "paths") if isinstance(guard, dict) else ()
        return {_str(e, "path"): _str(e, "requires_gate") for e in entries if _str(e, "path")}

    @property
    def budgets(self) -> dict[str, int]:
        policy = self.raw.get("review_policy")
        raw = policy.get("budgets") if isinstance(policy, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(v, int) and k in BUDGET_NAME_VALUES}

    @property
    def composition(self) -> str:
        """How gate ④ takes its reading: `auto` composes from the plan's tasks, `whole` never does.

        `auto` is the default because composition is what keeps the peak of one launch at the size
        of one task rather than of a cycle. `whole` is the escape for a repository that would
        rather pay for one reading of everything — and it is what the policy forces at `critical`
        risk regardless of this setting (`review_policy.coverage_blocks`).
        """
        policy = self.raw.get("review_policy")
        value = policy.get("composition") if isinstance(policy, dict) else None
        return value if value in COMPOSITION_MODE_VALUES else "auto"

    @property
    def github(self) -> Mapping[str, Any]:
        value = self.raw.get("github")
        return value if isinstance(value, dict) else {}

    def unsandboxed_code_profiles(self) -> list[str]:
        """Profiles that run repository-derived code on the host — a policy failure (plan §10.1).

        Reported by `doctor` rather than raised: a freshly initialized repository legitimately
        starts here, and the honest answer is "not compliant yet, here is the command", not a
        crash on first run.
        """
        return sorted({profile.name for profile in self._unsandboxed_profiles()})

    def unsandboxed_build_targets(self) -> list[str]:
        """The `rein oci build --profile <x>` arguments that would sandbox those profiles.

        Separate from the names above because they are not the same strings: reporting names an
        offending *profile*, fixing it names a *Containerfile*. Printing the first where the
        second belongs is how `doctor`, `rein next`, and both shipped configs came to
        recommend `--profile quality`, which no packaged Containerfile answers to.
        """
        return sorted({profile.build_target for profile in self._unsandboxed_profiles()})

    def sandbox_setup_command(self) -> str:
        """The one command that finishes the job for this config (:func:`sandbox_setup_command`)."""
        return sandbox_setup_command(self.unsandboxed_build_targets())

    def _unsandboxed_profiles(self) -> list[ExecutorProfile]:
        if not isinstance(self.raw.get("executors"), dict):
            return []
        profiles = (self.profile_for(role) for role in ("implementer", "reviewer", "quality_gate"))
        return [p for p in profiles if p is not None and not p.is_sandboxed]


# --- Event ---------------------------------------------------------------------
#
# Unlike the documents above these are *constructed* by Loose Rein rather than read from
# author-written files, so they are real dataclasses with an explicit canonical mapping.


@dataclass(frozen=True)
class Event:
    """One append-only audit record, chained to its predecessor (plan §18.5).

    `event_digest` covers the canonical form of every other field, so a rewritten event breaks
    its own digest; `prev_event_digest` chains it, so a deleted or reordered event breaks the
    next one's link. Both are computed by :mod:`rein.event_chain`, not here.
    """

    seq: int
    id: str
    tx_id: str
    ts: str
    event: str
    cycle_id: str
    actor: str = ""
    subject_ids: tuple[str, ...] = ()
    prev_event_digest: str = ""
    event_digest: str = ""
    detail: Mapping[str, Any] = field(default_factory=dict)

    def payload(self) -> dict[str, Any]:
        """The canonical mapping `event_digest` is computed over — every field but the digest itself."""
        return {
            "seq": self.seq,
            "id": self.id,
            "tx_id": self.tx_id,
            "ts": self.ts,
            "event": self.event,
            "cycle_id": self.cycle_id,
            "actor": self.actor,
            "subject_ids": list(self.subject_ids),
            "prev_event_digest": self.prev_event_digest,
            "detail": dict(self.detail),
        }

    def to_mapping(self) -> dict[str, Any]:
        """The full record as written to `events.ndjson` (payload plus `event_digest`)."""
        return {**self.payload(), "event_digest": self.event_digest}

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Event:
        """Build an Event from a parsed NDJSON line. Assumes the line already passed the schema."""
        subject_ids = raw.get("subject_ids") or []
        detail = raw.get("detail") or {}
        return cls(
            seq=int(raw["seq"]),
            id=str(raw["id"]),
            tx_id=str(raw.get("tx_id", "")),
            ts=str(raw.get("ts", "")),
            event=str(raw["event"]),
            cycle_id=str(raw.get("cycle_id", "")),
            actor=str(raw.get("actor", "")),
            subject_ids=tuple(str(s) for s in subject_ids) if isinstance(subject_ids, list) else (),
            prev_event_digest=str(raw.get("prev_event_digest", "")),
            event_digest=str(raw.get("event_digest", "")),
            detail=detail if isinstance(detail, dict) else {},
        )


# --- cross-reference validation (plan §23) ------------------------------------


def _dangling(
    plan: Plan, section: str, id_getter: str, refs: Iterable[str], target: str, target_label: str
) -> Iterator[str]:
    known = plan.ids(target)
    for ref in refs:
        if ref not in known:
            yield f"{section}/{id_getter}: unknown {target_label} id {ref!r}"


def cross_reference_errors(plan: Plan) -> list[str]:
    """Every dangling or malformed ID reference in `plan` (plan §23's table).

    JSON Schema can check that ``claim_ids`` is a list of ``C-\\d+`` strings; only this can
    check that ``C-002`` names a claim that exists. A dangling reference is where an AI's
    invented ID would otherwise survive review looking exactly like a real one.
    """
    errors: list[str] = []

    # ID shape and uniqueness, per section.
    for section, kind in (("claims", "claim"), ("tasks", "task")):
        seen: set[str] = set()
        for entry in _maps(plan.raw, section):
            element_id = _str(entry, "id")
            if not ID_PATTERNS[kind].match(element_id):
                errors.append(f"{section}: {element_id!r} does not match the {kind} id pattern")
            if element_id in seen:
                errors.append(f"{section}: duplicate id {element_id!r}")
            seen.add(element_id)

    task_ids = plan.ids("tasks")
    for task in plan.tasks:
        errors += _dangling(plan, "tasks", task.id, task.claim_ids, "claims", "claim")
        for blocker in task.blocked_by:
            if blocker not in task_ids:
                errors.append(f"tasks/{task.id}: unknown blocked_by task id {blocker!r}")
            elif blocker == task.id:
                errors.append(f"tasks/{task.id}: blocked_by lists itself")
        for path in (*task.scope_include, *task.scope_exclude):
            if not is_repo_path(path):
                errors.append(f"tasks/{task.id}: scope path {path!r} is not a safe repo-relative path")
        errors += _acceptance_errors(task)

    errors += _cycle_errors(plan)
    return errors


def _acceptance_errors(task: Task) -> list[str]:
    """Shape checks the schema cannot make about one task's acceptance criteria.

    Ids are unique *within the task* (they are scoped to it), and an evidence kind must carry
    what that kind is made of — a `command` with no argv, or an `artifact` naming no path, is a
    criterion that would report itself established by doing nothing at all.
    """
    errors: list[str] = []
    seen: set[str] = set()
    for entry in task.acceptance:
        ac_id = _str(entry, "id")
        if ac_id in seen:
            errors.append(f"tasks/{task.id}: duplicate acceptance id {ac_id!r}")
        seen.add(ac_id)
        evidence = entry.get("evidence")
        if not isinstance(evidence, dict):
            continue
        kind = _str(evidence, "kind")
        if kind == "command" and not _ids(evidence, "command"):
            errors.append(f"tasks/{task.id}/{ac_id}: evidence kind 'command' with no command to run")
        if kind == "artifact" and not _ids(evidence, "paths"):
            errors.append(f"tasks/{task.id}/{ac_id}: evidence kind 'artifact' with no paths to require")
    return errors


def _cycle_errors(plan: Plan) -> list[str]:
    """Cycles in the task DAG, reported as the participating ids (plan §16.4 "DAG acyclic")."""
    blocked: dict[str, tuple[str, ...]] = {t.id: t.blocked_by for t in plan.tasks}
    state: dict[str, int] = {}  # 0 = unvisited, 1 = on stack, 2 = done
    found: list[str] = []

    def visit(node: str, trail: list[str]) -> None:
        if state.get(node) == 2:
            return
        if state.get(node) == 1:
            cycle = trail[trail.index(node) :]
            found.append("tasks: dependency cycle " + " -> ".join([*cycle, node]))
            return
        state[node] = 1
        for parent in blocked.get(node, ()):
            if parent in blocked:
                visit(parent, [*trail, node])
        state[node] = 2

    for task_id in blocked:
        visit(task_id, [])
    return sorted(set(found))
