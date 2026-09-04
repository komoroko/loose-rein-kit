"""The reading half of gate ④: what a reviewer is shown, and how a composed review says so.

`review_reading` decides which bytes reach a reviewer. These pin the two properties that make a
review readable in slices without making it dishonest: a reading is a function of its *own* content
(so it survives every commit that could not have changed it), and a slice that nobody read is named
rather than counted as read.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rein import diff_facts, digests, models, review_policy, review_reading
from rein import repo as repo_mod
from rein import usage as usage_mod


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
    """A composed review must never present a statement about one slice as a reading of the tree,
    and the digest it hands the comparator has to be over the statements it hands the comparator."""
    from rein import actual_extraction, security_review

    def readout(unit: str) -> review_reading.ReadOut:
        return review_reading.ReadOut(
            reading=review_reading.Reading(unit=unit, include=(f"{unit}/",)),
            extraction=actual_extraction.ExtractionResult(
                actual_statements=({"id": "AST-001", "statement": unit},),
                coverage={},
                actual_digest="sha256:" + "0" * 64,
            ),
            security=security_review.SecurityResult(findings=()),
        )

    composed = review_reading.merge([readout("T-001"), readout("T-002")], coverage={})
    assert [s["reading"] for s in composed.statements] == ["T-001", "T-002"]
    assert [s["id"] for s in composed.statements] == ["AST-001", "AST-002"]
    assert composed.actual_digest == digests.of({"actual_statements": list(composed.statements), "coverage": {}})


def test_a_whole_change_reading_keeps_the_digest_the_extractor_minted() -> None:
    """The stamp is not added there, and the reason is the digest: it binds the statements the
    comparator is handed, and the extractor minted it over the statements it produced. `whole` is
    what `coverage.composition.mode` already says, so the field would cost that binding to repeat
    what the manifest states."""
    from rein import actual_extraction, security_review

    minted = "sha256:" + "a" * 64
    only = review_reading.ReadOut(
        reading=review_reading.WHOLE_READING,
        extraction=actual_extraction.ExtractionResult(
            actual_statements=({"id": "AST-001", "statement": "x"},), coverage={}, actual_digest=minted
        ),
        security=security_review.SecurityResult(findings=()),
    )

    composed = review_reading.merge([only], coverage={"anything": 1})
    assert composed.actual_digest == minted
    assert "reading" not in composed.statements[0]


def test_a_reading_over_budget_says_which_one() -> None:
    limits = {"max_diff_bytes": 10}
    review_reading.refuse_over_budget(10, limits, unit="T-001")
    with pytest.raises(review_reading.ReviewError, match="the reading of T-001"):
        review_reading.refuse_over_budget(11, limits, unit="T-001")
    with pytest.raises(review_reading.ReviewError, match="this change"):
        review_reading.refuse_over_budget(11, limits)


# --- a composed review, end to end --------------------------------------------


def _composed_repo(tmp_path: Path, *, base_files: Mapping[str, str] | None = None) -> Path:
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
    for name, body in (base_files or {}).items():
        (tmp_path / name).write_text(body, encoding="utf-8")
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
def test_a_slice_holding_no_signal_is_not_where_the_risk_floor_drops(tmp_path: Path) -> None:
    """The floor is a property of the change, not of a slice of it. Every reading was being asked
    at its *own* floor — so a task whose diff happens to match no detector signal instructed the
    extractor at `low` while the change was `high`, and the extractor's coverage was validated
    against that lower bar. The parameter existed for this and the only caller passed the slice."""
    from rein import review, review_reading
    from tests.test_review import _reviewers

    root = _composed_repo(tmp_path)
    # One slice carries a security-boundary signal; the other and the seam carry none.
    (root / "alpha" / "mod.py").write_text("def check_token(t):\n    return t\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a boundary in one slice only")

    floors: list[str] = []

    def reviewer(role: str, request: Any) -> str:
        import json

        if role == "comparator":
            return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
        if role == "actual_extractor":
            floors.append(str(request["deterministic_facts"]["risk_floor"]))
            return json.dumps({"actual_statements": [], "coverage": {}})
        return json.dumps({"findings": []})

    repo = repo_mod.Repo(root)
    review.generate(repo, _reviewers(reviewer))

    whole, _effective = review_reading.whole_change_risk(
        repo,
        None,
        base=review_reading.resolve_base(repo, None, None),
        head=repo._git_rc("rev-parse", "HEAD")[1].strip(),
        exclude=review_reading.not_the_product(repo, None),
    )
    assert whole == "high", "the change as a whole crosses a security boundary"
    assert floors == [whole] * 3, floors


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


# --- what composition refuses to do -------------------------------------------


def _scoped_plan() -> models.Plan:
    from tests._support import make_claim, make_plan, make_task

    return models.Plan(
        make_plan(
            claims=[make_claim()],
            tasks=[
                make_task("T-001", claim_ids=["C-001"], scope_include=["alpha/"]),
                make_task("T-002", claim_ids=["C-001"], scope_include=["alpha/shared.py"]),
            ],
        )
    )


def test_a_critical_change_is_read_whole_whatever_the_configuration_says() -> None:
    """Decided here, once, instead of in two places that disagreed.

    `coverage_blocks` refuses a composed review at critical while config.yaml promised the change
    would simply be read whole there. Neither happened: `generate` had the effective risk one line
    above this call and did not pass it, so a critical change was read in slices and then blocked
    for having been — and the remedy the block named ("re-read it whole") could not be carried out,
    because the mode lives inside gate ③'s frozen digest and `rein review generate` has no override.
    """
    plan, changed = _scoped_plan(), ["alpha/mod.py", "alpha/shared.py"]
    assert [r.unit for r in review_reading.plan_readings(plan, changed)] != [review_reading.WHOLE]
    assert [r.unit for r in review_reading.plan_readings(plan, changed, risk="critical")] == [review_reading.WHOLE]
    # And only there: the limit is about what composition cannot rule out, not about caution.
    assert [r.unit for r in review_reading.plan_readings(plan, changed, risk="high")] != [review_reading.WHOLE]


def test_a_carried_finding_is_answered_by_exactly_one_reading() -> None:
    """A path two scopes both cover is inside three readings — both tasks' and the seam's, which
    lists it *because* more than one scope covers it. Asked per reading, all three were handed the
    same carried finding, all three were required to re-state it, and `merge` keeps the id of
    anything a reading carried: one review carrying `SEC-001` three times.
    """
    readings = review_reading.plan_readings(_scoped_plan(), ["alpha/mod.py", "alpha/shared.py", "loose.py"])
    assert review_reading.SEAM in {r.unit for r in readings}
    prior: list[Mapping[str, Any]] = [
        {"id": "SEC-001", "code_anchors": [{"path": "alpha/shared.py", "line": 1}]},
        {"id": "SEC-002", "code_anchors": [{"path": "loose.py", "line": 1}]},
        {"id": "SEC-003"},
    ]
    assigned = review_reading.priors_by_reading(readings, prior)
    assert [f["id"] for f in assigned["T-002"]] == ["SEC-001"], "the most specific scope owns it"
    assert [f["id"] for f in assigned["T-001"]] == []
    assert [f["id"] for f in assigned[review_reading.SEAM]] == ["SEC-002", "SEC-003"], "unowned, and anchorless"
    handed = [f["id"] for findings in assigned.values() for f in findings]
    assert sorted(handed) == ["SEC-001", "SEC-002", "SEC-003"], "each carried finding travels once"


def test_the_whole_change_reading_carries_every_prior() -> None:
    """There is no slice to own anything, so the one reading answers for all of them."""
    prior: list[Mapping[str, Any]] = [{"id": "SEC-001", "code_anchors": [{"path": "anywhere.py"}]}, {"id": "SEC-002"}]
    assigned = review_reading.priors_by_reading([review_reading.WHOLE_READING], prior)
    assert [f["id"] for f in assigned[review_reading.WHOLE]] == ["SEC-001", "SEC-002"]


@pytest.mark.integration
def test_a_carried_finding_outside_every_scope_is_re_read_whole(tmp_path: Path) -> None:
    """End to end: the composition that cannot hold a carried finding is not the one taken.

    The security stage is handed a checkout, not only its slice's diff, so it can anchor a finding
    at a file this change never touched — `README.md` here, which no task scope declares and which
    the seam therefore never lists. Carried into the next generation, that finding has no reading
    to answer for it, and the reviewer that got it anyway could neither re-state nor resolve it.
    """
    import json

    from rein import review, security_review
    from tests.test_review import _reviewers

    # `legacy.py` is committed on the base, so it is in the tree the security stage is handed and
    # in no reading's diff — and no task scope declares it, so the seam never lists it either.
    root = _composed_repo(tmp_path, base_files={"legacy.py": "LEGACY = 1\n"})
    repo = repo_mod.Repo(root)
    blob = _git(root, "rev-parse", "HEAD:legacy.py").strip()
    finding = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "the change reintroduces a credential path this file still reaches",
        "blocking": True,
        "code_anchors": [{"path": "legacy.py", "blob": "git-blob:" + blob, "start_line": 1, "end_line": 1}],
    }

    def reviewer(role: str, request: Any) -> str:
        if role == "comparator":
            return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
        if role == "security_reviewer":
            # What was carried in is re-stated, as a real reviewer must; the finding is minted from
            # one reading only, so the carry-over is one finding rather than one per slice.
            carried = list(security_review.prior_blocking_of(request))
            fresh = [finding] if not carried and "alpha/mod.py" in str(request.get("diff", "")) else []
            return json.dumps({"findings": carried + fresh})
        return json.dumps({"actual_statements": [], "coverage": {}})

    first = review.generate(repo, _reviewers(reviewer))
    assert first["coverage"]["composition"]["mode"] == "composed"
    assert [f["id"] for f in first["security"]["findings"]] == ["SEC-001"]

    second = review.generate(repo, _reviewers(reviewer), force=True)
    assert second["coverage"]["composition"]["mode"] == "whole", "no slice can answer for SEC-001"
    assert [f["id"] for f in second["security"]["findings"]] == ["SEC-001"], "and it is still carried"


def test_a_carried_finding_no_reading_can_see_stops_the_composition() -> None:
    """The assignment above answers *which* reading; this answers whether one exists at all.

    A carried finding is anchored where no task scope reaches and the seam does not list it —
    the security stage reads a checkout, not only the diff, so it can anchor a finding to a file
    this change never touched. There is then no reading that can be held to it: the reviewer that
    got it would be asked about code it was never sent, could neither re-state nor resolve it, and
    the refusal for dropping a carried finding is not passable from there. Handing it to whichever
    reading came first is the wrong repair; the change is not one these slices cover, so
    `review.generate` reads it whole.
    """
    readings = review_reading.plan_readings(_scoped_plan(), ["alpha/mod.py"])
    assert review_reading.SEAM not in {r.unit for r in readings}, "nothing shared, nothing unowned"
    unreachable: list[Mapping[str, Any]] = [{"id": "SEC-009", "code_anchors": [{"path": "beta/caller.py"}]}]
    assert review_reading.unowned_priors(readings, unreachable) == ["SEC-009"]
    # And nothing else is: a finding a scope covers, and one the whole-change reading holds.
    inside: list[Mapping[str, Any]] = [{"id": "SEC-001", "code_anchors": [{"path": "alpha/mod.py"}]}]
    assert review_reading.unowned_priors(readings, inside) == []
    assert review_reading.unowned_priors([review_reading.WHOLE_READING], unreachable) == []


def test_the_reading_shown_a_finding_is_the_task_it_is_filed_against() -> None:
    """Two derivations of "whose finding is this" would eventually name two different tasks.

    `findings.owner_of_path` decides who a finding is *filed* against; `priors_by_reading` decides
    who is *shown* it and required to re-state it. For a finding anchored in more than one place
    those have to be the same task, so both take the first anchor that has an owner at all.
    """
    from rein import findings as findings_mod

    plan = _scoped_plan()
    readings = review_reading.plan_readings(plan, ["alpha/mod.py", "alpha/shared.py", "loose.py"])
    finding: Mapping[str, Any] = {
        "id": "SEC-004",
        "code_anchors": [{"path": "alpha/mod.py"}, {"path": "alpha/shared.py"}],
    }
    filed = findings_mod.owner_of_path(plan, "alpha/mod.py")
    shown = next(unit for unit, carried in review_reading.priors_by_reading(readings, [finding]).items() if carried)
    assert shown == filed == "T-001", "the first anchor that has an owner, on both sides"


class _NoReviewers:
    def for_role(self, role: str) -> Any:
        raise AssertionError("a replay that raised for a reason of ours must not re-read from a model")

    def spend(self) -> dict[str, usage_mod.Usage]:
        return {}


def test_a_stored_answer_that_fails_for_a_reason_other_than_validation_is_not_swallowed(tmp_path: Path) -> None:
    """ "The stored answer no longer validates" was `except Exception`.

    So a `TypeError` from a refactored validator read as a stale entry: every cached stage was
    dropped and re-read from a model, at full price, behind one warning line. A bug in this process
    crashes; only a validation error means the stored bytes can no longer pass.
    """
    from rein import review_cache

    cache = review_cache.StageCache(tmp_path)
    cache.write("actual_extraction", "k", "{}", usage_mod.Usage.unavailable())

    def boom(_reviewer: Any) -> Any:
        raise TypeError("a validator was refactored")

    with pytest.raises(TypeError, match="refactored"):
        review_reading.cached_stage(
            cache, "actual_extraction", "k", set(), boom, _NoReviewers(), reused=usage_mod.Ledger()
        )
    assert cache.has("actual_extraction", "k"), "an entry dropped for a bug of ours is an entry paid for twice"


def test_a_stored_answer_that_no_longer_validates_is_dropped_and_re_read(tmp_path: Path) -> None:
    """The recovery this catch is actually for: a release tightened a validator under an entry
    taken before it, and leaving it in place would wedge the review behind bytes that can never
    pass again."""
    from rein import review_cache

    cache = review_cache.StageCache(tmp_path)
    cache.write("actual_extraction", "k", "{}", usage_mod.Usage.unavailable())
    attempts: list[str] = []

    def run(_reviewer: Any) -> str:
        attempts.append("call")
        if len(attempts) == 1:
            raise review_policy.ReviewPolicyError("a field this release requires is absent")
        return "fresh"

    class _Reviewers:
        def for_role(self, role: str) -> Any:
            return lambda request: review_policy.Answer("{}")

        def spend(self) -> dict[str, usage_mod.Usage]:
            return {}

    assert (
        review_reading.cached_stage(
            cache, "actual_extraction", "k", set(), run, _Reviewers(), reused=usage_mod.Ledger()
        )
        == "fresh"
    )
    assert attempts == ["call", "call"], "the stale entry was dropped and the stage ran for real"
