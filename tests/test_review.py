"""review.py assembles the grounded machine review from validated pieces (plan §12, §17, §30).

The pure `assemble` is pinned without any of the machinery; `generate` is exercised end to end with
a fake reviewer over a real git repo (a single injected callable that answers each stage), proving
the wiring writes a schema-valid review.yaml and resets the human half.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from rein import (
    actual_extraction,
    common,
    conformance,
    diff_facts,
    event_chain,
    models,
    review,
    review_cache,
    review_policy,
    security_review,
)
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_config, make_plan, make_state, seed_repo


def test_assemble_is_schema_valid_and_counts_verdicts() -> None:
    binding = {
        "change_digest": "sha256:" + "a" * 64,
        "plan_digest": "sha256:" + "b" * 64,
        "environment_digest": "sha256:" + "c" * 64,
    }
    coverage = {
        "diff_digest": "sha256:" + "d" * 64,
        "analyzed_files": 2,
        "analyzed_bytes": 1024,
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
def test_what_the_change_requires_of_a_person_survives_the_schema_validated_write(review_repo: Path) -> None:
    """The section is assembled from a plan declaration and a blind reading, and it has to be a shape
    the review schema accepts — a section that only ever exists in memory proves nothing about the
    document a reviewer opens.
    """
    import json

    import yaml

    (review_repo / "db").mkdir()
    (review_repo / "db" / "schema.sql").write_text("create table users (id int);\n", encoding="utf-8")
    plan_path = review_repo / ".rein" / "plan.yaml"
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["tasks"][0]["operator_surface"] = [
        {"kind": "persistence", "name": "users", "paths": ["db/schema.sql"], "adr": "ADR-001"}
    ]
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "declare a surface")

    blob = _git(review_repo, "rev-parse", "HEAD:db/schema.sql").strip()

    def reviewer(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
        facts = request.get("deterministic_facts", {})
        if isinstance(facts, dict) and "signals" in facts:
            return json.dumps({"findings": []})
        return json.dumps(
            {
                "actual_statements": [
                    {
                        "id": "AST-001",
                        "statement": "the users table has one integer column",
                        "category": "persistence",
                        "confidence": "high",
                        "code_anchors": [
                            {
                                "path": "db/schema.sql",
                                "blob": "git-blob:" + blob,
                                "start_line": 1,
                                "end_line": 1,
                            }
                        ],
                    }
                ],
                "coverage": {},
            }
        )

    review.generate(repo_mod.Repo(review_repo), reviewer)
    stored = store_mod.Store(repo_mod.Repo(review_repo)).read_review()
    assert stored is not None
    section = stored.machine["brief"]["requirements_on_people"]
    # Declared and read out: a count, not a row — and the as-built view names the blob it can serve.
    assert section["as_declared"]["count"] == 1
    entry = section["as_declared"]["entries"][0]
    assert entry["adr"] == "ADR-001" and entry["statement_ids"] == ["AST-001"]
    assert [built["path"] for built in entry["as_built"]] == ["db/schema.sql"]


@pytest.mark.integration
def test_the_orientation_brief_is_derived_into_the_machine_half(review_repo: Path) -> None:
    """The brief is built inside `generate`, not when the pane asks for it.

    Recomputing it on read would show a brief about the working tree beside claims bound to
    `subject_head_sha` — two descriptions of different trees on one screen. Building it here also
    means it is schema-checked by the same write every other section goes through.
    """
    import yaml

    state_path = review_repo / ".rein" / "state.yaml"
    document = yaml.safe_load(state_path.read_text(encoding="utf-8"))
    document["tasks"] = {"T-001": {"status": "done", "evidence": {"steps": [{"name": "test"}]}}}
    state_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "task done")

    machine = review.generate(repo_mod.Repo(review_repo), _fake_reviewer)
    brief_section = machine["brief"]
    assert [row["task_id"] for row in brief_section["delivered"]] == ["T-001"]
    assert brief_section["verification"]["steps"] == 2
    # `check` was established for no task, so it is named; `test` was, so it is only counted.
    assert brief_section["verification"]["established_for_nothing"] == ["check"]
    # `make_config`'s default profiles are host ones, so the boundary reports what that really is.
    assert {row["network"] for row in brief_section["execution_boundary"]} == {"unconfined"}
    # It survived the schema-validated write, not only the in-memory assembly.
    stored = store_mod.Store(repo_mod.Repo(review_repo)).read_review()
    assert stored is not None and stored.machine["brief"] == brief_section


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


# -- the change the reviewers are allowed to read ------------------------------
#
# What used to be here pinned the *other* answer to the same question: the whole head-side body of
# every changed file, sent beside the diff under a character cap. Measured over one cycle of this
# repository that cap dropped 69% of what it meant to send, and what survived was each file's
# first 40 KB — for a large module, its docstring and its imports, with the changed functions not
# in it. The context now comes from the diff itself, where every byte of it is next to a change.


def _extract_request(seen: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
    """The extractor's request, keyed the way `_staged_reviewer` keys it.

    The security reviewer also gets a `diff` and also gets no Expected Model, and it answers on
    another thread — so anything less specific than this picks whichever request happened to be
    appended first.
    """
    return next(
        r
        for r in seen
        if "diff" in r and "expected_model" not in r and "signals" not in r.get("deterministic_facts", {})
    )


@pytest.mark.integration
def test_the_reviewers_get_the_code_around_the_hunk_not_only_the_hunk(review_repo: Path) -> None:
    """A hunk without its surrounding code cannot answer "was this guard removed or moved?"."""
    body = "".join(f"def before_{i}():\n    return {i}\n\n" for i in range(12))
    (review_repo / "src.py").write_text(body, encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")
    (review_repo / "src.py").write_text(body + "def charge():\n    return 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add charge")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    extract = _extract_request(seen)
    assert "def charge" in extract["diff"]
    # git's default three lines would stop at before_11; the widened one reaches further back.
    assert "def before_5" in extract["diff"]
    assert extract["deterministic_facts"]["context"]["context_lines"] == review.CONTEXT_LADDER[0]


def test_the_ladder_narrows_until_it_fits_and_says_so(review_repo: Path) -> None:
    """A reviewer must never read "the rest of this function is not here" as "there is no more"."""
    repo = repo_mod.Repo(review_repo)
    body = "".join(f"def before_{i}():\n    return {i}\n\n" for i in range(40))
    (review_repo / "src.py").write_text(body, encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")
    (review_repo / "src.py").write_text(body + "def charge():\n    return 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add charge")
    base = _git(review_repo, "rev-parse", "HEAD~1")
    files = diff_facts.analyze(review._diff(repo, base, "HEAD")).files

    widest = review._reviewable(repo, base, "HEAD", files, plain="", ceiling=10**9)
    assert widest.context_lines == review.CONTEXT_LADDER[0]
    assert "narrowed_from" not in widest.as_facts()

    # One byte less than the widest rung costs: the ladder has to step down, and say that it did.
    narrowed = review._reviewable(repo, base, "HEAD", files, plain="", ceiling=len(widest.text.encode("utf-8")) - 1)
    assert narrowed.context_lines < review.CONTEXT_LADDER[0]
    assert narrowed.as_facts()["narrowed_from"] == review.CONTEXT_LADDER[0]
    assert len(narrowed.text.encode("utf-8")) < len(widest.text.encode("utf-8"))


def test_a_change_too_big_for_the_narrowest_rung_is_still_reviewed(review_repo: Path) -> None:
    """`_refuse_over_budget` already passed on this diff; a second, quieter refusal here would
    leave the operator with a review that cannot be taken and no sentence saying why."""
    repo = repo_mod.Repo(review_repo)
    (review_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")
    base = _git(review_repo, "rev-parse", "HEAD~1")
    facts = diff_facts.analyze(review._diff(repo, base, "HEAD"))

    reviewable = review._reviewable(repo, base, "HEAD", facts.files, plain="the plain one", ceiling=1)
    assert reviewable.context_lines == review.PLAIN_CONTEXT
    assert reviewable.text == "the plain one"


@pytest.mark.integration
def test_the_reviewable_diff_never_carries_the_expected_model(review_repo: Path) -> None:
    """The extractor stays blind: the SSOT is not part of the change it is shown."""
    (review_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    extract = _extract_request(seen)
    assert "relevant_code" not in extract
    assert ".rein/plan.yaml" not in extract["diff"]


# -- the launch has nothing to read but its request ----------------------------
#
# The transport passed no `cwd`, so every stage inherited rein's — the repository root. An agent
# CLI reads its working directory, and that directory is where `AGENTS.md` explains the Expected
# Model and `.rein/plan.yaml` *is* the Expected Model. `assert_blind` guards the payload and could
# never have caught it, because the priming did not travel in the payload.


@pytest.mark.integration
def test_a_reviewer_is_launched_outside_the_repository(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_run(cmd: list[str], cwd: str | None = None, **kwargs: Any) -> tuple[int, str]:
        seen.append({"cwd": cwd, "listing": sorted(os.listdir(cwd)) if cwd else []})
        return 0, json.dumps({"actual_statements": [], "coverage": {}})

    monkeypatch.setattr(common, "run", fake_run)
    reviewer = review._adapter_reviewer(repo_mod.Repo(review_repo), "actual_extractor")
    reviewer({"diff": "", "deterministic_facts": {}})

    assert seen, "the transport never launched anything"
    where = seen[0]["cwd"]
    assert where and Path(where).resolve() != review_repo.resolve()
    assert seen[0]["listing"] == [], "the launch was given a directory with something in it"


def test_every_stage_carries_its_own_contract() -> None:
    """What the stage must produce used to be nowhere in the request, so the only thing telling it
    was whatever the CLI loaded from the working directory — which is the same thing that primed
    it. Cutting that directory is only honest if the question travels with the request."""
    extract = actual_extraction.build_request(
        trusted_base_sha="a" * 40, subject_head_sha="b" * 40, diff_text="d", deterministic_facts={}
    )
    compare = conformance.build_request(expected_model={"claims": []}, actual_statements=[], actual_digest="d")
    security = security_review.build_request(
        diff_text="d", deterministic_facts={}, trusted_base_sha="a" * 40, subject_head_sha="b" * 40
    )
    for request in (extract, compare, security):
        assert request["contract"].strip()


def test_the_extractor_contract_supplies_no_expectation() -> None:
    """The one stage whose whole value is that it has not seen the plan. Its own instructions are
    the last place a mention of one would be noticed.

    Checked against the module's own never-list rather than a hand-picked list of phrases, so this
    keeps testing the right thing when that list moves. Saying an expectation is *absent* is not
    supplying one, which is why the text may name the words it refuses to carry.
    """
    contract = actual_extraction.contract()
    words = set(re.findall(r"[a-z_]+", contract.lower()))
    supplied = {key for key in actual_extraction.FORBIDDEN_KEYS if key in words}
    assert supplied == set(), f"the extractor's own contract names {sorted(supplied)}"
    assert "actually does" in contract.lower()


def test_a_contract_names_only_vocabulary_the_schema_allows() -> None:
    """A contract is what a reviewer is told to produce and the schema is what refuses it, so the
    two cannot be separate lists — the failure is a whole stage's output rejected at the write.

    The expected side is read out of the schema *here*, by a second path, rather than by calling
    the same helper the contract calls: asserting a function agrees with itself proves nothing.
    """
    schema = models.schema("review")["$defs"]["machine"]["properties"]
    statement_categories = schema["actual_extraction"]["items"]["properties"]["category"]["enum"]
    finding_categories = schema["security"]["properties"]["findings"]["items"]["properties"]["category"]["enum"]

    assert set(actual_extraction.categories()) == set(statement_categories)
    assert set(security_review.categories()) == set(finding_categories)
    for category in statement_categories:
        assert category in actual_extraction.contract()
    for category in finding_categories:
        assert category in security_review.contract()
    for verdict in models.VERDICT_VALUES:
        assert verdict in conformance.contract()


def test_a_schema_path_that_is_not_an_enum_is_refused() -> None:
    """A mistyped path lands on a mapping and iterates its keys, so the contract would name a
    vocabulary nobody chose and every answer using it would be refused at the write — with nothing
    anywhere saying the list came from the wrong place."""
    with pytest.raises(review_policy.ReviewPolicyError, match="no enum at"):
        review_policy.review_schema_enum("actual_extraction", "items", "properties")


def test_each_stage_goes_to_the_adapter_configured_for_it(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The Actual Extractor and the Comparator must not be the same opinion (§12.4), and the whole
    of what keeps them apart is this routing: one callable per role, picked from the shape of the
    request. It had no test. A mix-up here does not fail — it produces a review that looks entirely
    valid and was written by one model playing both parts.
    """
    routed: list[str] = []

    def fake_adapter(
        repo: repo_mod.Repo,
        role: str = "code_reviewer",
        *,
        config: Any = None,
        spend: dict[str, int] | None = None,
    ) -> Any:
        def call(request: Mapping[str, Any]) -> str:
            routed.append(role)
            if spend is not None:
                spend[role] = spend.get(role, 0) + 1
            return _fake_reviewer(request)

        return call

    monkeypatch.setattr(review, "_adapter_reviewer", fake_adapter)
    spend: dict[str, int] = {}
    reviewer = review._staged_reviewer(repo_mod.Repo(review_repo), spend=spend)

    reviewer(
        actual_extraction.build_request(
            trusted_base_sha="a" * 40,
            subject_head_sha="b" * 40,
            diff_text="d",
            deterministic_facts={"coverage": {}, "risk_floor": "low", "files": []},
        )
    )
    reviewer(conformance.build_request(expected_model={"claims": []}, actual_statements=[], actual_digest="d"))
    reviewer(
        security_review.build_request(
            diff_text="d",
            deterministic_facts={"signals": [], "files": []},
            trusted_base_sha="a" * 40,
            subject_head_sha="b" * 40,
        )
    )

    assert routed == ["actual_extractor", "comparator", "security_reviewer"]
    assert set(spend) == set(routed)  # and the ledger is threaded through to every one of them


def test_a_reviewer_is_told_which_blocks_it_may_not_drop() -> None:
    """`run_security_review` refuses an answer that drops a previously blocking finding, and was
    refusing on knowledge the reviewer had never been given: the ids were a Python argument to the
    validator and nothing more. A regeneration with a blocker standing had to re-invent `SEC-001`
    by coincidence to pass a check whose own instruction is "resolve the finding and re-run"."""
    request = security_review.build_request(
        diff_text="d",
        deterministic_facts={},
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
        prior_blocking_ids=["SEC-001"],
    )
    assert request["prior_blocking"] == ["SEC-001"]
    assert "prior_blocking" in request["contract"]
    # Nothing to carry is not an empty list to explain: the key stays out of the request entirely.
    clean = security_review.build_request(
        diff_text="d", deterministic_facts={}, trusted_base_sha="a" * 40, subject_head_sha="b" * 40
    )
    assert "prior_blocking" not in clean


@pytest.mark.integration
def test_the_anchors_a_reviewer_needs_are_handed_over_not_looked_up(review_repo: Path) -> None:
    """Every anchor is validated against the committed blob and the file's line count. Both were
    the reviewer's to find out, which it could only do by reading the repository it was launched
    in — the same access this change removes."""
    (review_repo / "src.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    files = {entry["path"]: entry for entry in _extract_request(seen)["deterministic_facts"]["files"]}
    assert files["src.py"]["blob"] == f"git-blob:{_git(review_repo, 'rev-parse', 'HEAD:src.py')}"
    # Counted exactly as `review_policy.validate_anchor` counts it, so a range this permits is a
    # range that passes: two lines and the empty one a trailing newline leaves.
    assert files["src.py"]["lines"] == 3


@pytest.mark.integration
def test_a_product_file_called_plan_does_not_read_as_priming(review_repo: Path) -> None:
    """`assert_blind` walks the request for Expected-Model *keys*, so a path used as a mapping key
    is a filename being read as structure. A product with a root-level file called `plan` — or
    `claims`, `solution`, `rationale` — failed every review with "the extractor request carries
    Expected-Model keys ['plan']": a sentence about priming, describing a file nobody had primed
    anything with. The payload this replaced was keyed by path too and had the same hole."""
    (review_repo / "plan").write_text("a product file that happens to be called that\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add plan")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=base)
    listed = [entry["path"] for entry in _extract_request(seen)["deterministic_facts"]["files"]]
    assert "plan" in listed


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


# -- what the reviewers are handed is the product ------------------------------


def _budget_repo(root: Path, ceiling: int) -> Path:
    config = make_config()
    config["review_policy"] = {"budgets": {"max_diff_bytes": ceiling}}
    seed_repo(root, state=make_state(project="rv", phase="build"), plan=make_plan(), config=config)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")
    return root


@pytest.mark.integration
def test_the_ssot_is_not_in_the_diff_the_reviewers_read(review_repo: Path) -> None:
    """The review's subject and the reviewers' diff have to be answers about one thing.

    `change_digest` has always left `.rein/` out — as do the tree fingerprint and every task commit
    — while `_diff` handed the whole of it over as if a schema payload and an event log were code
    somebody wrote. A field report measured that at 27% of a normal cycle's diff, and it pushed the
    blind extractor's request past the model's hard context ceiling: a gate ④ that could not be
    produced at all.
    """
    seed = _git(review_repo, "rev-parse", "HEAD")
    (review_repo / "src.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (review_repo / ".rein" / "scratch.txt").write_text("SSOT-CHURN\n" * 200, encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "code beside bookkeeping")

    seen: list[Mapping[str, Any]] = []
    review.generate(repo_mod.Repo(review_repo), _capturing_reviewer(seen), base=seed)

    diffs = [str(request["diff"]) for request in seen if "diff" in request]
    assert diffs, "no stage was handed a diff at all"
    assert all("SSOT-CHURN" not in diff for diff in diffs), "the SSOT reached a reviewer"
    assert any("return 42" in diff for diff in diffs), "the code under review did not"


@pytest.mark.integration
def test_the_coverage_manifest_measures_the_product_not_the_bookkeeping(review_repo: Path) -> None:
    """Withheld is not the same as out of scope.

    A lockfile is *in* the change with its body folded, and the manifest goes on reporting it
    unread. `.rein/` is not in the change, so counting it here would be a coverage gap invented out
    of something no reviewer was ever meant to read — and `analyzed_bytes` is the actual the one
    byte-denominated budget is measured against, which would then be measuring the orchestrator.
    """
    base = _git(review_repo, "rev-parse", "HEAD")
    (review_repo / "src.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
    (review_repo / ".rein" / "scratch.txt").write_text("SSOT-CHURN\n" * 200, encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "code beside bookkeeping")

    machine = review.generate(repo_mod.Repo(review_repo), _fake_reviewer, base=base)

    head = _git(review_repo, "rev-parse", "HEAD")
    product = _git(review_repo, "diff", f"{base}..{head}", "--", ".", ":(exclude).rein")
    # `_git` strips, and the diff the manifest measured did not; compare on the payload instead.
    assert machine["coverage"]["analyzed_bytes"] == len((product + "\n").encode("utf-8"))
    assert not [entry for entry in machine["coverage"].get("generated_files", []) if ".rein" in entry["path"]]


@pytest.mark.integration
def test_a_diff_over_the_budget_is_refused_before_a_model_is_launched(tmp_path: Path) -> None:
    """`max_diff_bytes` was reachable only after the pipeline it makes impossible.

    The budget is measured at the freeze, off the finished manifest. So a change too big for the
    reviewer stages to run against never reached the sentence that says what to do about it: three
    launches were paid for, and what came back said "the adapter exited 1". It is the same wall
    either way — a diff over this limit cannot be frozen once generated — so refusing here removes
    the cost, not the option.
    """
    root = _budget_repo(tmp_path, 4096)
    seed = _git(root, "rev-parse", "HEAD")
    (root / "big.py").write_text("x = 1\n" * 4000, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a change nobody can hold at once")

    seen: list[Mapping[str, Any]] = []
    repo = repo_mod.Repo(root)
    with pytest.raises(review.ReviewError) as excinfo:
        review.generate(repo, _capturing_reviewer(seen), base=seed)

    assert seen == [], "a reviewer was launched for a review that could never be frozen"
    assert "max_diff_bytes" in str(excinfo.value)
    assert "/revise" in str(excinfo.value) and "review_policy.budgets" in str(excinfo.value)
    events, defects = event_chain.scan(repo.events)
    assert not defects, defects
    assert events[-1].event == "review_failed"
    assert events[-1].detail.get("stage") == "coverage"


@pytest.mark.integration
def test_a_diff_within_the_budget_still_generates(tmp_path: Path) -> None:
    """The refusal is a ceiling, not a new precondition: under it, nothing changed."""
    root = _budget_repo(tmp_path, 4096)
    seed = _git(root, "rev-parse", "HEAD")
    (root / "small.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "a change one person can hold")

    machine = review.generate(repo_mod.Repo(root), _fake_reviewer, base=seed)
    assert machine["status"] == "generated"


def test_a_failed_adapter_reports_what_it_said(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason was in hand and thrown away.

    `common.run` merges stderr into its output, so "Prompt is too long · the request is ~1,061,094
    tokens (limit 1,000,000)" was already there when the error was raised as `exited 1`. A field
    report of three identical failures was diagnosable only by wrapping the CLI in a logging shim.
    """
    said = "Prompt is too long · the request is ~1,061,094 tokens (limit 1,000,000)"
    monkeypatch.setattr(common, "run", lambda *a, **k: (1, said))
    call = review._adapter_reviewer(repo_mod.Repo(review_repo), "actual_extractor")

    with pytest.raises(review_policy.ReviewPolicyError) as excinfo:
        call({"diff": "diff --git a/x b/x\n"})
    assert "actual_extractor adapter exited 1" in str(excinfo.value)
    assert "Prompt is too long" in str(excinfo.value)


def test_an_adapter_that_said_nothing_is_reported_as_saying_nothing(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent failure must not read as a truncated one — the two call for different next moves."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (127, "   \n"))
    call = review._adapter_reviewer(repo_mod.Repo(review_repo), "comparator")

    with pytest.raises(review_policy.ReviewPolicyError) as excinfo:
        call({"expected_model": {}})
    assert "exited 127 and said nothing" in str(excinfo.value)


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


@pytest.mark.integration
def test_a_failed_extraction_does_not_wait_out_the_security_stage(review_repo: Path) -> None:
    """The failure that should surface in seconds used to wait out the sibling it could not cancel,
    so it was reported when the *discarded* call ended: measured 1m36s and 3m54s late across two
    runs of one cycle, each having paid in full for a security review nobody read.

    Pinned with a *real* subprocess on the security side, because that is the thing the fix acts on:
    `Future.cancel()` is a no-op on a running task, and `shutdown(wait=False, cancel_futures=True)`
    only moves the wait to interpreter exit. Nothing but killing the process ends this early.
    """
    started = threading.Event()
    child_pid: list[int] = []
    pid_file = review_repo / "security.pid"

    def reviewer(request: Mapping[str, Any]) -> str:
        facts = request.get("deterministic_facts")
        if isinstance(facts, dict) and "signals" in facts:
            started.set()
            common.run(
                [
                    sys.executable,
                    "-c",
                    f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); time.sleep(120)",
                ]
            )
            return "{}"
        assert started.wait(timeout=10), "the security stage never started"
        for _ in range(200):  # it has to be launched, not merely submitted, before we fail
            recorded = pid_file.read_text(encoding="utf-8").strip() if pid_file.exists() else ""
            if recorded.isdigit():  # the file exists before it is written; only a number counts
                child_pid.append(int(recorded))
                break
            time.sleep(0.05)
        raise review_policy.ReviewPolicyError("the actual_extractor adapter exited 1, saying: session limit")

    began = time.monotonic()
    with pytest.raises(review_policy.ReviewPolicyError, match="session limit"):
        review.generate(repo_mod.Repo(review_repo), reviewer)
    elapsed = time.monotonic() - began

    assert elapsed < 30, f"the extraction failure waited {elapsed:.0f}s for the stage it discarded"
    assert child_pid, "the security stage never got as far as a launch, so nothing was proved"
    # Reaped, not merely signalled: leaving the `with` joined the worker, which returned only once
    # `common.run` had collected the killed process — so this is a fact, not a race.
    with pytest.raises(OSError):  # ProcessLookupError: the launch was killed, not left running
        os.kill(child_pid[0], 0)


def test_the_review_is_the_same_document_whatever_the_stages_race(review_repo: Path) -> None:
    """Concurrency buys wall-clock time and must buy nothing else: the merge order and the event
    order are fixed, so two generations of the same HEAD assemble identically."""
    repo = repo_mod.Repo(review_repo)
    first = review.generate(repo, _fake_reviewer)
    second = review.generate(repo, _fake_reviewer, force=True)
    for machine in (first, second):
        machine["binding"].pop("generated_at", None)
    assert first == second


# --- waiting out a machine failure, and refusing to wait out the other kind ----
#
# `review generate` had no `--supervise`, so a session limit meant a human noticing and running the
# command again — each retry paying for the whole pipeline, which is what brings the *next* session
# limit closer.


def _adapter_failure(output: str, rc: int = 1) -> review_policy.AdapterFailure:
    return review_policy.AdapterFailure(f"the actual_extractor adapter exited {rc}", rc=rc, output=output)


def test_a_capacity_failure_is_worth_waiting_for() -> None:
    assert review._worth_waiting_for(_adapter_failure("You've hit your session limit · resets 3pm"))


def test_a_request_that_did_not_fit_is_not() -> None:
    """It classifies as transient — rightly, for a build that can relaunch cold — but this pipeline
    has no session to reset, so the same request would be the same size on every attempt."""
    assert not review._worth_waiting_for(_adapter_failure("API Error: Prompt is too long"))


def test_an_adapter_that_is_not_installed_is_not() -> None:
    assert not review._worth_waiting_for(_adapter_failure("could not run 'claude': ...", rc=127))


def test_supervise_runs_it_again_and_returns_the_review_that_worked(review_repo: Path) -> None:
    attempts: list[int] = []

    def flaky() -> Any:
        attempts.append(1)
        if len(attempts) == 1:

            def fail(request: Mapping[str, Any]) -> str:
                raise _adapter_failure("You've hit your session limit · resets 3pm")

            return fail
        return _fake_reviewer

    machine = review._generate_cli(
        repo_mod.Repo(review_repo),
        {},
        force=False,
        supervise=True,
        interval_sec=1,
        make_reviewer=flaky,
    )
    assert len(attempts) == 2
    assert machine["status"] == "generated"


def test_without_supervise_the_first_failure_is_the_answer(review_repo: Path) -> None:
    def fail(request: Mapping[str, Any]) -> str:
        raise _adapter_failure("You've hit your session limit · resets 3pm")

    with pytest.raises(review_policy.AdapterFailure):
        review._generate_cli(
            repo_mod.Repo(review_repo), {}, force=False, supervise=False, interval_sec=1, make_reviewer=lambda: fail
        )


def test_the_bytes_put_in_front_of_each_stage_are_reported() -> None:
    """Measured at the transport, because what this pipeline sends is what decides whether it can
    run at all — and an estimate reported as a measurement is the habit this codebase refuses."""
    assert review.spend_summary({}) == ""
    line = review.spend_summary({"actual_extractor": 4096, "security_reviewer": 2048})
    assert line.startswith("review: sent 6KiB over 2 stage(s)")
    assert line.index("actual_extractor") < line.index("security_reviewer")  # worst first


# --- a subject that has not moved is not re-read -------------------------------
#
# A field run recorded `review_generated` fifteen times in one cycle. Three reviewer stages were
# paid for each time to read the same bytes, and each one reset the human half — so the answers
# recorded against a change nothing had touched were discarded too.


def test_regenerating_an_unmoved_subject_calls_no_reviewer(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting)
    assert calls == []


def test_regenerating_an_unmoved_subject_appends_no_event(review_repo: Path) -> None:
    """A log that records commands run rather than changes made is a log nobody can aggregate."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    before = [e.event for e in store_mod.Store(repo).read_events()]
    review.generate(repo, _fake_reviewer)
    assert [e.event for e in store_mod.Store(repo).read_events()] == before


def test_regenerating_an_unmoved_subject_keeps_the_human_answers(review_repo: Path) -> None:
    """The expensive half of the waste: a reviewer part-way through gate ④ who ran the command
    again lost everything they had recorded, about a change that had not moved."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    store = store_mod.Store(repo)
    existing = store.read_review()
    assert existing is not None
    with store.transaction() as tx:
        tx.write("review", {**existing.raw, "human": {"status": "in_progress"}})
        tx.append("decision_recorded", cycle_id="demo-cycle")

    review.generate(repo, _fake_reviewer)
    after = store.read_review()
    assert after is not None and after.human_status == "in_progress"


def test_force_ignores_the_stored_answers_and_launches_every_stage(review_repo: Path) -> None:
    """The escape hatch says "read it again anyway", and pays the full price of saying so."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)

    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting, force=True)
    assert len(calls) == 3, "every stage is re-read, not just the ones whose inputs moved"


def test_a_re_reading_that_says_something_else_resets_the_human_half(review_repo: Path) -> None:
    """The reset follows the *document*, not the command.

    A regeneration whose reading changed is a different review and no prior answer speaks for it
    (plan §6.6). One whose reading came out identical is the same review, and resetting over it
    would discard answers about a document that still stands, which is what running the pipeline
    on every invocation used to do fifteen times in one cycle.
    """
    import json

    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    store = store_mod.Store(repo)
    existing = store.read_review()
    assert existing is not None
    with store.transaction() as tx:
        tx.write("review", {**existing.raw, "human": {"status": "in_progress"}})
        tx.append("decision_recorded", cycle_id="demo-cycle")

    def a_finding(request: Mapping[str, Any]) -> str:
        facts = request.get("deterministic_facts", {})
        if isinstance(facts, dict) and "signals" in facts:
            return json.dumps(
                {
                    "findings": [
                        {
                            "id": "SEC-001",
                            "severity": "high",
                            "category": "credential_exposure",
                            "attack_scenario": "the reviewer container could reach a host credential",
                            "blocking": True,
                        }
                    ]
                }
            )
        return _fake_reviewer(request)

    review.generate(repo, a_finding, force=True)
    after = store.read_review()
    assert after is not None and after.human_status == "not_started"
    assert [e.event for e in store.read_events()].count("review_generated") == 2


def test_a_task_promoted_after_the_review_re_derives_the_brief_without_a_reviewer(review_repo: Path) -> None:
    """A task moving to `done` moves no reviewer's input, and must move no reviewer.

    `change_digest` covers the committed tree minus `.rein/`, so a task promoted from
    `awaiting-evidence` after a human recorded what they saw moves none of the code digests. The
    orientation brief is derived from exactly that, and reusing across it served a document the
    repository had since contradicted. The repair was to add `tasks_digest` to a *pipeline-wide*
    reuse key, which fixed the brief by paying for three model launches to re-read code nobody had
    touched. The brief is now re-derived on every generation, and no stage's key mentions
    `state.yaml` at all.
    """
    import yaml

    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    assert "delivered" not in store_mod.Store(repo).read_review().machine["brief"]  # type: ignore[union-attr]

    path = review_repo / ".rein" / "state.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["tasks"] = {"T-001": {"status": "done", "evidence": {"steps": [{"name": "test"}]}}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting)
    after = store_mod.Store(repo).read_review()
    assert after is not None
    assert [row["task_id"] for row in after.machine["brief"]["delivered"]] == ["T-001"]
    assert calls == [], "the brief is derived in code; re-deriving it must launch nothing"


def test_editing_the_plan_re_runs_only_the_comparator(review_repo: Path) -> None:
    """The extractor has never seen a plan, and the security reviewer never will.

    One `subject` digest used to decide whether all three stages ran, so a one-word edit to a
    claim re-read the whole change twice over for an answer that could not depend on it.
    """
    import yaml

    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)

    path = review_repo / ".rein" / "plan.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["claims"][0]["statement"] = document["claims"][0]["statement"] + " (reworded)"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting)
    assert len(calls) == 1 and "expected_model" in calls[0]
    events = [e.event for e in store_mod.Store(repo).read_events()]
    assert events.count("comparison_generated") == 2
    assert events.count("actual_extraction_generated") == 1
    assert events.count("security_review_generated") == 1


def test_a_stage_that_succeeded_before_a_failure_is_not_paid_for_twice(review_repo: Path) -> None:
    """The pipeline writes nothing when a later stage fails, so an extraction measured at over six
    minutes was thrown away because the comparator came back malformed."""
    repo = repo_mod.Repo(review_repo)

    def broken_comparator(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            return "not json at all"
        return _fake_reviewer(request)

    with pytest.raises(review_policy.ReviewPolicyError):
        review.generate(repo, broken_comparator)

    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting)
    assert len(calls) == 1 and "expected_model" in calls[0], "only the stage that failed is re-run"


def test_a_completed_generation_keeps_only_the_answers_it_used(review_repo: Path) -> None:
    """No expiry and no knob: what a finished run did not use, it deletes."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    cache = review_repo / review_cache.CACHE_DIR
    assert len(list(cache.glob("*.json"))) == 3
    first_extraction = next(cache.glob("actual_extraction-*.json"))

    later = review_repo / "src" / "later.py"
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_text("VALUE = 2\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "another change")

    review.generate(repo, _fake_reviewer)
    assert not first_extraction.exists(), "the previous change's reading is gone, not accumulating"
    assert len(list(cache.glob("*.json"))) == 3


def test_a_stored_answer_that_no_longer_validates_is_dropped_and_re_read(review_repo: Path) -> None:
    """A release that tightens a validator must not wedge every open review behind bytes that can
    never pass again — and the re-read must be a real launch, not a silent pass."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    cache = review_repo / review_cache.CACHE_DIR
    poisoned = next(cache.glob("actual_extraction-*.json"))
    poisoned.write_text(json.dumps({"stage": "actual_extraction", "answer": "{}"}), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(request)

    review.generate(repo, counting)
    assert any("expected_model" not in c and "signals" not in c.get("deterministic_facts", {}) for c in calls)
    stored = json.loads(next(cache.glob("actual_extraction-*.json")).read_text(encoding="utf-8"))
    assert stored["answer"] != "{}", "the entry that could not be believed was replaced, not kept"


def test_a_moved_head_is_re_read(review_repo: Path) -> None:
    """The identity check is over the committed tree, so a commit is what makes it a new subject."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _fake_reviewer)
    later = review_repo / "src" / "later.py"
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_text("VALUE = 2\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "another change")

    review.generate(repo, _fake_reviewer)
    assert [e.event for e in store_mod.Store(repo).read_events()].count("review_generated") == 2


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


@pytest.mark.integration
def test_a_review_that_could_not_read_its_own_inputs_is_recorded_too(review_repo: Path) -> None:
    """The four SSOT reads sat outside the block that records a failure.

    So the failure that needs no reviewer at all — plan.yaml not parsing — was the one kind still
    leaving the log with nothing in it. state.yaml is read first so the event can name the cycle:
    an event that cannot is refused, which would make recording the failure a chain defect.
    """
    repo = repo_mod.Repo(review_repo)
    repo.plan.write_text("claims: [oh dear\n", encoding="utf-8")

    with pytest.raises(Exception) as caught:
        review.generate(repo, _fake_reviewer)
    assert not isinstance(caught.value, review.ReviewError)  # the parse error itself, not a wrapped one

    events, defects = event_chain.scan(repo.events)
    assert not defects, defects
    assert events[-1].event == "review_failed"
    assert events[-1].detail.get("stage") == "inputs"
    assert events[-1].cycle_id  # the log is queried per cycle; an event that names none is unfindable


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
