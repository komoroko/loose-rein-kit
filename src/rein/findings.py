"""Which task has to answer each blocking finding of the grounded review.

Gate ④ produces findings against *code*, and the loop repairs *tasks*. Somebody has been closing
that gap by hand — reading a security finding's anchor, deciding which ticket owns that file, and
typing the id into `rein revise --impacted`. Everything needed to derive it is already recorded:
each finding is grounded in a code anchor whose path was validated against the committed tree
(`review_policy.validate_anchor`), and each task declares the paths it owns (`scope.include`).

Three kinds of finding, and they are not attributed the same way:

* **security findings** carry their own anchors — path to task, directly.
* **extra behaviours** name the actual statements they came from; those carry the anchors.
* **claim verdicts** need no path at all. A claim that came back `diverged` or `missing` belongs
  to whichever task the plan says answers it, which is a stronger link than any file.

A finding nothing owns is **not** guessed at. It is reported, and a human decides — an undeclared
scope means unbounded, so "no task covers this path" really means the plan does not say, and
picking the nearest task would be inventing the answer this module exists to derive.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rein import common, models

logger = logging.getLogger(__name__)

#: Claim verdicts that mean the code did not answer the claim. `unverified` and `unknown` are not
#: here: they say the review could not tell, which is a coverage problem rather than a task's.
FAILING_VERDICTS = frozenset({"diverged", "missing"})


@dataclass(frozen=True)
class Attribution:
    """One blocking finding and the task that has to answer it. `task_id` empty when none does."""

    finding_id: str
    kind: str
    task_id: str
    #: What decided it — the anchor path, or the claim id. Printed so the mapping can be argued with.
    basis: str

    @property
    def owned(self) -> bool:
        return bool(self.task_id)


def owner_of_path(plan: models.Plan, path: str) -> str:
    """The task whose declared scope covers `path` most specifically, or "" when none declares it.

    Longest match wins, the same way `common.longest_cover` decides everywhere else. A task that
    declared no scope is skipped rather than treated as covering everything: unbounded is the right
    reading when *checking* whether work strayed, and the wrong one when *asking* who owns a file —
    it would make the first scope-less task in the plan the owner of the whole repository.
    """
    best, best_len = "", -1
    for task in plan.tasks:
        if not task.scope_include:
            continue
        cover = common.longest_cover(path, task.scope_include)
        if cover is not None and len(cover) > best_len:
            best, best_len = task.id, len(cover)
    return best


def _anchor_paths(entry: Mapping[str, object]) -> list[str]:
    anchors = entry.get("code_anchors")
    if not isinstance(anchors, list):
        return []
    return [str(a.get("path", "")) for a in anchors if isinstance(a, dict) and a.get("path")]


def _first_owned(plan: models.Plan, paths: Sequence[str]) -> tuple[str, str]:
    for path in paths:
        owner = owner_of_path(plan, path)
        if owner:
            return owner, path
    return "", paths[0] if paths else ""


def attribute(plan: models.Plan, review: models.Review | None) -> list[Attribution]:
    """Every blocking finding of the machine review, with the task that has to answer it."""
    if review is None or not review.is_generated:
        return []
    found: list[Attribution] = []

    for finding in review.blocking_security_findings:
        task_id, basis = _first_owned(plan, _anchor_paths(finding))
        found.append(Attribution(str(finding.get("id", "SEC-?")), "security", task_id, basis))

    statements = {str(s.get("id")): s for s in review.machine.get("actual_extraction", []) if isinstance(s, dict)}
    for extra in review.extra_behaviors:
        if extra.get("blocking") is not True:
            continue
        paths: list[str] = []
        for statement_id in extra.get("actual_statement_ids", []) or []:
            paths += _anchor_paths(statements.get(str(statement_id), {}))
        task_id, basis = _first_owned(plan, paths)
        found.append(Attribution(str(extra.get("id", "EXTRA-?")), "extra_behavior", task_id, basis))

    for result in review.claim_results:
        if str(result.get("verdict", "")) not in FAILING_VERDICTS:
            continue
        claim_id = str(result.get("claim_id", ""))
        owner = next((t.id for t in plan.tasks if claim_id in t.claim_ids), "")
        found.append(Attribution(claim_id, "claim", owner, claim_id))
    return found


def seeds(attributions: Sequence[Attribution]) -> list[str]:
    """The task ids to hand `revise.impacted_closure`, deduplicated and ordered."""
    return sorted({a.task_id for a in attributions if a.owned})


def unowned(attributions: Sequence[Attribution]) -> list[Attribution]:
    """The findings no task's declared scope claims. A human decides where these go."""
    return [a for a in attributions if not a.owned]


def render(attributions: Sequence[Attribution]) -> str:
    """The mapping, printed so it can be disagreed with before anything is marked."""
    if not attributions:
        return "the machine review has no blocking findings."
    lines = [f"{len(attributions)} blocking finding(s) from the machine review:"]
    for a in attributions:
        where = a.task_id or "**no task declares this**"
        lines.append(f"  {a.finding_id:<12} {a.kind:<15} -> {where}   ({a.basis or 'no anchor'})")
    missing = unowned(attributions)
    if missing:
        lines.append(
            f"\n{len(missing)} finding(s) belong to no declared scope. They are not guessed at: decide where "
            "they go and pass those ids to `rein revise --impacted` yourself."
        )
    return "\n".join(lines)
