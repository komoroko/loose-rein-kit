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
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from rein import (
    actual_extraction,
    adapters,
    common,
    conformance,
    diff_facts,
    digests,
    event_chain,
    models,
    review,
    review_cache,
    review_policy,
    review_transport,
    security_review,
)
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, seed_repo


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


class _reviewers:  # noqa: N801 — reads as a factory at every call site
    """The `Reviewers` the pipeline takes, from a fake that answers with raw JSON for a named role.

    The role is given, not inferred: the fakes used to recover it from the request shape because
    the transport did, and both were reading a payload for something the caller already knew.

    `charge` is what each answered launch reports as its cost, the way the real transport's ledger
    is filled — so a test can ask what a run paid without the pipeline having a ledger passed into
    it. A fake that raises is charged nothing, which is the launch that never answered.
    """

    def __init__(
        self,
        answer: Callable[[str, Mapping[str, Any]], str],
        *,
        charge: usage_mod.Usage | None = None,
    ) -> None:
        self._answer = answer
        self._charge = charge
        self._spent: dict[str, usage_mod.Usage] = {}

    def for_role(self, role: str) -> review_policy.Reviewer:
        def call(request: Mapping[str, Any]) -> review_policy.Answer:
            text = self._answer(role, request)
            if self._charge is None:
                return review_policy.Answer(text)
            usage_mod.merged(self._spent, role, self._charge)
            return review_policy.Answer(text, self._charge)

        return call

    def spend(self) -> dict[str, usage_mod.Usage]:
        return dict(self._spent)


def _fake_reviewer(role: str, request: Mapping[str, Any]) -> str:
    """One callable answering every stage minimally-but-validly."""
    import json

    if role == "comparator":  # echo the digest it was handed, no claims
        return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
    if role == "security_reviewer":
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
    machine = review.generate(repo, _reviewers(_fake_reviewer))
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

    def reviewer(role: str, request: Mapping[str, Any]) -> str:
        if role == "comparator":
            return json.dumps({"claims": [], "actual_digest": request["actual_digest"]})
        if role == "security_reviewer":
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

    review.generate(repo_mod.Repo(review_repo), _reviewers(reviewer))
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

    machine = review.generate(repo_mod.Repo(review_repo), _reviewers(_fake_reviewer))
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

    def reviewer(role: str, request: Mapping[str, Any]) -> str:
        if role == "comparator":
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
        if role == "security_reviewer":
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

    machine = review.generate(repo_mod.Repo(review_repo), _reviewers(reviewer))
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
    review.generate(repo, _reviewers(_fake_reviewer))
    review.complete(repo)  # no challenges, no blockers → freezes
    stored = store_mod.Store(repo).read_review()
    assert stored is not None and stored.human_status == "frozen"


# -- what the comparator is actually handed ------------------------------------


def _capturing_reviewer(seen: list[Mapping[str, Any]]) -> Any:
    def reviewer(role: str, request: Mapping[str, Any]) -> str:
        seen.append(request)
        return _fake_reviewer(role, request)

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
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=base)
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
    files = diff_facts.analyze(review._diff(repo, base, "HEAD", (repo_mod.SSOT_DIR,))).files

    widest = review._reviewable(repo, base, "HEAD", files, (repo_mod.SSOT_DIR,), plain="", ceiling=10**9)
    assert widest.context_lines == review.CONTEXT_LADDER[0]
    assert "narrowed_from" not in widest.as_facts()

    # One byte less than the widest rung costs: the ladder has to step down, and say that it did.
    narrowed = review._reviewable(
        repo, base, "HEAD", files, (repo_mod.SSOT_DIR,), plain="", ceiling=len(widest.text.encode("utf-8")) - 1
    )
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
    facts = diff_facts.analyze(review._diff(repo, base, "HEAD", (repo_mod.SSOT_DIR,)))

    reviewable = review._reviewable(
        repo, base, "HEAD", facts.files, (repo_mod.SSOT_DIR,), plain="the plain one", ceiling=1
    )
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
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=base)
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
        return 0, agent_envelope(json.dumps({"actual_statements": [], "coverage": {}}))

    monkeypatch.setattr(common, "run", fake_run)
    reviewer = review_transport._adapter_reviewer(
        repo_mod.Repo(review_repo), "actual_extractor", ledger=usage_mod.Ledger()
    )
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
    of what keeps them apart is this routing: one reviewer per role, asked for by name. It used to
    be one callable that recovered the role from the shape of the request. A mix-up here does not
    fail — it produces a review that looks entirely valid and was written by one model playing
    both parts.
    """
    routed: list[str] = []

    def fake_adapter(
        repo: repo_mod.Repo,
        role: str,
        *,
        config: Any = None,
        ledger: usage_mod.Ledger,
        reading: review_transport.SharedReading | None = None,
    ) -> Any:
        def call(request: Mapping[str, Any]) -> review_policy.Answer:
            routed.append(role)
            ledger.add(role, usage_mod.Usage.unavailable())
            return review_policy.Answer(_fake_reviewer(role, request))

        return call

    monkeypatch.setattr(review_transport, "_adapter_reviewer", fake_adapter)
    reviewers = review_transport.StagedReviewers(repo_mod.Repo(review_repo))

    for role in ("actual_extractor", "comparator", "security_reviewer"):
        reviewers.for_role(role)({"actual_digest": "d"})

    assert routed == ["actual_extractor", "comparator", "security_reviewer"]
    # And the one ledger is what every role's launch is charged to.
    assert set(reviewers.spend()) == set(routed)


def test_a_role_the_pipeline_does_not_launch_is_not_answerable(review_repo: Path) -> None:
    """`for_role` is a lookup, not a factory: a role nobody configured a stage for has no
    reviewer, and inventing one on demand is how a fourth opinion would enter the review."""
    reviewers = review_transport.StagedReviewers(repo_mod.Repo(review_repo))
    with pytest.raises(KeyError):
        reviewers.for_role("implementer")


# --- one reading of the change, two verdicts -----------------------------------
#
# The extractor and the security reviewer are handed the same diff — up to `max_diff_bytes`, so up
# to half a megabyte — and were launched separately, each paying to read all of it. Measured on an
# 82 KB payload: two independent launches $0.2153, a priming turn plus two branches $0.1298.
#
# Serialising them into one session is not the answer. The second stage would read the first
# stage's conclusions, and catching what the extraction's frame missed is the security review's
# whole value. A fork shares the reading and not the readings.


def _record(name: str) -> Any:

    return adapters.ADAPTER_TABLE[name]


def _reading(ledger: usage_mod.Ledger | None = None) -> review_transport.SharedReading:
    return review_transport.SharedReading(
        repo_mod.Repo(Path(".")), _record("claude"), timeout=0.0, ledger=ledger or usage_mod.Ledger()
    )


def _a_reading_request(diff: str = "the diff") -> dict[str, Any]:
    return actual_extraction.build_request(
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
        diff_text=diff,
        deterministic_facts={"coverage": {}, "risk_floor": "low", "files": []},
    )


def _a_security_request(diff: str = "the diff") -> dict[str, Any]:
    return security_review.build_request(
        diff_text=diff,
        deterministic_facts={"signals": [], "files": []},
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
    )


def test_only_a_cli_that_can_branch_a_session_can_share_a_reading() -> None:
    """Resuming is not enough. A CLI that can only *continue* a session would hand the second stage
    the first stage's answer, which is the correlated blindness this exists to avoid."""

    assert _record("claude").forkable
    assert not _record("codex").forkable and not _record("gemini").forkable
    for adapter in adapters.ADAPTER_TABLE.values():
        assert not adapter.fork_flags or adapter.resumable, adapter.name


def test_two_roles_on_one_launch_share_a_reading_and_two_launches_do_not() -> None:
    """Decided on the argv the roles are actually launched with, not on the adapter's name and not
    on `independence_group` — nothing in the launcher varies by the group today."""
    roles = ("actual_extractor", "security_reviewer")
    same = models.Config.parse(json.dumps(make_config()))
    assert review_transport.shareable_reading(same, roles) is not None

    raw = make_config()
    raw["agents"]["security_reviewer"] = {"adapter": "codex"}
    split = models.Config.parse(json.dumps(raw))
    assert review_transport.shareable_reading(split, roles) is None

    raw = make_config()
    raw["agents"]["actual_extractor"] = {"adapter": "gemini"}
    raw["agents"]["security_reviewer"] = {"adapter": "gemini"}
    unforkable = models.Config.parse(json.dumps(raw))
    assert review_transport.shareable_reading(unforkable, roles) is None


def test_two_roles_on_different_models_do_not_share_a_reading() -> None:
    """The model is in the argv now, so it is in the comparison. A cache written by one model is
    not another's, and the reading would be paid for twice anyway."""
    raw = make_config()
    raw["agents"]["actual_extractor"] = {"adapter": "claude", "model": "opus"}
    raw["agents"]["security_reviewer"] = {"adapter": "claude", "model": "sonnet"}
    split = models.Config.parse(json.dumps(raw))
    assert review_transport.shareable_reading(split, ("actual_extractor", "security_reviewer")) is None


def test_the_shipped_scaffold_lets_the_two_diff_readers_share() -> None:
    """§12.4 constrains the extractor/comparator pair and says nothing about the security reviewer,
    so the scaffold puts it on the extractor's model — the two stages that read the same diff read
    it once. They still reach independent verdicts: the session is branched, never continued."""
    from rein import data, strict_yaml

    scaffold = models.Config(strict_yaml.load_mapping(data.read_text("scaffold/rein/config.yaml")))
    assert scaffold.model("actual_extractor") == scaffold.model("security_reviewer")
    assert scaffold.model("comparator") != scaffold.model("actual_extractor")
    assert review_transport.shareable_reading(scaffold, ("actual_extractor", "security_reviewer")) is not None


def test_a_branch_carries_a_pointer_where_the_reading_was(monkeypatch: pytest.MonkeyPatch) -> None:
    """One key moves. `deterministic_facts` is shared too, but the diff is ~98% of the payload and
    a request that exists in two shapes is a request that will drift."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(common, "run", _capturing_run(sent))
    reading = _reading()

    branched = reading.without_the_reading(_a_reading_request("A" * 500))
    assert branched["diff"] == {"in_previous_message": True, "digest": digests.of_bytes(b"A" * 500)}
    assert branched["contract"] == _a_reading_request()["contract"]
    assert len(sent) == 1 and sent[0]["diff"] == "A" * 500, "the priming turn carries the real reading"


def test_the_reading_precedes_every_volatile_field_in_a_request() -> None:
    """A prompt cache matches on an exact *prefix*, and JSON preserves insertion order.

    `subject_head_sha` sat immediately in front of 400–800 KB of diff, so a commit — any commit,
    including one that cannot touch the diff — invalidated the cache for the whole reading, in the
    two stages whose entire cost is that reading. The shared session's own docstring rests on the
    cache being hit; this is the ordering that lets it be.
    """
    for request in (_a_reading_request(), _a_security_request()):
        keys = list(request)
        assert keys.index("diff") < keys.index("subject_head_sha")
        assert keys.index("diff") < keys.index("trusted_base_sha")
        assert keys.index("diff") < keys.index("deterministic_facts")
        assert keys[0] == "contract", "the one field that never moves goes first"

    with_tests = security_review.build_request(
        diff_text="the diff",
        tests_diff="the tests",
        deterministic_facts={"signals": [], "files": []},
        trusted_base_sha="a" * 40,
        subject_head_sha="b" * 40,
    )
    keys = list(with_tests)
    assert keys.index("tests_diff") < keys.index("subject_head_sha"), "the other half of one reading"


def test_the_priming_turn_carries_nothing_volatile(monkeypatch: pytest.MonkeyPatch) -> None:
    """The prefix two branches share must be the same bytes on a re-run whose diff has not moved.

    The two shas were duplicated into it out of a request that keeps them anyway — a 40-character
    field changing on every commit, in the one turn whose whole purpose is to be a cache prefix.
    """
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(common, "run", _capturing_run(sent))
    _reading().branch_flags(_a_reading_request("A" * 500))
    moved = dict(_a_reading_request("A" * 500), subject_head_sha="c" * 40)
    _reading().branch_flags(moved)

    assert len(sent) == 2
    assert sent[0] == sent[1], "a commit that did not touch the diff rewrote the shared prefix"
    assert set(sent[0]) == {"instruction", "diff"}


def test_the_comparator_has_no_reading_to_share(monkeypatch: pytest.MonkeyPatch) -> None:
    """It is given the Actual and the Expected, never the code — and §12.4 forbids it sharing with
    the extractor in any case. Its request must pass through untouched and prime nothing."""
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(common, "run", _capturing_run(sent))
    reading = _reading()
    request = conformance.build_request(expected_model={"claims": []}, actual_statements=[], actual_digest="d")

    assert reading.without_the_reading(request) is request
    assert reading.branch_flags(request) == ()
    assert sent == [], "the comparator primed a reading it does not read"


def test_one_priming_turn_serves_both_stages(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []
    monkeypatch.setattr(common, "run", _capturing_run(sent))
    reading = _reading()

    first = reading.branch_flags(_a_reading_request())
    second = reading.branch_flags(_a_security_request())

    assert len(sent) == 1, "the reading was primed once, not once per stage"
    assert first == second, "both stages branch the same session"
    assert first[0] == "--resume" and first[-1] == "--fork-session"


def test_branching_one_session_about_two_different_changes_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A mismatch is the pipeline moving underneath the review, not something to paper over by
    sending the diff again."""
    monkeypatch.setattr(common, "run", _capturing_run([]))
    reading = _reading()
    reading.branch_flags(_a_reading_request("one change"))
    with pytest.raises(review_transport.TransportError, match="different changes"):
        reading.branch_flags(_a_security_request("another change"))


def test_the_priming_turn_is_held_to_the_same_blindness_as_the_extractor(monkeypatch: pytest.MonkeyPatch) -> None:
    """A new path into the extractor's context without the guard is how priming comes back."""
    monkeypatch.setattr(common, "run", _capturing_run([]))
    primed: list[Mapping[str, Any]] = []
    monkeypatch.setattr(actual_extraction, "assert_blind", lambda request: primed.append(request))
    _reading().branch_flags(_a_reading_request())
    assert primed and "diff" in primed[0]


def test_a_priming_turn_that_fails_stops_the_review_rather_than_falling_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A "prime failed, launch them separately" path would hide a broken adapter behind a bill
    twice the size; `--supervise` already waits out what time alone fixes."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (1, "Prompt is too long"))
    with pytest.raises(review_policy.AdapterFailure, match="could not be primed"):
        _reading().branch_flags(_a_reading_request())


def test_the_priming_turn_is_counted_under_a_name_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """It belongs to no stage — it is what both of them read. Folding it into either role would
    make that role's cost a fiction."""
    monkeypatch.setattr(common, "run", _capturing_run([]))
    ledger = usage_mod.Ledger()
    _reading(ledger).branch_flags(_a_reading_request())
    spend = ledger.totals()
    assert set(spend) == {"shared_reading"}
    assert spend["shared_reading"].launches == 1


def _capturing_run(sent: list[dict[str, Any]]) -> Any:
    def run(cmd: list[str], cwd: str | None = None, **kwargs: Any) -> tuple[int, str]:
        sent.append(json.loads(kwargs.get("input_text") or "{}"))
        return 0, agent_envelope(review_transport._PRIME_ACK)

    return run


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
        prior_blocking=[BLOCKING_FINDING],
    )
    assert request["prior_blocking"] == [BLOCKING_FINDING]
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
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=base)
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
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=base)
    listed = [entry["path"] for entry in _extract_request(seen)["deterministic_facts"]["files"]]
    assert "plan" in listed


@pytest.mark.integration
def test_change_digest_excludes_the_rein_dir(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    head = _git(review_repo, "rev-parse", "HEAD")
    before = review.change_digest(repo, head, (repo_mod.SSOT_DIR,))
    # A new file under .rein/ must not move the change digest (it is bound by its own digests).
    (review_repo / ".rein" / "scratch.txt").write_text("bound elsewhere\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "touch ssot")
    same = review.change_digest(repo, _git(review_repo, "rev-parse", "HEAD"), (repo_mod.SSOT_DIR,))
    assert same == before
    # A change to real source, on the other hand, does move it.
    (review_repo / "src.py").write_text("print('changed')\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "real change")
    moved = review.change_digest(repo, _git(review_repo, "rev-parse", "HEAD"), (repo_mod.SSOT_DIR,))
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
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=seed)

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

    machine = review.generate(repo_mod.Repo(review_repo), _reviewers(_fake_reviewer), base=base)

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
        review.generate(repo, _reviewers(_capturing_reviewer(seen)), base=seed)

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

    machine = review.generate(repo_mod.Repo(root), _reviewers(_fake_reviewer), base=seed)
    assert machine["status"] == "generated"


def test_a_failed_adapter_reports_what_it_said(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason was in hand and thrown away.

    `common.run` merges stderr into its output, so "Prompt is too long · the request is ~1,061,094
    tokens (limit 1,000,000)" was already there when the error was raised as `exited 1`. A field
    report of three identical failures was diagnosable only by wrapping the CLI in a logging shim.
    """
    said = "Prompt is too long · the request is ~1,061,094 tokens (limit 1,000,000)"
    monkeypatch.setattr(common, "run", lambda *a, **k: (1, said))
    call = review_transport._adapter_reviewer(repo_mod.Repo(review_repo), "actual_extractor", ledger=usage_mod.Ledger())

    with pytest.raises(review_policy.ReviewPolicyError) as excinfo:
        call({"diff": "diff --git a/x b/x\n"})
    assert "actual_extractor adapter exited 1" in str(excinfo.value)
    assert "Prompt is too long" in str(excinfo.value)


def test_an_adapter_that_said_nothing_is_reported_as_saying_nothing(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A silent failure must not read as a truncated one — the two call for different next moves."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (127, "   \n"))
    call = review_transport._adapter_reviewer(repo_mod.Repo(review_repo), "comparator", ledger=usage_mod.Ledger())

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
    assert review._prior_blocking(_stored_review(BASE_A), BASE_A) == [BLOCKING_FINDING]
    assert review._prior_blocking(None, BASE_A) == []


def test_a_blocking_finding_does_not_follow_the_review_onto_a_different_base() -> None:
    """A finding is a statement about a change. Change the base and it is about something else.

    Carried by id alone, a finding taken against base A kept blocking a regeneration against
    base B — a different diff, sometimes not even containing the code the finding named — and the
    only way past it was for the reviewer to re-assert something it could no longer see.
    """
    assert review._prior_blocking(_stored_review(BASE_A), BASE_B) == []
    # A review that never recorded a base cannot claim to be about this one either.
    assert review._prior_blocking(_stored_review(""), BASE_A) == []


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

    def reviewer(role: str, request: Mapping[str, Any]) -> str:
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
        return _fake_reviewer(role, request)

    machine = review.generate(repo_mod.Repo(review_repo), _reviewers(reviewer))

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

    def reviewer(role: str, request: Mapping[str, Any]) -> str:
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
        review.generate(repo_mod.Repo(review_repo), _reviewers(reviewer))
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
    first = review.generate(repo, _reviewers(_fake_reviewer))
    second = review.generate(repo, _reviewers(_fake_reviewer), force=True)
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

            def fail(role: str, request: Mapping[str, Any]) -> str:
                # Names no reset time on purpose: with one, the loop would wait until it, which is
                # the point of `_supervise_delay` and is tested there rather than by sleeping here.
                raise _adapter_failure("429 rate limit exceeded")

            return _reviewers(fail)
        return _reviewers(_fake_reviewer)

    machine, _ = review._generate_cli(
        repo_mod.Repo(review_repo),
        force=False,
        supervise=True,
        interval_sec=1,
        make_reviewers=flaky,
    )
    assert len(attempts) == 2
    assert machine["status"] == "generated"


def test_supervise_waits_until_the_reset_the_cli_named() -> None:
    """Extracting the time, printing it, and sleeping a fixed interval anyway learned nothing.

    Seven attempts, 900s apart, each refused in ~0.4s against a limit that lifted at 06:50 — and
    the host session ended before it did.
    """
    envelope = json.dumps(
        {
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your session limit \u00b7 resets 6:50am (Asia/Tokyo)",
            "type": "result",
            "duration_ms": 412,
        }
    )
    delay, hint = review._supervise_delay(_adapter_failure(envelope), interval_sec=900)

    assert delay > 900, "a fifteen-minute interval against a limit that lifts hours from now"
    assert delay <= review.MAX_SUPERVISE_SLEEP_SEC
    # The hint is the sentence, not a slice landing mid-token in the telemetry that follows it.
    assert hint == "resets 6:50am (Asia/Tokyo)"


def test_supervise_falls_back_to_the_interval_when_no_time_is_named() -> None:
    delay, hint = review._supervise_delay(_adapter_failure("429 rate limit exceeded"), interval_sec=900)
    assert (delay, hint) == (900, "")


def test_without_supervise_the_first_failure_is_the_answer(review_repo: Path) -> None:
    def fail(role: str, request: Mapping[str, Any]) -> str:
        raise _adapter_failure("You've hit your session limit · resets 3pm")

    with pytest.raises(review_policy.AdapterFailure):
        review._generate_cli(
            repo_mod.Repo(review_repo),
            force=False,
            supervise=False,
            interval_sec=1,
            make_reviewers=lambda: _reviewers(fail),
        )


def test_what_each_stage_cost_is_reported_in_tokens_not_in_bytes_on_stdin() -> None:
    """Bytes on stdin could not see the system prompt, the CLI's own project instructions, or the
    cache, so they answered "what did rein send" and never "what did this cost"."""
    assert usage_mod.summarize({}, what="review") == ""
    heavy = usage_mod.Usage(available=True, launches=1, input_tokens=40_000, output_tokens=900)
    light = usage_mod.Usage(available=True, launches=1, input_tokens=2_000, output_tokens=100)
    line = usage_mod.summarize({"actual_extractor": heavy, "security_reviewer": light}, what="review")
    assert line.startswith("review: 42.0k input + 1000 output tokens over 2 launch(es)")
    assert line.index("actual_extractor") < line.index("security_reviewer")  # worst first


def test_a_stage_whose_adapter_reports_nothing_is_named_rather_than_counted_as_free() -> None:
    line = usage_mod.summarize({"security_reviewer": usage_mod.Usage.unavailable()}, what="review")
    assert "no adapter here reports token usage" in line
    assert "usage unavailable for security_reviewer" in line


# --- a subject that has not moved is not re-read -------------------------------
#
# A field run recorded `review_generated` fifteen times in one cycle. Three reviewer stages were
# paid for each time to read the same bytes, and each one reset the human half — so the answers
# recorded against a change nothing had touched were discarded too.


def test_regenerating_an_unmoved_subject_calls_no_reviewer(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting))
    assert calls == []


def test_regenerating_an_unmoved_subject_appends_no_artefact_event(review_repo: Path) -> None:
    """A log that records commands run rather than changes made is a log nobody can aggregate.

    `run_measured` is excluded because it is not a claim about the artefact: it says a run happened
    and what it cost, which is true whether or not the document moved — and a regeneration that
    changed nothing after paying for three launches is exactly the run that must not go unmeasured.
    """
    repo = repo_mod.Repo(review_repo)

    def artefact_events() -> list[str]:
        return [e.event for e in store_mod.Store(repo).read_events() if e.event != "run_measured"]

    review.generate(repo, _reviewers(_fake_reviewer))
    before = artefact_events()
    review.generate(repo, _reviewers(_fake_reviewer))
    assert artefact_events() == before


def test_regenerating_an_unmoved_subject_keeps_the_human_answers(review_repo: Path) -> None:
    """The expensive half of the waste: a reviewer part-way through gate ④ who ran the command
    again lost everything they had recorded, about a change that had not moved."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    store = store_mod.Store(repo)
    existing = store.read_review()
    assert existing is not None
    with store.transaction() as tx:
        tx.write("review", {**existing.raw, "human": {"status": "in_progress"}})
        tx.append("decision_recorded", cycle_id="demo-cycle")

    review.generate(repo, _reviewers(_fake_reviewer))
    after = store.read_review()
    assert after is not None and after.human_status == "in_progress"


def test_force_ignores_the_stored_answers_and_launches_every_stage(review_repo: Path) -> None:
    """The escape hatch says "read it again anyway", and pays the full price of saying so."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))

    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting), force=True)
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
    review.generate(repo, _reviewers(_fake_reviewer))
    store = store_mod.Store(repo)
    existing = store.read_review()
    assert existing is not None
    with store.transaction() as tx:
        tx.write("review", {**existing.raw, "human": {"status": "in_progress"}})
        tx.append("decision_recorded", cycle_id="demo-cycle")

    def a_finding(role: str, request: Mapping[str, Any]) -> str:
        if role == "security_reviewer":
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
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(a_finding), force=True)
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
    review.generate(repo, _reviewers(_fake_reviewer))
    assert "delivered" not in store_mod.Store(repo).read_review().machine["brief"]  # type: ignore[union-attr]

    path = review_repo / ".rein" / "state.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["tasks"] = {"T-001": {"status": "done", "evidence": {"steps": [{"name": "test"}]}}}
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting))
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
    review.generate(repo, _reviewers(_fake_reviewer))

    path = review_repo / ".rein" / "plan.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["claims"][0]["statement"] = document["claims"][0]["statement"] + " (reworded)"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting))
    assert len(calls) == 1 and "expected_model" in calls[0]
    events = [e.event for e in store_mod.Store(repo).read_events()]
    assert events.count("comparison_generated") == 2
    assert events.count("actual_extraction_generated") == 1
    assert events.count("security_review_generated") == 1


def test_a_stage_that_succeeded_before_a_failure_is_not_paid_for_twice(review_repo: Path) -> None:
    """The pipeline writes nothing when a later stage fails, so an extraction measured at over six
    minutes was thrown away because the comparator came back malformed."""
    repo = repo_mod.Repo(review_repo)

    def broken_comparator(role: str, request: Mapping[str, Any]) -> str:
        if role == "comparator":
            return "not json at all"
        return _fake_reviewer(role, request)

    with pytest.raises(review_policy.ReviewPolicyError):
        review.generate(repo, _reviewers(broken_comparator))

    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting))
    assert len(calls) == 1 and "expected_model" in calls[0], "only the stage that failed is re-run"


def test_a_completed_generation_keeps_only_the_answers_it_used(review_repo: Path) -> None:
    """No expiry and no knob: what a finished run did not use, it deletes."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    cache = review_repo / review_cache.CACHE_DIR
    assert len(list(cache.glob("*.json"))) == 3
    first_extraction = next(cache.glob("actual_extraction-*.json"))

    later = review_repo / "src" / "later.py"
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_text("VALUE = 2\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "another change")

    review.generate(repo, _reviewers(_fake_reviewer))
    assert not first_extraction.exists(), "the previous change's reading is gone, not accumulating"
    assert len(list(cache.glob("*.json"))) == 3


def test_a_stored_answer_that_no_longer_validates_is_dropped_and_re_read(review_repo: Path) -> None:
    """A release that tightens a validator must not wedge every open review behind bytes that can
    never pass again — and the re-read must be a real launch, not a silent pass."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    cache = review_repo / review_cache.CACHE_DIR
    poisoned = next(cache.glob("actual_extraction-*.json"))
    poisoned.write_text(json.dumps({"stage": "actual_extraction", "answer": "{}"}), encoding="utf-8")

    calls: list[Mapping[str, Any]] = []

    def counting(role: str, request: Mapping[str, Any]) -> str:
        calls.append(request)
        return _fake_reviewer(role, request)

    review.generate(repo, _reviewers(counting))
    assert any("expected_model" not in c and "signals" not in c.get("deterministic_facts", {}) for c in calls)
    stored = json.loads(next(cache.glob("actual_extraction-*.json")).read_text(encoding="utf-8"))
    assert stored["answer"] != "{}", "the entry that could not be believed was replaced, not kept"


def test_a_moved_head_is_re_read(review_repo: Path) -> None:
    """The identity check is over the committed tree, so a commit is what makes it a new subject."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    later = review_repo / "src" / "later.py"
    later.parent.mkdir(parents=True, exist_ok=True)
    later.write_text("VALUE = 2\n", encoding="utf-8")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "another change")

    review.generate(repo, _reviewers(_fake_reviewer))
    assert [e.event for e in store_mod.Store(repo).read_events()].count("review_generated") == 2


@pytest.mark.integration
def test_a_review_that_could_not_be_produced_says_so_in_the_audit_log(review_repo: Path) -> None:
    """`events.ATTENTION_EVENTS` counts `actual_extraction_failed` and `review_failed` as things
    needing a human decision, and nothing anywhere emitted either — so every failure of gate ④'s
    own machinery left the log reporting "needing a human decision: 0".

    The extractor is the stage failed here because it is the one with an event of its own: its
    failure means no Actual was read out of the code at all.
    """

    def refusing(role: str, request: Mapping[str, Any]) -> str:
        if "expected_model" not in request and "signals" not in (request.get("deterministic_facts") or {}):
            raise actual_extraction.ExtractionError("the extractor returned prose, not a statement list")
        return _fake_reviewer(role, request)

    repo = repo_mod.Repo(review_repo)
    with pytest.raises(actual_extraction.ExtractionError):
        review.generate(repo, _reviewers(refusing))

    events, defects = event_chain.scan(repo.events)
    assert not defects, defects  # the failure path must leave the chain intact, not merely present
    # `run_measured` follows every run whatever its outcome; these are the artefact events.
    artefact = [e for e in events if e.event != "run_measured"]
    assert [e.event for e in artefact][-2:] == ["actual_extraction_failed", "review_failed"]
    assert artefact[-1].detail.get("stage") == "actual_extraction"
    assert "prose" in str(artefact[-1].detail.get("reason"))
    assert {"actual_extraction_failed", "review_failed"} <= events_mod.ATTENTION_EVENTS
    # Nothing was written: a review that failed must not leave a half-built machine half behind.
    stored = store_mod.Store(repo).read_review()
    assert stored is None or not stored.is_generated


@pytest.mark.integration
def test_a_comparison_failure_is_not_reported_as_a_failed_extraction(review_repo: Path) -> None:
    """Two different facts. The extractor failing means there is no Actual; the comparator failing
    means one exists and could not be held against the plan — and only the first has an event."""

    def refusing(role: str, request: Mapping[str, Any]) -> str:
        if role == "comparator":
            raise conformance.ComparatorError("the comparator cited a statement that does not exist")
        return _fake_reviewer(role, request)

    repo = repo_mod.Repo(review_repo)
    with pytest.raises(conformance.ComparatorError):
        review.generate(repo, _reviewers(refusing))

    # `run_measured` follows every run whatever its outcome, so the artefact events are what this
    # is about.
    kinds = [e.event for e in event_chain.scan(repo.events)[0] if e.event != "run_measured"]
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
        review.generate(repo, _reviewers(_fake_reviewer))
    assert not isinstance(caught.value, review.ReviewError)  # the parse error itself, not a wrapped one

    events, defects = event_chain.scan(repo.events)
    assert not defects, defects
    # A failure this early has no execution plan and billed nothing, so no run measurement follows.
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
    folded, paths = review.fold_bodies(_LOCKFILE_DIFF, files)

    assert paths == ["uv.lock"]
    assert "validate(request)" in folded, "the hand-written change must survive intact"
    assert 'version = "2"' not in folded, "the lockfile body was not withheld"
    assert "uv.lock" in folded, "the reader still has to be told the lockfile changed"
    assert "line(s) of mechanical change, body withheld" in folded


def test_a_deleted_files_body_is_withheld_and_named_as_a_deletion() -> None:
    """Reading a deleted function's body yields nothing "it is gone, it was N lines" does not say.

    One measured cycle sent 294 KB of a predecessor tool's deleted scaffolding to two opus stages,
    26.6% of the payload. The replacement says which fact it is standing for: a *deletion*, not a
    lockfile, because the two are withheld for different reasons and a reader is owed which.
    """
    files = diff_facts.parse_diff(_DELETION_DIFF)
    folded, paths = review.fold_bodies(_DELETION_DIFF, files)

    assert paths == ["scripts/agentloop/run.sh"]
    assert "validate(request)" in folded, "the hand-written change must survive intact"
    assert "legacy_entrypoint" not in folded, "the deleted body was not withheld"
    assert "scripts/agentloop/run.sh" in folded, "the reader still has to be told the file is gone"
    assert "@@ 3 line(s) removed with the file, body withheld @@" in folded, "the count is the removal"
    assert "mechanical" not in folded, "a deletion is not a mechanical change"


def test_a_removed_binary_keeps_the_one_line_that_says_it_was_binary() -> None:
    """Nothing to withhold, so nothing is: folding it would trade no bytes for that fact."""
    diff = (
        "diff --git a/fixtures/blob.bin b/fixtures/blob.bin\n"
        "deleted file mode 100644\n"
        "Binary files a/fixtures/blob.bin and /dev/null differ\n"
    )
    assert review.fold_bodies(diff, diff_facts.parse_diff(diff)) == (diff, [])


def test_a_change_with_nothing_to_withhold_is_passed_through_untouched() -> None:
    plain = "diff --git a/src/api.py b/src/api.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert review.fold_bodies(plain, diff_facts.parse_diff(plain)) == (plain, [])


def test_a_deletion_is_not_a_coverage_gap_and_not_a_read_file() -> None:
    """The manifest and the fold have to agree, or one of them is lying.

    `fold_bodies` withholds a deleted body, so the manifest may not call it analyzed; and a
    deletion is not unread either, since the path states the change in full. Recording it as
    unsupported shut gate ④ on a cycle whose only unreadable files had been *removed* — a block
    whose stated remedy ("split the unreadable part out of this scope") does not exist for a
    deletion.
    """
    facts = diff_facts.analyze(_DELETION_DIFF)
    manifest = facts.coverage.to_manifest()
    assert "unsupported_files" not in manifest, "a file that is gone is not a file that went unread"
    assert manifest["coverage_status"] == "sufficient"
    assert facts.coverage.analyzed_files == 1, "src/api.py, and not the file that is gone"


_DELETION_DIFF = """diff --git a/src/api.py b/src/api.py
index 1111111..2222222 100644
--- a/src/api.py
+++ b/src/api.py
@@ -1,2 +1,3 @@
 def handle():
+    validate(request)
     return ok()
diff --git a/scripts/agentloop/run.sh b/scripts/agentloop/run.sh
deleted file mode 100755
index 3333333..0000000
--- a/scripts/agentloop/run.sh
+++ /dev/null
@@ -1,3 +0,0 @@
-#!/bin/sh
-legacy_entrypoint "$@"
-exit 0
"""


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


# --- the priming turn's acknowledgement is a control signal, so it is checked (D3) ---------


def test_a_priming_turn_that_does_not_acknowledge_stops_the_review(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ack was asked for and thrown away. Whatever the model says in that turn is in *both*
    branches' context afterwards, so a model that answered by analysing the diff instead of acking
    would hand the extractor and the security reviewer one shared reading of it — the correlated
    blindness this class forks rather than continues in order to avoid, arriving through the door
    it opened. `actual_extraction.assert_blind` cannot see it: it guards the payload, not the
    answer."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (0, agent_envelope("This change adds a retry loop that…")))
    with pytest.raises(review_transport.TransportError, match="did not acknowledge"):
        _reading().branch_flags(_a_reading_request())


# --- who answered survives a cache hit (D2) -----------------------------------------------


@pytest.mark.integration
def test_a_reused_stage_keeps_the_model_that_answered_it(review_repo: Path) -> None:
    """`binding.independence` is read at the gate by `review_policy.independence_observed` — the
    half that catches a provider silently serving one model to both halves of a critical review.
    It is written from what the launches reported, so a stage served from the cache contributed
    nothing and the record went missing: the cache quietly stood a safety check down on exactly
    the runs it makes cheap. The model that answered is a property of the answer, not of who paid.
    """
    repo = repo_mod.Repo(review_repo)
    measured = _reviewers(_fake_reviewer, charge=usage_mod.Usage(available=True, launches=1, models=("claude-x",)))

    review.generate(repo, measured)
    first = store_mod.Store(repo).read_review()
    assert first is not None
    assert first.binding["independence"]["actual_extractor"]["model"] == "claude-x"

    # Second run: every stage answers from `.rein/work/`, so nothing is billed.
    again = _reviewers(_fake_reviewer, charge=usage_mod.Usage(available=True, launches=1, models=("claude-x",)))
    review.generate(repo, again)
    assert again.spend() == {}, "a run that launched nothing must not report a bill"
    stored = store_mod.Store(repo).read_review()
    assert stored is not None
    assert stored.binding["independence"]["actual_extractor"]["model"] == "claude-x"


def test_a_commit_that_cannot_change_the_payload_keeps_the_stage_keys() -> None:
    """A reviewer stage is not a function of HEAD's name.

    `change_digest` is taken over the committed tree with `not_the_product` applied, so it
    identifies the reviewed content exactly — and the diff, the file facts and the anchorable
    blobs all derive from it and the base. Keying on the sha as well meant a `.rein/`-only commit,
    a `docs/tasks/` edit or a `.gitignore` line threw away a half-megabyte opus extraction that
    could not have changed by a byte. Clearing one field report's first item took three such
    commits and paid for the extraction three times.
    """
    fixed = {
        "config": None,
        "change": "sha256:" + "1" * 64,
        "coverage_digest": "sha256:" + "2" * 64,
        "trusted_base": "b" * 40,
        "ceiling": 400_000,
        "risk_floor": "low",
        "prior_blocking": [],
    }
    assert review._stage_keys(**fixed) == review._stage_keys(**fixed)
    moved_content = review._stage_keys(**{**fixed, "change": "sha256:" + "3" * 64})
    assert moved_content != review._stage_keys(**fixed), "changed content must still miss"


def test_a_cache_entry_written_before_provenance_was_recorded_is_a_miss(tmp_path: Path) -> None:
    """Replaying it would put back exactly the hole this records, and there is nothing to migrate:
    `.rein/work/` is gitignored and dies with its worktree."""
    cache = review_cache.StageCache(tmp_path)
    path = cache._path("actual_extraction", "sha256:" + "a" * 64)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"stage": "actual_extraction", "answer": "{}"}), encoding="utf-8")
    assert cache.read("actual_extraction", "sha256:" + "a" * 64) is None


def test_an_entry_carries_the_launch_that_produced_it(tmp_path: Path) -> None:
    cache = review_cache.StageCache(tmp_path)
    spent = usage_mod.Usage(available=True, launches=1, input_tokens=7, cost_usd=0.5, models=("m",))
    cache.write("comparison", "sha256:" + "b" * 64, "{}", spent)
    entry = cache.read("comparison", "sha256:" + "b" * 64)
    assert entry is not None
    assert entry.answer == "{}"
    assert entry.usage.models == ("m",) and entry.usage.input_tokens == 7 and entry.usage.cost_usd == 0.5


def test_an_unmeasured_launch_stays_unmeasured_through_the_cache(tmp_path: Path) -> None:
    """ "We did not measure" and "it was free" must never render the same (usage.py)."""
    cache = review_cache.StageCache(tmp_path)
    cache.write("comparison", "sha256:" + "c" * 64, "{}", usage_mod.Usage.unavailable())
    entry = cache.read("comparison", "sha256:" + "c" * 64)
    assert entry is not None and entry.usage.available is False and entry.usage.launches == 1


# --- the run is the thing that ends (D5, D7) ----------------------------------------------


def _run_measurements(repo: repo_mod.Repo) -> list[Mapping[str, Any]]:
    return [dict(e.detail) for e in event_chain.load(repo.events) if e.event == "run_measured"]


@pytest.mark.integration
def test_a_run_that_changed_nothing_still_records_what_it_cost(review_repo: Path) -> None:
    """Cost was a field on `review_generated`, which made recording it conditional on the document
    moving. The two runs that most need measuring were therefore the two that recorded nothing: a
    regeneration whose machine half came out byte-identical (launches paid for, no event) and a
    failure."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    assert [d["outcome"] for d in _run_measurements(repo)] == ["generated"]

    # Force the launches to happen again over a document that will not move.
    paying = _reviewers(_fake_reviewer, charge=usage_mod.Usage(available=True, launches=1))
    review.generate(repo, paying, force=True)
    runs = _run_measurements(repo)
    assert [d["outcome"] for d in runs] == ["generated", "unchanged"]
    assert runs[-1]["billed_by_role"], "the launches this run paid for are on the record"


@pytest.mark.integration
def test_a_failed_run_records_what_it_cost_as_well_as_that_it_failed(review_repo: Path) -> None:
    repo = repo_mod.Repo(review_repo)

    def breaks(role: str, request: Mapping[str, Any]) -> str:
        if role == "comparator":
            raise review_policy.ReviewPolicyError("the comparator said nothing")
        return _fake_reviewer(role, request)

    with pytest.raises(review_policy.ReviewPolicyError):
        review.generate(repo, _reviewers(breaks, charge=usage_mod.Usage(available=True, launches=1)))
    measured = _run_measurements(repo)
    assert [d["outcome"] for d in measured] == ["failed"]
    assert measured[0]["billed_by_role"]["actual_extractor"]["launches"] == 1


@pytest.mark.integration
def test_the_run_records_the_plan_it_was_going_to_follow(review_repo: Path) -> None:
    """The run/reuse decision, the role and model per stage — all of it existed only as local
    variables, so the only way to know what a review was about to spend was to watch it spend it."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer))
    review.generate(repo, _reviewers(_fake_reviewer))

    plans = [d["plan"] for d in _run_measurements(repo)]
    first = {row["stage"]: row["decision"] for row in plans[0]["stages"]}
    assert first["actual_extraction"] == "run" and first["security_review"] == "run"
    # The comparator's key takes the Actual as an input, and the Actual does not exist yet.
    assert first["comparison"] == "undecided"
    second = {row["stage"]: row["decision"] for row in plans[1]["stages"]}
    assert second["actual_extraction"] == "reuse" and second["security_review"] == "reuse"


# --- adversarial: what the launch reports, and what it must not report ---------


def test_a_launch_that_failed_is_still_on_the_bill(review_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A launch that exited nonzero was paid for. The failure path is the only place that record
    can be made — a raise carries no return value, and this is why the transport keeps the ledger
    rather than handing the cost back to the pipeline."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (1, "the provider said no"))
    reviewers = review_transport.StagedReviewers(repo_mod.Repo(review_repo))

    with pytest.raises(review_policy.AdapterFailure):
        reviewers.for_role("comparator")({"expected_model": {}, "actual_digest": "d"})

    spent = reviewers.spend()
    assert spent["comparator"].launches == 1
    assert spent["comparator"].available is False  # counted, never priced


def test_the_priming_turn_is_not_charged_to_the_stage_that_triggered_it(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """It is what *both* reading stages read. Folding it into whichever of them happened to prime
    would make that stage's stored execution say it launched twice, and a later replay would
    report a cost the stage never had."""
    monkeypatch.setattr(common, "run", lambda *a, **k: (0, agent_envelope(review_transport._PRIME_ACK)))
    reviewers = review_transport.StagedReviewers(repo_mod.Repo(review_repo))

    answer = reviewers.for_role("actual_extractor")(_a_reading_request())

    # The Answer is what the cache entry is written from, so it carries this stage's launch only.
    assert answer.usage.launches == 1
    spent = reviewers.spend()
    assert spent["actual_extractor"].launches == 1
    assert spent["shared_reading"].launches == 1  # in the bill, under a name of its own


def test_a_run_that_reused_every_stage_records_what_it_did_not_pay_for(review_repo: Path) -> None:
    """`reused_by_role` is the other half of the measurement: a run whose bill is empty is not a
    run that did nothing, and the plan beside it says which stages it expected to replay."""
    repo = repo_mod.Repo(review_repo)
    review.generate(repo, _reviewers(_fake_reviewer, charge=usage_mod.Usage(available=True, launches=1)))
    review.generate(repo, _reviewers(_fake_reviewer, charge=usage_mod.Usage(available=True, launches=1)))

    second = _run_measurements(repo)[-1]
    assert second["outcome"] == "unchanged"
    assert "billed_by_role" not in second, "nothing was launched, so nothing was billed"
    assert set(second["reused_by_role"]) == {"actual_extractor", "comparator", "security_reviewer"}


def test_a_finding_that_stopped_blocking_is_recorded_where_it_outlives_the_document(
    review_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`review.yaml` holds one generation's findings and the next generation rewrites it, so a
    resolution recorded only there is gone the moment anyone regenerates. The chain never rotates,
    and a finding id needs no uniqueness across time there — nothing resolves a reference by it."""
    repo = repo_mod.Repo(review_repo)
    resolved = {
        "id": "SEC-001",
        "severity": "high",
        "category": "credential_exposure",
        "attack_scenario": "reaches a host cred",
        "blocking": False,
        "status": "resolved",
        "resolved_at": {"subject_head_sha": "f" * 40},
    }
    real = security_review.run_security_review

    def with_a_resolution(*args: Any, **kwargs: Any) -> security_review.SecurityResult:
        result = real(*args, **kwargs)
        return security_review.SecurityResult(findings=(*result.findings, resolved), resolved=(resolved,))

    monkeypatch.setattr(security_review, "run_security_review", with_a_resolution)
    review.generate(repo, _reviewers(_fake_reviewer))

    closed = [e for e in event_chain.load(repo.events) if e.event == "security_finding_resolved"]
    assert len(closed) == 1
    assert closed[0].subject_ids == ("SEC-001",)  # the finding is the event's subject, not a detail
    assert closed[0].detail["resolved_at"] == {"subject_head_sha": "f" * 40}


@pytest.mark.integration
def test_the_extractor_never_reads_the_tests_and_the_security_reviewer_does(review_repo: Path) -> None:
    """25% of the payload, and the wrong 25% for the stage whose value is that it read no plan.

    `test_render_grid_matches_expected_bins` is the requirement, restated. Sending it to the blind
    extractor is the same contamination as sending it the tickets, and it was the larger half.
    """
    (review_repo / "src.py").write_text("def charge(amount):\n    return amount\n", encoding="utf-8")
    (review_repo / "tests").mkdir(exist_ok=True)
    (review_repo / "tests" / "test_charge.py").write_text(
        "def test_charge_rejects_a_negative_amount():\n    assert True\n", encoding="utf-8"
    )
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add charge and its test")

    seen: list[Mapping[str, Any]] = []
    base = _git(review_repo, "rev-parse", "HEAD~1")
    review.generate(repo_mod.Repo(review_repo), _reviewers(_capturing_reviewer(seen)), base=base)

    extract = _extract_request(seen)
    assert "def charge" in extract["diff"]
    assert "test_charge_rejects_a_negative_amount" not in json.dumps(extract)

    security = next(r for r in seen if "signals" in r.get("deterministic_facts", {}))
    assert "def charge" in security["diff"], "the shared reading is the source half"
    assert "test_charge_rejects_a_negative_amount" in security["tests_diff"]
    # The halves are disjoint: the same bytes twice would undo the saving they were split for.
    assert "test_charge_rejects_a_negative_amount" not in security["diff"]


def test_splitting_a_diff_with_no_tests_costs_nothing() -> None:
    files = [diff_facts.DiffFile(path="src.py", hunks=())]
    assert review.split_tests("diff --git a/src.py b/src.py\n+x\n", files) == (
        "diff --git a/src.py b/src.py\n+x\n",
        "",
    )


def test_the_outlook_says_what_gate_4_would_be_asked_to_read(review_repo: Path) -> None:
    """Both of gate ④'s refusals are derivable from git at any moment, and both were first heard
    at gate ④ — where "split the scope" is not a move that exists, because everything is merged."""
    (review_repo / "src.py").write_text("x = 1\n", encoding="utf-8")
    (review_repo / "smoke.wav").write_bytes(b"RIFF\x00\x01\x02\x03binary")
    _git(review_repo, "add", "-A")
    _git(review_repo, "commit", "-qm", "add src and a committed binary")

    view = review.outlook(repo_mod.Repo(review_repo), base=_git(review_repo, "rev-parse", "HEAD~1"))
    assert view is not None
    assert view.unreadable == ("smoke.wav",), "one committed binary is what makes coverage insufficient"
    assert not view.over_budget
    assert "change under review:" in view.line() and "coverage insufficient" in view.line()


def test_an_over_budget_change_says_split_rather_than_raise(review_repo: Path) -> None:
    outlook = review.ChangeOutlook(diff_bytes=2_141_194, ceiling=524_288, unreadable=(), effective_risk="high")
    assert outlook.over_budget
    assert "OVER, split the scope" in outlook.line()


# --- what the payload is made of ----------------------------------------------


def _diff_of(*sized: tuple[str, int]) -> str:
    return "".join(
        f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1 +1 @@\n+{'x' * size}\n"
        for path, size in sized
    )


def test_the_payload_is_broken_down_by_the_kinds_the_levers_are_denominated_in() -> None:
    """The question a reader has once they know the payload is too big is *what it is made of*,
    and answering it took a hand-run script both times it mattered in the field. The categories are
    `classify_path`'s, not a new taxonomy: `test` is the half `split_tests` withholds from the
    blind extractor, `dependency`/`generated` are the mechanical kinds, `source` is the thing under
    review. A breakdown in categories nothing can act on would be trivia."""
    made = review.bytes_by_kind(_diff_of(("src/app.py", 400), ("tests/test_app.py", 200), ("uv.lock", 100)))
    assert list(made) == ["source", "test", "dependency"], "largest first"
    assert made["source"] > made["test"] > made["dependency"]


def test_no_byte_of_the_payload_escapes_the_breakdown() -> None:
    """A total that claims to be the payload must not quietly be a subset of it — including
    whatever git puts before the first `diff --git` header."""
    diff = "warning: something git said\n" + _diff_of(("src/app.py", 50), ("tests/test_app.py", 50))
    assert sum(review.bytes_by_kind(diff).values()) == len(diff.encode("utf-8"))


def test_the_split_and_the_breakdown_read_the_diff_the_same_way() -> None:
    """Two answers off one walk. A second copy of "find the header, attribute what follows" is how
    the two would come to disagree about what a file's bytes are."""
    diff = _diff_of(("src/app.py", 400), ("tests/test_app.py", 200))
    files = diff_facts.parse_diff(diff)
    source, tests = review.split_tests(diff, files)
    made = review.bytes_by_kind(diff)
    assert len(source.encode("utf-8")) == made["source"]
    assert len(tests.encode("utf-8")) == made["test"]


def test_all_product_code_names_nothing() -> None:
    """`made_of` answers "what would I remove", and "it is all product code" is the null answer to
    that. A board line that always ended in "made of: source 100%" would be noise."""
    one = review.ChangeOutlook(
        diff_bytes=100, ceiling=10, unreadable=(), effective_risk="low", composition=(("source", 100),)
    )
    assert one.made_of() == ""
    two = review.ChangeOutlook(
        diff_bytes=100, ceiling=10, unreadable=(), effective_risk="low", composition=(("source", 75), ("test", 25))
    )
    assert two.made_of() == "made of: source 0.1 KB (75%), test 0.0 KB (25%)"


def test_a_path_git_quoted_still_reaches_the_test_half(tmp_path: Path) -> None:
    """Against a real `git`, because what is under test is what git actually emits.

    `core.quotePath` defaults to true, so a name with a space or a non-ASCII byte arrives as
    `"b/na\\303\\257ve.py"`. A header pattern that only accepted `a/… b/…` did not fail on such a
    file — it did not see the header, so the file vanished from `parse_diff` (and from the Coverage
    Manifest and every scope check), and its lines were attributed to whichever file came before
    it. For this project that is the ordinary case: a deliverable is written in the user's
    language. The consequence was the one thing `split_tests` exists to prevent — a test file's
    assertions handed to the blind extractor.
    """

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/app.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/naïve テスト_test.py").write_text("assert f() == 1\n" * 20, encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "add")

    diff = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout
    assert 'diff --git "a/' in diff, "git no longer quotes; this test's premise is gone"

    files = diff_facts.parse_diff(diff)
    assert any("_test.py" in f.path for f in files), "the quoted file must exist for the manifest"

    source, tests = review.split_tests(diff, files)
    assert "assert f() == 1" not in source, "the blind extractor was handed the assertions"
    assert "assert f() == 1" in tests
    assert review.bytes_by_kind(diff)["test"] == len(tests.encode("utf-8"))


def test_a_change_that_is_all_lockfile_says_so(tmp_path: Path) -> None:
    """Silence on a single non-source kind was the bug with its sign reversed: 900 KB of lockfile
    and nothing else is where the answer to "what would I remove" is most obvious."""
    lockfile = review.ChangeOutlook(900_000, 500_000, (), "low", (("dependency", 900_000),))
    assert lockfile.made_of() == "made of: dependency 900.0 KB (100%)"
    # "It is all product code" stays silent: there is nothing to remove.
    assert review.ChangeOutlook(900_000, 500_000, (), "low", (("source", 900_000),)).made_of() == ""
