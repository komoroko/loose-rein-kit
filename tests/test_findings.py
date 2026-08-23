"""Which task answers each blocking finding — derived from the record, or reported as unowned.

The rule this file exists to hold: a finding no declared scope covers is **not** guessed at.
Picking the nearest task would be inventing the very answer the module claims to derive.
"""

from __future__ import annotations

from typing import Any

from rein import findings, models
from tests._support import make_claim, make_plan, make_review, make_task


def anchor(path: str) -> dict[str, Any]:
    return {"path": path, "start_line": 1, "end_line": 2, "blob": "git-blob:" + "a" * 40}


def plan_with_scopes(**scopes: list[str] | None) -> models.Plan:
    tasks = []
    for task_id, include in scopes.items():
        task = make_task(task_id.replace("_", "-").upper(), kind="parallel", claim_ids=[f"C-00{task_id[-1]}"])
        if include is not None:
            task["scope"] = {"include": include}
        tasks.append(task)
    claims = [make_claim(f"C-00{task_id[-1]}") for task_id in scopes]
    return models.Plan(make_plan(claims=claims, tasks=tasks))


def review_with(
    *,
    security: list[dict[str, Any]] | None = None,
    extras: list[dict[str, Any]] | None = None,
    statements: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
) -> models.Review:
    document = make_review(generated=True, security_findings=security or [], extra_behaviors=extras or [])
    document["machine"]["actual_extraction"] = statements or []
    document["machine"]["claims"] = claims or []
    return models.Review(document)


# --- path ownership -----------------------------------------------------------


def test_the_longest_declared_scope_wins() -> None:
    plan = plan_with_scopes(t_1=["src/"], t_2=["src/api/"])

    assert findings.owner_of_path(plan, "src/api/routes.py") == "T-2"
    assert findings.owner_of_path(plan, "src/other.py") == "T-1"


def test_a_task_with_no_declared_scope_owns_nothing() -> None:
    """Unbounded is the right reading when checking whether work strayed, and the wrong one here.

    Treating it as "covers everything" would make the first scope-less task in the plan the owner
    of the entire repository.
    """
    plan = plan_with_scopes(t_1=None, t_2=["src/api/"])

    assert findings.owner_of_path(plan, "docs/readme.md") == ""
    assert findings.owner_of_path(plan, "src/api/routes.py") == "T-2"


# --- the three kinds ----------------------------------------------------------


def test_a_security_finding_is_attributed_by_its_own_anchor() -> None:
    plan = plan_with_scopes(t_1=["src/auth/"], t_2=["src/api/"])
    review = review_with(
        security=[
            {
                "id": "SEC-001",
                "severity": "high",
                "category": "authz_bypass",
                "attack_scenario": "anyone can read anyone's record",
                "blocking": True,
                "code_anchors": [anchor("src/auth/check.py")],
            }
        ]
    )

    result = findings.attribute(plan, review)

    assert [(a.finding_id, a.kind, a.task_id) for a in result] == [("SEC-001", "security", "T-1")]
    assert result[0].basis == "src/auth/check.py"


def test_a_non_blocking_security_finding_is_left_alone() -> None:
    plan = plan_with_scopes(t_1=["src/auth/"])
    review = review_with(
        security=[
            {
                "id": "SEC-002",
                "severity": "low",
                "category": "other",
                "attack_scenario": "noted",
                "blocking": False,
                "code_anchors": [anchor("src/auth/check.py")],
            }
        ]
    )

    assert findings.attribute(plan, review) == []


def test_an_extra_behavior_is_attributed_through_the_statement_it_came_from() -> None:
    plan = plan_with_scopes(t_1=["src/auth/"], t_2=["src/api/"])
    review = review_with(
        extras=[
            {
                "id": "EXTRA-001",
                "statement_id": "S-001",
                "category": "new_default",
                "risk": "high",
                "grounded": True,
                "blocking": True,
                "actual_statement_ids": ["A-001"],
            }
        ],
        statements=[
            {
                "id": "A-001",
                "statement": "retries three times by default",
                "category": "default_value",
                "confidence": "high",
                "code_anchors": [anchor("src/api/client.py")],
            }
        ],
    )

    result = findings.attribute(plan, review)

    assert [(a.finding_id, a.task_id) for a in result] == [("EXTRA-001", "T-2")]


def test_a_failing_claim_is_attributed_by_the_plan_not_by_a_path() -> None:
    """A stronger link than any file: the plan says outright which task answers the claim."""
    plan = plan_with_scopes(t_1=["src/auth/"], t_2=["src/api/"])
    review = review_with(
        claims=[
            {
                "claim_id": "C-002",
                "verdict": "diverged",
                "integrity": {"status": "verified"},
                "semantic_support": {"status": "contradicted", "assessment_basis": "machine_assessed"},
                "conformance": {"status": "unknown"},
            }
        ]
    )

    result = findings.attribute(plan, review)

    assert [(a.finding_id, a.kind, a.task_id) for a in result] == [("C-002", "claim", "T-2")]


def test_a_claim_the_review_could_not_tell_about_is_not_a_task_s_problem() -> None:
    """`unverified` and `unknown` say the review could not look — a coverage problem, not a defect."""
    plan = plan_with_scopes(t_1=["src/auth/"])
    review = review_with(
        claims=[
            {
                "claim_id": "C-001",
                "verdict": "unverified",
                "integrity": {"status": "unavailable"},
                "semantic_support": {"status": "unknown", "assessment_basis": "machine_assessed"},
                "conformance": {"status": "unknown"},
            }
        ]
    )

    assert findings.attribute(plan, review) == []


# --- nothing is guessed at ----------------------------------------------------


def test_a_finding_no_scope_covers_is_reported_not_assigned() -> None:
    plan = plan_with_scopes(t_1=["src/auth/"], t_2=["src/api/"])
    review = review_with(
        security=[
            {
                "id": "SEC-003",
                "severity": "high",
                "category": "supply_chain",
                "attack_scenario": "a dependency nobody declared",
                "blocking": True,
                "code_anchors": [anchor("vendor/thing.py")],
            }
        ]
    )

    result = findings.attribute(plan, review)

    assert findings.unowned(result) == result
    assert findings.seeds(result) == []
    assert "no task declares this" in findings.render(result)
    assert "not guessed at" in findings.render(result)


def test_seeds_are_deduplicated_and_ordered() -> None:
    plan = plan_with_scopes(t_1=["src/auth/"], t_2=["src/api/"])
    review = review_with(
        security=[
            {
                "id": f"SEC-00{n}",
                "severity": "high",
                "category": "other",
                "attack_scenario": "x",
                "blocking": True,
                "code_anchors": [anchor(path)],
            }
            for n, path in enumerate(("src/api/a.py", "src/auth/b.py", "src/api/c.py"), start=1)
        ]
    )

    assert findings.seeds(findings.attribute(plan, review)) == ["T-1", "T-2"]


def test_a_review_that_was_never_generated_yields_nothing() -> None:
    plan = plan_with_scopes(t_1=["src/"])

    assert findings.attribute(plan, None) == []
    assert findings.attribute(plan, models.Review(make_review())) == []


def test_render_says_so_when_there_is_nothing_blocking() -> None:
    assert "no blocking findings" in findings.render([])
