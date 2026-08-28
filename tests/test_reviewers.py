"""The reviewer orchestration: blind input contracts and untrusted-output validation (§30.9).

These cover the three LLM reviewers without an LLM — a fake reviewer returns a crafted string,
and the module either validates it or refuses it. The two things worth pinning:

  * the Actual Extractor is handed inputs that provably exclude the Expected Model and the
    implementer's private explanation (priming defense, E2E-19);
  * every reviewer's output is untrusted — a forged anchor, a self-granted verdict, a fabricated
    citation, a rewritten Actual, or a dropped block is refused.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rein import actual_extraction, conformance, review_policy, security_review
from rein import repo as repo_mod


def fake(payload: dict[str, Any]) -> review_policy.Reviewer:
    """A reviewer that ignores its request and answers with a fixed JSON string."""

    def _reviewer(request: Mapping[str, Any]) -> review_policy.Answer:
        return review_policy.Answer(json.dumps(payload))

    return _reviewer


@pytest.fixture
def committed_repo(tmp_path: Path) -> repo_mod.Repo:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("a\nb\nc\nd\n", encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@e.x")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    return repo_mod.Repo(tmp_path)


def _blob(repo: repo_mod.Repo, path: str) -> str:
    return repo._git_rc("rev-parse", f"HEAD:{path}")[1].strip()


# --- actual extractor: blind input (plan §12.2, E2E-19) -----------------------


def test_extractor_request_excludes_the_expected_model() -> None:
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
        diff_text="diff",
        deterministic_facts={"signals": []},
    )
    for forbidden in actual_extraction.FORBIDDEN_KEYS:
        assert forbidden not in request


def test_extractor_refuses_a_primed_request() -> None:
    with pytest.raises(actual_extraction.ExtractionError, match="Expected-Model"):
        actual_extraction.assert_blind({"diff": "x", "deterministic_facts": {"expected_claim": "should retry"}})


@pytest.mark.integration
def test_extractor_output_with_a_valid_anchor_is_accepted(committed_repo: repo_mod.Repo) -> None:
    blob = _blob(committed_repo, "src/app.py")
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "reads the file",
                    "category": "io",
                    "confidence": "medium",
                    "code_anchors": [
                        {"path": "src/app.py", "start_line": 1, "end_line": 2, "blob": f"git-blob:{blob}"}
                    ],
                }
            ],
            "coverage": {"analyzed_files": 1},
        }
    )
    result = actual_extraction.run_extractor(
        actual_extraction.build_request(
            trusted_base_sha="a" * 40,
            subject_head_sha="b" * 40,
            diff_text="d",
            deterministic_facts={},
        ),
        reviewer,
        repo=committed_repo,
        commit="HEAD",
    )
    assert result.actual_statements[0]["id"] == "AST-001"
    assert result.actual_digest.startswith("sha256:")


@pytest.mark.integration
def test_extractor_rejects_a_fabricated_anchor(committed_repo: repo_mod.Repo) -> None:
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "x",
                    "category": "io",
                    "confidence": "high",
                    "code_anchors": [{"path": "src/ghost.py", "start_line": 1, "end_line": 1}],
                }
            ]
        }
    )
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="fabricated or stale"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


@pytest.mark.integration
def test_extractor_rejects_a_self_granted_integrity(committed_repo: repo_mod.Repo) -> None:
    blob = _blob(committed_repo, "src/app.py")
    reviewer = fake(
        {
            "actual_statements": [
                {
                    "id": "AST-001",
                    "statement": "x",
                    "category": "io",
                    "confidence": "high",
                    "integrity": {"status": "verified"},
                    "code_anchors": [
                        {"path": "src/app.py", "start_line": 1, "end_line": 1, "blob": f"git-blob:{blob}"}
                    ],
                }
            ]
        }
    )
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="integrity"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


def test_extractor_rejects_a_statement_without_an_anchor(committed_repo: repo_mod.Repo) -> None:
    reviewer = fake(
        {"actual_statements": [{"id": "AST-001", "statement": "x", "category": "io", "confidence": "high"}]}
    )
    request = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", deterministic_facts={}
    )
    with pytest.raises(actual_extraction.ExtractionError, match="at least one code anchor"):
        actual_extraction.run_extractor(request, reviewer, repo=committed_repo, commit="HEAD")


# --- comparator: never-list and independence (plan §24.3, §12.4, E2E-26) ------

_DISTINCT = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/sonnet"}}


def _comparator_request() -> dict[str, Any]:
    return conformance.build_request(
        expected_model={"claims": []},
        actual_statements=[],
        actual_digest="sha256:" + "1" * 64,
    )


def test_comparator_rejects_a_same_group_critical_review() -> None:
    same = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/opus"}}
    with pytest.raises(conformance.ComparatorError, match="not independent"):
        conformance.run_comparator(
            _comparator_request(),
            fake({"claims": []}),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=[],
            expected_claim_ids=[],
            effective_risk="critical",
            independence=same,
        )


def test_comparator_rejects_a_rewritten_actual() -> None:
    with pytest.raises(conformance.ComparatorError, match="rewrite or add Actual"):
        conformance.run_comparator(
            _comparator_request(),
            fake({"claims": [], "actual_statements": [{"id": "AST-009"}]}),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=[],
            expected_claim_ids=[],
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_rejects_an_invented_actual_reference() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "actual_statement_ids": ["AST-404"]}]}
    with pytest.raises(conformance.ComparatorError, match="never produced"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=["AST-001"],
            known_ids=["C-001"],
            expected_claim_ids=[],
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_comparator_accepts_a_clean_comparison() -> None:
    payload = {"claims": [{"claim_id": "C-001", "verdict": "aligned", "conformance": {"status": "observed"}}]}
    result = conformance.run_comparator(
        _comparator_request(),
        fake(payload),
        repo=repo_mod.Repo(Path("/x")),
        commit="HEAD",
        actual_statement_ids=[],
        known_ids=["C-001", "O-001"],
        expected_claim_ids=[],
        effective_risk="high",
        independence=_DISTINCT,
    )
    assert result.claims[0]["claim_id"] == "C-001"


def _extra(**over: Any) -> dict[str, Any]:
    base = {
        "id": "EXTRA-001",
        "category": "retry_timeout_fallback",
        "risk": "medium",
        "grounded": False,
        "blocking": False,
        "actual_statement_ids": ["AST-001"],
    }
    return {**base, **over}


def test_comparator_carries_the_extra_behaviors_it_found() -> None:
    payload = {"claims": [], "extra_behaviors": [_extra()]}
    result = conformance.run_comparator(
        _comparator_request(),
        fake(payload),
        repo=repo_mod.Repo(Path("/x")),
        commit="HEAD",
        actual_statement_ids=["AST-001"],
        known_ids=[],
        expected_claim_ids=[],
        effective_risk="high",
        independence=_DISTINCT,
    )
    assert [e["id"] for e in result.extra_behaviors] == ["EXTRA-001"]


def test_an_extra_behavior_must_name_the_code_it_was_read_from() -> None:
    """Unanchored, it is the Comparator describing code nobody read.

    No claim accounts for an extra behaviour — that is what makes it extra — so the plan cannot
    check the citation. The Actual is the only thing left that can.
    """
    payload = {"claims": [], "extra_behaviors": [_extra(actual_statement_ids=[])]}
    with pytest.raises(conformance.ComparatorError, match="names no Actual Statement"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=["AST-001"],
            known_ids=[],
            expected_claim_ids=[],
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_an_extra_behavior_cannot_cite_a_statement_the_extractor_never_produced() -> None:
    payload = {"claims": [], "extra_behaviors": [_extra(actual_statement_ids=["AST-404"])]}
    with pytest.raises(conformance.ComparatorError, match="never produced"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=["AST-001"],
            known_ids=[],
            expected_claim_ids=[],
            effective_risk="high",
            independence=_DISTINCT,
        )


def test_an_unclassified_extra_behavior_is_refused_not_defaulted() -> None:
    """There is no honest neutral category, so filing it under an invented one is worse than refusing."""
    payload = {"claims": [], "extra_behaviors": [_extra(category="it_seemed_useful")]}
    with pytest.raises(conformance.ComparatorError, match="not one this release classifies"):
        conformance.run_comparator(
            _comparator_request(),
            fake(payload),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
            actual_statement_ids=["AST-001"],
            known_ids=[],
            expected_claim_ids=[],
            effective_risk="high",
            independence=_DISTINCT,
        )


# --- security reviewer (plan §12.5) -------------------------------------------


def _prior(fid: str, **over: Any) -> dict[str, Any]:
    """A finding carried forward from the last review. Unanchored by default: nothing to re-check,
    so `resolution_of` cannot say it was fixed and the drop stays refused."""
    return {"id": fid, "severity": "high", "category": "credential_exposure", "attack_scenario": "x", **over}


def test_security_review_rejects_an_unknown_severity() -> None:
    payload = {"findings": [{"id": "SEC-001", "severity": "apocalyptic", "attack_scenario": "x", "blocking": True}]}
    with pytest.raises(security_review.SecurityReviewError, match="severity"):
        security_review.run_security_review({}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")


def test_security_review_refuses_to_drop_a_prior_blocking_finding() -> None:
    payload: dict[str, Any] = {"findings": []}
    with pytest.raises(security_review.SecurityReviewError, match="clear its own block"):
        security_review.run_security_review(
            {"prior_blocking": [_prior("SEC-001")]}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD"
        )


def test_security_review_refuses_to_downgrade_a_prior_blocking_finding() -> None:
    """The wider door the id-set check left open.

    "Did the review drop the finding?" was answered by comparing id sets, and every returned
    finding joined that set regardless of its `blocking` value. So re-listing `SEC-1` as
    non-blocking satisfied the check exactly as well as fixing it did — which is the one thing a
    reviewer is not allowed to do to its own block.
    """
    payload = {
        "findings": [{"id": "SEC-001", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": False}]
    }
    with pytest.raises(security_review.SecurityReviewError, match="cannot clear a blocking flag"):
        security_review.run_security_review(
            {"prior_blocking": [_prior("SEC-001")]}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD"
        )


def test_security_review_accepts_a_well_formed_finding() -> None:
    payload = {
        "findings": [{"id": "SEC-001", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": True}]
    }
    result = security_review.run_security_review({}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")
    assert len(result.blocking) == 1


def test_a_finding_records_the_change_it_is_a_statement_about() -> None:
    """A finding with no base is a finding that cannot be told apart from one about other code."""
    payload = {
        "findings": [{"id": "SEC-001", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": True}]
    }
    request = {"trusted_base_sha": "a" * 40, "subject_head_sha": "b" * 40}
    result = security_review.run_security_review(request, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")
    assert result.findings[0]["first_seen"] == {"trusted_base_sha": "a" * 40, "subject_head_sha": "b" * 40}


def test_a_carried_finding_keeps_the_change_it_was_first_found_against() -> None:
    """Stamping every regeneration with the head it ran at made the field say the opposite of its
    name: a blocking finding that survived three regenerations reported the third head as where it
    was first seen, and a human reading the review had no way to tell a standing finding from a
    new one."""
    payload = {
        "findings": [{"id": "SEC-001", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": True}]
    }
    carried = _prior("SEC-001", first_seen={"trusted_base_sha": "a" * 40, "subject_head_sha": "b" * 40})
    request = {"trusted_base_sha": "a" * 40, "subject_head_sha": "c" * 40, "prior_blocking": [carried]}
    result = security_review.run_security_review(request, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")
    assert result.findings[0]["first_seen"] == {"trusted_base_sha": "a" * 40, "subject_head_sha": "b" * 40}


def test_a_finding_the_reviewer_derived_afresh_is_first_seen_now() -> None:
    """There is continuity only where the pipeline carries a finding forward. A finding nobody
    carried has none, and this change is the honest answer."""
    payload = {
        "findings": [{"id": "SEC-002", "severity": "high", "attack_scenario": "reaches a host cred", "blocking": True}]
    }
    carried = _prior("SEC-001", first_seen={"trusted_base_sha": "a" * 40, "subject_head_sha": "b" * 40}, blocking=True)
    request = {"trusted_base_sha": "a" * 40, "subject_head_sha": "c" * 40, "prior_blocking": [carried]}
    with pytest.raises(security_review.SecurityReviewError, match="dropped previously blocking"):
        security_review.run_security_review(request, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")

    request = {"trusted_base_sha": "a" * 40, "subject_head_sha": "c" * 40}
    result = security_review.run_security_review(request, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")
    assert result.findings[0]["first_seen"] == {"trusted_base_sha": "a" * 40, "subject_head_sha": "c" * 40}


# --- the answer is open-world; the claim list is not (D1) ---------------------


def test_a_claim_the_comparator_never_answered_is_recorded_as_unknown() -> None:
    """An LLM's output says what it chose to speak about, never that it spoke about everything.

    Read as a closed ledger — which is how this used to be read — a Comparator returning one of
    three claims produced a review saying `claims_total: 1, aligned: 1`, with no verdict, no
    decision card and no gate block for the other two. The one check that existed ran the other
    way: every id returned had to exist in the plan, so a fabricated claim was caught and a missing
    one was invisible.
    """
    payload = {"claims": [{"claim_id": "C-002", "verdict": "aligned", "conformance": {"status": "observed"}}]}
    result = conformance.run_comparator(
        _comparator_request(),
        fake(payload),
        repo=repo_mod.Repo(Path("/x")),
        commit="HEAD",
        actual_statement_ids=[],
        known_ids=["C-001", "C-002", "C-003"],
        expected_claim_ids=["C-001", "C-002", "C-003"],
        effective_risk="high",
        independence=_DISTINCT,
    )
    assert [c["claim_id"] for c in result.claims] == ["C-001", "C-002", "C-003"]
    assert result.unanswered == ("C-001", "C-003")
    silent = result.claims[0]
    assert silent["verdict"] == "unknown"
    assert silent["integrity"] == {"status": "unavailable"}
    assert silent["semantic_support"]["status"] == "unknown"
    assert result.claims[1]["verdict"] == "aligned"  # what the Comparator did say is untouched


def test_an_unanswered_claim_becomes_a_decision_a_human_has_to_make() -> None:
    """The completion is only worth anything if it reaches the screen. `unknown` is in
    `decision_cards._UNSETTLED_VERDICTS`, so filling the gap with it puts the claim in front of a
    human rather than shortening the review by one row."""
    from rein import decision_cards

    result = conformance.run_comparator(
        _comparator_request(),
        fake({"claims": []}),
        repo=repo_mod.Repo(Path("/x")),
        commit="HEAD",
        actual_statement_ids=[],
        known_ids=["C-001"],
        expected_claim_ids=["C-001"],
        effective_risk="high",
        independence=_DISTINCT,
    )
    statements, cards = decision_cards.derive_cards(claims=result.claims, plan_risk={"C-001": "high"})
    assert len(cards) == 1 and cards[0]["risk"] == "high"
    assert {s["applicability"]["subject_id"] for s in statements} == {"C-001"}


# --- ids are the reviewer's, and they are checked (D6) ------------------------


def test_a_claim_answered_twice_is_refused_not_de_duplicated(committed_repo: repo_mod.Repo) -> None:
    """Two verdicts for one claim is a contradictory answer, not a partial one. Keeping the first
    silently discarded the other — and `diverged` beside `aligned` is exactly the row that raises
    a decision card, so the quieter half was what opened the gate."""

    def row(verdict: str) -> dict[str, Any]:
        return {
            "claim_id": "C-001",
            "verdict": verdict,
            "integrity": {"status": "unavailable"},
            "semantic_support": {"status": "unknown", "assessment_basis": "machine_assessed"},
            "conformance": {"status": "unknown"},
        }

    with pytest.raises(conformance.ComparatorError, match="used more than once"):
        conformance.run_comparator(
            {"actual_digest": "d"},
            fake({"claims": [row("aligned"), row("diverged")], "actual_digest": "d"}),
            repo=committed_repo,
            commit="HEAD",
            actual_statement_ids=[],
            known_ids=["C-001"],
            expected_claim_ids=["C-001"],
            effective_risk="low",
            independence={},
        )


def test_the_extractor_may_not_mint_the_same_statement_id_twice() -> None:
    """Everything downstream resolves references by these ids: the comparator is validated against
    a `set` of them, and `findings` indexes statements by id to reach their anchors. A duplicate
    collapses in the first and takes the last writer in the second, so an extra behaviour ends up
    anchored — and attributed to a task — by a statement nobody cited."""
    twice = {"id": "AST-001", "statement": "x", "category": "io", "confidence": "high"}
    with pytest.raises(actual_extraction.ExtractionError, match="more than once"):
        actual_extraction.run_extractor(
            {"contract": "c"},
            fake({"actual_statements": [dict(twice), dict(twice)]}),
            repo=repo_mod.Repo(Path("/x")),
            commit="HEAD",
        )


def test_the_extractor_may_not_mint_an_id_of_another_shape() -> None:
    payload = {"actual_statements": [{"id": "statement one", "statement": "x", "category": "io", "confidence": "high"}]}
    with pytest.raises(actual_extraction.ExtractionError, match="not an Actual Statement id"):
        actual_extraction.run_extractor({"contract": "c"}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")


def test_a_security_finding_id_of_another_shape_is_refused() -> None:
    payload = {"findings": [{"id": "SEC-1", "severity": "high", "attack_scenario": "x", "blocking": True}]}
    with pytest.raises(security_review.SecurityReviewError, match="not a security finding id"):
        security_review.run_security_review({}, fake(payload), repo=repo_mod.Repo(Path("/x")), commit="HEAD")


# --- a security finding's life (D4) -------------------------------------------


def test_a_finding_whose_anchored_code_is_gone_may_be_dropped(committed_repo: repo_mod.Repo) -> None:
    """The case that had no exit. A blocking finding was carried forward while the trusted base
    held — which, on a work branch, is the whole cycle — and dropping it was refused whether or not
    the change had fixed it. Fixing the code therefore made the review unproducible, so one
    blocking finding shut gate ④ permanently."""
    head = committed_repo._git("rev-parse", "HEAD").strip()
    blob = committed_repo._git("rev-parse", f"{head}:src/app.py").strip()
    prior = _prior(
        "SEC-001",
        blocking=True,
        code_anchors=[{"path": "src/app.py", "blob": f"git-blob:{blob}", "start_line": 2, "end_line": 3}],
    )

    # Still there, verbatim: the drop is the reviewer clearing its own block.
    with pytest.raises(security_review.SecurityReviewError, match="still in the tree"):
        security_review.run_security_review(
            {"prior_blocking": [prior]}, fake({"findings": []}), repo=committed_repo, commit=head
        )

    # The anchored lines are removed and committed. The same drop is now a resolution.
    (committed_repo.root / "src" / "app.py").write_text("a\nd\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(committed_repo.root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(committed_repo.root), "commit", "-qm", "fix"], check=True, capture_output=True)
    fixed = committed_repo._git("rev-parse", "HEAD").strip()

    result = security_review.run_security_review(
        {"prior_blocking": [prior]}, fake({"findings": []}), repo=committed_repo, commit=fixed
    )
    assert [f["id"] for f in result.findings] == ["SEC-001"]  # kept, never deleted
    assert result.findings[0]["status"] == "resolved"
    assert result.findings[0]["resolved_at"] == {"subject_head_sha": fixed}
    # And named separately, because the document is not where the resolution survives: the next
    # generation re-derives its findings from a reviewer with no memory of this one.
    assert [f["id"] for f in result.resolved] == ["SEC-001"]
    assert result.blocking == ()


def test_an_unrelated_edit_to_the_same_file_does_not_resolve_a_finding(committed_repo: repo_mod.Repo) -> None:
    """The tempting check — "does the anchor still validate?" — fails the moment the file's blob
    differs, so any edit anywhere in the file would read as a fix. The anchored *text* is what the
    finding was about."""
    head = committed_repo._git("rev-parse", "HEAD").strip()
    blob = committed_repo._git("rev-parse", f"{head}:src/app.py").strip()
    prior = _prior(
        "SEC-001",
        blocking=True,
        code_anchors=[{"path": "src/app.py", "blob": f"git-blob:{blob}", "start_line": 2, "end_line": 3}],
    )
    (committed_repo.root / "src" / "app.py").write_text("a\nb\nc\nd\ne\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(committed_repo.root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(committed_repo.root), "commit", "-qm", "unrelated"], check=True, capture_output=True
    )
    moved = committed_repo._git("rev-parse", "HEAD").strip()
    with pytest.raises(security_review.SecurityReviewError, match="still in the tree"):
        security_review.run_security_review(
            {"prior_blocking": [prior]}, fake({"findings": []}), repo=committed_repo, commit=moved
        )


def test_an_unanchored_finding_cannot_close_itself(committed_repo: repo_mod.Repo) -> None:
    """Nothing to re-check means this cannot say, and "cannot say" is not "resolved". Such a
    finding is closed by a human's `dispute_finding` or not at all."""
    head = committed_repo._git("rev-parse", "HEAD").strip()
    with pytest.raises(security_review.SecurityReviewError, match="still in the tree"):
        security_review.run_security_review(
            {"prior_blocking": [_prior("SEC-001", blocking=True)]},
            fake({"findings": []}),
            repo=committed_repo,
            commit=head,
        )
