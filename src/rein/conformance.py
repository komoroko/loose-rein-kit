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

from rein import repo as repo_mod
from rein import review_policy


class ComparatorError(RuntimeError):
    """The Comparator was not independent, or produced output that could not be trusted."""


def build_request(
    *,
    expected_model: Mapping[str, Any],
    actual_statements: Iterable[Mapping[str, Any]],
    actual_digest: str,
) -> dict[str, Any]:
    """Assemble the Comparator's input (plan §12.3). The Actual arrives read-only, digest-bound."""
    return {
        "expected_model": dict(expected_model),
        "actual_statements": [dict(a) for a in actual_statements],
        "actual_digest": actual_digest,
    }


@dataclass(frozen=True)
class ComparatorResult:
    """The validated per-claim comparison and the actual-coverage gaps the Comparator found."""

    claims: tuple[dict[str, Any], ...]
    actual_coverage_gaps: tuple[dict[str, Any], ...]


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

    if problems:
        raise ComparatorError("conformance rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return ComparatorResult(claims=tuple(claims), actual_coverage_gaps=tuple(gaps))


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


def _string_list(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []
