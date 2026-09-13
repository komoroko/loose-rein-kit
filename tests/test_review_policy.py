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

from rein import decision_cards, diff_facts, models, review_policy
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
    facts = diff_facts.analyze(_one_file("design/logo.psd", added=["\x00some bytes"]))
    assert facts.coverage.coverage_status == "insufficient"  # still honest about not reading it
    assert review_policy.coverage_gap_risk(facts) == "low"
    assert review_policy.effective_risk(review_policy.risk_inputs_from_facts(facts)) == "low"


def test_a_signal_inside_an_unreadable_file_still_prices_the_gap_high() -> None:
    facts = diff_facts.analyze(_one_file("design/logo.psd", removed=["\x00 if not authorized: raise Denied()"]))
    assert review_policy.coverage_gap_risk(facts) == "high"


def test_a_binary_is_a_high_gap_because_nothing_was_read() -> None:
    facts = diff_facts.analyze("diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n")
    assert review_policy.coverage_gap_risk(facts) == "high"


def test_a_removed_binary_prices_no_gap_at_all() -> None:
    """`high` was the price of *unread bytes*; a deletion leaves none, so there is nothing to price."""
    diff = "diff --git a/logo.png b/logo.png\ndeleted file mode 100644\nBinary files a/logo.png and /dev/null differ\n"
    facts = diff_facts.analyze(diff)
    assert review_policy.coverage_gap_risk(facts) == "low"
    assert review_policy.effective_risk(review_policy.risk_inputs_from_facts(facts)) == "low"


def test_a_dependency_change_is_medium_by_the_detector_and_not_by_the_gap() -> None:
    """The `medium` a dependency change is worth reaches `effective_risk` once, not twice.

    `detect_signals` raises a `dependency` hit on every changed manifest or lockfile, and that is
    `detector_risk_floor`. Pricing the same fact a second time as a coverage gap made the manifest
    `insufficient` for a change every byte of which had been read, with no remedy at gate ④.
    """
    facts = diff_facts.analyze(_one_file("uv.lock", added=['name = "requests"']))
    assert facts.coverage.coverage_status == "sufficient"
    assert review_policy.coverage_gap_risk(facts) == "low"
    assert facts.risk_floor == "medium"
    assert review_policy.effective_risk(review_policy.risk_inputs_from_facts(facts)) == "medium"


def _review_over(diff_text: str) -> models.Review:
    """A generated review carrying the manifest `diff_facts` built for this diff, and nothing else."""
    manifest = diff_facts.analyze(diff_text).coverage.to_manifest()
    return models.Review({"machine": {"status": "generated", "coverage": manifest}, "human": {"status": "not_started"}})


def test_a_cycle_that_adds_a_dependency_is_not_blocked_by_its_own_lockfile() -> None:
    """The shape that had no way through gate ④: code, the manifest that declares its new
    dependency, and the lockfile that is the product of declaring it.

    Neither remedy the block named existed. There is no scope that holds the code and not the
    lock; a slice holding only the lock was `insufficient` by itself; and the risk it was measured
    against was frozen by a human at gate ③, so lowering it is a false statement about the change
    rather than a repair.
    """
    diff = (
        _one_file("src/client.py", added=["    resp = requests.get(url, timeout=30)"])
        + _one_file("pyproject.toml", added=['  "requests>=2.32",'])
        + _one_file("uv.lock", added=['name = "requests"', 'version = "2.32.3"'])
    )
    assert review_policy.coverage_blocks(_review_over(diff), "high") == []
    assert review_policy.coverage_blocks(_review_over(diff), "critical") == []


def test_a_coverage_block_names_the_files_it_is_about() -> None:
    """The remedy it offers is about files, so it says which. "Split the unreadable part out of
    this scope" over a manifest that would not name the part is an instruction nobody can follow."""
    diff = _one_file("src/client.py", added=["    call()"]) + _one_file("design/logo.psd", added=["\x00bytes"])
    blocked = review_policy.coverage_blocks(_review_over(diff), "high")
    assert blocked and "design/logo.psd" in blocked[0]
    assert "src/client.py" not in blocked[0]


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


def test_a_refused_answer_carries_the_bytes_that_were_refused() -> None:
    """`Expecting value: line 1 column 1 (char 0)` is the same message for an empty answer, a
    fenced one, a prose preamble, and a refusal — four different repairs.

    A field run paid for 1,929 output tokens of security review and threw every byte away under
    that sentence, leaving the failure undiagnosable.
    """
    for raw, expected in [
        ("", "''"),
        ("   ", "'   '"),
        ("here you go:\n```json\n{}\n```", "here you go"),
        ("I'm sorry, I can't help with that.", "I'm sorry"),
    ]:
        with pytest.raises(review_policy.ReviewPolicyError) as caught:
            review_policy.parse_reviewer_output(raw)
        assert expected in str(caught.value), (raw, str(caught.value))


def test_a_long_answer_is_excerpted_and_says_so() -> None:
    """The excerpt travels into a console line and an `events.ndjson` `detail.reason`; neither is
    the place for a model's whole answer."""
    with pytest.raises(review_policy.ReviewPolicyError) as caught:
        review_policy.parse_reviewer_output("x" * 5000)
    message = str(caught.value)
    assert f"first {review_policy.UNPARSEABLE_EXCERPT_CHARS} of 5000 chars" in message
    assert len(message) < 5000


def test_one_enclosing_fence_is_a_frame_and_not_leniency() -> None:
    """The bytes between the fences are the whole answer, and they are parsed strictly.

    Refusing this cost real launches, repeatedly, on answers that were correct JSON wearing the
    wrapper a chat interface puts on every code block — and told the operator only "unparseable".
    Removing an unambiguous frame is not crediting a reviewer with having said something; the
    tests below are what that would look like, and they still refuse.
    """
    assert review_policy.parse_reviewer_output('```json\n{"findings": []}\n```') == {"findings": []}
    assert review_policy.parse_reviewer_output('```\n{"findings": []}\n```') == {"findings": []}


@pytest.mark.parametrize(
    "raw",
    [
        'here you go:\n```json\n{"findings": []}\n```',  # a preamble is not a frame
        '```json\n{"findings": []}\n```\nhope that helps',  # nor is a trailing remark
        '```json\n{"a": 1}\n```\n```json\n{"b": 2}\n```',  # two answers are not one
        '```json\n{"findings": [\n```',  # a frame around nothing parseable
    ],
)
def test_leniency_is_still_not_the_repair(raw: str) -> None:
    """What the strictness is actually for: a reviewer that cannot speak the contract has said
    nothing, and crediting it with something is how that stops being true."""
    with pytest.raises(review_policy.ReviewPolicyError):
        review_policy.parse_reviewer_output(raw)


def test_a_stage_schema_is_derived_from_the_one_that_refuses_the_answer() -> None:
    """A second description of the shape, written to constrain the model, would be a second thing
    to keep in step with the validator — which is the failure mode this whole file is about."""
    schema = review_policy.stage_output_schema("security_reviewer")
    declared = models.schema("review")["$defs"]["machine"]["properties"]["security"]
    assert schema["required"] == ["findings"]
    asked, holds = schema["properties"]["findings"]["items"], declared["properties"]["findings"]["items"]
    assert asked["properties"] == holds["properties"]
    # The one field the document requires and the reviewer is not asked for: `blocking` is written
    # by `review_policy.blocks` from the severity, so a CLI constrained by this schema must not be
    # made to invent it — that is the contract field this release removed, arriving by the back door.
    assert "blocking" in holds["required"]
    assert asked["required"] == [name for name in holds["required"] if name != "blocking"]
    assert "machine" not in schema["$defs"], "28 KB of the 35, and nothing a stage answers refs it"
    assert review_policy.stage_output_schema("code_reviewer") == {}, "a role with no declared shape"


def test_deriving_a_stage_schema_does_not_edit_the_one_on_disk() -> None:
    """The strip above is a copy. Mutating the loaded schema would make the *document* validator
    stop requiring `blocking` too, for every caller in the process."""
    review_policy.stage_output_schema("security_reviewer")
    declared = models.schema("review")["$defs"]["machine"]["properties"]["security"]
    assert "blocking" in declared["properties"]["findings"]["items"]["required"]


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


# --- integrity and downgrades (plan §24.2, §13.5) -----------------------------


def test_risk_downgrade_below_floor_is_rejected() -> None:
    assert review_policy.reject_risk_downgrade("low", "high", subject="C-001")
    assert review_policy.reject_risk_downgrade("critical", "high") == []


def test_blocking_is_priced_by_the_policy_and_not_by_a_reviewer() -> None:
    """The `blocking` flag used to be a contract field, so the author of a finding also set what it
    cost. It is a function of the severity now, and the severity is all a reviewer states."""
    assert review_policy.blocks("critical")
    assert review_policy.blocks("high")
    assert not review_policy.blocks("medium")
    assert not review_policy.blocks("low")


def test_a_grounded_extra_behaviour_never_blocks() -> None:
    """Behaviour a requirement already accounts for is not a finding about the change, at any risk."""
    assert not review_policy.blocks("critical", grounded=True)


def test_an_unrecognised_risk_blocks() -> None:
    """A policy engine's default is the closed one. The stage validator refuses the answer that
    produced it anyway, so what this settles is only which way the unreachable case falls."""
    assert review_policy.blocks("")
    assert review_policy.blocks("catastrophic")


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


# --- a security finding's life, seen from the gate (D4) -----------------------

_A_FINDING = {
    "id": "SEC-001",
    "severity": "high",
    "category": "credential_exposure",
    "attack_scenario": "the reviewer container reaches a host credential",
    "blocking": True,
}


def test_a_resolved_finding_no_longer_holds_the_gate_shut() -> None:
    """`resolved` is not the reviewer's word for it: `security_review.resolution_of` records it only
    when the code the finding anchored to is gone from the tree. The finding stays in the document —
    that is the record of what closed it — and stops being a blocker."""
    review = _review(machine={"security": {"findings": [{**_A_FINDING, "status": "resolved"}]}})
    assert review.blocking_security_findings == ()
    assert review.security_findings, "a resolved finding is kept, never deleted"
    assert not [r for r in review_policy.blocking_reasons(review, "low") if "SEC-001" in r]


def test_an_open_finding_still_holds_the_gate_shut() -> None:
    review = _review(machine={"security": {"findings": [_A_FINDING]}})
    assert [r for r in review_policy.blocking_reasons(review, "low") if "SEC-001" in r]


def test_a_human_may_dispute_a_finding_the_change_did_not_touch() -> None:
    """The only way out for a finding with no anchors — nothing for `resolution_of` to re-check, so
    it cannot say it was fixed. `dispute_finding` was already in the schema's disposition list and
    already offered on every decision card; it simply had no effect on the gate. It is not "accept
    the risk", which no card offers: it is a human saying the finding is not true, on the record."""
    review = models.Review(
        {
            "machine": {"status": "generated", "security": {"findings": [_A_FINDING]}},
            "human": {
                "status": "in_progress",
                "dispositions": [{"subject_id": "SEC-001", "action": "dispute_finding"}],
            },
        }
    )
    assert not [r for r in review_policy.blocking_reasons(review, "low") if "SEC-001" in r]
    # Any other disposition is not a dispute. "I will revise the implementation" leaves it standing.
    still = models.Review(
        {
            "machine": review.machine,
            "human": {"status": "in_progress", "dispositions": [{"subject_id": "SEC-001", "action": "reduce_scope"}]},
        }
    )
    assert [r for r in review_policy.blocking_reasons(still, "low") if "SEC-001" in r]


def test_a_resolved_finding_asks_the_human_nothing() -> None:
    """A card is a question. `security_review.resolution_of` answered this one against the
    committed tree, which is a stronger answer than the card would collect — and left in, a fixed
    `high` finding raised a mandatory card, so fixing the code was what stopped the freeze."""
    resolved = {**_A_FINDING, "blocking": False, "status": "resolved", "resolved_at": {"subject_head_sha": "f" * 40}}
    _, cards = decision_cards.derive_cards(
        claims=[], gaps=[], extra_behaviors=[], security_findings=[resolved], plan_risk={}, plan_domains={}
    )
    assert cards == []


def test_an_open_finding_still_asks() -> None:
    _, cards = decision_cards.derive_cards(
        claims=[],
        gaps=[],
        extra_behaviors=[],
        security_findings=[{**_A_FINDING, "status": "open"}],
        plan_risk={},
        plan_domains={},
    )
    assert [c["risk"] for c in cards] == ["high"]


def test_a_finding_with_no_status_at_all_still_asks() -> None:
    """Fail closed, the same way `blocking_security_findings` does: only the word `resolved`
    silences a finding, never the absence of one."""
    _, cards = decision_cards.derive_cards(
        claims=[], gaps=[], extra_behaviors=[], security_findings=[_A_FINDING], plan_risk={}, plan_domains={}
    )
    assert [c["risk"] for c in cards] == ["high"]
