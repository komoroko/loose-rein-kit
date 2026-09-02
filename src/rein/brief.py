"""The gate ④ orientation brief: what was built, and under what conditions — derived, not written.

Gate ④ used to hand a human a boundary (`scope`) and then, immediately, a stack of Decision Cards.
Everything between those two — what the cycle actually delivered, which dependencies moved, which
sandbox and which network posture each gate step ran under, what the code was observed to do, what
the gate established and what it left open — existed only as facts scattered across `plan.yaml`,
`state.yaml`, `config.yaml` and the machine review. A reviewer who needed them reconstructed them
from a diff, once per card. That reconstruction is the cost this module removes.

**Derived, never authored.** Like `decision_cards`, every value here is a restatement of something
the SSOT already records: a task id and its frozen title, a path, a command, an image reference, a
statement id. Nothing here is a sentence this file composed. That is deliberate and it is the
schema's rule, not a preference — review.yaml has no free-form prose field, because a sentence with
no epistemic status is exactly what a model fills in when it has no source. Where the brief carries
something a reviewer wrote, it carries the **confidence and the code anchor with it**: the rule was
never "no text", it was "no text without its status", and an id the reader had to go and resolve
somewhere else was how that rule got implemented back when there was nowhere to show the status.
An implementer's own account (`residuals.accounts`) travels the same way — labelled as the claim it
is, never as a finding.

**Bound to the reviewed tree, not recomputed on read.** `derive` runs inside `review.generate` and
its result is stored in the machine half, so the brief a reviewer reads describes the same commit
range as the claims beside it. Recomputing it when the pane asks would quietly show a brief about
the working tree next to a review about `subject_head_sha`.

What it deliberately does **not** carry: anything `scope_block` already states (the commit range,
the coverage manifest, the budget) and anything `binding` already states (independence). A second
copy of a number is a second thing that can disagree with the first.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rein import common, diff_facts, models

#: How the brief reaches the reviewed tree: given a repo-relative path, the identity and size of
#: its blob at `binding.subject_head_sha`, or None when that commit has no such file. Injected
#: rather than imported so this module stays pure — `derive` takes already-read SSOT, and a
#: function that goes and reads git would make every test of it need a repository.
BlobFacts = Callable[[str], dict[str, Any] | None]

#: Cap on any path list in the brief. Past this a section is saying "a lot changed here", which is
#: a fact the count carries better than the two-hundredth filename does.
MAX_PATHS = 100

#: Cap on the delivered-task table. `state.yaml` allows 512 tasks; a cycle that delivered more than
#: this has a scope problem no screen fixes.
MAX_TASKS = 200

#: Cap on the as-built file list carried for one declaration. A declaration naming more places than
#: this is not describing one surface.
MAX_AS_BUILT = 8

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


def _declarations(plan: models.Plan | None) -> list[dict[str, Any]]:
    """Every `operator_surface` entry in the frozen plan, tagged with the task that declared it."""
    if plan is None:
        return []
    rows = []
    for task in plan.tasks:
        for entry in task.operator_surface:
            kind = str(entry.get("kind", ""))
            paths = [str(p) for p in entry.get("paths", ()) if isinstance(p, str)]
            if kind not in models.OPERATOR_SURFACE_KIND_VALUES or not paths:
                continue
            row: dict[str, Any] = {"task_id": task.id, "kind": kind, "name": str(entry.get("name", "")), "paths": paths}
            if entry.get("adr"):
                row["adr"] = str(entry["adr"])
            rows.append(row)
    return rows


def _anchor_paths(statement: Mapping[str, Any]) -> list[str]:
    anchors = statement.get("code_anchors")
    if not isinstance(anchors, list):
        return []
    return [str(a["path"]) for a in anchors if isinstance(a, Mapping) and a.get("path")]


def _requirements_on_people(
    plan: models.Plan | None,
    actual_statements: Sequence[Mapping[str, Any]],
    blob_facts: BlobFacts | None,
) -> dict[str, Any]:
    """What this change now requires of a person, sorted by whether anyone foresaw it.

    This is the one part of the orientation that can change an approval, so it is the one part
    ordered by decision value rather than by category:

    - **`undeclared` first.** An operator-facing behaviour the code was read to have, that no task
      declared at gate ③. Nobody decided this would be someone's job; the approver is being asked
      to sign over it anyway. Carried with its confidence and its anchor, because a sentence
      without those is not evidence of anything.
    - **`unobserved`.** A declaration nothing was read out about. Either it was not built or it
      could not be read, and the two are the reviewer's to tell apart — which is why this says
      only that the reading is absent.
    - **`as_declared` as a count.** Foreseen and present is the boring case, and a table of boring
      rows is where the two above go to hide. The entries travel with it for a reader who asks.

    Matching is **category equality plus path coverage** (`common.path_covered`, the same rule as a
    task's `scope`) and stops there. A declaration's `name` is prose a human wrote and a statement
    is prose a model wrote; declaring them "the same surface" because they read alike is exactly
    the overclaim the rest of this module refuses to make.
    """
    declarations = _declarations(plan)
    matched: dict[int, list[str]] = {i: [] for i in range(len(declarations))}
    undeclared: list[dict[str, Any]] = []

    for statement in actual_statements:
        category = str(statement.get("category", ""))
        statement_id = str(statement.get("id", ""))
        if category not in models.OPERATOR_SURFACE_KIND_VALUES or not statement_id:
            continue
        paths = _anchor_paths(statement)
        # Every declaration it lands in, not the first: two tasks may declare the same area, and
        # crediting only one of them would report the other as "nothing was read out about this"
        # while the reading sits in the same list.
        hits = [
            index
            for index, declaration in enumerate(declarations)
            if declaration["kind"] == category
            and any(common.path_covered(path, pattern) for path in paths for pattern in declaration["paths"])
        ]
        if hits:
            for index in hits:
                matched[index].append(statement_id)
            continue
        row: dict[str, Any] = {
            "category": category,
            "statement_id": statement_id,
            "statement": str(statement.get("statement", "")),
            "confidence": str(statement.get("confidence", "low")),
        }
        if paths:
            row["paths"] = sorted(set(paths))[:MAX_PATHS]
        undeclared.append(row)

    unobserved: list[dict[str, Any]] = []
    as_declared: list[dict[str, Any]] = []
    for index, declaration in enumerate(declarations):
        row = dict(declaration)
        built = _as_built(declaration["paths"], blob_facts)
        if built:
            row["as_built"] = built
        if matched[index]:
            row["statement_ids"] = matched[index][:MAX_PATHS]
            as_declared.append(row)
        else:
            unobserved.append(row)

    section: dict[str, Any] = {}
    if undeclared:
        section["undeclared"] = undeclared[:MAX_PATHS]
    if unobserved:
        section["unobserved"] = unobserved[:MAX_PATHS]
    if as_declared:
        section["as_declared"] = {"count": len(as_declared), "entries": as_declared[:MAX_PATHS]}
    return section


def _as_built(paths: Sequence[str], blob_facts: BlobFacts | None) -> list[dict[str, Any]]:
    """How to reach each declared path *as it ends up*, at the commit the review is bound to.

    The identity of the blob and its size, never its body: review.yaml is not a second copy of the
    repository, and a document that grew a schema file every cycle would stop being readable for
    the same reason the diff already is not. The pane fetches the body from that commit when a
    reader asks for it.

    A path with no blob is simply absent — a declaration may name a file the change deleted, or one
    that was never created, and both are things `unobserved` is already saying more directly.
    """
    if blob_facts is None:
        return []
    out = []
    for path in sorted(set(paths))[:MAX_AS_BUILT]:
        facts = blob_facts(path)
        if facts is not None:
            out.append({"path": path, **facts})
    return out


#: The step name that means "this one launches the deliverable". Nothing in the config schema marks
#: it, so the name is the only signal there is — which is exactly why the absence of a step by this
#: name has to be reported as *that*, and not as "the smoke step has no command".
LAUNCH_STEP = "smoke"


def _operations(config: models.Config | None) -> dict[str, Any]:
    """Whether anything ever launched the deliverable, and whether the gate insists on it.

    A green test suite over a package that cannot start is the failure the launch step exists to
    catch, so it is a fact a reviewer must see at gate ④ rather than infer from the absence of one.

    Two states used to collapse into one empty result, and the scaffold's own default fell through
    both. A step under another name reported `{}`, which the review screen rendered as "the smoke
    step has no command" — a claim about a step that may well have had one. And the shipped
    `["true"]` reported a non-empty command, so the panel said nothing at all about a deliverable
    that had never been started. `placeholder` is the same argv test `doctor.check_quality_gate`
    applies, so the two cannot say different things about one config.
    """
    if config is None:
        return {}
    for step in config.quality_gate:
        if step.kind != "command" or step.name != LAUNCH_STEP:
            continue
        return {
            "command": list(step.command),
            "required": step.required,
            "placeholder": tuple(step.command) in models.PLACEHOLDER_COMMANDS,
        }
    return {}


def _verification(config: models.Config | None, state: models.State | None) -> dict[str, Any]:
    """How many DoD steps ran for something, and which ones ran for nothing.

    A step configured but established for no task is the interesting row: it means every task's
    diff missed the step's `paths:`, or the run never got that far. The other rows say "the gate
    did its job", which is what everybody assumed before opening the screen — so they are a number,
    and only the exceptions are lines. Counting recorded evidence rather than reading the config
    alone is what makes the difference visible at all.
    """
    if config is None:
        return {}
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
    section: dict[str, Any] = {"steps": len(config.quality_gate)}
    unestablished = [step.name for step in config.quality_gate if not established.get(step.name)]
    if unestablished:
        section["established_for_nothing"] = unestablished[:MAX_PATHS]
    return section


#: The negative-control outcomes that are not a control. `discriminating` is the one that says the
#: experiment ran and answered, so it is a count; these two say it did not, and they are the rows.
_UNCONTROLLED = ("no_tests_changed", "undetermined")


def _control(state: models.State | None) -> dict[str, Any]:
    """Which landed tasks' greens were controlled, and which were not.

    The DoD's green is the only automated evidence a task's `done` rests on, and the negative
    control is what asks whether it could have been red: the command steps re-established over the
    base with only the task's test half applied. `build_loop` records the outcome beside the status
    and, until this section existed, **nothing read it** — not the orient brief, not the decision
    cards, not `approve`'s readiness. So `no_tests_changed`, whose whole purpose is to put "this
    task's green rests on tests nobody wrote for it" on the record rather than leave it a silence,
    reached the human as a silence in a different file. (`template_lint`'s
    `check_declared_properties_are_read` cannot catch this shape by construction: it is a literal
    name search and the writer names the field.)

    It follows `_verification`'s convention, which is the right one here for the same reason: the
    tasks whose control answered say what everybody assumed on opening the screen, so they are a
    number, and only the ones where the experiment could not be taken are lines.

    **It blocks nothing.** A task whose work is genuinely covered by tests that already existed is a
    real thing, and turning this into a gate would make the loop demand a test per task rather than
    evidence per claim — the judgement `_negative_control` makes explicitly. This says what happened;
    what the tests are worth is the per-task reviewer's question, and its findings arrive beside
    this one (`residual_findings`).
    """
    tasks = state.raw.get("tasks") if state is not None else None
    rows: dict[str, list[dict[str, str]]] = {result: [] for result in _UNCONTROLLED}
    controlled = 0
    for task_id, entry in sorted((tasks if isinstance(tasks, dict) else {}).items()):
        if not isinstance(entry, dict) or str(entry.get("status", "")) not in _LANDED:
            continue
        evidence = entry.get("evidence")
        control = evidence.get("negative_control") if isinstance(evidence, dict) else None
        if not isinstance(control, Mapping):
            continue
        result = str(control.get("result", ""))
        if result == "discriminating":
            controlled += 1
        elif result in rows:
            row = {"task_id": str(task_id)}
            detail = control.get("detail")
            if detail:
                row["detail"] = str(detail)
            rows[result].append(row)
    section: dict[str, Any] = {}
    if controlled:
        section["discriminating"] = controlled
    for result in _UNCONTROLLED:
        if rows[result]:
            section[result] = rows[result][:MAX_TASKS]
    return section


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
    accounts = _accounts(tasks)
    if accounts:
        residual["accounts"] = accounts
    return residual


def _accounts(tasks: Mapping[str, Any]) -> list[dict[str, Any]]:
    """What the last implementer said about each task that did **not** land.

    `handoff.report` was written by every attempt and read by two things: the loop, to phrase a
    failure, and the next attempt's dossier. Nobody approving the gate ever saw it — and the task
    it is about is one the approver is being asked to sign *around*, which is precisely when the
    reason matters.

    Only unfinished tasks. For work that landed, the implementer's own account of it is the one
    input the blind extractor is forbidden to see (`actual_extraction.FORBIDDEN_KEYS`), and putting
    it in front of the human at the moment of decision would move that priming from the extractor
    to the person whose judgement the whole arrangement exists to protect.

    A claim, not a finding. The loop already checks `touched` against the real diff and treats a
    disagreement as a finding; this is the sentence beside it, and it is labelled as such.
    """
    out: list[dict[str, Any]] = []
    for task_id, entry in sorted(tasks.items()):
        if not isinstance(entry, dict) or str(entry.get("status", "")) in _LANDED:
            continue
        handoff = entry.get("handoff")
        report = handoff.get("report") if isinstance(handoff, dict) else None
        if not isinstance(report, Mapping):
            continue
        summary = str(report.get("summary", "")).strip()
        if not summary:
            continue
        out.append(
            {
                "task_id": task_id,
                "outcome": str(report.get("outcome", "")),
                "summary": summary,
            }
        )
    return out[:MAX_TASKS]


def derive(
    *,
    plan: models.Plan | None,
    state: models.State | None,
    config: models.Config | None,
    actual_statements: Sequence[Mapping[str, Any]] = (),
    changed_paths: Sequence[str] = (),
    blob_facts: BlobFacts | None = None,
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

    requirements = _requirements_on_people(plan, actual_statements, blob_facts)
    if requirements:
        sections["requirements_on_people"] = requirements

    operations = _operations(config)
    if operations:
        sections["operations"] = operations

    verification = _verification(config, state)
    if verification:
        sections["verification"] = verification

    control = _control(state)
    if control:
        sections["control"] = control

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
