"""review.py assembles the grounded machine review from validated pieces (plan §12, §17, §30).

The pure `assemble` is pinned without any of the machinery; `generate` is exercised end to end with
a fake reviewer over a real git repo (a single injected callable that answers each stage), proving
the wiring writes a schema-valid review.yaml and resets the human half.
"""

from __future__ import annotations

import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rein import actual_extraction, conformance, diff_facts, event_chain, models, review
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, seed_repo


def test_assemble_is_schema_valid_and_counts_verdicts() -> None:
    binding = {
        "change_digest": "sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "toolchain_digest": "sha256:" + "c" * 64,
    }
    coverage = {
        "diff_digest": "sha256:" + "d" * 64,
        "analyzed_files": 2,
        "analyzed_bytes": 1024,
        "truncated": False,
        "coverage_status": "sufficient",
    }
    claims = [
        {
            "claim_id": "C-001",
            "verdict": "aligned",
            "integrity": {"status": "verified"},
            "semantic_support": {"status": "supported", "assessment_basis": "machine_assessed"},
            "conformance": {"status": "observed"},
        },
        {
            "claim_id": "C-002",
            "verdict": "diverged",
            "integrity": {"status": "verified"},
            "semantic_support": {"status": "contradicted", "assessment_basis": "machine_assessed"},
            "conformance": {"status": "observed"},
        },
    ]
    machine = review.assemble(binding=binding, coverage=coverage, actual_statements=[], claims=claims)
    assert models.schema_errors({"machine": machine, "human": {"status": "not_started"}}, "review") == []
    assert machine["summary"]["claims_total"] == 2
    assert machine["summary"]["aligned"] == 1 and machine["summary"]["diverged"] == 1
    assert machine["status"] == "generated"


# -- generate over a real repo -------------------------------------------------


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _fake_reviewer(request: Mapping[str, Any]) -> str:
    """One callable answering every stage minimally-but-validly, keyed on the request shape."""
    import json

    if "expected_model" in request:  # the comparator: echo the digest it was handed, no claims
        return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
    facts = request.get("deterministic_facts", {})
    if isinstance(facts, dict) and "signals" in facts:  # the security reviewer
        return json.dumps({"findings": []})
    return json.dumps({"actual_statements": [], "coverage": {}})  # the blind extractor


@pytest.fixture
def review_repo(tmp_path: Path) -> Path:
    seed_repo(
        tmp_path,
        state=make_state(project="rv", phase="build"),
        plan=make_plan(),
        config=make_config(),
    )
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return tmp_path


@pytest.mark.integration
def test_generate_writes_a_schema_valid_review_and_resets_human(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    machine = review.generate(repo, _fake_reviewer)
    assert machine["status"] == "generated"
    stored = store_mod.Store(repo).read_review()
    assert stored is not None and stored.is_generated
    assert stored.human_status == "not_started"  # a fresh machine review is a fresh review
    assert stored.machine.get("binding", {}).get("subject_head_sha")


@pytest.mark.integration
def test_an_extra_behavior_the_comparator_found_reaches_the_review(review_repo: Path) -> None:
    """`extra_behaviors` is the section that answers "did it build something nobody asked for?".

    It was assembled from a parameter no call site ever passed, so the summary's "extra
    behaviours: 0" was an empty list's length rather than a reading — prose standing in for
    evidence, in the one product that refuses that everywhere else.
    """
    import json

    (review_repo / "src").mkdir(exist_ok=True)
    (review_repo / "src" / "app.py").write_text("def handle():\n    return 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "app")
    blob = _git(review_repo, "rev-parse", "HEAD:src/app.py")

    def reviewer(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            return json.dumps(
                {
                    "claims": [],
                    "actual_digest": request["actual_digest"],
                    "extra_behaviors": [
                        {
                            "id": "EXTRA-001",
                            "category": "retry_timeout_fallback",
                            "risk": "high",
                            "grounded": False,
                            "blocking": True,
                            "actual_statement_ids": ["AST-001"],
                        }
                    ],
                }
            )
        facts = request.get("deterministic_facts", {})
        if isinstance(facts, dict) and "signals" in facts:
            return json.dumps({"findings": []})
        return json.dumps(
            {
                "actual_statements": [
                    {
                        "id": "AST-001",
                        "statement": "retries three times before giving up",
                        "category": "control_flow",
                        "confidence": "medium",
                        "code_anchors": [
                            {"path": "src/app.py", "start_line": 1, "end_line": 2, "blob": f"git-blob:{blob}"}
                        ],
                    }
                ],
                "coverage": {"analyzed_files": 1},
            }
        )

    machine = review.generate(repo_mod.Repo(review_repo), reviewer)
    assert models.schema_errors({"machine": machine, "human": {"status": "not_started"}}, "review") == []
    assert [e["id"] for e in machine["extra_behaviors"]] == ["EXTRA-001"]
    # Ungrounded, so it becomes a question a human has to answer — not a count in a summary.
    subjects = {s["applicability"]["subject_id"] for s in machine.get("statements", [])}
    assert "EXTRA-001" in subjects


def test_extra_behavior_statement_ids_continue_past_the_gaps() -> None:
    """Both lists become decision-card subjects; a shared id puts one's question on the other's options."""
    gaps = review._coverage_gaps([{"id": "GAP-001"}, {"id": "GAP-002"}])
    extras = review._extra_behaviors([{"id": "EXTRA-001", "category": "new_default"}], gaps=gaps)
    assert [g["statement_id"] for g in gaps] == ["STMT-001", "STMT-002"]
    assert extras[0]["statement_id"] == "STMT-003"


def test_an_extra_behavior_that_omits_grounded_still_reaches_a_human() -> None:
    """`grounded: true` is what takes it off the human's list, so an absent flag must not do that."""
    extras = review._extra_behaviors([{"id": "EXTRA-001", "category": "persistence"}], gaps=[])
    assert extras[0]["grounded"] is False


@pytest.mark.integration
def test_generate_then_complete_freezes_a_clean_review(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    review.complete(repo)  # no challenges, no blockers → freezes
    stored = store_mod.Store(repo).read_review()
    assert stored is not None and stored.human_status == "frozen"


# -- what the comparator is actually handed ------------------------------------


def _capturing_reviewer(seen: list[Mapping[str, Any]]) -> Any:
    def reviewer(request: Mapping[str, Any]) -> str:
        seen.append(request)
        return _fake_reviewer(request)

    return reviewer


def test_accepting_the_risk_is_not_a_disposition() -> None:
    """§15.4: a critical unknown cannot be waved through by declaring it acceptable."""
    assert "accepted-risk" not in models.DISPOSITION_VALUES
    assert "accept_risk" not in models.DISPOSITION_VALUES


# -- the code the reviewers are allowed to read --------------------------------


@pytest.mark.integration
def test_the_reviewers_get_the_changed_files_not_only_the_diff(review_repo: Path) -> None:
    """A hunk without its surrounding code cannot answer "was this guard removed or moved?"."""
    (review_repo / "src.py").write_text("def charge():\n    return 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    extract = next(r for r in seen if "relevant_code" in r and "diff" in r)
    assert "def charge" in extract["relevant_code"]["src.py"]


@pytest.mark.integration
def test_a_file_over_the_cap_is_truncated_and_says_so(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reviewer must never read "this is all of it" from a file that was cut short."""
    monkeypatch.setattr(review, "RELEVANT_CODE_CHARS", 40)
    (review_repo / "src.py").write_text("# padding\n" * 40, encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    extract = next(r for r in seen if "relevant_code" in r and "diff" in r)
    assert len(extract["relevant_code"]["src.py"]) == 40
    assert extract["deterministic_facts"]["relevant_code"]["truncated_to_char_cap"] == ["src.py"]


@pytest.mark.integration
def test_relevant_code_never_carries_the_expected_model(review_repo: Path) -> None:
    """The extractor stays blind: a file named `plan` in the tree must not smuggle one in."""
    (review_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    extract = next(r for r in seen if "relevant_code" in r and "diff" in r)
    assert ".rein/plan.yaml" not in extract["relevant_code"]


@pytest.mark.integration
def test_change_digest_excludes_the_rein_dir(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    head = _git(review_repo, "rev-parse", "HEAD")
    before = review.change_digest(repo, head)
    # A new file under .rein/ must not move the change digest (it is bound by its own digests).
    (review_repo / ".rein" / "scratch.txt").write_text("bound elsewhere\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "touch ssot")
    same = review.change_digest(repo, _git(review_repo, "rev-parse", "HEAD"))
    assert same == before
    # A change to real source, on the other hand, does move it.
    (review_repo / "src.py").write_text("print('changed')\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "real change")
    moved = review.change_digest(repo, _git(review_repo, "rev-parse", "HEAD"))
    assert moved != before


# -- what the pipeline had been handing its own stages -------------------------


def test_the_independence_groups_come_from_the_config_that_declares_them() -> None:
    """`independence_group` is the whole substitute for buying a second AI provider.
    Returning one hardcoded group for both roles made every critical review fail as
    "not independent" while the configured pair reached nothing but a doctor warning."""
    config = models.Config(make_config())
    assert review._independence(config) == {
        "actual_extractor": {"group": "claude/opus"},
        "comparator": {"group": "claude/sonnet"},
    }


def test_an_unset_independence_group_is_empty_not_invented() -> None:
    assert review._independence(None) == {"actual_extractor": {"group": ""}, "comparator": {"group": ""}}


BASE_A, BASE_B = "a" * 40, "b" * 40

BLOCKING_FINDING = {
    "id": "SEC-001",
    "severity": "high",
    "category": "credential_exposure",
    "attack_scenario": "the reviewer container reaches a host credential",
    "blocking": True,
}


def _stored_review(base: str) -> models.Review:
    from tests._support import make_review

    return models.Review(make_review(generated=True, security_findings=[BLOCKING_FINDING], base_sha=base))


def test_prior_blocking_findings_are_carried_into_the_next_security_review() -> None:
    """A reviewer may not clear its own block by regenerating and omitting the finding — but
    nothing passed the previous findings in, so the check had nothing to compare against."""
    assert review._prior_blocking_ids(_stored_review(BASE_A), BASE_A) == ["SEC-001"]
    assert review._prior_blocking_ids(None, BASE_A) == []


def test_a_blocking_finding_does_not_follow_the_review_onto_a_different_base() -> None:
    """A finding is a statement about a change. Change the base and it is about something else.

    Carried by id alone, a finding taken against base A kept blocking a regeneration against
    base B — a different diff, sometimes not even containing the code the finding named — and the
    only way past it was for the reviewer to re-assert something it could no longer see.
    """
    assert review._prior_blocking_ids(_stored_review(BASE_A), BASE_B) == []
    # A review that never recorded a base cannot claim to be about this one either.
    assert review._prior_blocking_ids(_stored_review(""), BASE_A) == []


# --- the pipeline's shape -----------------------------------------------------


def test_the_security_stage_does_not_wait_behind_the_extraction(review_repo: Path) -> None:
    """It reads the diff and the relevant code and consumes nothing the extractor produces, so
    three stages at up to fifteen minutes each ran end to end for no reason but the order they
    were written in. Pinned by the one observable consequence: the security request is issued
    before the extraction has answered.
    """
    started = threading.Event()
    order: list[str] = []
    lock = threading.Lock()

    def reviewer(request: Mapping[str, Any]) -> str:
        facts = request.get("deterministic_facts")
        security = isinstance(facts, dict) and "signals" in facts
        with lock:
            order.append("security" if security else "chain")
        if security:
            started.set()
        else:
            # The extractor/comparator chain refuses to answer until the security stage has been
            # asked. If they were serial this would deadlock, which is exactly the point.
            assert started.wait(timeout=10), "the security stage never started while the chain was running"
        return _fake_reviewer(request)

    machine = review.generate(repo_mod.Repo(review_repo), reviewer)

    assert "security" in order
    assert "findings" in machine["security"]  # the stage answered, off the chain's thread


def test_the_review_is_the_same_document_whatever_the_stages_race(review_repo: Path) -> None:
    """Concurrency buys wall-clock time and must buy nothing else: the merge order and the event
    order are fixed, so two generations of the same HEAD assemble identically."""
    repo = repo_mod.Repo(review_repo)
    first = review.generate(repo, _fake_reviewer)
    second = review.generate(repo, _fake_reviewer)
    for machine in (first, second):
        machine["binding"].pop("generated_at", None)
    assert first == second


@pytest.mark.integration
def test_a_review_that_could_not_be_produced_says_so_in_the_audit_log(review_repo: Path) -> None:
    """`events.ATTENTION_EVENTS` counts `actual_extraction_failed` and `review_failed` as things
    needing a human decision, and nothing anywhere emitted either — so every failure of gate ④'s
    own machinery left the log reporting "needing a human decision: 0".

    The extractor is the stage failed here because it is the one with an event of its own: its
    failure means no Actual was read out of the code at all.
    """

    def refusing(request: Mapping[str, Any]) -> str:
        if "expected_model" not in request and "signals" not in (request.get("deterministic_facts") or {}):
            raise actual_extraction.ExtractionError("the extractor returned prose, not a statement list")
        return _fake_reviewer(request)

    repo = repo_mod.Repo(review_repo)
    with pytest.raises(actual_extraction.ExtractionError):
        review.generate(repo, refusing)

    events, defects = event_chain.scan(repo.events)
    assert not defects, defects  # the failure path must leave the chain intact, not merely present
    kinds = [e.event for e in events]
    assert kinds[-2:] == ["actual_extraction_failed", "review_failed"]
    assert events[-1].detail.get("stage") == "actual_extraction"
    assert "prose" in str(events[-1].detail.get("reason"))
    assert {"actual_extraction_failed", "review_failed"} <= events_mod.ATTENTION_EVENTS
    # Nothing was written: a review that failed must not leave a half-built machine half behind.
    stored = store_mod.Store(repo).read_review()
    assert stored is None or not stored.is_generated


@pytest.mark.integration
def test_a_comparison_failure_is_not_reported_as_a_failed_extraction(review_repo: Path) -> None:
    """Two different facts. The extractor failing means there is no Actual; the comparator failing
    means one exists and could not be held against the plan — and only the first has an event."""

    def refusing(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            raise conformance.ComparatorError("the comparator cited a statement that does not exist")
        return _fake_reviewer(request)

    repo = repo_mod.Repo(review_repo)
    with pytest.raises(conformance.ComparatorError):
        review.generate(repo, refusing)

    kinds = [e.event for e in event_chain.scan(repo.events)[0]]
    assert kinds[-1] == "review_failed"
    assert "actual_extraction_failed" not in kinds


# --- what the reviewers are actually handed ------------------------------------

_LOCKFILE_DIFF = """diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -1,2 +1,3 @@
 def handle():
+    validate(request)
     return ok()
diff --git a/uv.lock b/uv.lock
index 3333333..4444444 100644
--- a/uv.lock
+++ b/uv.lock
@@ -1,4 +1,4 @@
-version = "1"
-name = "a"
+version = "2"
+name = "b"
"""


def test_a_lockfiles_body_is_withheld_and_the_code_is_not() -> None:
    """Eight hundred lines saying "the dependencies moved" bury the twelve that say anything.

    Redaction, not summarisation: nothing is described or interpreted, and what replaces the hunk
    is the fact that it was there and how big it was.
    """
    files = diff_facts.parse_diff(_LOCKFILE_DIFF)
    folded, paths = review.fold_mechanical(_LOCKFILE_DIFF, files)

    assert paths == ["uv.lock"]
    assert "validate(request)" in folded, "the hand-written change must survive intact"
    assert 'version = "2"' not in folded, "the lockfile body was not withheld"
    assert "uv.lock" in folded, "the reader still has to be told the lockfile changed"
    assert "line(s) of mechanical change, body withheld" in folded


def test_a_change_with_nothing_mechanical_is_passed_through_untouched() -> None:
    plain = "diff --git a/src/api.py b/src/api.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert review.fold_mechanical(plain, diff_facts.parse_diff(plain)) == (plain, [])


def test_the_coverage_manifest_still_reads_the_whole_diff() -> None:
    """The honesty property the folding must not touch.

    What the manifest measures is how much of the change could be analysed; folding a file before
    counting it would be measuring the fold. A dependency change goes on making the coverage
    `insufficient` for exactly the reason it always did.
    """
    facts = diff_facts.analyze(_LOCKFILE_DIFF)
    manifest = facts.coverage.to_manifest()
    assert manifest["coverage_status"] == "insufficient"
    assert manifest["dependency_semantics_analyzed"] is False
