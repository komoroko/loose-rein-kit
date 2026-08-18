"""Verify decision_cards.py: the derived judgement surface of gate ④.

These cards are the one artefact the human is answerable for, so what matters here is that they are
*derivations* — every finding that needs a decision produces one, none is invented, and the option
set never grows an "accept the risk" entry the disposition vocabulary deliberately omits.
"""

from __future__ import annotations

from typing import Any

import pytest

from rein import human_review, models, review


def _claim(claim_id: str = "C-001", verdict: str = "diverged", **extra: Any) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "verdict": verdict,
        "integrity": {"status": "verified"},
        "semantic_support": {"status": "contradicted", "assessment_basis": "machine_assessed"},
        "conformance": {"status": "unknown"},
        **extra,
    }


def _machine(**kwargs: Any) -> dict[str, Any]:
    binding = {
        "change_digest": "sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "toolchain_digest": "sha256:" + "c" * 64,
    }
    coverage = {
        "diff_digest": "sha256:" + "d" * 64,
        "coverage_status": "sufficient",
        "analyzed_files": 1,
        "analyzed_bytes": 1024,
        "analyzed_hunks": 1,
        "truncated": False,
    }
    kwargs.setdefault("actual_statements", [])
    kwargs.setdefault("claims", [])
    return review.assemble(binding=binding, coverage=coverage, **kwargs)


def _review(machine: dict[str, Any], human: dict[str, Any] | None = None) -> models.Review:
    return models.Review({"machine": machine, "human": human or {"status": "not_started"}})


# --- derivation -----------------------------------------------------------------


def test_every_unsettled_finding_becomes_exactly_one_card() -> None:
    machine = _machine(
        claims=[_claim("C-001", "diverged"), _claim("C-002", "aligned"), _claim("C-003", "missing")],
        gaps=[{"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "high"}],
        extra_behaviors=[
            {
                "id": "EXTRA-001",
                "statement_id": "STMT-002",
                "category": "new_default",
                "risk": "medium",
                "grounded": False,
            },
            {
                "id": "EXTRA-002",
                "statement_id": "STMT-003",
                "category": "new_default",
                "risk": "low",
                "grounded": True,
            },
        ],
        security={"findings": [{"id": "SEC-001", "severity": "high", "category": "ssrf", "blocking": True}]},
    )
    subjects = [c["question"].split()[0] for c in machine["decision_cards"]]
    # aligned C-002 and the grounded EXTRA-002 are settled; everything else needs a human
    assert subjects == ["C-001", "C-003", "GAP-001", "EXTRA-001", "SEC-001"]


def test_an_all_aligned_review_asks_for_no_decisions() -> None:
    machine = _machine(claims=[_claim("C-001", "aligned"), _claim("C-002", "aligned")])
    assert "decision_cards" not in machine and "statements" not in machine


def test_no_card_offers_accepting_the_risk() -> None:
    """plan §15.4: there is no disposition meaning "we looked at it and moved on"."""
    machine = _machine(
        claims=[_claim("C-001", "diverged")],
        gaps=[{"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "critical"}],
        security={"findings": [{"id": "SEC-001", "severity": "critical", "category": "authz_bypass"}]},
    )
    by_id = {s["id"]: s for s in machine["statements"]}
    for card in machine["decision_cards"]:
        for option in card["options"]:
            action = by_id[option["statement_id"]]["applicability"]["disposition"]
            assert action in models.DISPOSITION_VALUES
            assert "accept" not in action


def test_option_statements_are_labelled_machine_inferred() -> None:
    """These sentences are written by decision_cards.py; no status means "assembled from a template"."""
    machine = _machine(claims=[_claim("C-001", "diverged")])
    assert {s["epistemic_status"] for s in machine["statements"]} == {"machine_inferred"}


def test_card_risk_and_domains_come_from_the_frozen_plan(tmp_path_factory: pytest.TempPathFactory) -> None:
    from tests._support import make_claim, make_plan

    plan = models.Plan(make_plan(claims=[make_claim("C-001", risk="critical")]))
    plan.raw["claims"][0]["domains"] = ["payments", "Not A Slug"]
    machine = _machine(claims=[_claim("C-001", "diverged")], plan=plan)
    card = machine["decision_cards"][0]
    assert card["risk"] == "critical"
    # a malformed domain is dropped rather than making the whole review unstorable
    assert card["requires_domains"] == ["payments"]


def test_security_cards_route_to_the_security_domain() -> None:
    machine = _machine(security={"findings": [{"id": "SEC-001", "severity": "high", "category": "ssrf"}]})
    assert machine["decision_cards"][0]["requires_domains"] == ["security"]


def test_minted_statement_ids_never_collide_with_an_existing_reference() -> None:
    """`review._coverage_gaps` invents STMT-<index> for a gap the Comparator left unlabelled."""
    machine = _machine(
        claims=[_claim("C-001", "diverged")],
        gaps=[
            {"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "high"},
            {"id": "GAP-002", "kind": "evidence_gap", "statement_id": "STMT-002", "risk": "high"},
        ],
    )
    minted = {s["id"] for s in machine["statements"]}
    assert not minted & {"STMT-001", "STMT-002"}


def test_derived_review_is_schema_valid() -> None:
    machine = _machine(
        claims=[_claim("C-001", "diverged")],
        gaps=[
            {
                "id": "GAP-001",
                "kind": "evidence_gap",
                "statement_id": "STMT-001",
                "risk": "high",
                "blocking": False,
            }
        ],
        security={
            "findings": [
                {
                    "id": "SEC-001",
                    "severity": "critical",
                    "category": "authz_bypass",
                    "attack_scenario": "An unauthenticated caller reaches the admin route.",
                    "code_anchors": [],
                    "blocking": True,
                }
            ]
        },
        budget_limits=human_review.DEFAULT_BUDGET,
    )
    assert models.schema_errors({"machine": machine, "human": {"status": "not_started"}}, "review") == []


def test_review_budget_snapshot_matches_the_live_measurement() -> None:
    """The recorded snapshot and human_review's live report must not be able to disagree."""
    machine = _machine(
        claims=[_claim(f"C-{n:03d}", "diverged") for n in range(1, 4)],
        budget_limits=human_review.DEFAULT_BUDGET,
    )
    recorded = {row["name"]: row["actual"] for row in machine["review_budget"]}
    live = human_review.budget_actuals(_review(machine), {})
    for name in ("max_critical_decisions", "max_human_statements"):
        assert recorded[name] == live[name], name


# --- recording an answer ---------------------------------------------------------


def test_recording_a_decision_requires_a_real_card_option_and_confidence() -> None:
    machine = _machine(claims=[_claim("C-001", "diverged")])
    rev = _review(machine)
    with pytest.raises(ValueError, match="unknown decision card"):
        human_review.record_decision(rev, {}, "DC-404", "A", confidence="high")
    with pytest.raises(ValueError, match="is not an option"):
        human_review.record_decision(rev, {}, "DC-001", "Z", confidence="high")
    with pytest.raises(ValueError, match="confidence must be"):
        human_review.record_decision(rev, {}, "DC-001", "A", confidence="fairly-sure")


def test_a_decision_may_be_changed_while_the_review_is_open() -> None:
    """Unlike a challenge, which records what the reviewer thought before the reveal."""
    machine = _machine(claims=[_claim("C-001", "diverged")])
    rev = _review(machine)
    human = human_review.record_decision(rev, {}, "DC-001", "A", confidence="low", reason="first pass")
    human = human_review.record_decision(rev, human, "DC-001", "C", confidence="high", reason="after digging")
    assert human["decisions"] == [{"card_id": "DC-001", "choice": "C", "confidence": "high", "reason": "after digging"}]


def test_unanswered_high_risk_decisions_block_the_freeze() -> None:
    machine = _machine(
        claims=[_claim("C-001", "diverged")],
        gaps=[{"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "high"}],
    )
    rev = _review(machine)
    assert any("unanswered high/critical decision cards" in b for b in human_review.completion_blockers(rev))
    # DC-002 is the high-risk gap card; the low-risk claim card does not block
    human = human_review.record_decision(rev, {}, "DC-002", "A", confidence="high")
    assert not any("decision cards" in b for b in human_review.completion_blockers(_review(machine), human))


def test_low_risk_cards_do_not_block(caplog: pytest.LogCaptureFixture) -> None:
    """Making every card mandatory is how a forcing function becomes a formality."""
    machine = _machine(claims=[_claim("C-001", "diverged")])  # no plan → risk low
    assert human_review.unanswered_decisions(_review(machine), {}) == []


# --- challenge risk-scoping (derived from the same Decision Cards, not a separate section) ---
#
# A challenge is not a generated artefact any more: it is a high/critical Decision Card asked in
# two beats, so risk-scoping is exercised directly through the claims/gaps that produce cards.


def test_only_high_and_critical_cards_are_asked_as_challenges() -> None:
    machine = _machine(
        claims=[_claim("C-001", "aligned")],
        gaps=[
            {"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "low"},
            {"id": "GAP-002", "kind": "evidence_gap", "statement_id": "STMT-002", "risk": "critical"},
            {"id": "GAP-003", "kind": "evidence_gap", "statement_id": "STMT-003", "risk": "medium"},
        ],
    )
    ids = [c["id"] for c in human_review.challenges(_review(machine))]
    assert ids == ["DC-002"]  # only the critical gap's card


def test_a_low_risk_review_is_challenge_complete_from_the_start() -> None:
    machine = _machine(
        claims=[_claim("C-001", "aligned")],
        gaps=[
            {"id": "GAP-001", "kind": "evidence_gap", "statement_id": "STMT-001", "risk": "low"},
            {"id": "GAP-002", "kind": "evidence_gap", "statement_id": "STMT-002", "risk": "medium"},
        ],
    )
    rev = _review(machine)
    assert human_review.challenges(rev) == ()
    assert human_review.challenges_complete(rev, {}) is True


def test_the_challenge_set_is_capped_hardest_first() -> None:
    machine = _machine(
        claims=[_claim("C-001", "aligned")],
        gaps=[
            *(
                {"id": f"GAP-{n:03d}", "kind": "evidence_gap", "statement_id": f"STMT-{n:03d}", "risk": "high"}
                for n in range(1, 6)
            ),
            {"id": "GAP-009", "kind": "evidence_gap", "statement_id": "STMT-009", "risk": "critical"},
        ],
    )
    asked = [c["id"] for c in human_review.challenges(_review(machine))]
    assert len(asked) == human_review.MAX_CHALLENGES
    critical_card = next(c["id"] for c in machine["decision_cards"] if c["risk"] == "critical")
    assert asked[0] == critical_card  # critical outranks the high ones regardless of id order
