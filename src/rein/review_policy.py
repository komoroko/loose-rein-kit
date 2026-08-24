"""The Policy Engine: it decides risk, coverage, and blocking — the reviewers only propose.

Every reviewer is untrusted output (plan §12.7). An LLM extractor can claim a
behavior it never grounded, mark its own finding non-blocking, or quietly lower a change's
risk so it slips under the evidence bar. This module is the boundary that refuses all three,
mechanically, before any reviewer text reaches a human:

  effective risk   the max of every risk contributor (plan §13.5); an AI can never lower it,
                   so a diff that deletes a guard is at least `high` whatever the plan says.
  code anchors     a reviewer's "this happens at file:line" is checked against the *committed*
                   blob at that path — a fabricated or stale anchor is rejected (§12.7).
  self-attestation a reviewer may not set `integrity: verified` (that is derived from digests,
                   not claimed) nor clear a `blocking` flag the policy set (§24.2, §24.3).
  known ids only   every Claim/Task id a reviewer references must exist in the frozen plan —
                   an invented `C-999` is a fabricated citation, not evidence.
  independence     a critical review needs the Actual Extractor and the Comparator in distinct
                   groups; the same session answering both is not a second opinion (E2E-26).
  size/shape       output past the size, depth, or array-length caps is refused, not truncated.

Everything here is pure or read-only over the committed tree, so the whole policy is testable
against crafted-malicious reviewer payloads without running a model.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from rein import diff_facts as diff_facts_mod
from rein import digests, models, strict_yaml
from rein import repo as repo_mod

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


class Reviewer(Protocol):
    """A reviewer: given a JSON-serializable request, it returns raw output text.

    The output is *untrusted* — it is parsed strictly and validated by this module before any
    of it is believed. The real implementation runs an agent adapter in an OCI sandbox with a
    strict JSON stdin/stdout contract, a timeout, and process cleanup; a fake stands in for it
    in tests. Either way, nothing here trusts the text it returns.
    """

    def __call__(self, request: Mapping[str, Any]) -> str: ...


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
    a binary, a diff the caller had to truncate — and it is `high`. Otherwise the signal
    detector did scan those files line by line (it is language-neutral), so the gap inherits the
    highest risk any signal matched *inside* them, and a dependency change contributes `medium`
    because no lexical scan accounts for what the new versions do. This never lowers a risk some
    other contributor raised: `effective_risk` still takes the max over all eight.
    """
    manifest = facts.coverage
    if manifest.coverage_status == "sufficient":
        return "low"
    if manifest.truncated or not manifest.binary_semantics_analyzed:
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
    return tuple(str(value) for value in node)


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
    start, end = _int(anchor.get("start_line")), _int(anchor.get("end_line"))
    if start < 1 or end < start or end > total:
        return [f"anchor {path}: line range {start}-{end} is outside the file (1-{total})"]
    return []


def _int(value: object) -> int:
    return value if isinstance(value, int) else -1


# --- self-attestation the policy forbids (plan §24.2, §24.3) ------------------


def reject_self_attestation(claim_result: Mapping[str, Any]) -> list[str]:
    """A reviewer may not hand itself `integrity: verified` — integrity is derived, not claimed.

    `integrity.status = verified` is a fact the Policy Engine establishes from a matching code
    anchor digest. A claim result that arrives already carrying it
    is a reviewer grading its own homework (plan §2.3, §12.7).
    """
    integrity = claim_result.get("integrity")
    status = str(integrity.get("status")) if isinstance(integrity, Mapping) else ""
    if status == "verified":
        cid = claim_result.get("claim_id", "?")
        return [f"claim {cid}: a reviewer cannot self-report `integrity: verified` — it is derived from digests"]
    return []


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
    """A critical review needs the Actual Extractor and Comparator in distinct groups.

    Reusing one model session's output for both halves is not a second opinion (plan §12.4).
    Distinctness is enforced on the declared group string *and*, when present, the prompt
    digest — identical prompts to two named groups are still one observation (plan §12.4).
    """
    if not models.risk_at_least(effective, "critical"):
        return True, "independence is only required for a critical review"
    extractor = independence.get("actual_extractor")
    comparator = independence.get("comparator")
    if not isinstance(extractor, Mapping) or not isinstance(comparator, Mapping):
        return False, "a critical review must declare both actual_extractor and comparator groups"
    e_group, c_group = str(extractor.get("group", "")), str(comparator.get("group", ""))
    if not e_group or not c_group:
        return False, "a critical review must declare a non-empty group for each reviewer"
    if e_group == c_group:
        return False, (
            f"critical review is not independent: the Actual Extractor and Comparator share the group "
            f"{e_group!r} — reusing one session for both is not a second opinion"
        )
    e_prompt, c_prompt = str(extractor.get("prompt_digest", "")), str(comparator.get("prompt_digest", ""))
    if e_prompt and c_prompt and e_prompt == c_prompt:
        return False, "critical review reused one prompt for both reviewers — distinct groups, one observation"
    return True, "distinct independence groups"


# --- blocking (the gate-4 decision) -------------------------------------------


def coverage_blocks(review: models.Review, effective: str) -> list[str]:
    """Coverage insufficiency blocks a high/critical review (plan §13.4).

    At high/critical, a gap means "Extra Behavior: undeterminable", which cannot be waved
    through as zero (plan §2.4). Below that, the gap is recorded but does not block the gate.
    """
    if not models.risk_at_least(effective, "high"):
        return []
    if not review.coverage:
        return ["no coverage manifest — Extra Behavior is undeterminable, not zero, on a high/critical change"]
    gaps = [c for c in review.coverage if str(c.get("coverage_status")) != "sufficient"]
    if not gaps:
        return []
    return [
        f"coverage is insufficient for a {effective} change ({len(gaps)} diff partition(s)) — "
        "split the unreadable part out of this scope, or reduce the change's risk"
    ]


def blocking_reasons(review: models.Review, effective: str) -> list[str]:
    """Every mechanical reason this review cannot open gate 4, aggregated (plan §14, §15)."""
    reasons: list[str] = []
    for finding in review.blocking_security_findings:
        reasons.append(f"blocking security finding {finding.get('id', '?')}: {finding.get('attack_scenario', '')}")
    for gap in review.machine.get("gaps", []) if isinstance(review.machine.get("gaps"), list) else []:
        if isinstance(gap, Mapping) and gap.get("blocking") is True:
            reasons.append(f"blocking gap {gap.get('id', '?')} ({gap.get('kind', '?')})")
    for extra in review.extra_behaviors:
        if extra.get("blocking") is True:
            grounded = "" if extra.get("grounded") is True else ", ungrounded"
            reasons.append(f"blocking extra behavior {extra.get('id', '?')} ({extra.get('category', '?')}{grounded})")
    reasons += coverage_blocks(review, effective)
    return reasons
