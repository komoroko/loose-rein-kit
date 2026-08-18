"""The human review is a decision procedure, so every rule here is a fixed fact (plan §14, §30).

These tests never open a browser: they build a `review.yaml` in memory and pin the sequence gate,
the challenge/mismatch/counterfactual chain, the expertise routing (E2E-05), the budget block
(E2E-30), and the machine-digest staleness that refuses a raced write (E2E-08) while leaving a
human-only update non-staling (E2E-09).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rein import human_review, models
from tests._support import make_review


def _review(*, machine: dict[str, Any] | None = None, human: dict[str, Any] | None = None) -> models.Review:
    base_machine: dict[str, Any] = {
        "status": "generated",
        "binding": {
            "change_digest": "sha256:" + "a" * 64,
            "plan_digest": "sha256:" + "b" * 64,
            "toolchain_digest": "sha256:" + "c" * 64,
        },
        "coverage": [
            {
                "diff_digest": "sha256:" + "d" * 64,
                "analyzed_files": 1,
                "analyzed_bytes": 1024,
                "truncated": False,
                "coverage_status": "sufficient",
            }
        ],
        "actual_extraction": [],
        "claims": [],
    }
    base_machine.update(machine or {})
    return models.Review({"machine": base_machine, "human": human or {"status": "not_started"}})


def _challenge(cid: str, expected: str, *, risk: str = "high", claim_ids: list[str] | None = None) -> dict[str, Any]:
    """A high/critical Decision Card — the unit `human_review.challenges` derives its two-beat
    sequence from. `evidence` is what the API withholds until the reviewer answers (plan §14.2)."""
    return {
        "id": cid,
        "risk": risk,
        "question": "how does the retry path behave?",
        "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
        "evidence": {
            "expected_choice": expected,
            "expected": {"statement": "a lost response never double-commits"},
            "actual_statement_ids": claim_ids or [],
        },
    }


# --- challenge sequence (plan §14.2, §21.2) -----------------------------------


def test_next_challenge_strips_the_evidence() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    nxt = human_review.next_challenge(review, dict(review.human))
    assert nxt is not None and nxt["id"] == "DC-001"
    assert "evidence" not in nxt  # the expected choice must not ride along with the question


def test_challenges_are_not_complete_until_answered() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    human = dict(review.human)
    assert human_review.challenges_complete(review, human) is False
    answered = human_review.record_challenge_answer(review, human, "DC-001", "B", confidence="low")
    assert human_review.challenges_complete(review, answered) is True


def test_a_mismatch_opens_a_counterfactual_that_one_answer_does_not_close() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    human = human_review.record_challenge_answer(review, dict(review.human), "DC-001", "A", confidence="high")
    assert human_review.mismatched_challenges(review, human) == ["DC-001"]
    assert human_review.open_counterfactuals(review, human) == ["DC-001"]
    # A mismatch stays open until a corrected model is recorded.
    assert human_review.challenges_complete(review, human) is False
    resolved = human_review.record_counterfactual(human, "DC-001", corrected_model="the key is reused on retry")
    assert human_review.open_counterfactuals(review, resolved) == []
    assert human_review.challenges_complete(review, resolved) is True


def test_answering_an_unknown_challenge_is_rejected() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    with pytest.raises(ValueError, match="unknown challenge"):
        human_review.record_challenge_answer(review, dict(review.human), "DC-999", "B", confidence="low")


# --- expertise routing (plan §14.9, E2E-05) -----------------------------------


def _critical_decision_review(human: dict[str, Any] | None = None) -> models.Review:
    return _review(
        machine={
            "decision_cards": [
                {
                    "id": "DC-001",
                    "question": "how does retry behave?",
                    "risk": "critical",
                    "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
                    "requires_domains": ["idempotency"],
                }
            ]
        },
        human=human,
    )


def test_unfamiliar_domain_blocks_without_a_remedy() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "unfamiliar"}]}
    review = _critical_decision_review(human)
    gaps = human_review.expertise_gaps(review, dict(review.human))
    assert gaps == [{"domain": "idempotency", "level": "unfamiliar"}]


def test_undeclared_domain_is_itself_a_gap() -> None:
    review = _critical_decision_review()
    assert human_review.expertise_gaps(review, dict(review.human)) == [{"domain": "idempotency", "level": "undeclared"}]


def test_a_requested_expert_discharges_the_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "unfamiliar"}]}
    review = _critical_decision_review(human)
    remedied = human_review.request_expert(dict(review.human), "idempotency", ["DC-001"], reason="need a domain check")
    assert human_review.expertise_gaps(review, remedied) == []


def test_a_scope_reduction_disposition_on_the_card_discharges_the_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "partial"}]}
    review = _critical_decision_review(human)
    remedied = human_review.record_disposition(dict(review.human), "DC-001", "reduce_scope", note="drop the retry path")
    assert human_review.expertise_gaps(review, remedied) == []


def test_familiar_domain_is_never_a_gap() -> None:
    human = {"status": "in_progress", "expertise": [{"domain": "idempotency", "level": "familiar"}]}
    review = _critical_decision_review(human)
    assert human_review.expertise_gaps(review, dict(review.human)) == []


# --- review budget (plan §14.10, E2E-30) --------------------------------------


def test_too_many_critical_decisions_requires_a_scope_split() -> None:
    cards = [
        {
            "id": f"DC-{i:03d}",
            "question": "q",
            "risk": "critical",
            "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
        }
        for i in range(1, 7)  # six critical cards, limit is five
    ]
    review = _review(machine={"decision_cards": cards})
    blown = human_review.scope_split_required(review, dict(review.human))
    assert blown == ["max_critical_decisions"]


def test_budget_within_limits_is_not_blown() -> None:
    review = _review(machine={"scenarios": [{"id": "SCN-001", "kind": "happy_path", "statement_ids": ["STMT-001"]}]})
    assert human_review.scope_split_required(review, dict(review.human)) == []


def test_a_config_limit_overrides_the_default() -> None:
    statements = [{"id": f"STMT-{i:03d}", "text": "x", "epistemic_status": "machine_inferred"} for i in range(1, 4)]
    review = _review(machine={"statements": statements})
    assert human_review.scope_split_required(review, dict(review.human)) == []
    blown = human_review.scope_split_required(review, dict(review.human), {"max_human_statements": 2})
    assert blown == ["max_human_statements"]


def test_a_diff_too_large_for_one_sitting_blows_its_budget() -> None:
    """A hardcoded actual of 0 for `max_diff_bytes_per_partition` would mean it never fires.

    The reasoning was that partitioning enforced the limit upstream — but the detector partitions on
    a *line* count and never measured bytes, so the one budget denominated in bytes could not be
    exceeded by a change of any size. The whole point of the budget is that a change too large to
    hold in one head splits, so it has to be measurable.
    """
    coverage = [
        {
            "diff_digest": "sha256:" + "d" * 64,
            "analyzed_files": 400,
            "analyzed_bytes": 900_000,  # the default ceiling is 524288
            "truncated": False,
            "coverage_status": "sufficient",
        }
    ]
    review = _review(machine={"coverage": coverage})
    assert human_review.scope_split_required(review, dict(review.human)) == ["max_diff_bytes_per_partition"]
    blocker = next(b for b in human_review.completion_blockers(review, dict(review.human)) if "budget" in b)
    assert "/revise" in blocker and "review_policy.budgets" in blocker
    assert not human_review.can_freeze(review, dict(review.human))


def test_the_budget_is_per_partition_so_the_measure_is_the_max_not_the_sum() -> None:
    """A change split into readable pieces is within budget however many pieces there are."""
    coverage = [
        {
            "diff_digest": "sha256:" + c * 64,
            "analyzed_files": 10,
            "analyzed_bytes": 300_000,
            "truncated": False,
            "coverage_status": "sufficient",
        }
        for c in "de"
    ]
    review = _review(machine={"coverage": coverage})  # 600_000 summed, 300_000 per partition
    assert human_review.budget_actuals(review, dict(review.human))["max_diff_bytes_per_partition"] == 300_000
    assert human_review.scope_split_required(review, dict(review.human)) == []


def test_a_coverage_entry_that_measured_nothing_is_refused_rather_than_read_as_zero() -> None:
    """`analyzed_bytes` was optional and defaulted to 0 here, documented as "within budget" — an
    unmeasured partition passing a size check it was never held to. That is the one direction this
    file must not round towards, so the schema requires it and a document without it does not parse.
    A review written before the measure is regenerated, not tolerated."""
    document = make_review(generated=True)
    del document["machine"]["coverage"][0]["analyzed_bytes"]
    with pytest.raises(models.DocumentError) as caught:
        models.Review.parse(json.dumps(document))
    assert "analyzed_bytes" in str(caught.value)


def test_the_screen_uses_the_ceilings_the_review_was_generated_against() -> None:
    """A configured budget must reach the freeze check, or the human signs a different review.

    `review generate` merges `review_policy.budgets` into the `review_budget` snapshot the receipt
    binds. Recomputing the report against the hardcoded defaults meant a project that deliberately
    raised a limit was still blocked at the default.
    """
    statements = [{"id": f"STMT-{i:03d}", "text": "x", "epistemic_status": "machine_inferred"} for i in range(1, 41)]
    snapshot = [{"name": "max_human_statements", "limit": 60, "actual": 40, "exceeded": False}]
    review = _review(machine={"statements": statements, "review_budget": snapshot})
    assert human_review.recorded_limits(review) == {"max_human_statements": 60}
    assert human_review.scope_split_required(review, dict(review.human)) == []

    # …and without the snapshot the same review is over the default ceiling of 30.
    default_ceilings = _review(machine={"statements": statements})
    assert human_review.scope_split_required(default_ceilings, dict(default_ceilings.human)) == ["max_human_statements"]


# --- staleness / optimistic concurrency (plan §17.5, E2E-08/09) ---------------


def test_a_stale_machine_digest_is_refused() -> None:
    review = _review()
    with pytest.raises(human_review.StaleReview, match="changed since"):
        human_review.assert_machine_current(review, "sha256:" + "0" * 64)


def test_the_current_machine_digest_passes() -> None:
    review = _review()
    human_review.assert_machine_current(review, review.machine_digest())  # no raise


def test_a_human_only_update_does_not_change_the_machine_digest() -> None:
    # E2E-09: answering a challenge changes `human`, never `machine`.
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    before = review.machine_digest()
    human = human_review.record_challenge_answer(review, dict(review.human), "DC-001", "B", confidence="low")
    after = models.Review({"machine": dict(review.machine), "human": human})
    assert after.machine_digest() == before
    assert after.human_digest() != review.human_digest()


# --- completion readiness (plan §21.5) ----------------------------------------


def test_completion_is_blocked_on_an_ungenerated_review() -> None:
    review = models.Review({"machine": {"status": "not_generated"}, "human": {"status": "not_started"}})
    assert human_review.completion_blockers(review)


def test_a_clean_review_can_freeze() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})
    human = human_review.record_challenge_answer(review, dict(review.human), "DC-001", "B", confidence="high")
    # The challenge is the unprimed read; the decision is the substantive judgement — both are
    # required, or a review could freeze having answered a quiz while deciding nothing.
    human = human_review.record_decision(review, human, "DC-001", "A", confidence="high")
    assert human_review.completion_blockers(review, human) == []
    frozen = human_review.freeze(review, human)
    assert frozen["status"] == "frozen"


def test_freeze_refuses_while_a_blocker_stands() -> None:
    review = _review(machine={"decision_cards": [_challenge("DC-001", "B")]})  # unanswered challenge
    with pytest.raises(ValueError, match="cannot freeze"):
        human_review.freeze(review, dict(review.human))


def test_blocking_security_finding_blocks_completion() -> None:
    finding = {
        "id": "SEC-001",
        "severity": "critical",
        "category": "authz_bypass",
        "attack_scenario": "x",
        "blocking": True,
    }
    review = _review(machine={"security": {"findings": [finding]}})
    assert any("security" in b for b in human_review.completion_blockers(review, dict(review.human)))


def test_a_diverged_high_risk_claim_blocks_completion_until_it_is_decided() -> None:
    """A claim the code was not shown to satisfy is a decision card, and an unanswered
    high/critical card blocks the freeze — there is no separate pass/fail verdict to lean on."""
    card = {
        "id": "DC-001",
        "subject_id": "C-001",
        "kind": "claim",
        "risk": "high",
        "question": "C-001 is 'diverged'. What happens to it?",
        "options": ["revise_implementation", "revise_design"],
    }
    review = _review(machine={"decision_cards": [card]})
    assert any("DC-001" in b for b in human_review.completion_blockers(review, dict(review.human)))
