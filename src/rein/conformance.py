"""The Conformance Comparator: does the Actual match the Expected? (plan §12.3)

The Comparator is the second, independent half of the review. It sees the frozen Expected
Model and the Actual Statements (already digest-bound by the blind extractor) — and it decides,
per claim, whether the code does what the plan said it would. The two halves disagreeing is the
signal; that is why the extractor is never shown the plan.

What it may *not* do is the point (plan §24.3). It cannot:

  - rewrite or add an Actual Statement — the Actual is the extractor's, referenced read-only,
    and a gap in it is an `actual_coverage_gap`, not something the Comparator fills in;
  - fabricate a claim id — every citation must resolve in the frozen plan;
  - lower the change's risk below its effective floor;
  - fill an Unknown with natural language.

For a *critical* change the Comparator and the extractor must be in distinct independence
groups (plan §12.4): one model session answering both halves is not a second opinion (E2E-26).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from rein import models, review_policy
from rein import repo as repo_mod


class ComparatorError(RuntimeError):
    """The Comparator was not independent, or produced output that could not be trusted."""


def contract() -> str:
    """What the Comparator is being asked for, carried *in* the request.

    Carried here for the same reason the extractor's is (`actual_extraction.contract`): the launch
    is given nothing to read but this. For this stage the point is less about blindness — it is
    handed the Expected Model on purpose — than about the answer being a function of the request
    rather than of whatever the CLI happened to load from a working directory.
    """
    verdicts = "|".join(sorted(models.VERDICT_VALUES))
    return (
        "Compare the Expected Model against the Actual, and report where they meet and where they "
        "do not.\n"
        "\n"
        "`actual_statements` is READ-ONLY. It was extracted by somebody who never saw the expected "
        "claims, and it is bound by `actual_digest`. You may cite it; you may not rewrite it, add "
        "to it, or return it.\n"
        "\n"
        "Answer with one JSON object and no other text:\n"
        '{"actual_digest": "<echo the one you were given, unchanged>", '
        '"claims": [{"claim_id": "<a claim id from the expected model>", '
        '"actual_statement_ids": ["AST-001"], '
        f'"verdict": "<one of {verdicts}>", '
        '"integrity": {"status": "verified|failed|unavailable"}, '
        '"semantic_support": {"status": "supported|contradicted|conflicted|unknown", '
        '"assessment_basis": "machine_assessed"}, '
        '"conformance": {"status": "observed|partial|unknown"}, "unknowns": ["<optional>"]}], '
        '"actual_coverage_gaps": [{"id": "GAP-001", "kind": "actual_coverage_gap", '
        '"risk": "low|medium|high|critical", "blocking": <bool>}], '
        '"extra_behaviors": [{"id": "EXTRA-001", "actual_statement_ids": ["AST-002"], '
        f'"category": "<one of {"|".join(sorted(EXTRA_CATEGORIES))}>", '
        '"risk": "low|medium|high|critical", "grounded": <bool>, "blocking": <bool>}]}\n'
        "\n"
        "Every rule below is checked, not trusted:\n"
        "- A claim id you did not receive, or an Actual Statement id the extractor did not "
        "produce, is a fabricated citation and the whole comparison is rejected.\n"
        "- `assessment_basis` is `machine_assessed`. You are a machine; the other values mean an "
        "experiment, an expert, or a proof, and claiming one is a lie about who did the work.\n"
        "- A claim's `risk` may not be lower than the change's effective risk.\n"
        "- `extra_behaviors` is behaviour in the Actual that no expected claim accounts for. Each "
        "one must cite the Actual Statement it was read from — that section is the only answer to "
        '"did this build something nobody asked for?", and an empty list is a finding, not a '
        "formality.\n"
        "- There is no single `verified`. The three axes are separate on purpose: integrity is a "
        "fact, semantic_support is your judgement, conformance is an observation."
    )


def build_request(
    *,
    expected_model: Mapping[str, Any],
    actual_statements: Iterable[Mapping[str, Any]],
    actual_digest: str,
) -> dict[str, Any]:
    """Assemble the Comparator's input (plan §12.3). The Actual arrives read-only, digest-bound."""
    return {
        "contract": contract(),
        "expected_model": dict(expected_model),
        "actual_statements": [dict(a) for a in actual_statements],
        "actual_digest": actual_digest,
    }


#: What an extra behaviour can be (review.schema.json `machine.extra_behaviors[].category`).
#: A category outside this list is refused rather than defaulted: there is no neutral value that
#: would be honest, and inventing one would file behaviour nobody classified under a name nobody
#: chose.
EXTRA_CATEGORIES = frozenset(
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


@dataclass(frozen=True)
class ComparatorResult:
    """The validated per-claim comparison, the actual-coverage gaps, and the extra behaviours."""

    claims: tuple[dict[str, Any], ...]
    actual_coverage_gaps: tuple[dict[str, Any], ...]
    extra_behaviors: tuple[dict[str, Any], ...] = ()


def run_comparator(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
    actual_statement_ids: Iterable[str],
    known_ids: Iterable[str],
    effective_risk: str,
    independence: Mapping[str, Any],
) -> ComparatorResult:
    """Run the Comparator and validate it against the never-list (plan §24.3) and independence."""
    ok, message = review_policy.independence_ok(independence, effective_risk)
    if not ok:
        raise ComparatorError(message)

    document = review_policy.parse_reviewer_output(reviewer(request), what="conformance")

    # §24.3: the Comparator must not rewrite the Actual. It references statements read-only.
    for forbidden in ("actual_statements", "actual_extraction"):
        if forbidden in document:
            raise ComparatorError(f"the Comparator returned `{forbidden}` — it cannot rewrite or add Actual Statements")
    if str(document.get("actual_digest", actual_digest_of(request))) != actual_digest_of(request):
        raise ComparatorError("the Comparator's actual_digest does not match the extraction it was given")

    actual_ids = set(actual_statement_ids)
    known = set(known_ids)
    problems: list[str] = []
    claims: list[dict[str, Any]] = []
    raw_claims = document.get("claims")
    if not isinstance(raw_claims, list):
        raise ComparatorError("conformance: `claims` must be a list")
    for index, claim in enumerate(raw_claims):
        if not isinstance(claim, Mapping):
            problems.append(f"claims[{index}] is not a mapping")
            continue
        problems += _validate_claim(
            claim,
            actual_ids=actual_ids,
            known=known,
            effective_risk=effective_risk,
        )
        claims.append(dict(claim))

    gaps_raw = document.get("actual_coverage_gaps")
    gaps = [dict(g) for g in gaps_raw if isinstance(g, Mapping)] if isinstance(gaps_raw, list) else []

    extras_raw = document.get("extra_behaviors")
    extras: list[dict[str, Any]] = []
    if isinstance(extras_raw, list):
        for index, extra in enumerate(extras_raw):
            if not isinstance(extra, Mapping):
                problems.append(f"extra_behaviors[{index}] is not a mapping")
                continue
            problems += _validate_extra(extra, actual_ids=actual_ids)
            extras.append(dict(extra))

    if problems:
        raise ComparatorError("conformance rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return ComparatorResult(
        claims=tuple(claims),
        actual_coverage_gaps=tuple(gaps),
        extra_behaviors=tuple(extras),
    )


def actual_digest_of(request: Mapping[str, Any]) -> str:
    return str(request.get("actual_digest", ""))


def _validate_claim(
    claim: Mapping[str, Any],
    *,
    actual_ids: set[str],
    known: set[str],
    effective_risk: str,
) -> list[str]:
    problems: list[str] = []
    cid = str(claim.get("claim_id", "?"))

    # The Comparator references the extractor's statements read-only — it cannot invent one.
    referenced_actual = _string_list(claim.get("actual_statement_ids"))
    for aid in referenced_actual:
        if aid not in actual_ids:
            problems.append(f"{cid}: references Actual Statement {aid!r}, which the extractor never produced")

    # §12.7 / §24.3: no fabricated claim citations.
    problems += [f"{cid}: {p}" for p in review_policy.validate_citations({cid}, known, what="conformance")]

    # §24.2: the Comparator cannot self-report `integrity: verified`.
    problems += review_policy.reject_self_attestation(claim)

    # An AI cannot lower the change's risk below its effective floor.
    if claim.get("risk") is not None:
        problems += review_policy.reject_risk_downgrade(str(claim.get("risk")), effective_risk, subject=cid)
    return problems


def _validate_extra(extra: Mapping[str, Any], *, actual_ids: set[str]) -> list[str]:
    """An extra behaviour is a statement about the Actual, so it must anchor in the Actual.

    This is the one section the plan cannot check the Comparator against — by definition no claim
    accounts for it, so `validate_citations` has nothing to resolve the entry to. The Actual is
    what is left: an extra behaviour that names no Actual Statement is the Comparator describing
    code nobody read, and it is refused for the same reason a fabricated claim citation is.

    The change's risk floor is deliberately *not* applied here. A claim's risk restates the
    change's, so `reject_risk_downgrade` belongs there; an extra behaviour's risk is a property of
    that behaviour, and forcing every one of them up to a critical change's floor would report ten
    critical findings where there is one.
    """
    problems: list[str] = []
    eid = str(extra.get("id", "?"))
    referenced = _string_list(extra.get("actual_statement_ids"))
    if not referenced:
        problems.append(f"{eid}: names no Actual Statement — an extra behaviour must cite the code it was read from")
    for aid in referenced:
        if aid not in actual_ids:
            problems.append(f"{eid}: references Actual Statement {aid!r}, which the extractor never produced")
    category = str(extra.get("category", ""))
    if category not in EXTRA_CATEGORIES:
        problems.append(f"{eid}: category {category!r} is not one this release classifies")
    return problems


def _string_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []
