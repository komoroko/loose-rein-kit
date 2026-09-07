"""Which of gate ④'s findings the loop repairs, and which reach a human.

Inside a task, judging and repairing were both automated. At gate ④ only judging was, and the one
route a machine-found *code* defect had back into the code was `rein revise --to build
--from-review` — which marked the task and its whole dependent closure `needs-revision`, a status
about the *plan*, and so demanded a `/tasks` reconcile and a re-approval of gate ③ for a repair
that changed no requirement, no claim and no plan.

These pin the routing that replaced it: a finding goes where it goes because of what repairing it
would change, never because of who found it.
"""

from __future__ import annotations

from typing import Any

from rein import models, repair
from tests._support import make_claim, make_plan, make_review, make_task


def _plan_with_scopes() -> models.Plan:
    api = make_task("T-001", claim_ids=["C-001"])
    api["scope"] = {"include": ["src/api/"]}
    ui = make_task("T-002", claim_ids=["C-002"])
    ui["scope"] = {"include": ["src/ui/"]}
    return models.Plan(make_plan(claims=[make_claim("C-001"), make_claim("C-002")], tasks=[api, ui]))


def _finding(fid: str, path: str) -> dict[str, Any]:
    return {
        "id": fid,
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "a caller reaches a host credential",
        "blocking": True,
        "code_anchors": [{"path": path, "blob": "git-blob:" + "a" * 40, "start_line": 1, "end_line": 2}],
    }


def _review(*, claims: list[dict[str, Any]] | None = None, **over: Any) -> models.Review:
    document = make_review(generated=True, **over)
    if claims is not None:
        document["machine"]["claims"] = claims
    return models.Review(document)


def test_a_security_finding_a_task_scope_owns_is_the_loops_to_repair() -> None:
    """Nothing about it is a question. The reviewer read the code and named the lines; the plan
    says whose they are; and the repair changes no claim, no criterion and no requirement."""
    routing = repair.route(
        _plan_with_scopes(),
        _review(security_findings=[_finding("SEC-001", "src/api/client.py")]),
    )
    assert [r.task_id for r in routing.code] == ["T-001"]
    assert routing.judgement == () and routing.unowned == ()
    assert routing.repairable


def test_findings_are_grouped_by_task_in_plan_order() -> None:
    """One launch answers everything about one scope — an implementer asked the same question
    twice about the same files is two launches for one reading."""
    routing = repair.route(
        _plan_with_scopes(),
        _review(
            security_findings=[
                _finding("SEC-002", "src/ui/page.tsx"),
                _finding("SEC-001", "src/api/client.py"),
                _finding("SEC-003", "src/api/auth.py"),
            ]
        ),
    )
    assert [r.task_id for r in routing.code] == ["T-001", "T-002"]
    assert [a.finding_id for a in routing.code[0].items] == ["SEC-001", "SEC-003"]


def test_a_finding_no_declared_scope_owns_is_never_guessed_at() -> None:
    """ "No task covers this path" means the plan does not say, and picking the nearest task would
    be inventing the answer the attribution exists to derive."""
    routing = repair.route(
        _plan_with_scopes(),
        _review(security_findings=[_finding("SEC-001", "vendor/thing.py")]),
    )
    assert routing.code == ()
    assert [a.finding_id for a in routing.unowned] == ["SEC-001"]


def test_a_diverged_claim_is_a_judgement_until_a_human_makes_it_a_repair() -> None:
    """The one thing the review cannot decide for itself: a claim the code did not answer means
    either the code is wrong or the plan is, and nothing mechanical separates them.

    `decision_cards.OPTION_DISPOSITION` has always named the repair each option means, and nothing
    read it — every card was answered and no answer did anything. `revise_implementation` is a
    human saying the code is the mistaken half, which hands the subject back to the loop.
    """
    review = _review(claims=[{"claim_id": "C-001", "verdict": "diverged", "risk": "high"}])
    routing = repair.route(_plan_with_scopes(), review)
    assert routing.code == ()
    assert [a.finding_id for a in routing.judgement] == ["C-001"]

    answered = repair.route(
        _plan_with_scopes(),
        review,
        human={"dispositions": [{"subject_id": "C-001", "action": "revise_implementation"}]},
    )
    assert [r.task_id for r in answered.code] == ["T-001"]
    assert answered.judgement == ()


def test_any_other_card_answer_keeps_the_subject_with_the_human() -> None:
    """`revise_design`, `reduce_scope` and a dispute all say something other than "the code is
    wrong", and none of them is this loop's to act on."""
    review = _review(claims=[{"claim_id": "C-001", "verdict": "missing", "risk": "high"}])
    for action in ("revise_design", "revise_requirement", "reduce_scope", "dispute_finding"):
        routing = repair.route(
            _plan_with_scopes(),
            review,
            human={"dispositions": [{"subject_id": "C-001", "action": action}]},
        )
        assert routing.code == (), action
        assert [a.finding_id for a in routing.judgement] == ["C-001"], action


def test_an_ungenerated_review_routes_nothing() -> None:
    """ "It did not say" must never read as "it found nothing"."""
    assert repair.route(_plan_with_scopes(), None) == repair.Routing()
    assert repair.route(_plan_with_scopes(), models.Review(make_review())) == repair.Routing()
