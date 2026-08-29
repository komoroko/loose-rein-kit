"""The structured Security Reviewer (plan §12.5): findings, not prose, and a hard blocking flag.

A security review is a list of `findings[]`, each with a severity, an attack scenario,
optional code anchors, and a `blocking` flag. Structured so the gate can act on it mechanically:
while any blocking finding stands, gate 4 does not open (plan §12.5), and no amount of reviewer
prose can wave it through — the Policy Engine reads the flag, not the paragraph.

Like every reviewer, the output is untrusted (plan §12.7): the severity must be a known value,
each code anchor is validated against the committed blob, and a fabricated finding id or an
oversize payload is refused. Because the previous review's blocking findings are carried in,
this module also refuses a regeneration that quietly drops a blocking finding without it being
resolved (a reviewer cannot clear its own block — plan §12.7).

**A finding has a life, and it does not end because a reviewer stopped mentioning it.** Until it
had one, a finding's only state was presence in the newest generated list, so "the change fixed
it" and "the reviewer forgot it" arrived as the same observation and the policy — correctly, given
what it could see — refused both. That left no way through at all: on a work branch the trusted
base does not move, so a blocking finding was carried forward for the life of the cycle and gate ④
became unreachable the moment one was filed. :func:`resolution_of` settles it on something neither
the reviewer nor this process can talk its way past: the anchors the finding itself named are
re-checked against the committed tree, and the finding is `resolved` only when none of them
resolves any more. A finding that named no anchor cannot be closed this way, and is left to a
human's `dispute_finding`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rein import repo as repo_mod
from rein import review_policy

SEVERITY_VALUES = frozenset({"low", "medium", "high", "critical"})

#: The shape a finding id must have, read from the schema that enforces it. Checked here as well
#: as at the write because everything in between refers to a finding by this id.
FINDING_ID_RE = re.compile(review_policy.review_schema_pattern("securityFindingId"))


class SecurityReviewError(RuntimeError):
    """The security review produced output that could not be trusted."""


def categories() -> tuple[str, ...]:
    """The finding categories, read from the schema that enforces them."""
    return review_policy.review_schema_enum(
        "security", "properties", "findings", "items", "properties", "category", "enum"
    )


def contract() -> str:
    """What the security reviewer is being asked for, carried *in* the request.

    Carried here for the same reason the other two stages' contracts are
    (`actual_extraction.contract`): the launch is given nothing else to read, so everything the
    answer needs — the anchors' blobs and line counts included — has to arrive with the question.
    """
    return (
        "Review the change below for security and for the ways it could be attacked. Findings, "
        "not prose: the gate reads the flag, never the paragraph.\n"
        "\n"
        "Answer with one JSON object and no other text:\n"
        '{"findings": [{"id": "SEC-001", '
        f'"severity": "<one of {"|".join(sorted(SEVERITY_VALUES))}>", '
        f'"category": "<one of {"|".join(categories())}>", '
        '"attack_scenario": "<who does what, and what they get>", "blocking": <bool>, '
        '"code_anchors": [{"path": "<repo-relative path>", "start_line": <int>, '
        '"end_line": <int>, "blob": "<the blob deterministic_facts.files gives for that path>"}]}]}\n'
        "\n"
        "Every rule below is checked, not trusted:\n"
        "- A finding must state an attack scenario. A category on its own says nothing anybody "
        "can act on.\n"
        "- `blocking` is an explicit boolean. While one blocking finding stands, gate 4 does not "
        "open, and no amount of explanation waves it through.\n"
        "- Anchors are verified against the committed tree. `deterministic_facts.files` lists a "
        "blob and a line count for every path in the change; use them.\n"
        "- `prior_blocking`, when present, lists findings a previous review recorded as blocking "
        "about this same base, each with the anchors it named. Re-state one while the code still "
        "has it; leave it out once the change resolves it. Which of those two you did is not taken "
        "on your word — the anchors are re-checked against the committed tree, and leaving out a "
        "finding whose code is still there is refused as you clearing your own block.\n"
        "- `tests_diff`, when present, is the test half of the same change, sent to you and to "
        "nobody else. Tests are code an agent wrote and they run with the operator's credentials, "
        "so review them as code: a fixture that reaches the network, a credential in a test "
        "constant, a helper that shells out. Anchor into them exactly as into anything else.\n"
        "- An empty `findings` list is a real answer. Say it rather than inventing something."
    )


def build_request(
    *,
    diff_text: str,
    tests_diff: str = "",
    deterministic_facts: Mapping[str, Any],
    trusted_base_sha: str,
    subject_head_sha: str,
    prior_blocking: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The security reviewer's input: the change, widened around each hunk, and the signals.

    `prior_blocking` is in the request because :func:`run_security_review` refuses an answer that
    drops one — and it was refusing on knowledge the reviewer had never been given. The ids were a
    Python argument to the validator and nothing more, so a regeneration with a blocker standing
    had to re-invent `SEC-001` by coincidence to get past a check whose own docstring says "resolve
    the finding and re-run". A constraint that cannot be seen is not a constraint, it is a trap;
    the review it fails is one nobody could have passed.

    Whole findings rather than ids, because the anchors are what decides whether a drop is a
    resolution or an omission (:func:`resolution_of`) — and because a reviewer told only "SEC-001
    was blocking" cannot re-state a finding it has no description of.
    """
    request: dict[str, Any] = {
        "contract": contract(),
        "trusted_base_sha": trusted_base_sha,
        "subject_head_sha": subject_head_sha,
        "diff": diff_text,
        "deterministic_facts": dict(deterministic_facts),
    }
    # The test half of the change, which only this stage is sent (`review.split_tests`). It rides
    # in its own key rather than concatenated into `diff` because `diff` is the reading both
    # reading stages share and prime one session with — and the blind extractor must not read a
    # test suite that paraphrases the requirements back to it.
    if tests_diff:
        request["tests_diff"] = tests_diff
    prior = [dict(finding) for finding in prior_blocking if str(finding.get("id", ""))]
    if prior:
        request["prior_blocking"] = prior
    return request


@dataclass(frozen=True)
class SecurityResult:
    """The validated security findings and whether any of them blocks the gate."""

    findings: tuple[dict[str, Any], ...]
    #: Findings this run closed: carried forward blocking, and the code they anchored to is gone.
    #: Named separately from `findings` because a resolution is an *event* — the caller records it
    #: in the audit chain, which is the only place it outlives this generation's document.
    resolved: tuple[dict[str, Any], ...] = ()

    @property
    def blocking(self) -> tuple[dict[str, Any], ...]:
        return tuple(f for f in self.findings if f.get("blocking") is True)

    def to_section(self) -> dict[str, Any]:
        return {"findings": [dict(f) for f in self.findings]}


def prior_blocking_of(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The blocking findings this request carries forward (`build_request`)."""
    carried = request.get("prior_blocking")
    if not isinstance(carried, list):
        return []
    return [dict(f) for f in carried if isinstance(f, Mapping) and str(f.get("id", ""))]


def _anchored_lines(repo: repo_mod.Repo, anchor: Mapping[str, Any]) -> list[str] | None:
    """The exact lines this anchor was read from, out of the blob it recorded. None if unreadable.

    Read from the *recorded* blob rather than from any commit, because that object is what the
    finding is a statement about — it is still reachable while the branch that carried it is, and
    when it is not, "cannot say" is the answer rather than a guess.
    """
    blob = str(anchor.get("blob", "")).removeprefix("git-blob:")
    if not blob:
        return None
    rc, content = repo._git_rc("cat-file", "-p", blob)
    if rc != 0:
        return None
    lines = content.splitlines()
    start, end = anchor.get("start_line"), anchor.get("end_line")
    if not isinstance(start, int) or not isinstance(end, int) or start < 1 or end < start or end > len(lines):
        return None
    return lines[start - 1 : end]


def resolution_of(
    finding: Mapping[str, Any],
    *,
    repo: repo_mod.Repo,
    commit: str,
) -> dict[str, Any] | None:
    """The `resolved` record for a carried-forward finding the change has closed, or None.

    The question a dropped finding raises is "is the code it named still there?", and the anchors
    already carry everything needed to answer it without asking anybody: the blob the lines were
    read from, and which lines. So the anchored text is fetched from that blob and looked for in
    the file at `commit`. Still present, verbatim and contiguous — the drop is the reviewer
    clearing its own block, and it is refused. Gone from every anchor — the finding is a statement
    about code this head does not have.

    Deliberately not `review_policy.validate_anchor`, which is the wrong question here: it fails
    the moment the file's blob differs, so *any* unrelated edit to the same file would read as a
    resolution. The text is what the finding was about.

    None also when the finding named no anchor at all, or when a blob is no longer reachable:
    there is nothing to re-check, this cannot say so, and an unanchored finding is closed by a
    human's `dispute_finding` or not at all.
    """
    anchors = finding.get("code_anchors")
    if not isinstance(anchors, list) or not anchors:
        return None
    for anchor in anchors:
        if not isinstance(anchor, Mapping):
            return None
        path = str(anchor.get("path", ""))
        lines = _anchored_lines(repo, anchor)
        if not path or lines is None:
            return None
        rc, content = repo._git_rc("show", f"{commit}:{path}")
        if rc != 0:
            continue  # the path is gone at this head — nothing of this anchor survives
        if _contains_run(content.splitlines(), lines):
            return None  # still there, verbatim — the code this finding named is untouched
    return {
        **{k: v for k, v in finding.items() if k != "blocking"},
        "blocking": False,
        "status": "resolved",
        "resolved_at": {"subject_head_sha": commit},
    }


def _contains_run(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """Does `haystack` contain `needle` as a contiguous run of lines?

    An empty needle would match anything, which would resolve every finding whose anchor pointed
    at nothing; it is treated as "still there" so that the refusal, not the resolution, is what an
    unanswerable anchor produces.
    """
    if not needle:
        return True
    span = len(needle)
    return any(list(haystack[i : i + span]) == list(needle) for i in range(len(haystack) - span + 1))


def run_security_review(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
) -> SecurityResult:
    """Run the security reviewer and validate its findings (plan §12.5, §12.7).

    The blocking findings the previous review recorded *about the same base* are read from the
    request, which is also where the reviewer reads them. They used to be a second argument to this
    function, and nothing made the two agree: the enforcement and the disclosure were separate
    facts, which is how the enforcement came to be applied to a reviewer that had never been shown
    them. One source, or eventually one of them is wrong.

    A regeneration that drops one, or that re-emits it with `blocking: false`, is a reviewer
    clearing its own block, and the policy refuses both — the second was the wider door: the check
    compared id sets, so re-listing `SEC-001` as non-blocking satisfied it exactly as well as
    fixing the finding did.

    What the refusal used to have no answer for is the finding the change *did* fix. Dropping it
    was refused just the same, so a blocking finding shut gate ④ for the rest of the cycle. A drop
    is now settled by :func:`resolution_of` against the committed tree: a finding whose anchored
    code is gone from this head is carried into the document as `status: resolved` — kept in this
    generation's findings, and named in `resolved` so the caller can record it in the audit chain,
    which is where it outlives the document. One whose code is still there is refused exactly as
    before.

    Each finding also records the base and head it was **first** found against. A finding is a
    statement about a change, and until now nothing in the document said which one.
    """
    prior_blocking = prior_blocking_of(request)
    prior_by_id = {str(f.get("id", "")): f for f in prior_blocking}
    document = review_policy.parse_reviewer_output(reviewer(request).text, what="security review")
    raw = document.get("findings")
    if not isinstance(raw, list):
        raise SecurityReviewError("security review: `findings` must be a list")

    this_change = {
        "trusted_base_sha": str(request.get("trusted_base_sha", "")),
        "subject_head_sha": str(request.get("subject_head_sha", "")),
    }
    problems: list[str] = []
    findings: list[dict[str, Any]] = []
    still_blocking: set[str] = set()
    for index, finding in enumerate(raw):
        if not isinstance(finding, Mapping):
            problems.append(f"findings[{index}] is not a mapping")
            continue
        problems += _validate_finding(finding, repo=repo, commit=commit)
        fid = str(finding.get("id", ""))
        problems += review_policy.reject_blocking_removal(fid, finding.get("blocking"), fid in prior_by_id)
        if finding.get("blocking") is True:
            still_blocking.add(fid)
        seen = _first_seen(prior_by_id.get(fid), this_change)
        findings.append({**dict(finding), "status": "open", "first_seen": seen})
    unresolved: list[str] = []
    closed: list[dict[str, Any]] = []
    for fid, prior in prior_by_id.items():
        if fid in still_blocking:
            continue
        resolved = resolution_of(prior, repo=repo, commit=commit)
        if resolved is None:
            unresolved.append(fid)
        else:
            findings.append(resolved)
            closed.append(resolved)
    if unresolved:
        problems.append(
            f"the review dropped previously blocking finding(s) {sorted(unresolved)} whose anchored code is "
            "still in the tree — a reviewer cannot clear its own block. Fix the code (the finding then closes "
            "itself), re-state the finding while it stands, or dispute it in the human review"
        )

    problems += review_policy.reject_duplicate_ids(findings, what="security review")

    if problems:
        raise SecurityReviewError("security review rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return SecurityResult(findings=tuple(findings), resolved=tuple(closed))


def _first_seen(prior: Mapping[str, Any] | None, this_change: Mapping[str, str]) -> dict[str, str]:
    """Where this finding was first found: the carried record when there is one, else this change.

    Stamping every generation with the head it was regenerated at made the field say the opposite
    of its name — a blocking finding that survived three regenerations reported the third head as
    where it was first seen. Continuity exists exactly where the pipeline carries a finding
    forward (`_prior_blocking`); a finding the reviewer re-derived from scratch has none, and this
    change is then the honest answer.
    """
    carried = (prior or {}).get("first_seen")
    if isinstance(carried, Mapping) and carried:
        return {str(k): str(v) for k, v in carried.items()}
    return {k: v for k, v in this_change.items() if v}


def _validate_finding(finding: Mapping[str, Any], *, repo: repo_mod.Repo, commit: str) -> list[str]:
    problems: list[str] = []
    fid = str(finding.get("id", "?"))
    if not FINDING_ID_RE.match(fid):
        problems.append(f"{fid!r} is not a security finding id — the shape is SEC-001, SEC-002, …")
    severity = str(finding.get("severity", ""))
    if severity not in SEVERITY_VALUES:
        problems.append(f"{fid}: severity {severity!r} is not one of {sorted(SEVERITY_VALUES)}")
    if not str(finding.get("attack_scenario", "")).strip():
        problems.append(f"{fid}: a finding must state an attack scenario, not just a category")
    if not isinstance(finding.get("blocking"), bool):
        problems.append(f"{fid}: `blocking` must be an explicit boolean")
    anchors = finding.get("code_anchors")
    if isinstance(anchors, list):
        for anchor in anchors:
            if isinstance(anchor, Mapping):
                problems += [f"{fid}: {p}" for p in review_policy.validate_anchor(repo, commit, anchor)]
    return problems
