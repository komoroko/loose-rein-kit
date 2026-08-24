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
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from rein import repo as repo_mod
from rein import review_policy

SEVERITY_VALUES = frozenset({"low", "medium", "high", "critical"})


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
        "about this same base. Dropping one, or re-filing it as non-blocking, is you clearing your "
        "own block and it is refused: re-state it while the code still has it, and leave it out "
        "only once the change resolves it.\n"
        "- An empty `findings` list is a real answer. Say it rather than inventing something."
    )


def build_request(
    *,
    diff_text: str,
    deterministic_facts: Mapping[str, Any],
    trusted_base_sha: str,
    subject_head_sha: str,
    prior_blocking_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """The security reviewer's input: the change, widened around each hunk, and the signals.

    `prior_blocking_ids` is in the request because :func:`run_security_review` refuses an answer
    that drops one — and it was refusing on knowledge the reviewer had never been given. The ids
    were a Python argument to the validator and nothing more, so a regeneration with a blocker
    standing had to re-invent `SEC-001` by coincidence to get past a check whose own docstring says
    "resolve the finding and re-run". A constraint that cannot be seen is not a constraint, it is a
    trap; the review it fails is one nobody could have passed.
    """
    request: dict[str, Any] = {
        "contract": contract(),
        "trusted_base_sha": trusted_base_sha,
        "subject_head_sha": subject_head_sha,
        "diff": diff_text,
        "deterministic_facts": dict(deterministic_facts),
    }
    prior = [str(fid) for fid in prior_blocking_ids if str(fid)]
    if prior:
        request["prior_blocking"] = prior
    return request


@dataclass(frozen=True)
class SecurityResult:
    """The validated security findings and whether any of them blocks the gate."""

    findings: tuple[dict[str, Any], ...]

    @property
    def blocking(self) -> tuple[dict[str, Any], ...]:
        return tuple(f for f in self.findings if f.get("blocking") is True)

    def to_section(self) -> dict[str, Any]:
        return {"findings": [dict(f) for f in self.findings]}


def prior_blocking_of(request: Mapping[str, Any]) -> list[str]:
    """The blocking findings this request carries forward (`build_request`)."""
    carried = request.get("prior_blocking")
    return [str(fid) for fid in carried] if isinstance(carried, list) else []


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

    Each finding also records the base and head it was found against. A finding is a statement
    about a change, and until now nothing in the document said which one.
    """
    prior_blocking_ids = prior_blocking_of(request)
    document = review_policy.parse_reviewer_output(reviewer(request), what="security review")
    raw = document.get("findings")
    if not isinstance(raw, list):
        raise SecurityReviewError("security review: `findings` must be a list")

    first_seen = {
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
        problems += review_policy.reject_blocking_removal(fid, finding.get("blocking"), fid in set(prior_blocking_ids))
        if finding.get("blocking") is True:
            still_blocking.add(fid)
        findings.append({**dict(finding), "first_seen": {k: v for k, v in first_seen.items() if v}})

    dropped = sorted(set(prior_blocking_ids) - still_blocking)
    if dropped:
        problems.append(
            f"the review dropped previously blocking finding(s) {dropped} — a reviewer cannot clear its own block; "
            "resolve the finding and re-run, or it stays blocking"
        )

    if problems:
        raise SecurityReviewError("security review rejected:\n" + "\n".join(f"  - {p}" for p in problems))
    return SecurityResult(findings=tuple(findings))


def _validate_finding(finding: Mapping[str, Any], *, repo: repo_mod.Repo, commit: str) -> list[str]:
    problems: list[str] = []
    fid = str(finding.get("id", "?"))
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
