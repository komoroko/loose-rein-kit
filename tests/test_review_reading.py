"""The reading half of gate ④: what a reviewer is shown, and how a composed review says so.

`review_reading` decides which bytes reach a reviewer. These pin the two properties that make a
review readable in slices without making it dishonest: a reading is a function of its *own* content
(so it survives every commit that could not have changed it), and a slice that nobody read is named
rather than counted as read.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import diff_facts, models, review_policy, review_reading
from rein import repo as repo_mod


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def two_dir_repo(tmp_path: Path) -> Path:
    """A repository whose change touches two directories, so a reading can cover one of them."""
    (tmp_path / ".rein").mkdir()
    _git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    for directory in ("alpha", "beta"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "mod.py").write_text(f"def {directory}():\n    return 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "both")
    return tmp_path


# --- a reading is a function of its own content -------------------------------


def test_a_reading_narrows_the_diff_to_its_own_paths(two_dir_repo: Path) -> None:
    repo = repo_mod.Repo(two_dir_repo)
    base = _git(two_dir_repo, "rev-parse", "HEAD~1")
    whole = review_reading.diff_of(repo, base, "HEAD", (repo_mod.SSOT_DIR,))
    slice_ = review_reading.diff_of(repo, base, "HEAD", (repo_mod.SSOT_DIR,), include=("alpha/",))
    assert "alpha/mod.py" in whole and "beta/mod.py" in whole
    assert "alpha/mod.py" in slice_ and "beta/mod.py" not in slice_


def test_a_narrowed_reading_cannot_reach_past_the_exclusion(two_dir_repo: Path) -> None:
    """`include` narrows *within* `exclude`, never against it — otherwise a reading pointed at the
    SSOT would hand the blind extractor the plan it must never see."""
    repo = repo_mod.Repo(two_dir_repo)
    base = _git(two_dir_repo, "rev-parse", "HEAD~1")
    (two_dir_repo / ".rein" / "note.yaml").write_text("hidden: true\n", encoding="utf-8")
    _git(two_dir_repo, "add", "-A")
    _git(two_dir_repo, "commit", "-qm", "ssot")
    aimed = review_reading.diff_of(repo, base, "HEAD", (repo_mod.SSOT_DIR,), include=(repo_mod.SSOT_DIR,))
    assert aimed == ""


def test_a_readings_content_digest_ignores_what_it_does_not_cover(two_dir_repo: Path) -> None:
    """The property the whole composition rests on: a reading taken when one task landed is still
    the answer to the same question after every later task has landed."""
    repo = repo_mod.Repo(two_dir_repo)
    before = review_reading.change_digest(repo, "HEAD", (repo_mod.SSOT_DIR,), include=("alpha/",))
    (two_dir_repo / "beta" / "mod.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    _git(two_dir_repo, "add", "-A")
    _git(two_dir_repo, "commit", "-qm", "move beta")
    after = review_reading.change_digest(repo, "HEAD", (repo_mod.SSOT_DIR,), include=("alpha/",))
    whole = review_reading.change_digest(repo, "HEAD", (repo_mod.SSOT_DIR,))
    assert after == before, "a commit outside a reading's paths must not invalidate it"
    assert whole != before


def test_two_readings_of_the_same_bytes_are_not_one_answer() -> None:
    """A task and the seam over it can cover the same file, and they are different questions."""

    def keys(unit: str) -> dict[str, str]:
        return review_reading.reading_keys(
            config=None,
            change="sha256:" + "1" * 64,
            coverage_digest="sha256:" + "2" * 64,
            trusted_base="b" * 40,
            ceiling=400_000,
            risk_floor="low",
            prior_blocking=[],
            unit=unit,
        )

    assert keys("T-001") == keys("T-001")
    assert keys("T-001") != keys("seam")


# --- a slice nobody read is named, never counted as read ----------------------


def _measured(unit: str, paths: list[str], *, digest: str = "sha256:" + "9" * 64) -> Any:
    files = [diff_facts.DiffFile(path=p, hunks=()) for p in paths]
    facts = diff_facts.analyze("")
    return review_reading.ReadingFacts(
        reading=review_reading.Reading(unit=unit, include=tuple(paths)),
        diff_text="",
        facts=diff_facts.DiffFacts(**{**facts.__dict__, "files": files}),
        reviewable=review_reading.Reviewable(text="", context_lines=3),
        anchorable=(),
        coverage={"diff_digest": digest, "analyzed_bytes": 10},
        content_digest=digest,
    )


def test_one_whole_reading_records_that_it_was_whole() -> None:
    """Written down rather than left implicit: a reader must never have to infer it."""
    manifest = review_reading.compose_coverage(
        {"coverage_status": "sufficient"},
        [_measured(review_reading.WHOLE, ["a.py"])],
        changed_paths=["a.py"],
    )
    assert manifest["composition"]["mode"] == "whole"
    assert manifest["composition"]["readings"][0]["unit"] == "whole"
    assert "unread_paths" not in manifest["composition"]
    assert manifest["coverage_status"] == "sufficient"


def test_a_changed_path_no_reading_covered_makes_the_manifest_insufficient() -> None:
    """The answer to the objection composition has to answer. Splitting a diff hides the seam, and
    hiding the seam is what lets "extra behaviours: 0" be said about bytes nobody looked at."""
    manifest = review_reading.compose_coverage(
        {"coverage_status": "sufficient"},
        [_measured("T-001", ["alpha/mod.py"]), _measured("T-002", ["beta/mod.py"])],
        changed_paths=["alpha/mod.py", "beta/mod.py", "stray.py"],
    )
    assert manifest["composition"]["mode"] == "composed"
    assert manifest["composition"]["unread_paths"] == ["stray.py"]
    assert manifest["coverage_status"] == "insufficient"


def test_a_composed_review_is_refused_at_critical() -> None:
    """Composition reads each slice and the seam, which is enough to say what each slice does and
    not enough to rule out behaviour that exists only once two of them are in one tree."""
    review = models.Review(
        {
            "machine": {
                "status": "generated",
                "coverage": {"coverage_status": "sufficient", "composition": {"mode": "composed", "readings": []}},
            },
            "human": {"status": "not_started"},
        }
    )
    assert review_policy.coverage_blocks(review, "high") == []
    blocked = review_policy.coverage_blocks(review, "critical")
    assert blocked and "composed" in blocked[0]


def test_a_statement_carries_the_reading_it_came_out_of() -> None:
    stamped = review_reading.stamped([{"id": "AST-001", "statement": "x"}], "T-003")
    assert stamped == [{"id": "AST-001", "statement": "x", "reading": "T-003"}]


def test_a_reading_over_budget_says_which_one() -> None:
    limits = {"max_diff_bytes": 10}
    review_reading.refuse_over_budget(10, limits, unit="T-001")
    with pytest.raises(review_reading.ReviewError, match="the reading of T-001"):
        review_reading.refuse_over_budget(11, limits, unit="T-001")
    with pytest.raises(review_reading.ReviewError, match="this change"):
        review_reading.refuse_over_budget(11, limits)


# --- a composed review, end to end --------------------------------------------


def _composed_repo(tmp_path: Path) -> Path:
    """A repository whose plan scopes two tasks, and whose change touches both plus a stray file."""
    from tests._support import make_claim, make_config, make_plan, make_state, make_task, seed_repo

    seed_repo(
        tmp_path,
        state=make_state(project="rv", phase="build"),
        plan=make_plan(
            claims=[make_claim()],
            tasks=[
                make_task("T-001", claim_ids=["C-001"], scope_include=["alpha/"]),
                make_task("T-002", claim_ids=["C-001"], scope_include=["beta/"]),
            ],
        ),
        config=make_config(),
    )
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    # The review is taken against `main` (the plan's base commit is not in this repository), so the
    # change has to live on a branch above it.
    _git(tmp_path, "checkout", "-q", "-b", "work")
    for directory in ("alpha", "beta"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "mod.py").write_text(f"def {directory}():\n    return 1\n", encoding="utf-8")
    (tmp_path / "stray.py").write_text("STRAY = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "both and a stray")
    return tmp_path


def _reviewer_seeing(seen: list[tuple[str, str]]) -> Any:
    """A reviewer that records which diff each reading stage was sent, and answers minimally."""
    import json

    def reviewer(role: str, request: Any) -> str:
        if role == "comparator":
            return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
        seen.append((role, str(request.get("diff", ""))))
        if role == "security_reviewer":
            return json.dumps({"findings": []})
        return json.dumps({"actual_statements": [], "coverage": {}})

    return reviewer


@pytest.mark.integration
def test_a_scoped_plan_is_read_one_task_at_a_time_and_says_so(tmp_path: Path) -> None:
    """The whole point: one launch holds one task's slice, not a cycle — and the review records
    which readings it was assembled from, so nobody has to infer it."""
    from rein import review
    from tests.test_review import _reviewers

    root = _composed_repo(tmp_path)
    seen: list[tuple[str, str]] = []
    machine = review.generate(repo_mod.Repo(root), _reviewers(_reviewer_seeing(seen)))

    composition = machine["coverage"]["composition"]
    assert composition["mode"] == "composed"
    assert [r["unit"] for r in composition["readings"]] == ["T-001", "T-002", "seam"]

    extractions = [diff for role, diff in seen if role == "actual_extractor"]
    assert len(extractions) == 3, "one reading per scoped task, plus the seam"
    assert "alpha/mod.py" in extractions[0] and "beta/mod.py" not in extractions[0]
    assert "beta/mod.py" in extractions[1] and "alpha/mod.py" not in extractions[1]
    # The stray file no task's scope declares is nobody's slice, so it is the seam's.
    assert "stray.py" in extractions[2]
    assert "unread_paths" not in composition, "the seam covered what the slices did not"


@pytest.mark.integration
def test_a_composed_review_reuses_the_readings_the_change_did_not_move(tmp_path: Path) -> None:
    """The measurement this exists for: a fix inside one task's scope re-reads that task, and the
    tasks beside it stay read."""
    from rein import review
    from tests.test_review import _reviewers

    root = _composed_repo(tmp_path)
    repo = repo_mod.Repo(root)
    review.generate(repo, _reviewers(_reviewer_seeing([])))

    (root / "alpha" / "mod.py").write_text("def alpha():\n    return 2\n", encoding="utf-8")
    # Only the file the fix touched: `add -A` would sweep up whatever the run left in the
    # repository (a lock file, the cache) and put the change in a different reading.
    _git(root, "add", "alpha/mod.py")
    _git(root, "commit", "-qm", "fix alpha")

    again: list[tuple[str, str]] = []
    review.generate(repo, _reviewers(_reviewer_seeing(again)))
    units = [diff for role, diff in again if role == "actual_extractor"]
    assert len(units) == 1, "only the reading whose content moved is read again"
    assert "alpha/mod.py" in units[0]


@pytest.mark.integration
def test_the_operator_can_ask_for_one_reading_of_everything(tmp_path: Path) -> None:
    """`whole` is the escape for a repository that would rather pay for one reading."""
    import yaml

    from rein import review
    from tests.test_review import _reviewers

    root = _composed_repo(tmp_path)
    config_path = root / ".rein" / "config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config.setdefault("review_policy", {})["composition"] = "whole"
    config_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    _git(root, "add", ".rein/config.yaml")
    _git(root, "commit", "-qm", "read it whole")

    seen: list[tuple[str, str]] = []
    machine = review.generate(repo_mod.Repo(root), _reviewers(_reviewer_seeing(seen)))
    assert machine["coverage"]["composition"]["mode"] == "whole"
    assert len([diff for role, diff in seen if role == "actual_extractor"]) == 1


def test_a_composition_past_what_one_review_may_carry_is_refused_not_cut() -> None:
    """A list silently cut is a review that says less than it read, which is the failure
    "extra behaviours: 0" exists to prevent. 0.3.7 deleted a `truncated` field for this reason."""
    from rein import actual_extraction, security_review

    def readout(unit: str, count: int) -> review_reading.ReadOut:
        statements = [{"id": f"AST-{i + 1:03d}", "statement": "x"} for i in range(count)]
        return review_reading.ReadOut(
            reading=review_reading.Reading(unit=unit, include=(f"{unit}/",)),
            extraction=actual_extraction.ExtractionResult(
                actual_statements=tuple(statements), coverage={}, actual_digest="sha256:" + "0" * 64
            ),
            security=security_review.SecurityResult(findings=()),
        )

    half = review_reading.MAX_STATEMENTS // 2 + 1
    with pytest.raises(review_reading.ReviewError, match="past what one review may carry"):
        review_reading.merge([readout("a", half), readout("b", half)], coverage={})


@pytest.mark.integration
def test_every_reading_is_held_to_the_extractors_blindness(tmp_path: Path) -> None:
    """`assert_blind` walks each request's whole tree, and a composed review builds one per
    reading — a new path into the extractor's context is how priming comes back."""
    from rein import review
    from tests.test_review import _reviewers

    root = _composed_repo(tmp_path)
    seen: list[tuple[str, str]] = []
    review.generate(repo_mod.Repo(root), _reviewers(_reviewer_seeing(seen)))
    extractions = [diff for role, diff in seen if role == "actual_extractor"]
    assert len(extractions) == 3
    for diff in extractions:
        assert "C-001" not in diff and "claim" not in diff.lower()
