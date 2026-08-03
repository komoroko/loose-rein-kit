"""review.py assembles the grounded machine review from validated pieces (plan §12, §17, §30).

The pure `assemble` is pinned without any of the machinery; `generate` is exercised end to end with
a fake reviewer over a real git repo (a single injected callable that answers each stage), proving
the wiring writes a schema-valid review.yaml and resets the human half.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rein import models, review
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


def test_prior_blocking_findings_are_carried_into_the_next_security_review() -> None:
    """A reviewer may not clear its own block by regenerating and omitting the finding — but
    nothing passed the previous findings in, so the check had nothing to compare against."""
    from tests._support import make_review

    finding = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "the reviewer container reaches a host credential",
        "blocking": True,
    }
    stored = models.Review(make_review(generated=True, security_findings=[finding]))
    assert review._prior_blocking_ids(stored) == ["SEC-001"]
    assert review._prior_blocking_ids(None) == []
