"""The gate ④ orientation brief: what was built, and under what conditions — derived, not written.

Gate ④ used to hand a human a boundary (`scope`) and then, immediately, a stack of Decision Cards.
Everything between those two — what the cycle actually delivered, which dependencies moved, which
sandbox and which network posture each gate step ran under, what the code was observed to do, what
the gate established and what it left open — existed only as facts scattered across `plan.yaml`,
`state.yaml`, `config.yaml` and the machine review. A reviewer who needed them reconstructed them
from a diff, once per card. That reconstruction is the cost this module removes.

**Derived, never authored.** Like `decision_cards`, every value here is a restatement of something
the SSOT already records: a task id and its frozen title, a path, a command, an image reference, a
statement *id*. Nothing here is a sentence this file composed. That is deliberate and it is the
schema's rule, not a preference — review.yaml has no free-form prose field, because a sentence with
no epistemic status is exactly what a model fills in when it has no source. Where the brief points
at something a reviewer wrote (`behaviour`), it points by **id** into `actual_extraction`, which
carries the status and the code anchors; it does not copy the text out and strip them.

**Bound to the reviewed tree, not recomputed on read.** `derive` runs inside `review.generate` and
its result is stored in the machine half, so the brief a reviewer reads describes the same commit
range as the claims beside it. Recomputing it when the pane asks would quietly show a brief about
the working tree next to a review about `subject_head_sha`.

What it deliberately does **not** carry: anything `scope_block` already states (the commit range,
the coverage manifest, the budget) and anything `binding` already states (independence). A second
copy of a number is a second thing that can disagree with the first.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rein import diff_facts, models

#: Cap on any path list in the brief. Past this a section is saying "a lot changed here", which is
#: a fact the count carries better than the two-hundredth filename does.
MAX_PATHS = 100

#: Cap on the delivered-task table. `state.yaml` allows 512 tasks; a cycle that delivered more than
#: this has a scope problem no screen fixes.
MAX_TASKS = 200

#: The statement categories a reviewer is being oriented about, in the order they are shown. Not
#: every category the extractor may emit — `control_flow` and `default_value` are the substance of
#: the comparison itself, and repeating them here would turn an orientation into a second review.
#: These four are the ones that answer "what does this change expose, store, protect, and depend on".
BEHAVIOUR_CATEGORIES: tuple[str, ...] = ("public_interface", "persistence", "security_boundary", "dependency")

#: Task statuses that mean the work landed. `awaiting-evidence` landed too — its code merged and
#: passed everything; only the task is parked — so it belongs in `delivered` *and* in `residuals`.
_LANDED = frozenset({"done", "awaiting-evidence"})


def _paths_of_kind(changed: Sequence[str], kind: str) -> list[str]:
    """Changed paths `diff_facts` classifies as `kind`, capped and sorted."""
    return sorted(p for p in changed if diff_facts.classify_path(p) == kind)[:MAX_PATHS]


def _task_entry(task: models.Task, entry: Mapping[str, Any]) -> dict[str, Any]:
    row: dict[str, Any] = {
        "task_id": task.id,
        "title": task.title,
        "kind": task.kind,
        "risk": task.risk,
        "status": str(entry.get("status", "todo")),
    }
    if task.claim_ids:
        row["claim_ids"] = list(task.claim_ids)
    commit = str(entry.get("completed_commit", ""))
    if commit:
        row["commit"] = commit
    return row


def _delivered(plan: models.Plan | None, state: models.State | None) -> list[dict[str, Any]]:
    """The tasks whose work is in the reviewed tree, in plan order.

    Plan order rather than completion order: the plan is what the human froze at gate ③, and a
    table sorted by when an agent happened to finish is a table about the agent.
    """
    if plan is None or state is None:
        return []
    tasks = state.raw.get("tasks")
    tasks = tasks if isinstance(tasks, dict) else {}
    rows = []
    for task in plan.tasks:
        entry = tasks.get(task.id)
        if isinstance(entry, dict) and str(entry.get("status", "")) in _LANDED:
            rows.append(_task_entry(task, entry))
    return rows[:MAX_TASKS]


def _execution_boundary(config: models.Config | None) -> list[dict[str, Any]]:
    """Where each DoD step ran: its sandbox, its pinned image, and its network posture.

    The network line is the one that is easy to assume and expensive to be wrong about. It is not
    an aspiration here — `executors.OciExecutor.run` refuses any profile whose `network_profile` is
    not `none`, so what this prints is what the runtime enforced. A `host` profile is shown as what
    it is: a step that ran with whatever the machine had, which is a different claim entirely.
    """
    if config is None:
        return []
    profiles = config.profiles
    rows = []
    for step in config.quality_gate:
        profile = profiles.get(step.executor_profile)
        row: dict[str, Any] = {
            "step": step.name,
            "kind": step.kind,
            "profile": step.executor_profile,
        }
        if step.command:
            row["command"] = list(step.command)
        if step.agent_role:
            row["agent_role"] = step.agent_role
        if profile is not None:
            row["sandbox"] = profile.kind
            if profile.image:
                row["image"] = profile.image
            # An `oci` profile with no `network_profile` set is `none` by the executor's own
            # default; a `host` profile has no network boundary to report at all, and saying
            # "none" about it would be a claim the runtime never made.
            row["network"] = (profile.network_profile or "none") if profile.is_sandboxed else "unconfined"
        rows.append(row)
    return rows


def _environment_drift(state: models.State | None, config: models.Config | None) -> dict[str, Any]:
    """Is the sandbox the evidence was produced in the one gate ③ approved? {} when it is, or
    when the freeze recorded nothing to compare against.

    Gate ③ deliberately does not freeze the image pin: a task that adds a dependency makes the
    pinned image wrong, and rebuilding it is a rebuild of the same sandbox rather than a change of
    decision. That permission is what this section pays for. The approver at gate ④ is signing over
    evidence, and "the environment it was produced in moved after the plan was approved" is a fact
    about that evidence — not a blocker, and not something they should have to go and look for.

    Reported as the two digests and nothing else. Naming *which* image moved would mean reading a
    config.yaml that has since been rewritten again; the `environment_repinned` events in the chain
    are where that history actually lives.
    """
    if state is None or config is None:
        return {}
    frozen = state.plan_environment_digest
    if not frozen:
        return {}
    live = config.environment_digest()
    if live == frozen:
        return {}
    return {"approved_at_gate_three": frozen, "evidence_produced_in": live}


def _behaviour(actual_statements: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """What the blind extractor read out, grouped by the categories an orientation is about.

    By id and by anchor path, never by copying the statement text. The text lives in
    `actual_extraction` with its confidence and its code anchors attached; a copy here would be the
    same sentence with its epistemic status left behind, which is the one thing this document does
    not allow.
    """
    grouped: dict[str, dict[str, Any]] = {}
    for statement in actual_statements:
        category = str(statement.get("category", ""))
        statement_id = str(statement.get("id", ""))
        # No id, no row. This section is a set of pointers into `actual_extraction`, and an entry
        # the reader cannot follow is worse than an absent one: it says something was read out
        # while giving no way to see what.
        if category not in BEHAVIOUR_CATEGORIES or not statement_id:
            continue
        bucket = grouped.setdefault(category, {"category": category, "statement_ids": [], "paths": set()})
        bucket["statement_ids"].append(statement_id)
        anchors = statement.get("code_anchors")
        if isinstance(anchors, list):
            for anchor in anchors:
                if isinstance(anchor, Mapping) and anchor.get("path"):
                    bucket["paths"].add(str(anchor["path"]))
    rows = []
    for category in BEHAVIOUR_CATEGORIES:
        found = grouped.get(category)
        if found is None:
            continue
        rows.append(
            {
                "category": category,
                "statement_ids": found["statement_ids"][:MAX_PATHS],
                "paths": sorted(found["paths"])[:MAX_PATHS],
            }
        )
    return rows


def _operations(config: models.Config | None) -> dict[str, Any]:
    """Whether anything ever launched the deliverable, and whether the gate insists on it.

    A green test suite over a package that cannot start is the failure the smoke step exists to
    catch, so "smoke has no command" is a fact a reviewer must see at gate ④ rather than infer from
    the absence of one.
    """
    if config is None:
        return {}
    for step in config.quality_gate:
        if step.name != "smoke":
            continue
        return {"command": list(step.command), "required": step.required}
    return {}


def _verification(config: models.Config | None, state: models.State | None) -> list[dict[str, Any]]:
    """Each DoD step, and how many landed tasks recorded it established against their own tree.

    A step configured but established for no task is the interesting row: it means every task's
    diff missed the step's `paths:`, or the run never got that far. Counting recorded evidence
    rather than reading the config alone is what makes the difference visible.
    """
    if config is None:
        return []
    established: dict[str, int] = {}
    tasks = state.raw.get("tasks") if state is not None else None
    for entry in (tasks if isinstance(tasks, dict) else {}).values():
        if not isinstance(entry, dict) or str(entry.get("status", "")) not in _LANDED:
            continue
        evidence = entry.get("evidence")
        steps = evidence.get("steps") if isinstance(evidence, dict) else None
        for step in steps if isinstance(steps, list) else []:
            if isinstance(step, Mapping) and step.get("name"):
                established[str(step["name"])] = established.get(str(step["name"]), 0) + 1
    return [{"step": step.name, "established_for": established.get(step.name, 0)} for step in config.quality_gate]


def _residuals(state: models.State | None) -> dict[str, Any]:
    """What is still open — the part of gate ④ that is easiest to approve past without noticing."""
    if state is None:
        return {}
    tasks = state.raw.get("tasks")
    tasks = tasks if isinstance(tasks, dict) else {}
    by_status: dict[str, list[str]] = {}
    for task_id, entry in sorted(tasks.items()):
        if isinstance(entry, dict):
            by_status.setdefault(str(entry.get("status", "todo")), []).append(task_id)
    residual: dict[str, Any] = {}
    for status, key in (("awaiting-evidence", "awaiting_evidence"), ("blocked", "blocked"), ("todo", "unstarted")):
        if by_status.get(status):
            residual[key] = by_status[status][:MAX_TASKS]
    open_requests = [str(cr.get("id", "")) for cr in state.change_requests_for("build", "open")]
    if open_requests:
        residual["open_change_requests"] = open_requests[:MAX_TASKS]
    return residual


def derive(
    *,
    plan: models.Plan | None,
    state: models.State | None,
    config: models.Config | None,
    actual_statements: Sequence[Mapping[str, Any]] = (),
    changed_paths: Sequence[str] = (),
) -> dict[str, Any]:
    """Assemble the orientation brief. Pure: every argument is already-read SSOT.

    Empty sections are dropped rather than emitted empty. "No migrations changed" and "we did not
    look at migrations" are different statements, and an empty list in a document whose whole
    premise is that absence must be distinguishable from unmeasured would say the wrong one. A
    section that is absent is a section with nothing to report; a section that is present reports
    what it found.
    """
    sections: dict[str, Any] = {}
    delivered = _delivered(plan, state)
    if delivered:
        sections["delivered"] = delivered

    stack: dict[str, Any] = {}
    for key, kind in (("dependency_files", "dependency"), ("generated_files", "generated")):
        paths = _paths_of_kind(changed_paths, kind)
        if paths:
            stack[key] = paths
    if stack:
        sections["stack"] = stack

    migrations = _paths_of_kind(changed_paths, "migration")
    if migrations:
        sections["data"] = {"migrations": migrations}

    boundary = _execution_boundary(config)
    if boundary:
        sections["execution_boundary"] = boundary

    drift = _environment_drift(state, config)
    if drift:
        sections["environment_drift"] = drift

    behaviour = _behaviour(actual_statements)
    if behaviour:
        sections["behaviour"] = behaviour

    operations = _operations(config)
    if operations:
        sections["operations"] = operations

    verification = _verification(config, state)
    if verification:
        sections["verification"] = verification

    residuals = _residuals(state)
    if residuals:
        sections["residuals"] = residuals

    return sections


#: Cap on the residual findings carried to gate ④, mirroring the per-review cap a single reviewer
#: may hand back (`dossier.MAX_FINDINGS`). Past it the list is truncated and says so — a silent cut
#: would make "no more findings" and "we stopped listing" the same thing on screen.
MAX_RESIDUAL_FINDINGS = 50


def residual_findings(state: models.State | None) -> list[dict[str, Any]]:
    """Per-task review findings that were never resolved, carried to the human at gate ④.

    The per-task reviewer's `must_fix` findings are resolved inside the build loop or the task
    blocks; its `consider` findings stop nothing by design and were written to the task's handoff —
    where, until this existed, they were read by nobody. Both `build.md` and the reviewer's own
    prompt told the reviewer those findings would reach a human at gate ④, and the state schema
    says so too. This is the code that makes that true.

    Each finding is stamped with **the tree it was made against**, not the reviewed HEAD. A
    reviewer looked at one task's worktree at one moment; the merged tree it is being read beside
    may have moved since. Presenting the two as the same observation would be exactly the overclaim
    the rest of this document is built to prevent, so the commit and the fingerprint travel with it
    and the reader can see the distance.
    """
    if state is None:
        return []
    tasks = state.raw.get("tasks")
    tasks = tasks if isinstance(tasks, dict) else {}
    out: list[dict[str, Any]] = []
    for task_id, entry in sorted(tasks.items()):
        if not isinstance(entry, dict):
            continue
        handoff = entry.get("handoff")
        review = handoff.get("review") if isinstance(handoff, dict) else None
        findings = review.get("findings") if isinstance(review, dict) else None
        if not isinstance(findings, list):
            continue
        evidence = entry.get("evidence")
        tree = str(evidence.get("tree", "")) if isinstance(evidence, dict) else ""
        commit = str(entry.get("completed_commit", ""))
        for finding in findings:
            if not isinstance(finding, Mapping) or not finding.get("statement"):
                continue
            row: dict[str, Any] = {
                "task_id": task_id,
                "severity": str(finding.get("severity", "consider")),
                "statement": str(finding["statement"]),
            }
            if finding.get("anchor"):
                row["anchor"] = str(finding["anchor"])
            if commit:
                row["observed_commit"] = commit
            if tree:
                row["observed_tree"] = tree
            out.append(row)
    return out[:MAX_RESIDUAL_FINDINGS]
