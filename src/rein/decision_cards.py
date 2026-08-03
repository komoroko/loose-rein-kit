"""Decision Cards and the statements their options mean — derived, not asked for.

Gate ④ asks a human to *decide*, not to read. The schema carries `decision_cards` and
`statements`, but nothing in `review.assemble` produces them — it emits claims, gaps,
extra behaviours and security findings and stops. Left there, the gate inverts:
`completion_blockers` demands an answer to every high/critical card (a comprehension check)
while the substantive judgement has no place to be recorded at all.

This module closes that by *derivation*, not by prompting. Every card here is a mechanical
restatement of a finding the review already made:

  - a claim the Comparator did not find aligned,
  - a gap,
  - an extra behaviour that is not grounded in any requirement,
  - a security finding.

Asking a model to invent the decision list would put the one artefact the human is answerable for
back inside the thing being reviewed. Deriving it means the cards cannot disagree with the findings,
cannot omit one, and need no adapter change to start working.

The options are `models.DISPOSITION_VALUES`, minus the ones that do not apply to the subject.
Note what no card offers: accepting the risk. That absence is the policy (plan §15.4) — it is not
an oversight, and a card generator that added an "accept" option would quietly repeal it.

Each option's meaning is a `statements[]` entry carrying `epistemic_status: machine_inferred`,
because these sentences are written by this file rather than observed in code or supported by a
source. There is no status meaning "assembled from a template", and labelling them anything
stronger would be the overclaim the whole vocabulary exists to prevent.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rein import models

#: The schema's `domain` shape, kept here because this is the only module that mints one.
_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Verdicts that leave a claim needing a human decision. `aligned` is the only one that does not.
_UNSETTLED_VERDICTS = frozenset({"diverged", "missing", "unverified", "unknown"})

#: The security domain is the one `requires_domains` value this module knows without being told;
#: every other domain comes from the plan claim's own `domains`, never guessed from prose.
_SECURITY_DOMAIN = "security"

#: Option ids are single upper-case letters (schema), so a card may not exceed 8 options anyway.
_OPTION_LETTERS = "ABCDEFGH"


def _risk_of(mapping: Mapping[str, Any], default: str = "low") -> str:
    risk = str(mapping.get("risk", default))
    return risk if risk in models.RISK_VALUES else default


def _severity_of(finding: Mapping[str, Any]) -> str:
    severity = str(finding.get("severity", "medium"))
    return severity if severity in models.RISK_VALUES else "medium"


_STMT_RE = re.compile(r"^STMT-(\d{3,})$")


def next_statement_index(*referenced: Iterable[object]) -> int:
    """One past the highest `STMT-N` any of `referenced` already mentions (1 when there are none).

    Ids minted here must not land on one something else already cites. `review._coverage_gaps`
    fills a gap's required `statement_id` with `STMT-<its own index>` when the Comparator supplied
    none, so minting from 1 would hand GAP-001's statement id to an unrelated decision-card option —
    the gap would then read as being about "return it to the implementer".
    """
    highest = 0
    for group in referenced:
        for value in group:
            match = _STMT_RE.match(str(value))
            if match:
                highest = max(highest, int(match.group(1)))
    return highest + 1


class _IdMinter:
    """Sequential STMT-/DC- ids. Deterministic order in, deterministic ids out."""

    def __init__(self, first_statement: int = 1) -> None:
        self._statement = first_statement - 1
        self._card = 0

    def statement(self) -> str:
        self._statement += 1
        return f"STMT-{self._statement:03d}"

    def card(self) -> str:
        self._card += 1
        return f"DC-{self._card:03d}"


def _subjects(
    claims: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]],
    extra_behaviors: Sequence[Mapping[str, Any]],
    security_findings: Sequence[Mapping[str, Any]],
    plan_risk: Mapping[str, str],
    plan_domains: Mapping[str, tuple[str, ...]],
) -> list[dict[str, Any]]:
    """Every finding that needs a human decision, in a stable order: claims, gaps, extras, security.

    A grounded extra behaviour is deliberately not a subject: the requirement that grounds it has
    already been decided. An ungrounded one is the loop asking "did you want this at all?".
    """
    subjects: list[dict[str, Any]] = []
    for claim in claims:
        if str(claim.get("verdict", "unknown")) not in _UNSETTLED_VERDICTS:
            continue
        cid = str(claim.get("claim_id", ""))
        subjects.append(
            {
                "subject_id": cid,
                "kind": "claim",
                "risk": plan_risk.get(cid, "low"),
                "domains": plan_domains.get(cid, ()),
                "question": (
                    f"{cid} is '{claim.get('verdict', 'unknown')}': the implementation was not shown to "
                    f"satisfy what the plan says it must. What happens to it?"
                ),
                "options": ("revise_implementation", "revise_design", "run_experiment", "request_expert", "dispute"),
                # Withheld until the reviewer records their own read (human_review.challenges).
                # Expected is what the plan says; Actual is what a reviewer that never saw the plan
                # read out of the code. Their disagreement is the whole signal.
                "evidence": {
                    "expected": claim.get("expected"),
                    "actual_statement_ids": list(claim.get("actual_statement_ids", ()) or ()),
                    "conformance": claim.get("conformance"),
                    "semantic_support": claim.get("semantic_support"),
                    "expected_choice": "revise_implementation",
                },
            }
        )
    for gap in gaps:
        gid = str(gap.get("id", ""))
        subjects.append(
            {
                "subject_id": gid,
                "kind": "gap",
                "risk": _risk_of(gap),
                "domains": (),
                "question": (
                    f"{gid} ({gap.get('kind', 'evidence_gap')}) is open"
                    f"{' and blocks the gate' if gap.get('blocking') is True else ''}. How is it closed?"
                ),
                "options": ("revise_implementation", "run_experiment", "request_expert", "reduce_scope", "dispute"),
                "evidence": {"gap": dict(gap), "expected_choice": "revise_implementation"},
            }
        )
    for extra in extra_behaviors:
        if extra.get("grounded") is True:
            continue
        eid = str(extra.get("id", ""))
        subjects.append(
            {
                "subject_id": eid,
                "kind": "extra_behavior",
                "risk": _risk_of(extra),
                "domains": (),
                "question": (
                    f"{eid} ({extra.get('category', 'unknown')}) is behaviour no requirement asked for. "
                    "Keep it or remove it?"
                ),
                "options": ("reduce_scope", "revise_requirement", "request_expert", "dispute"),
                "evidence": {"extra_behavior": dict(extra), "expected_choice": "reduce_scope"},
            }
        )
    for finding in security_findings:
        sid = str(finding.get("id", ""))
        subjects.append(
            {
                "subject_id": sid,
                "kind": "security",
                "risk": _severity_of(finding),
                "domains": (_SECURITY_DOMAIN,),
                "question": (
                    f"{sid} ({finding.get('category', 'other')}, severity {finding.get('severity', 'medium')}): "
                    f"{finding.get('attack_scenario', 'no scenario recorded')} What is done about it?"
                ),
                "options": ("revise_implementation", "reduce_scope", "request_expert", "dispute"),
                "evidence": {"finding": dict(finding), "expected_choice": "revise_implementation"},
            }
        )
    return subjects


#: The sentence each option means, phrased as what the human is choosing to do. Keyed by the option
#: token used in `_subjects`; the disposition action a UI records is the same token, minus `dispute`
#: which maps to `dispute_finding` because a dispute must carry a reason.
_OPTION_TEXT: dict[str, str] = {
    "revise_implementation": "Return it to the implementer: change the code until the review can be regenerated clean.",
    "revise_design": "Change the expectation: the plan, not the code, is what was wrong here.",
    "revise_requirement": "Adopt it as intended: reopen the requirement so this behaviour is something we asked for.",
    "run_experiment": "Run an experiment first: decide this once there is an observation to decide on.",
    "request_expert": "Route it to a domain expert: this is outside what a general reviewer should sign for.",
    "reduce_scope": "Take it out of scope: remove the behaviour rather than carry an undecided risk.",
    "dispute": "Dispute the finding, with a reason: a review that cannot be contradicted makes it infallible.",
}

#: Option token → the `models.DISPOSITION_VALUES` action a recorded answer becomes.
OPTION_DISPOSITION: dict[str, str] = {
    "revise_implementation": "revise_implementation",
    "revise_design": "revise_design",
    "revise_requirement": "revise_requirement",
    "run_experiment": "run_experiment",
    "request_expert": "request_expert",
    "reduce_scope": "reduce_scope",
    "dispute": "dispute_finding",
}


def derive_cards(
    *,
    claims: Sequence[Mapping[str, Any]] = (),
    gaps: Sequence[Mapping[str, Any]] = (),
    extra_behaviors: Sequence[Mapping[str, Any]] = (),
    security_findings: Sequence[Mapping[str, Any]] = (),
    plan_risk: Mapping[str, str] | None = None,
    plan_domains: Mapping[str, tuple[str, ...]] | None = None,
    first_statement: int = 1,
    max_cards: int = 64,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return `(statements, decision_cards)` for everything the review left for a human to settle.

    `max_cards` is the schema's ceiling, not a review budget: a run that produces more decisions than
    a person can hold is caught by `review_policy` budgets, which split the scope rather than
    truncate the list. Truncating here would hide decisions; the cap exists only so a pathological
    review cannot produce a document the schema refuses to store.
    """
    minter = _IdMinter(first_statement)
    statements: list[dict[str, Any]] = []
    cards: list[dict[str, Any]] = []
    subjects = _subjects(claims, gaps, extra_behaviors, security_findings, plan_risk or {}, plan_domains or {})
    for subject in subjects[:max_cards]:
        options = []
        for letter, token in zip(_OPTION_LETTERS, subject["options"], strict=False):
            statement_id = minter.statement()
            statements.append(
                {
                    "id": statement_id,
                    "text": _OPTION_TEXT[token],
                    "epistemic_status": "machine_inferred",
                    "applicability": {"subject_id": subject["subject_id"], "disposition": OPTION_DISPOSITION[token]},
                }
            )
            options.append({"id": letter, "statement_id": statement_id})
        card: dict[str, Any] = {
            "id": minter.card(),
            "question": subject["question"][:2000],
            "risk": subject["risk"],
            "options": options,
        }
        evidence = subject.get("evidence")
        if evidence:
            card["evidence"] = evidence
        domains = [d for d in subject["domains"] if _is_domain(d)]
        if domains:
            card["requires_domains"] = domains[:16]
        cards.append(card)
    return statements, cards


def _is_domain(value: object) -> bool:
    """Schema `domain`: a lower-case slug. A plan may carry anything, so only valid slugs go through.

    Dropping an unparseable domain rather than raising is deliberate: `requires_domains` widens what
    the reviewer must declare expertise in, and a malformed plan entry must not be able to make a
    review unstorable — the schema would reject the whole document for one bad string.
    """
    return isinstance(value, str) and bool(_DOMAIN_RE.match(value))


def derive_review_budget(
    *,
    limits: Mapping[str, int],
    decision_cards: Sequence[Mapping[str, Any]] = (),
    statements: Sequence[Mapping[str, Any]] = (),
    gaps: Sequence[Mapping[str, Any]] = (),
) -> list[dict[str, Any]]:
    """The budget snapshot recorded with the review, measured the same way `human_review` measures it.

    Recording it is what lets a receipt say which ceilings were in force when the review was
    signed; `human_review.budget_report` recomputes the live values for the screen, and the two agree
    because both read `models.BUDGET_NAMES` and the same definitions.
    """
    actuals = {
        "max_critical_decisions": sum(1 for c in decision_cards if _risk_of(c) == "critical"),
        "max_human_statements": len(statements),
        "max_unresolved_low_medium_unknowns": sum(
            1 for g in gaps if _risk_of(g) in ("low", "medium") and g.get("blocking") is not True
        ),
        # Enforced upstream: diff_facts partitions rather than truncates, so at review time the
        # measured value is always within budget by construction.
        "max_diff_bytes_per_partition": 0,
    }
    return [
        {
            "name": name,
            "limit": int(limits.get(name, 0)),
            "actual": actuals[name],
            "exceeded": actuals[name] > int(limits.get(name, 0)),
        }
        for name in models.BUDGET_NAMES
    ]
