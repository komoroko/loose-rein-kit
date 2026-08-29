"""The Policy Engine: it decides risk, coverage, and blocking — the reviewers only propose.

Every reviewer is untrusted output (plan §12.7). An LLM extractor can claim a
behavior it never grounded, mark its own finding non-blocking, or quietly lower a change's
risk so it slips under the evidence bar. This module is the boundary that refuses all three,
mechanically, before any reviewer text reaches a human:

  effective risk   the max of every risk contributor (plan §13.5); an AI can never lower it,
                   so a diff that deletes a guard is at least `high` whatever the plan says.
  code anchors     a reviewer's "this happens at file:line" is checked against the *committed*
                   blob at that path — a fabricated or stale anchor is rejected (§12.7).
  integrity        derived here from the anchors a claim's citations rest on, never sent by a
                   reviewer (`derive_integrity`); nor may one clear a `blocking` flag the policy
                   set (§24.2, §24.3).
  known ids only   every Claim/Task id a reviewer references must exist in the frozen plan —
                   an invented `C-999` is a fabricated citation, not evidence.
  independence     a critical review needs the Actual Extractor and the Comparator in distinct
                   groups; the same session answering both is not a second opinion (E2E-26).
  size/shape       output past the size, depth, or array-length caps is refused, not truncated.

Everything here is pure or read-only over the committed tree, so the whole policy is testable
against crafted-malicious reviewer payloads without running a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from rein import common, digests, models, strict_yaml
from rein import diff_facts as diff_facts_mod
from rein import repo as repo_mod
from rein import usage as usage_mod

# Output caps (plan §12.7). A reviewer past these is refused — never silently truncated.
MAX_OUTPUT_BYTES = 512 * 1024
MAX_DEPTH = 12
MAX_ARRAY = 4096


class ReviewPolicyError(RuntimeError):
    """A reviewer's output violated the policy and cannot be trusted."""


class AdapterFailure(ReviewPolicyError):
    """A reviewer's adapter exited nonzero, so there is no output to judge at all.

    Its parent is about output that cannot be trusted; this is about a launch that produced none.
    The distinction is worth a type only because something has to decide whether waiting would
    help — `faults.classify_launch` answers that from `(rc, output)`, and the message string this
    used to be had already flattened both into prose.
    """

    def __init__(self, message: str, *, rc: int, output: str) -> None:
        super().__init__(message)
        self.rc = rc
        self.output = output


# --- the untrusted reviewer boundary ------------------------------------------


#: Which configured role answers for each reviewer stage. One map, because two modules read it:
#: the pipeline asks for the role its stage needs, and the transport builds a launcher for each
#: role in it. Written twice, they drift, and a drift here is a stage answered by the wrong
#: adapter — the §12.4 violation that does not fail, it just produces a review one model wrote
#: both halves of. The ledgers are keyed by role, not by stage, because that is what
#: `binding.independence` and the spend summary are keyed on.
STAGE_ROLE: Mapping[str, str] = {
    "actual_extraction": "actual_extractor",
    "comparison": "comparator",
    "security_review": "security_reviewer",
}


@dataclass(frozen=True)
class Answer:
    """One reviewer launch's whole result: what it said, and what saying it cost.

    A launch produces two facts and this used to be able to carry one. The cost went out of band
    instead — into a ledger dict threaded down through four layers and mutated from two threads,
    and *again* into a `contextvars.ContextVar` so that the cache entry could record which model
    answered. Two channels for one value, one of them ambient, and a flag at three call sites to
    keep the ambient one from charging a stage for a launch it did not make. Returning it is the
    whole fix.

    `usage` is unmeasured (`available=False`) for an adapter that reports no envelope, and for
    `review_cache.replay`, which launches nothing — the stored provenance of a replayed answer is
    read from its cache entry, not from here.
    """

    text: str
    usage: usage_mod.Usage = field(default_factory=usage_mod.Usage)


class Reviewer(Protocol):
    """A reviewer: given a JSON-serializable request, it answers.

    The answer is *untrusted* — it is parsed strictly and validated by this module before any
    of it is believed. The real implementation (`review_transport`) launches an agent CLI
    on the host, in an empty directory, with a strict JSON stdin/stdout contract and process
    cleanup; a fake stands in for it in tests, and `review_cache.replay` stands in for it when the
    same question was already answered. Either way, nothing here trusts the text it returns.

    Not a sandbox, and this docstring used to say it was. The OCI rule is about running
    *repository-derived* code (`executors`); a reviewer runs no repository code — it is handed the
    diff, and the empty working directory is what keeps it from reading anything else.
    """

    def __call__(self, request: Mapping[str, Any]) -> Answer: ...


class Reviewers(Protocol):
    """Every role's reviewer, and the running bill of what launching them has cost.

    The pipeline knows which stage it is running and therefore which role must answer it; it used
    to throw that away and hand one callable to all three, which then recovered the role by
    *sniffing the request shape* (`"expected_model" in request`). A role is not something to infer
    from a payload when the caller already has it.

    The ledger belongs to the transport because the transport is what pays — including for the
    launches no single stage owns (a shared reading's priming turn) and the ones that failed
    before returning anything. `spend` is read, never written, by the pipeline.
    """

    def for_role(self, role: str) -> Reviewer: ...

    def spend(self) -> dict[str, usage_mod.Usage]: ...


def parse_reviewer_output(raw: str, *, what: str = "reviewer output") -> dict[str, Any]:
    """Parse a reviewer's raw output strictly, then enforce the shape caps (plan §12.7).

    Strict JSON only — duplicate keys, NaN, and Infinity are refused at the parser (the same
    boundary every document crosses). A parse failure or an over-cap shape is a hard
    error: a reviewer that cannot speak the contract has said nothing, not something lenient.
    """
    try:
        document = strict_yaml.load_json_mapping(raw, what=what)
    except strict_yaml.StrictParseError as exc:
        raise ReviewPolicyError(f"{what}: unparseable ({exc})") from None
    problems = validate_shape(document, what=what)
    if problems:
        raise ReviewPolicyError("; ".join(problems))
    return document


# --- effective risk (plan §13.5) ----------------------------------------------


@dataclass(frozen=True)
class RiskInputs:
    """Every contributor to a change's effective risk. The max wins; an AI cannot lower it."""

    claim_risk: str = "low"
    task_risk: str = "low"
    domain_risk: str = "low"
    public_surface_risk: str = "low"
    external_side_effect_risk: str = "low"
    security_boundary_risk: str = "low"
    detector_risk_floor: str = "low"
    coverage_gap_risk: str = "low"

    def values(self) -> list[str]:
        return [
            self.claim_risk,
            self.task_risk,
            self.domain_risk,
            self.public_surface_risk,
            self.external_side_effect_risk,
            self.security_boundary_risk,
            self.detector_risk_floor,
            self.coverage_gap_risk,
        ]


def effective_risk(inputs: RiskInputs) -> str:
    """The highest of every risk contributor (plan §13.5)."""
    return models.max_risk(inputs.values())


def coverage_gap_risk(facts: diff_facts_mod.DiffFacts) -> str:
    """How much the part of a diff that went unread actually matters (plan §13.4).

    An `insufficient` manifest is a statement about *reading*, not about danger, and conflating
    the two closed a loop: the gap raised the effective risk, and the raised risk was what made
    the gap blocking, so a diff holding a single unreadable file had no way through gate ④ —
    scope split included, since splitting never removes the file.

    The gap is therefore worth what was in the files it covers. Nothing could be read at all —
    a binary — and it is `high`. Otherwise the signal
    detector did scan those files line by line (it is language-neutral), so the gap inherits the
    highest risk any signal matched *inside* them, and a dependency change contributes `medium`
    because no lexical scan accounts for what the new versions do. This never lowers a risk some
    other contributor raised: `effective_risk` still takes the max over all eight.
    """
    manifest = facts.coverage
    if manifest.coverage_status == "sufficient":
        return "low"
    if not manifest.binary_semantics_analyzed:
        return "high"
    unread = {entry.get("path", "") for entry in (*manifest.unsupported_files, *manifest.generated_files)}
    risks = [hit.risk for hit in facts.signals if hit.path in unread]
    if not manifest.dependency_semantics_analyzed:
        risks.append("medium")
    return models.max_risk(risks)


def risk_inputs_from_facts(
    facts: diff_facts_mod.DiffFacts, *, claim_risk: str = "low", task_risk: str = "low", domain_risk: str = "low"
) -> RiskInputs:
    """Assemble the risk inputs the detector can supply from a diff (plan §13.5).

    The plan/claim/task/domain contributions come from the frozen plan (passed in); the rest
    are read straight off the deterministic signals, so a security-boundary or side-effect
    change floors the risk regardless of how the change was described.
    """
    by_signal = {hit.signal: hit.risk for hit in facts.signals}
    coverage_gap = coverage_gap_risk(facts)
    return RiskInputs(
        claim_risk=claim_risk,
        task_risk=task_risk,
        domain_risk=domain_risk,
        public_surface_risk=by_signal.get("public_surface", "low"),
        external_side_effect_risk=by_signal.get("side_effect", "low"),
        security_boundary_risk=by_signal.get("security_boundary", "low"),
        detector_risk_floor=facts.risk_floor,
        coverage_gap_risk=coverage_gap,
    )


# --- output shape caps (plan §12.7) -------------------------------------------


def validate_shape(payload: object, *, what: str = "reviewer output") -> list[str]:
    """Refuse output past the byte, depth, or array-length caps. Refuse, never truncate."""
    problems: list[str] = []
    try:
        size = len(digests.canonical(payload))
    except digests.DigestError as exc:
        return [f"{what}: not canonically serializable ({exc})"]
    if size > MAX_OUTPUT_BYTES:
        problems.append(f"{what}: {size} bytes exceeds the {MAX_OUTPUT_BYTES}-byte cap")
    depth, array = _shape(payload)
    if depth > MAX_DEPTH:
        problems.append(f"{what}: nesting depth {depth} exceeds the {MAX_DEPTH} cap")
    if array > MAX_ARRAY:
        problems.append(f"{what}: an array of {array} exceeds the {MAX_ARRAY} cap")
    return problems


def _shape(node: object, depth: int = 0) -> tuple[int, int]:
    if isinstance(node, Mapping):
        results = [_shape(v, depth + 1) for v in node.values()]
        return _combine(depth, 0, results)
    if isinstance(node, list):
        results = [_shape(v, depth + 1) for v in node]
        return _combine(depth, len(node), results)
    return depth, 0


def _combine(depth: int, array: int, results: list[tuple[int, int]]) -> tuple[int, int]:
    max_depth = max([depth, *(d for d, _ in results)])
    max_array = max([array, *(a for _, a in results)])
    return max_depth, max_array


# --- known-id citations (plan §12.7) ------------------------------------------


def validate_citations(referenced: Iterable[str], known: Iterable[str], *, what: str = "reviewer output") -> list[str]:
    """Every id a reviewer cites must exist in the frozen plan — an invented id is a fabrication."""
    known_set = set(known)
    return [f"{what}: cites unknown id {rid!r} (not in the frozen plan)" for rid in sorted(set(referenced) - known_set)]


# --- the vocabulary a contract may name ---------------------------------------


def review_schema_enum(*path: str) -> tuple[str, ...]:
    """The enum `review.schema.json` holds at `path`, walked from `$defs.machine.properties`.

    Read from the schema rather than restated in prose, because the prose in question is what a
    reviewer is *told to produce* and the schema is what refuses it. Two lists would eventually
    disagree, and the way that failure shows up is a whole stage's output rejected at the write
    for naming a category somebody moved.

    The path is spelled out by the caller rather than guessed at from a section name: the sections
    are not the same shape (`actual_extraction` is an array, `security` is an object holding one),
    and a helper that inferred the difference would be one more thing to be wrong.
    """
    node: Any = models.schema("review")["$defs"]["machine"]["properties"]
    for key in path:
        node = node[key]
    if not isinstance(node, list):
        # Without this a mistyped path lands on a mapping and iterates its *keys*, so a contract
        # would name a vocabulary nobody chose and every answer using it would be refused at the
        # write — with nothing anywhere saying the list came from the wrong place.
        raise ReviewPolicyError(f"review.schema.json has no enum at {'.'.join(path)}")
    return tuple(str(value) for value in node)


def review_schema_pattern(name: str) -> str:
    """The regex `review.schema.json` holds at `$defs.<name>.pattern`.

    Read rather than restated for the same reason :func:`review_schema_enum` is: an id shape
    written down twice eventually disagrees, and the way that failure shows up is a whole stage
    rejected at the write for an id the stage itself had accepted.
    """
    node = models.schema("review")["$defs"].get(name, {})
    pattern = node.get("pattern") if isinstance(node, dict) else None
    if not isinstance(pattern, str):
        raise ReviewPolicyError(f"review.schema.json has no pattern at $defs.{name}")
    return pattern


def reject_duplicate_ids(entries: Iterable[Mapping[str, Any]], *, what: str) -> list[str]:
    """(problems) for a reviewer answer that minted the same id twice.

    Ids are the reviewer's, and everything downstream resolves references by them: the comparator
    is validated against a `set` of the extractor's ids, and `findings` indexes statements by id
    to find their anchors. A duplicate silently collapses in the first and takes the last writer in
    the second, so an extra behaviour ends up anchored — and attributed to a task — by a statement
    nobody cited.
    """
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in entries:
        eid = str(entry.get("id", ""))
        if eid in seen and eid not in duplicates:
            duplicates.append(eid)
        seen.add(eid)
    return [
        f"{what}: id {eid!r} was used more than once — ids are how everything else refers to this" for eid in duplicates
    ]


# --- code anchors (plan §12.7) ------------------------------------------------


def validate_anchor(repo: repo_mod.Repo, commit: str, anchor: Mapping[str, Any]) -> list[str]:
    """(problems) for one code anchor: path safe, blob matches the committed one, lines in range.

    A fabricated anchor ("this happens at client.py:81") or a stale blob (the code moved after
    the review) is the load-bearing lie a grounded review has to catch — the anchor is checked
    against the actual committed blob, not taken on faith.
    """
    path = str(anchor.get("path", ""))
    if not models.is_repo_path(path):
        return [f"anchor path {path!r} is not a safe repo-relative path"]
    blob_claim = str(anchor.get("blob", ""))
    rc, blob_out = repo._git_rc("rev-parse", f"{commit}:{path}")
    actual_blob = blob_out.strip()
    if rc != 0 or not actual_blob:
        return [f"anchor {path}@{commit[:12]}: no such committed blob (a fabricated or stale anchor)"]
    if blob_claim and blob_claim.removeprefix("git-blob:") != actual_blob:
        return [f"anchor {path}: blob {blob_claim} does not match the committed {actual_blob} (stale or forged)"]
    rc, content = repo._git_rc("show", f"{commit}:{path}")
    if rc != 0:
        return [f"anchor {path}@{commit[:12]}: cannot read the committed blob"]
    total = content.count("\n") + 1
    start, end = common.as_int(anchor.get("start_line"), -1), common.as_int(anchor.get("end_line"), -1)
    if start < 1 or end < start or end > total:
        return [f"anchor {path}: line range {start}-{end} is outside the file (1-{total})"]
    return []


# --- self-attestation the policy forbids (plan §24.2, §24.3) ------------------


def derive_integrity(
    repo: repo_mod.Repo,
    commit: str,
    statement_ids: Sequence[str],
    anchors_by_statement: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, Any]:
    """One claim's `integrity` axis, established from the anchors its citations rest on.

    "Integrity is derived from digests, never claimed" was the rule, and **nothing derived it**.
    The comparator's contract offered `verified` as the first of three legal values, a validator
    refused it unconditionally for every claim that used it, and the only status any review could
    ever carry was `unavailable` — an axis that existed as a slot. One field report: sixteen of
    eighteen claims refused, three launches and 727,272 cache-creation tokens discarded, gate ④
    unreachable, per run.

    What it means here:

    - `verified` — the claim cites at least one Actual Statement, and every code anchor of every
      cited statement still resolves to the committed blob at `commit`. The verdict rests on code
      that is really in the tree the review is bound to.
    - `failed` — some cited anchor does not. Within one generation the extractor validated those
      same anchors against this same commit, so this means the tree moved underneath a pipeline
      whose reading stages run for minutes at a time. Rare, and exactly the sort of thing a review
      must not go quiet about.
    - `unavailable` — the claim cites no Actual Statement, so there is nothing to check. That is
      what an unanswered claim gets, and it is a fact rather than a verdict.

    `code_anchor_digest` binds the answer to the anchors it was taken over, so a later reader can
    tell which evidence this status was about.
    """
    anchors = [dict(a) for sid in statement_ids for a in anchors_by_statement.get(sid, ())]
    if not anchors:
        return {"status": "unavailable"}
    failed = any(validate_anchor(repo, commit, anchor) for anchor in anchors)
    return {
        "status": "failed" if failed else "verified",
        "code_anchor_digest": digests.of(anchors),
    }


def reject_risk_downgrade(claimed_risk: str, floor: str, *, subject: str = "change") -> list[str]:
    """Reject any risk a reviewer set below the effective-risk floor (plan §13.5)."""
    if not models.risk_at_least(claimed_risk, floor):
        return [f"{subject}: risk {claimed_risk!r} is below the effective floor {floor!r} — an AI cannot lower it"]
    return []


def reject_blocking_removal(subject_id: str, reviewer_blocking: object, policy_blocking: bool) -> list[str]:
    """A reviewer may not clear a `blocking` flag the policy set (plan §12.7)."""
    if policy_blocking and reviewer_blocking is False:
        return [f"{subject_id}: a reviewer cannot clear a blocking flag the policy requires"]
    return []


# --- independence (plan §12.4, E2E-26) ----------------------------------------


def independence_ok(independence: Mapping[str, Any], effective: str) -> tuple[bool, str]:
    """A critical review needs the Actual Extractor and Comparator on distinct models.

    Checked **before** the comparator launches, which is the only moment a misconfiguration can be
    refused without paying for it. The group is `<adapter>/<model>` and is derived from what the
    role is launched with, so two distinct groups now mean two distinct launches — they used to
    mean two distinct strings beside launches that were identical, because nothing passed a model
    anywhere. A pair that declared `claude/opus` and `claude/sonnet` ran one model twice and passed
    this check on the strength of the labels.

    An empty group is a role with no model named, taking the CLI's default: two of those are the
    same launch, so at critical they are refused rather than assumed to differ (plan §2.4).

    What this cannot see is what actually answered — a provider that silently served a different
    model than it was asked for. :func:`independence_observed` is that half, checked at the gate
    against what the launches reported.
    """
    if not models.risk_at_least(effective, "critical"):
        return True, "independence is only required for a critical review"
    extractor = independence.get("actual_extractor")
    comparator = independence.get("comparator")
    if not isinstance(extractor, Mapping) or not isinstance(comparator, Mapping):
        return False, "a critical review must declare both actual_extractor and comparator groups"
    e_group, c_group = str(extractor.get("group", "")), str(comparator.get("group", ""))
    if not e_group or not c_group:
        return False, (
            "a critical review must name a model for the actual_extractor and the comparator — "
            "without one each takes the CLI's default, which is the same launch twice"
        )
    if e_group == c_group:
        return False, (
            f"critical review is not independent: the Actual Extractor and Comparator share the group "
            f"{e_group!r} — reusing one session for both is not a second opinion"
        )
    return True, "distinct independence groups"


def independence_observed(review: models.Review, effective: str) -> list[str]:
    """What the launches *reported* having used, checked against what the config asked for.

    The pre-launch check reads the configuration; this reads the receipt. A provider serving a
    different model than it was told to — a fallback under load, a deprecated alias — would leave
    the configuration saying two opinions and one model having given both, and only the launch's
    own report can tell. Silent on a review that recorded no observation: an adapter that does not
    report usage cannot be held to a measurement it never took, and the declared check above has
    already refused a critical pair that could not differ.
    """
    if not models.risk_at_least(effective, "critical"):
        return []
    seen = review.binding.get("independence")
    if not isinstance(seen, Mapping):
        return []
    models_by_role = {
        role: str(entry.get("model", ""))
        for role, entry in seen.items()
        if isinstance(entry, Mapping) and entry.get("model")
    }
    extractor, comparator = models_by_role.get("actual_extractor"), models_by_role.get("comparator")
    if extractor and comparator and extractor == comparator:
        return [
            f"critical review is not independent: the Actual Extractor and Comparator were both "
            f"answered by {extractor!r}, whatever the configuration asked for"
        ]
    return []


# --- blocking (the gate-4 decision) -------------------------------------------


def coverage_blocks(review: models.Review, effective: str) -> list[str]:
    """Coverage insufficiency blocks a high/critical review (plan §13.4).

    At high/critical, a gap means "Extra Behavior: undeterminable", which cannot be waved
    through as zero (plan §2.4). Below that, the gap is recorded but does not block the gate.
    """
    if not models.risk_at_least(effective, "high"):
        return []
    manifest = review.coverage
    if not manifest:
        return ["no coverage manifest — Extra Behavior is undeterminable, not zero, on a high/critical change"]
    if str(manifest.get("coverage_status")) == "sufficient":
        return []
    return [
        f"coverage is insufficient for a {effective} change — split the unreadable part out of "
        "this scope, or reduce the change's risk"
    ]


def disputed_subjects(human: Mapping[str, Any]) -> set[str]:
    """Subjects a human recorded `dispute_finding` against — "the reviewer is wrong", with a reason.

    The one way a security finding the code did *not* change can stop blocking. `dispute_finding`
    is already in the schema's disposition list and was already offered on every decision card; it
    simply had no effect on the gate, so a finding with no anchors — nothing for
    `security_review.resolution_of` to re-check — had no exit at all. This is not "accept the
    risk", which the card deliberately does not offer: it is a human saying the finding is not
    true, on the record, in a document a gate receipt binds.
    """
    return {
        str(entry.get("subject_id", ""))
        for entry in human.get("dispositions", []) or []
        if isinstance(entry, Mapping) and str(entry.get("action", "")) == "dispute_finding"
    }


def blocking_reasons(review: models.Review, effective: str, human: Mapping[str, Any] | None = None) -> list[str]:
    """Every mechanical reason this review cannot open gate 4, aggregated (plan §14, §15)."""
    disputed = disputed_subjects(human if human is not None else review.human)
    reasons: list[str] = []
    for finding in review.blocking_security_findings:
        if str(finding.get("id", "")) in disputed:
            continue
        reasons.append(f"blocking security finding {finding.get('id', '?')}: {finding.get('attack_scenario', '')}")
    for gap in review.machine.get("gaps", []) if isinstance(review.machine.get("gaps"), list) else []:
        if isinstance(gap, Mapping) and gap.get("blocking") is True:
            reasons.append(f"blocking gap {gap.get('id', '?')} ({gap.get('kind', '?')})")
    for extra in review.extra_behaviors:
        if extra.get("blocking") is True:
            grounded = "" if extra.get("grounded") is True else ", ungrounded"
            reasons.append(f"blocking extra behavior {extra.get('id', '?')} ({extra.get('category', '?')}{grounded})")
    reasons += coverage_blocks(review, effective)
    reasons += independence_observed(review, effective)
    return reasons
