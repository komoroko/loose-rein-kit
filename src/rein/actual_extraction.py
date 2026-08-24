"""The Blind Actual Behavior Extractor: describe what the code does, without being told what it
should do (plan §12.2).

The failure this prevents is *priming*. An extractor shown the Expected Claim, the plan's
rationale, or the implementer's own explanation tends to confirm them — it reads the code
looking for the behavior it was told to find, and reports that behavior whether or not the code
has it (plan §2.1, E2E-19). So the extractor is handed a deliberately narrow input:

  given        the trusted base SHA, the subject head snapshot, the final diff, the relevant
               code, the deterministic facts, and any *expectation-free* runtime trace.
  never given  `plan.yaml`, the Expected Claim, the rationale, an expected result, the
               implementer's self-explanation, or the Decision Card.

:func:`build_request` constructs only the first list, and :func:`assert_blind` re-checks the
serialized request for any forbidden key before it is ever sent — a defense in depth, because
the whole value of the extractor collapses the moment it is primed.

Its output is untrusted like every reviewer's (plan §12.7): each Actual Statement's code anchor
is validated against the committed blob, so a behavior asserted without real code behind it is
rejected, not displayed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rein import digests, review_policy
from rein import repo as repo_mod

#: Keys that must never reach the extractor — the Expected Model in any of its forms (plan §12.2,
#: §24.2). Checked against the whole serialized request, not just its top level.
FORBIDDEN_KEYS = frozenset(
    {
        "plan",
        "expected",
        "expected_claim",
        "expected_claims",
        "claims",
        "solution",
        "solutions",
        "rationale",
        "expected_exit_code",
        "expected_result",
        "implementer_explanation",
        "self_explanation",
        "decision_card",
        "decision_cards",
    }
)

#: The confidence values an Actual Statement may carry.
CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})


class ExtractionError(RuntimeError):
    """The extractor was primed, or produced output that could not be trusted."""


def categories() -> tuple[str, ...]:
    """The Actual Statement categories, read from the schema that enforces them.

    Read rather than restated so the contract below cannot name a vocabulary the write would then
    refuse. The same reason `OPERATOR_SURFACE_KINDS` is a subset of this list rather than a list
    of its own.
    """
    return review_policy.review_schema_enum("actual_extraction", "items", "properties", "category", "enum")


def contract() -> str:
    """What the extractor is being asked for, carried *in* the request.

    It used to be carried nowhere. The transport launched the adapter in rein's own working
    directory — the repository — and whatever the CLI loaded from there was the only thing telling
    it what to produce. For this stage that is a hole in the floor: the repository root is where
    `AGENTS.md` explains the Expected Model, and `.rein/plan.yaml` *is* the Expected Model. A blind
    extraction that can read the plan is not blind, and `assert_blind` only ever guarded the
    payload.

    So the request says what it wants and the launch is given nothing else to read
    (`review._adapter_reviewer`). Which means everything the answer needs has to be *in* here —
    the anchors' blobs and line counts included, since the extractor can no longer look them up.
    """
    return (
        "Read the change below and report WHAT THE CODE ACTUALLY DOES.\n"
        "\n"
        "Report only behaviour you can point at in the code. Do not reconstruct intent, do not say "
        "what the change is *for*, and do not close a gap with what a careful author would "
        "presumably have wanted. This reading is worth having precisely because it was made "
        "without any of that, so a guess costs more than a silence.\n"
        "\n"
        "Answer with one JSON object and no other text:\n"
        '{"actual_statements": [{"id": "AST-001", "statement": "<what the code does>", '
        f'"category": "<one of {"|".join(categories())}>", '
        f'"confidence": "<one of {"|".join(sorted(CONFIDENCE_VALUES))}>", '
        '"code_anchors": [{"path": "<repo-relative path>", "start_line": <int>, '
        '"end_line": <int>, "blob": "<the blob deterministic_facts.files gives for that path>"}], '
        '"observed_conditions": ["<optional>"], "unknowns": ["<optional>"]}], '
        '"coverage": {"risk_floor": "<not lower than deterministic_facts.risk_floor>"}}\n'
        "\n"
        "Every rule below is checked against the committed tree, not taken on trust:\n"
        "- A statement with no code anchor is refused. No anchor, no assertion.\n"
        "- `path`, `blob` and the line range must match a real committed blob. "
        "`deterministic_facts.files` lists a blob and a line count for every path in the "
        "change; use them rather than guessing, and never anchor outside that range.\n"
        "- Do not send an `integrity` field. Integrity is derived from your anchors, never claimed.\n"
        "- `coverage.risk_floor` may not be lower than the one in the deterministic facts.\n"
        "- Number the ids AST-001, AST-002, … A whole extraction is rejected if any part of it "
        "fails, so say less rather than reaching past what you can anchor."
    )


def build_request(
    *,
    trusted_base_sha: str,
    subject_head_sha: str,
    diff_text: str,
    deterministic_facts: Mapping[str, Any],
    runtime_observations: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble the extractor's input — and only the blind-safe parts of it (plan §12.2).

    `runtime_observations` must already be stripped of expected values by the caller; this is
    where an *observation* (what happened) is allowed but an *expectation* (what should have
    happened) is not.
    """
    request: dict[str, Any] = {
        "contract": contract(),
        "trusted_base_sha": trusted_base_sha,
        "subject_head_sha": subject_head_sha,
        "diff": diff_text,
        "deterministic_facts": dict(deterministic_facts),
    }
    if runtime_observations:
        request["runtime_observations"] = dict(runtime_observations)
    assert_blind(request)
    return request


def assert_blind(request: Mapping[str, Any]) -> None:
    """Raise if any forbidden (Expected-Model) key appears anywhere in the request.

    A structural guard, not a substitute for building the request correctly: it walks the whole
    tree so a forbidden key nested inside `deterministic_facts` is caught too.
    """
    found = sorted(_forbidden_keys_in(request))
    if found:
        raise ExtractionError(
            f"the extractor request carries Expected-Model keys {found} — priming it defeats the "
            "point of a blind Actual extraction (plan §12.2)"
        )


def _forbidden_keys_in(node: object) -> set[str]:
    found: set[str] = set()
    if isinstance(node, Mapping):
        for key, value in node.items():
            if key in FORBIDDEN_KEYS:
                found.add(str(key))
            found |= _forbidden_keys_in(value)
    elif isinstance(node, list):
        for item in node:
            found |= _forbidden_keys_in(item)
    return found


@dataclass(frozen=True)
class ExtractionResult:
    """A validated blind extraction: the actual statements and the digest they are bound by."""

    actual_statements: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    actual_digest: str


def run_extractor(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
    risk_floor: str = "low",
) -> ExtractionResult:
    """Run the extractor and validate its output — anchors, confidence, and the never-list.

    The output is untrusted (plan §12.7): every code anchor is checked against the committed
    blob, an unknown confidence is refused, and any `integrity: verified` the extractor tried to
    grant itself is rejected (§24.2). The `actual_digest` binds the validated statements so the
    Comparator cannot be handed anything the extractor did not actually produce.
    """
    assert_blind(request)  # never send a primed request, even one built elsewhere
    document = review_policy.parse_reviewer_output(reviewer(request), what="actual extraction")

    raw_statements = document.get("actual_statements")
    if not isinstance(raw_statements, list):
        raise ExtractionError("actual extraction: `actual_statements` must be a list")

    problems: list[str] = []
    statements: list[dict[str, Any]] = []
    for index, statement in enumerate(raw_statements):
        if not isinstance(statement, Mapping):
            problems.append(f"actual_statements[{index}] is not a mapping")
            continue
        problems += _validate_statement(statement, repo=repo, commit=commit)
        statements.append(dict(statement))

    coverage = document.get("coverage")
    coverage_map = dict(coverage) if isinstance(coverage, Mapping) else {}
    problems += _validate_extractor_risk(coverage_map, risk_floor)

    if problems:
        raise ExtractionError("actual extraction rejected:\n" + "\n".join(f"  - {p}" for p in problems))

    actual_digest = digests.of({"actual_statements": statements, "coverage": coverage_map})
    return ExtractionResult(
        actual_statements=tuple(statements),
        coverage=coverage_map,
        actual_digest=actual_digest,
    )


def _validate_statement(statement: Mapping[str, Any], *, repo: repo_mod.Repo, commit: str) -> list[str]:
    problems: list[str] = []
    sid = str(statement.get("id", "?"))
    confidence = str(statement.get("confidence", ""))
    if confidence not in CONFIDENCE_VALUES:
        problems.append(f"{sid}: confidence {confidence!r} is not one of {sorted(CONFIDENCE_VALUES)}")
    # An extractor must not grant itself integrity — that is derived, never claimed (§24.2).
    if "integrity" in statement:
        problems.append(f"{sid}: an Actual Statement cannot carry an `integrity` field — integrity is derived")
    anchors = statement.get("code_anchors")
    if not isinstance(anchors, list) or not anchors:
        problems.append(f"{sid}: an Actual Statement must cite at least one code anchor (no anchor, no assertion)")
        return problems
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            problems.append(f"{sid}: a code anchor must be a mapping")
            continue
        problems += [f"{sid}: {p}" for p in review_policy.validate_anchor(repo, commit, anchor)]
    return problems


def _validate_extractor_risk(coverage: Mapping[str, Any], risk_floor: str) -> list[str]:
    """The extractor may not certify coverage that would lower the detector's risk floor."""
    claimed = str(coverage.get("risk_floor", risk_floor)) if coverage.get("risk_floor") else risk_floor
    return review_policy.reject_risk_downgrade(claimed, risk_floor, subject="actual extraction coverage")
