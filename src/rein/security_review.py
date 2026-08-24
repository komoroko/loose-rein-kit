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


def build_request(
    *,
    diff_text: str,
    deterministic_facts: Mapping[str, Any],
    trusted_base_sha: str,
    subject_head_sha: str,
) -> dict[str, Any]:
    """The security reviewer's input: the change, widened around each hunk, and the signals."""
    return {
        "trusted_base_sha": trusted_base_sha,
        "subject_head_sha": subject_head_sha,
        "diff": diff_text,
        "deterministic_facts": dict(deterministic_facts),
    }


@dataclass(frozen=True)
class SecurityResult:
    """The validated security findings and whether any of them blocks the gate."""

    findings: tuple[dict[str, Any], ...]

    @property
    def blocking(self) -> tuple[dict[str, Any], ...]:
        return tuple(f for f in self.findings if f.get("blocking") is True)

    def to_section(self) -> dict[str, Any]:
        return {"findings": [dict(f) for f in self.findings]}


def run_security_review(
    request: Mapping[str, Any],
    reviewer: review_policy.Reviewer,
    *,
    repo: repo_mod.Repo,
    commit: str,
    prior_blocking_ids: Iterable[str] = (),
) -> SecurityResult:
    """Run the security reviewer and validate its findings (plan §12.5, §12.7).

    `prior_blocking_ids` are the blocking findings the previous review recorded *about the same
    base* (`review._prior_blocking_ids`). A regeneration that drops one, or that re-emits it with
    `blocking: false`, is a reviewer clearing its own block, and the policy refuses both — the
    second was the wider door: the check compared id sets, so re-listing `SEC-001` as
    non-blocking satisfied it exactly as well as fixing the finding did.

    Each finding also records the base and head it was found against. A finding is a statement
    about a change, and until now nothing in the document said which one.
    """
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
