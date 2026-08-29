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

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rein import common, models, review_policy
from rein import repo as repo_mod


class ComparatorError(common.ReinError, RuntimeError):
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
        "- Do not send an `integrity` field. The three axes are separate on purpose and integrity "
        "is the one that is not yours: it is derived from the anchors your citations rest on, "
        "re-checked against the committed blobs. semantic_support is your judgement, conformance "
        "is an observation, integrity is a fact about the tree.\n"
        "- Answer for EVERY claim in the expected model. A claim you leave out is not read as "
        "absent, it is recorded as `unknown` and put in front of a human as an unanswered "
        "decision; say `unknown` yourself, with what you were missing, rather than being silent."
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
    """The validated per-claim comparison, the actual-coverage gaps, and the extra behaviours.

    `claims` is one entry per claim in the *frozen plan*, not one per entry the Comparator
    returned. `unanswered` names the ones it was silent about, which is the difference.
    """

    claims: tuple[dict[str, Any], ...]
    actual_coverage_gaps: tuple[dict[str, Any], ...]
    extra_behaviors: tuple[dict[str, Any], ...] = ()
    unanswered: tuple[str, ...] = ()


def run_comparator(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
    actual_statements: Sequence[Mapping[str, Any]],
    known_ids: Iterable[str],
    expected_claim_ids: Iterable[str],
    effective_risk: str,
    independence: Mapping[str, Any],
) -> ComparatorResult:
    """Run the Comparator and validate it against the never-list (plan §24.3) and independence.

    **The claim list is framed by the plan, not by the answer.** A model's output is open-world:
    what came back is the claims it chose to speak about, and nothing in it says whether that was
    all of them. Read as a closed ledger — which is how this used to read it — a Comparator that
    returned three of eight claims produced a review saying `claims_total: 3, aligned: 3`, with no
    verdict, no decision card and no gate block for the other five. The one check that existed ran
    the other way (`validate_citations`: every id returned must exist in the plan), so a fabricated
    claim was caught and a missing one was invisible.

    So the frozen plan supplies the rows and the Comparator fills them. A claim it was silent about
    is completed here as `unknown` on all three axes — the same "we did not measure" state the rest
    of this codebase names rather than infers — which `decision_cards` turns into a card a human
    must answer. Completed rather than refused because refusing would throw away three launches
    over one omission, and on a plan with many claims there would be no way out of it.
    """
    ok, message = review_policy.independence_ok(independence, effective_risk)
    if not ok:
        raise ComparatorError(message)

    document = review_policy.parse_reviewer_output(reviewer(request).text, what="conformance")

    # §24.3: the Comparator must not rewrite the Actual. It references statements read-only.
    for forbidden in ("actual_statements", "actual_extraction"):
        if forbidden in document:
            raise ComparatorError(f"the Comparator returned `{forbidden}` — it cannot rewrite or add Actual Statements")
    if str(document.get("actual_digest", actual_digest_of(request))) != actual_digest_of(request):
        raise ComparatorError("the Comparator's actual_digest does not match the extraction it was given")

    actual_ids = {str(a.get("id")) for a in actual_statements}
    anchors_by_statement = {
        str(a.get("id")): [dict(anchor) for anchor in (a.get("code_anchors") or []) if isinstance(anchor, Mapping)]
        for a in actual_statements
    }
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
        # A volunteered `integrity` is dropped rather than refused. The contract says not to send
        # one and this is derived below either way, so the field carries no information — and
        # refusing an answer over a field nobody reads is how three launches used to be thrown away
        # for a value the contract itself had offered first.
        row = dict(claim)
        row.pop("integrity", None)
        claims.append(row)

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

    # A claim answered twice is not a partial answer to fill in, it is a contradictory one: two
    # verdicts for one claim, and picking either is this module deciding which the comparator
    # meant. Refused rather than de-duplicated — a `diverged` beside an `aligned` is exactly the
    # row that raises a decision card, and dropping it would open the gate on the quieter half.
    problems += review_policy.reject_duplicate_ids(
        [{"id": claim.get("claim_id")} for claim in claims], what="conformance"
    )
    if problems:
        raise ComparatorError("conformance rejected:\n" + "\n".join(f"  - {p}" for p in problems))

    framed, unanswered = _frame_by_expected(claims, expected_claim_ids)
    # The one axis that is not the Comparator's to give (`review_policy.derive_integrity`). An
    # unanswered row already carries `unavailable` and cites nothing, so it derives to the same.
    for row in framed:
        row["integrity"] = review_policy.derive_integrity(
            repo, commit, _string_list(row.get("actual_statement_ids")), anchors_by_statement
        )
    return ComparatorResult(
        claims=tuple(framed),
        actual_coverage_gaps=tuple(gaps),
        extra_behaviors=tuple(extras),
        unanswered=tuple(unanswered),
    )


#: What a claim the Comparator never spoke about is recorded as. Every axis says the same thing —
#: nobody looked — because inventing any other value here would be this file answering a question
#: it did not ask. `machine_assessed` is the only honest basis: no experiment, expert or proof was
#: involved in producing this row, and the schema has no basis meaning "none".
_UNANSWERED_TEXT = "the comparator returned no result for this claim"


def _unanswered_claim(claim_id: str) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "verdict": "unknown",
        "integrity": {"status": "unavailable"},
        "semantic_support": {"status": "unknown", "assessment_basis": "machine_assessed"},
        "conformance": {"status": "unknown"},
        "unknowns": [_UNANSWERED_TEXT],
    }


def _frame_by_expected(
    claims: Sequence[Mapping[str, Any]], expected_claim_ids: Iterable[str]
) -> tuple[list[dict[str, Any]], list[str]]:
    """The plan's claims in the plan's order, each carrying the Comparator's row or an `unknown`.

    An answer that reached here has one row per claim id at most: `reject_duplicate_ids` above
    refuses two, because choosing between two verdicts for one claim is not this function's to
    make.
    """
    answered: dict[str, dict[str, Any]] = {str(claim.get("claim_id", "")): dict(claim) for claim in claims}
    framed: list[dict[str, Any]] = []
    unanswered: list[str] = []
    for cid in expected_claim_ids:
        row = answered.pop(cid, None)
        if row is None:
            row = _unanswered_claim(cid)
            unanswered.append(cid)
        framed.append(row)
    # Anything left answered a claim the plan does not have. `validate_citations` already refused
    # that above, so this is unreachable by a valid answer — and appending rather than dropping
    # keeps this function from being the place a claim silently disappears.
    framed += list(answered.values())
    return framed, unanswered


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
