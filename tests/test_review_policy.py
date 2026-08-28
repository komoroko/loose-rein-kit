"""The Policy Engine is the boundary that refuses untrusted reviewer output (plan §12.7, §30.9).

Everything here is pure or read-only over a committed tree, so each refusal is tested against a
crafted-malicious payload without running a model: a forged anchor, a self-granted `verified`, a
risk downgrade, a same-group "independent" critical review (E2E-26).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import diff_facts, models, review_policy
from rein import repo as repo_mod

# --- effective risk (plan §13.5) ----------------------------------------------


def test_effective_risk_is_the_max_contributor() -> None:
    inputs = review_policy.RiskInputs(claim_risk="low", security_boundary_risk="high", detector_risk_floor="medium")
    assert review_policy.effective_risk(inputs) == "high"


def test_risk_inputs_from_facts_floors_on_a_deleted_guard() -> None:
    diff = "diff --git a/s.py b/s.py\n--- a/s.py\n+++ b/s.py\n@@ -1 +1 @@\n-    if x: raise E\n"
    facts = diff_facts.analyze(diff)
    inputs = review_policy.risk_inputs_from_facts(facts, claim_risk="low")
    assert review_policy.effective_risk(inputs) == "high"  # an AI-declared "low" cannot survive this


# --- what an unread file is worth (plan §13.4) --------------------------------


def _one_file(path: str, *, added: list[str] | None = None, removed: list[str] | None = None) -> str:
    body = "".join(f"+{line}\n" for line in added or []) + "".join(f"-{line}\n" for line in removed or [])
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,2 @@\n x\n{body}"


def test_an_unreadable_file_with_nothing_risk_bearing_in_it_is_a_low_gap() -> None:
    """The gap is real and recorded; it is just not worth `high` on its own.

    Pricing every gap at `high` closed a loop — the gap raised the risk, and the risk was what
    made the gap blocking — so one unreadable file shut gate ④ with no way through.
    """
    facts = diff_facts.analyze(_one_file("design/logo.psd", added=["some text"]))
    assert facts.coverage.coverage_status == "insufficient"  # still honest about not reading it
    assert review_policy.coverage_gap_risk(facts) == "low"
    assert review_policy.effective_risk(review_policy.risk_inputs_from_facts(facts)) == "low"


def test_a_signal_inside_an_unreadable_file_still_prices_the_gap_high() -> None:
    facts = diff_facts.analyze(_one_file("design/logo.psd", removed=["if not authorized: raise Denied()"]))
    assert review_policy.coverage_gap_risk(facts) == "high"


def test_a_binary_is_a_high_gap_because_nothing_was_read() -> None:
    facts = diff_facts.analyze("diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n")
    assert review_policy.coverage_gap_risk(facts) == "high"


def test_a_dependency_change_prices_the_gap_medium() -> None:
    """No lexical scan says what the new versions do — but that is a `medium` unknown, not a wall."""
    facts = diff_facts.analyze(_one_file("uv.lock", added=['name = "requests"']))
    assert review_policy.coverage_gap_risk(facts) == "medium"


# --- shape caps (plan §12.7) --------------------------------------------------


def test_oversize_output_is_refused() -> None:
    payload = {"blob": "x" * (review_policy.MAX_OUTPUT_BYTES + 1)}
    assert any("exceeds" in p for p in review_policy.validate_shape(payload))


def test_too_deep_output_is_refused() -> None:
    node: dict[str, Any] = {}
    cur = node
    for _ in range(review_policy.MAX_DEPTH + 3):
        cur["n"] = {}
        cur = cur["n"]
    assert any("depth" in p for p in review_policy.validate_shape(node))


def test_parse_reviewer_output_rejects_non_json() -> None:
    with pytest.raises(review_policy.ReviewPolicyError, match="unparseable"):
        review_policy.parse_reviewer_output("not json at all")


def test_parse_reviewer_output_rejects_duplicate_keys() -> None:
    with pytest.raises(review_policy.ReviewPolicyError):
        review_policy.parse_reviewer_output('{"a": 1, "a": 2}')


# --- citations (plan §12.7) ---------------------------------------------------


def test_unknown_citation_is_rejected() -> None:
    problems = review_policy.validate_citations(["C-001", "SRC-999"], known=["C-001", "SRC-001"])
    assert len(problems) == 1
    assert "SRC-999" in problems[0]


def test_all_known_citations_pass() -> None:
    assert review_policy.validate_citations(["C-001"], known=["C-001", "SRC-001"]) == []


# --- self-attestation and downgrades (plan §24.2, §13.5) ----------------------


def test_reviewer_cannot_self_report_integrity_verified() -> None:
    claim = {"claim_id": "C-001", "integrity": {"status": "verified"}}
    assert any("cannot self-report" in p for p in review_policy.reject_self_attestation(claim))


def test_integrity_unknown_is_allowed() -> None:
    claim = {"claim_id": "C-001", "integrity": {"status": "unknown"}}
    assert review_policy.reject_self_attestation(claim) == []


def test_risk_downgrade_below_floor_is_rejected() -> None:
    assert review_policy.reject_risk_downgrade("low", "high", subject="C-001")
    assert review_policy.reject_risk_downgrade("critical", "high") == []


def test_reviewer_cannot_clear_a_policy_blocking_flag() -> None:
    assert review_policy.reject_blocking_removal("SEC-001", reviewer_blocking=False, policy_blocking=True)
    assert review_policy.reject_blocking_removal("SEC-001", reviewer_blocking=True, policy_blocking=True) == []


def _review(machine: dict[str, Any]) -> models.Review:
    return models.Review({"machine": {"status": "generated", **machine}, "human": {"status": "not_started"}})


# --- independence (plan §12.4, E2E-26) ----------------------------------------


def test_critical_review_rejects_same_independence_group() -> None:
    independence = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/opus"}}
    ok, message = review_policy.independence_ok(independence, "critical")
    assert not ok
    assert "not independent" in message


def test_critical_review_accepts_distinct_groups() -> None:
    independence = {"actual_extractor": {"group": "claude/opus"}, "comparator": {"group": "claude/sonnet"}}
    ok, _ = review_policy.independence_ok(independence, "critical")
    assert ok


def test_a_critical_review_with_no_model_named_cannot_show_independence() -> None:
    """Two roles that name no model both take the CLI's default, which is one launch twice. The
    group is derived from the model, so an empty group is exactly that case."""
    independence: dict[str, dict[str, str]] = {"actual_extractor": {}, "comparator": {}}
    ok, message = review_policy.independence_ok(independence, "critical")
    assert not ok
    assert "the CLI's default" in message


def test_the_gate_checks_what_answered_not_only_what_was_configured() -> None:
    """The pre-launch check reads the configuration; this reads the receipt. A provider serving a
    different model than it was told to would leave the config claiming two opinions and one model
    having given both, and only the launch's own report can say so."""
    review = _review(
        machine={
            "binding": {
                "independence": {
                    "actual_extractor": {"group": "claude/opus", "model": "claude-sonnet-5"},
                    "comparator": {"group": "claude/sonnet", "model": "claude-sonnet-5"},
                }
            }
        }
    )
    blocks = review_policy.independence_observed(review, "critical")
    assert blocks and "answered by 'claude-sonnet-5'" in blocks[0]
    assert blocks[0] in review_policy.blocking_reasons(review, "critical")


def test_an_unreported_model_is_not_read_as_agreement() -> None:
    """An adapter that reports no usage cannot be held to a measurement it never took — and the
    declared check has already refused a critical pair that could not differ."""
    review = _review(
        machine={
            "binding": {
                "independence": {
                    "actual_extractor": {"group": "claude/opus"},
                    "comparator": {"group": "claude/sonnet"},
                }
            }
        }
    )
    assert review_policy.independence_observed(review, "critical") == []
    assert review_policy.independence_observed(review, "high") == []


def test_non_critical_review_does_not_require_independence() -> None:
    ok, _ = review_policy.independence_ok({}, "high")
    assert ok


# --- code anchors (plan §12.7) ------------------------------------------------


@pytest.mark.integration
def test_anchor_validation_against_a_committed_blob(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("line1\nline2\nline3\n", encoding="utf-8")
    git("init", "-q")
    git("config", "user.email", "t@e.x")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-q", "-m", "c")
    repo = repo_mod.Repo(tmp_path)
    blob = repo._git_rc("rev-parse", "HEAD:src/app.py")[1].strip()

    # A real anchor within range validates.
    good = {"path": "src/app.py", "start_line": 1, "end_line": 2, "blob": f"git-blob:{blob}"}
    assert review_policy.validate_anchor(repo, "HEAD", good) == []

    # A fabricated path is rejected.
    assert review_policy.validate_anchor(repo, "HEAD", {"path": "src/nope.py", "start_line": 1, "end_line": 1})

    # A line range past the end of the file is rejected.
    over = {"path": "src/app.py", "start_line": 1, "end_line": 99}
    assert any("outside the file" in p for p in review_policy.validate_anchor(repo, "HEAD", over))

    # A stale blob (right path, wrong content hash) is rejected.
    stale = {"path": "src/app.py", "start_line": 1, "end_line": 1, "blob": "git-blob:" + "0" * 40}
    assert any("stale or forged" in p for p in review_policy.validate_anchor(repo, "HEAD", stale))


def test_anchor_rejects_an_unsafe_path() -> None:
    # No git needed: the path check fails before any git call.
    repo = repo_mod.Repo(Path("/nonexistent"))
    assert review_policy.validate_anchor(repo, "HEAD", {"path": "../etc/passwd", "start_line": 1, "end_line": 1})


def test_roundtrip_json_is_parseable() -> None:
    # Sanity: a well-formed reviewer document round-trips through the strict parser.
    document = review_policy.parse_reviewer_output(json.dumps({"findings": []}))
    assert document == {"findings": []}
