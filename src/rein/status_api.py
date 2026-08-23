"""Status aggregation: one JSON object, and one deterministic "what should I do next".

`/status`, `rein next`, and the dashboard all read this, so the answer to "where am I"
is computed once rather than narrated three times by three agents. :func:`next_action` is a
first-match decision table — the same state always yields the same recommendation, which is
what lets a human predict the tool instead of interviewing it.

Read-only, and **tolerant on purpose**: a missing plan (normal before `/tasks`), a half-edited
config, or a damaged audit chain must degrade to a warning rather than a crash. The dashboard
has to stay up precisely when the state is odd — that is when a human most needs to look at it.

The one thing tolerance never extends to is *reporting a problem as fine*. A damaged chain, a
broken gate ladder, and an unreadable state each get their own row near the top of the table,
so the recommendation is "diagnose this", never a phase command that would build on top of it.

Alongside the single recommendation there is a **queue**: `pending` is everything currently
standing between this repository and its next gate, each row carrying its own severity. The
recommendation answers "what do I do next"; the queue answers "how much is waiting, and how bad
is it" — a question a first-match table structurally cannot answer, because it stops at the
first row that matches. The queue's blocking rows come from :func:`rein.approve.readiness`,
the same function `rein approve <gate> --check` refuses on, so the board and the gate can
never disagree about what is in the way.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from rein import common, dag, dag_trace, event_chain, models, strict_yaml
from rein import events as events_mod
from rein import lock as lock_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

GATE_ORDER = models.GATE_ORDER
PHASE_ORDER = models.PHASE_ORDER

#: Phase → the gate its command presents for approval.
PHASE_GATE: dict[str, str] = {
    "requirements": "requirements",
    "design": "design",
    "tasks": "tasks",
    "build": "build",
    "verify": "release",
}
PHASE_COMMAND: dict[str, str] = {
    "requirements": "/req",
    "design": "/design",
    "tasks": "/tasks",
    "build": "/build",
    "verify": "/verify",
}
#: The phase each gate belongs to on the stepper (release is presented from /verify).
GATE_PHASE: dict[str, str] = {**{g: g for g in GATE_ORDER[:-1]}, "release": "verify"}

_PLACEHOLDER_HINTS = ("<enter", "product", "build/product")


@dataclass(frozen=True)
class Recommendation:
    """The next action, as a copy-able command plus a one-sentence why."""

    command: str
    kind: str  # run_phase | approve_gate | reconcile | resolve | setup | close | fix
    reason: str
    also: tuple[str, ...] = ()


def _is_placeholder(value: object) -> bool:
    return isinstance(value, str) and any(hint in value for hint in _PLACEHOLDER_HINTS)


def _no_agent_surface(repo: repo_mod.Repo) -> bool:
    """True when no agent surface is usable — neither recorded in the lock nor present on disk.

    Disk counts as much as the record. A repository cloned from the template carries working
    .claude/.github wrappers that nothing installed (`rein init` installs no surface), so
    reading the lock alone would tell that user their working /-commands do not exist. `doctor`
    is where the unrecorded copies are diagnosed; here they simply are a surface.
    """
    from rein import install as install_mod

    try:
        data = lock_mod.read(repo.lock)
    except lock_mod.LockError:
        return False
    if data is None or (data.get("integrations") or {}):
        return False
    return not any(install_mod.present_surfaces(repo, name) for name in install_mod.INTEGRATIONS)


def next_action(
    *,
    current_phase: str,
    gates: dict[str, str],
    counts: dict[str, int] | None,
    attention_count: int,
    chain_defects: int,
    template_mode: bool,
    placeholders: bool,
    gate_chain_broken: bool,
    plan_missing: bool,
    unsandboxed_profiles: list[str],
    unsandboxed_build_targets: list[str] | None = None,
    gate_ready: bool | None = None,
    open_change_requests: int = 0,
) -> Recommendation:
    """The deterministic decision table (first match wins).

    `gate_ready` is tri-state, for the same reason `pending_queue`'s `gate_blockers` is: `None`
    means readiness was **not probed**, which is not the same as probed-and-blocked. Collapsing
    them would let "we did not look" decide a recommendation.
    """
    # 1. The audit chain is the substrate every receipt binds. Nothing else matters until it is intact.
    if chain_defects:
        return Recommendation(
            command="rein events --verify",
            kind="fix",
            reason=f"The audit chain has {chain_defects} defect(s). No gate receipt can be issued against a "
            "damaged log — restore events.ndjson from git rather than rewriting it to agree.",
        )
    # 2. Not a product yet: the template must be initialized.
    if template_mode or placeholders:
        return Recommendation(
            command="rein init --name <product>",
            kind="setup",
            reason="This checkout is still the raw template (template_mode / placeholder state); "
            "initialize it into a product first.",
        )
    # 3. A broken gate ladder means an approval survived a roll back — repair, do not infer a phase from it.
    if gate_chain_broken:
        return Recommendation(
            command="rein doctor",
            kind="fix",
            reason="A downstream gate is approved while an upstream one is pending: an approval survived a "
            "roll back, so downstream work is standing on a decision that was withdrawn.",
        )
    # 4. needs-revision tasks park everything until the /tasks reconcile reclassifies them.
    if counts is not None and counts.get("needs-revision", 0) > 0:
        return Recommendation(
            command="/tasks",
            kind="reconcile",
            reason="needs-revision tasks exist; reconcile them (keep / modify / obsolete / new) and re-approve gate 3.",
            also=("rein dag --render",),
        )
    # 4b. A task waiting on evidence nothing here can produce. Re-running the build cannot move
    # it, so recommending one would send a person round a loop that has already finished.
    if counts is not None and counts.get("awaiting-evidence", 0) > 0:
        return Recommendation(
            command="rein evidence show",
            kind="evidence",
            reason="tasks are waiting on acceptance evidence this loop cannot obtain; observe it and record it "
            "with `rein evidence record`.",
        )
    # 5. Sandboxing is a precondition for running anything, so it precedes the phase rows.
    if unsandboxed_profiles:
        return Recommendation(
            command=models.sandbox_setup_command(unsandboxed_build_targets or unsandboxed_profiles),
            kind="fix",
            reason="Profile(s) " + ", ".join(unsandboxed_profiles) + " run repository-derived code on the host. "
            "This builds every packaged image, pins each digest in config.yaml and flips those profiles to "
            "`kind: oci`. It needs docker or podman on PATH, and the image still has to be able to run the "
            "step you point it at.",
            also=("rein doctor",),
        )
    # 6. Events awaiting a human decision block the release gate.
    if current_phase == "verify" and attention_count:
        return Recommendation(
            command="rein events --summary",
            kind="resolve",
            reason=f"{attention_count} event(s) await a human decision; record a disposition for each before "
            "the gate 5 release decision.",
        )
    # 7. Before the lifecycle starts, the human writes the brief.
    if current_phase == "brief":
        return Recommendation(
            command="/req",
            kind="run_phase",
            reason="Fill docs/00-product-brief.md, then run /req to start the requirements phase (gate 1).",
        )
    # 8. Everything approved: the cycle is over.
    if current_phase == "done" or all(gates.get(g) == "approved" for g in GATE_ORDER):
        return Recommendation(
            command="rein cycle-close --name <slug>",
            kind="close",
            reason="All gates are approved; archive this cycle's deliverables and reset for the next one.",
        )
    if plan_missing and current_phase not in {"brief", "requirements"}:
        return Recommendation(
            command="rein doctor",
            kind="fix",
            reason=f"current_phase is '{current_phase}' but there is no .rein/plan.yaml to work from.",
        )
    # 9. Inside a phase: pending gate → finish that phase; approved → advance.
    gate = PHASE_GATE.get(current_phase)
    if gate is not None:
        index = GATE_ORDER.index(gate) + 1
        if gates.get(gate) != "approved":
            # A human already read this and said "not yet". The phase command is still the right
            # recommendation, but the reason has to name what they asked for — otherwise the next
            # session re-derives the deliverable from scratch and answers nothing.
            if open_change_requests:
                return Recommendation(
                    command=PHASE_COMMAND[current_phase],
                    kind="reconcile",
                    reason=f"{open_change_requests} change request(s) you raised are still open, and gate "
                    f"{index} stays shut until they are. Read them, fix only what each one anchors, and mark "
                    "each addressed.",
                    also=(f"rein changes list --gate {gate}",),
                )
            # The phase is done and nothing mechanical is left: what remains is a person deciding.
            # This is the only row producing `approve_gate`, and so the only thing that ever turns
            # `waiting_on_human` on — the state the dashboard's title, favicon and notification
            # exist for. It reads the same `gate_blockers` the queue's `gate_ready` row does, so
            # the board and the recommendation cannot disagree.
            if gate_ready:
                return Recommendation(
                    command=f"rein approve {gate}",
                    kind="approve_gate",
                    reason=f"Phase '{current_phase}' is complete and gate {index} ({gate}) has no mechanical "
                    "blocker left — it is waiting on your decision. Read it in `rein ui` and approve there, or "
                    "run this yourself at a terminal; an agent never runs it for you.",
                    also=("rein ui", f"rein approve {gate} --check"),
                )
            also: tuple[str, ...] = (f"rein approve {gate} --check",)
            if current_phase == "build":
                also = ("rein build", *also)
            return Recommendation(
                command=PHASE_COMMAND[current_phase],
                kind="run_phase",
                reason=f"Phase '{current_phase}' is in progress; it ends by presenting gate {index} for the "
                "human's approval.",
                also=also,
            )
        next_phase = PHASE_ORDER[PHASE_ORDER.index(current_phase) + 1]
        if next_phase == "done":
            return Recommendation(
                command="rein cycle-close --name <slug>",
                kind="close",
                reason="The release gate is approved; close the cycle.",
            )
        also = ("rein build",) if next_phase == "build" else ()
        return Recommendation(
            command=PHASE_COMMAND[next_phase],
            kind="run_phase",
            reason=f"Gate {index} ({gate}) is approved; advance to the {next_phase} phase.",
            also=also,
        )
    return Recommendation(
        command="rein doctor",
        kind="fix",
        reason=f"current_phase '{current_phase}' is not in the lifecycle vocabulary; diagnose the SSOT.",
    )


def _handoffs(state: models.State | None) -> dict[str, dict[str, str]]:
    """Per task, the short form of what an interrupted attempt left for the next one.

    Only the two fields the board has room to say something with: a task that was retried says
    *why*, instead of a status that reads the same whether it is on its first attempt or its
    fourth. The full record (failure summary, remaining budget) is state.yaml's, not the UI's.
    """
    tasks = state.raw.get("tasks") if state else None
    if not isinstance(tasks, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for task_id, entry in tasks.items():
        handoff = entry.get("handoff") if isinstance(entry, dict) else None
        if not isinstance(handoff, dict):
            continue
        short = {k: str(handoff[k]) for k in ("failed_step", "salvage_state") if handoff.get(k)}
        if short:
            out[str(task_id)] = short
    return out


def completed_commits(state: models.State | None) -> dict[str, str]:
    """Per done task, the work-branch commit that landed it — what makes the board's "done" reviewable."""
    tasks = state.raw.get("tasks") if state else None
    if not isinstance(tasks, dict):
        return {}
    out: dict[str, str] = {}
    for task_id, entry in tasks.items():
        commit = entry.get("completed_commit") if isinstance(entry, dict) else None
        if isinstance(commit, str) and commit:
            out[str(task_id)] = commit
    return out


def _tasks_block(graph: dag.Graph, state: models.State | None = None) -> dict[str, object]:
    """The task-graph slice of the status object — every value derived, nothing stored."""
    fan = graph.fan_out()
    counts = graph.counts()
    handoffs = _handoffs(state)
    commits = completed_commits(state)
    return {
        "counts": {s: counts[s] for s in dag.STATUS_ORDER},
        "total": len(graph.tasks),
        "layers": graph.layers(),
        "critical_path": graph.critical_path(),
        "frontier": [
            {"id": t.id, "title": t.title, "kind": t.kind, "risk": t.risk, "fan_out": fan[t.id]}
            for t in graph.order_frontier()
        ],
        "rows": [
            {
                "id": t.id,
                "title": t.title,
                "kind": t.kind,
                "status": t.status,
                "risk": t.risk,
                "blocked_by": list(t.blocked_by),
                "claim_ids": list(t.claim_ids),
                "fan_out": fan[t.id],
                "handoff": handoffs.get(t.id, {}),
                "commit": commits.get(t.id, ""),
            }
            for t in graph.tasks
        ],
    }


#: Recommendation kinds that are the *agent's* next move rather than a call the human has to make.
#: Everything else in the decision table ends with a person deciding something.
_AGENT_KINDS = frozenset({"run_phase"})


def pending_decision(
    recommendation: Recommendation,
    awaiting_gate: str | None,
    *,
    pending: Sequence[Mapping[str, str]] = (),
) -> dict[str, object]:
    """The one decision currently waiting on a human, as an identity plus the smallest next action.

    Notifying per *event* would ping four times for a gate becoming current, an escalation
    opening, a task going needs-revision and a build finishing — two of them the same decision
    from different angles, one of them not a decision at all. What a person needs to be
    interrupted for is "there is something only you can settle, and here is the one command that
    settles it" — so the signal is derived once, here, from the same decision table `rein next`
    prints, and it carries an `id` that changes only when the decision itself does.

    The queue does **not** get to pick the decision. Interrupting on whatever happens to sort first
    would make the notification jitter every time an unrelated row appeared above it, and it would
    let the board and `rein next` recommend two different commands. What the queue contributes
    is `blocking`/`open`: how much is behind the one thing being pointed at, so a person can tell a
    single stuck task from a repository that needs an afternoon.
    """
    waiting = recommendation.kind not in _AGENT_KINDS
    subject = awaiting_gate if recommendation.kind == "approve_gate" else recommendation.kind
    return {
        "id": f"{recommendation.kind}:{subject}:{recommendation.command}" if waiting else "",
        "waiting_on_human": waiting,
        "kind": recommendation.kind,
        "headline": recommendation.reason,
        "action": recommendation.command,
        "blocking": sum(1 for item in pending if item["severity"] == "blocking"),
        "open": sum(1 for item in pending if item["severity"] != "info"),
    }


#: Queue severities, worst first. `blocking` means a gate cannot open while the row stands —
#: it is not a judgement about importance but about mechanism. `attention` needs a person but
#: blocks nothing yet; `info` is context that would otherwise only appear in a warning line.
PENDING_SEVERITY_ORDER: tuple[str, ...] = ("blocking", "attention", "info")

#: Task statuses that a human, not the loop, has to move.
_STUCK_TASK_STATUS: dict[str, str] = {
    "needs-revision": "task_revision",
    "blocked": "task_blocked",
    "awaiting-evidence": "task_awaiting_evidence",
}

#: What each stuck status actually asks of the person reading the board. Three statuses, three
#: different asks — pointing all of them at the same command would be the board saying "something
#: is wrong" and nothing more.
_STUCK_TASK_ACTION: dict[str, str] = {
    "task_revision": "/tasks",
    "task_blocked": "rein dag --render",
    "task_awaiting_evidence": "rein evidence show",
}


def _pending_item(severity: str, kind: str, subject: str, headline: str, action: str) -> dict[str, str]:
    """One queue row, with an `id` that survives re-derivation but changes when the row does.

    The id digests the headline rather than counting position: a queue row is the same row
    tomorrow only if it is still saying the same thing, and an index-based id would silently
    re-label every row below whichever one got fixed.
    """
    tag = hashlib.sha256(headline.encode("utf-8")).hexdigest()[:8]
    return {
        "id": f"{kind}:{subject}:{tag}",
        "severity": severity,
        "kind": kind,
        "subject": subject,
        "headline": headline,
        "action": action,
    }


def pending_queue(
    *,
    probe_gate: str | None,
    gate_blockers: Sequence[str] | None,
    chain_defects: int,
    unsandboxed_profiles: Sequence[str],
    unsandboxed_build_targets: Sequence[str],
    attention: Sequence[models.Event],
    task_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    """Everything standing between this repository and its next gate, worst first.

    Pure like :func:`next_action`, and for the same reason: the ordering of a human's work queue
    is a policy, and a policy that can only be exercised through a repository on disk is a policy
    nobody tests.

    `gate_blockers` is tri-state on purpose. `None` means readiness was **not probed** (no gate is
    in play, or the caller passed a probe that declined) — distinct from `[]`, which means it was
    probed and came back clean. Collapsing the two would let "we did not look" render as "there is
    nothing there", which is the one thing this codebase never allows a measurement to do.
    """
    items: list[dict[str, str]] = []

    if chain_defects:
        items.append(
            _pending_item(
                "blocking",
                "chain",
                "events.ndjson",
                f"the audit chain has {chain_defects} defect(s) — no gate receipt can be issued "
                "against a damaged log; restore it from git rather than rewriting it to agree",
                "rein events --verify",
            )
        )
    if unsandboxed_profiles:
        items.append(
            _pending_item(
                "blocking",
                "sandbox",
                ", ".join(unsandboxed_profiles),
                "profile(s) " + ", ".join(unsandboxed_profiles) + " run repository-derived code on the host",
                models.sandbox_setup_command(list(unsandboxed_build_targets) or list(unsandboxed_profiles)),
            )
        )
    if probe_gate is not None and gate_blockers is not None:
        # The remedy travels in the message: `readiness` writes each blocker as a sentence that
        # names what to run. The action here is the command that re-lists them authoritatively.
        check = f"rein approve {probe_gate} --check"
        items += [_pending_item("blocking", "gate_blocker", probe_gate, b, check) for b in gate_blockers]
        if not gate_blockers:
            index = GATE_ORDER.index(probe_gate) + 1
            items.append(
                _pending_item(
                    "attention",
                    "gate_ready",
                    probe_gate,
                    f"gate {index} ({probe_gate}) has no mechanical blocker left — it is waiting on your decision",
                    f"rein approve {probe_gate}",
                )
            )

    for event in attention:
        subjects = ", ".join(event.subject_ids) or "-"
        items.append(
            _pending_item(
                "attention",
                "escalation",
                subjects,
                f"#{event.seq} {event.event} ({subjects}) awaits a human decision",
                "rein events --summary",
            )
        )
    for row in task_rows:
        kind = _STUCK_TASK_STATUS.get(str(row.get("status")))
        if kind is None:
            continue
        task_id = str(row.get("id"))
        items.append(
            _pending_item(
                "attention",
                kind,
                task_id,
                f"{task_id} is {row.get('status')}: {row.get('title')}",
                _STUCK_TASK_ACTION[kind],
            )
        )
    # Warnings are deliberately absent: a warning is a diagnostic about reading the repository,
    # not a decision waiting in it, and it has its own section on every surface. The one that
    # would have mattered here — "readiness could not be probed" — is carried by `pending_deep`.

    # Stable: within a severity the emission order above is the reading order.
    return sorted(items, key=lambda item: PENDING_SEVERITY_ORDER.index(item["severity"]))


def _default_readiness(repo: repo_mod.Repo, gate: str) -> list[str]:
    """`approve.readiness`, imported at call time — `rein next` is on the hot path of every
    session start, and a board that is not being asked about a gate should not pay to import
    the module that answers gate questions.

    `already_approved_blocks=False`: the board is asking "what stands in the way of this gate",
    not confirming an approval — an already-approved gate reporting itself as its own blocker
    would say a healthy board is unready.
    """
    from rein import approve as approve_mod

    return approve_mod.readiness(repo, gate, already_approved_blocks=False)


def _trace_block(report: dag_trace.TraceReport) -> dict[str, object]:
    """The traceability slice: the verdict, plus the per-requirement thread the pane draws.

    `ok`/`errors`/`warnings` are the machine verdict `rein dag --trace` exits on. `requirements`
    is the same thread expanded one row per requirement, because a reader asking "which task answers
    R-3" should not have to re-derive it from two dictionaries. Both come from one TraceReport, so
    the table and the verdict can never disagree.
    """
    return {
        "ok": report.ok,
        "errors": report.errors,
        "warnings": report.warnings,
        "requirements": [
            {
                "id": rid,
                "nfr": dag_trace.is_nfr(rid),
                "claims": list(report.claims_by_requirement.get(rid, ())),
                "tasks": sorted(
                    {t for cid in report.claims_by_requirement.get(rid, ()) for t in report.tasks_by_claim.get(cid, ())}
                ),
            }
            for rid in report.requirements
        ],
    }


def _plan_block(plan: models.Plan) -> dict[str, object]:
    """The plan slice: what the Expected Model currently holds, and at what risk."""
    return {
        "cycle_id": plan.cycle_id,
        "digest": plan.digest(),
        "claims": len(plan.claims),
        "tasks": len(plan.tasks),
        "max_claim_risk": models.max_risk([c.risk for c in plan.claims]),
    }


def _review_block(review: models.Review | None) -> dict[str, object]:
    """The review slice. Deliberately reports the three axes separately — never one `verified`."""
    if review is None or not review.is_generated:
        return {"status": "not_generated"}
    verdicts: dict[str, int] = {}
    for result in review.claim_results:
        verdict = str(result.get("verdict", "unknown"))
        verdicts[verdict] = verdicts.get(verdict, 0) + 1
    return {
        "status": "generated",
        "machine_digest": review.machine_digest(),
        "human_status": review.human_status,
        "verdicts": verdicts,
        # "sufficient" or "undeterminable" — never a count that reads as "we checked and found none".
        "coverage": "sufficient" if review.coverage_sufficient else "undeterminable",
        "extra_behaviors": len(review.extra_behaviors) if review.coverage_sufficient else None,
        "blocking_security": len(review.blocking_security_findings),
    }


def task_status_of(root: str | Path | repo_mod.Repo) -> dict[str, str]:
    """`{task id: status}` for a repository, or {} when state.yaml cannot be read.

    Exists so that every view asking "is this attention event still open" reads the task statuses
    the same way. An unreadable state answers `{}`, which retires nothing — the safe direction for
    a queue whose job is to not lose a row.
    """
    repo = root if isinstance(root, repo_mod.Repo) else repo_mod.Repo(Path(root).resolve())
    try:
        state = store_mod.Store(repo).read_state()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError):
        return {}
    return dict(state.task_status) if state else {}


def collect_status(
    root: str | Path | repo_mod.Repo = ".",
    *,
    events_scanner: Callable[[Path], tuple[list[models.Event], list[event_chain.ChainDefect]]] | None = None,
    readiness_probe: Callable[[repo_mod.Repo, str], list[str]] | None = None,
) -> dict[str, object]:
    """The whole status object for the repository at `root`. Never raises for a readable repo.

    `events_scanner` is a seam for the dashboard: /api/status is the always-on poll, and
    answering it means reading the *whole* chain (a root is a digest over every record).
    Without the seam that reparse happens every few seconds for a file that rarely changes.

    `readiness_probe` is the same seam for the queue's blocking rows. Gate readiness costs git
    subprocesses and snapshot digests, which is fine once per `rein status` and wasteful
    several times a minute — so the dashboard passes a memoized probe, and a caller that passes
    one returning nothing gets `pending_deep: false` rather than a queue that quietly understates
    itself.
    """
    repo = root if isinstance(root, repo_mod.Repo) else repo_mod.Repo(Path(root).resolve())
    warnings: list[str] = []
    store = store_mod.Store(repo)

    state: models.State | None = None
    try:
        state = store.read_state()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read state.yaml: {exc}")
    if state is None and not warnings:
        warnings.append("no .rein/state.yaml yet")

    plan: models.Plan | None = None
    try:
        plan = store.read_plan()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read plan.yaml: {exc}")

    review: models.Review | None = None
    try:
        review = store.read_review()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read review.yaml: {exc}")

    config: models.Config | None = None
    try:
        config = store.read_config()
    except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
        warnings.append(f"cannot read config.yaml: {exc}")

    gates = {g: state.gate_status(g) for g in GATE_ORDER} if state else dict.fromkeys(GATE_ORDER, "pending")
    current_phase = state.current_phase if state else "brief"

    tasks_block: dict[str, object] | None = None
    counts: dict[str, int] | None = None
    trace_block: dict[str, object] | None = None
    if plan is not None:
        try:
            graph = dag.join(plan, state)
            tasks_block = _tasks_block(graph, state)
            counts = graph.counts()
            trace_block = _trace_block(dag_trace.trace_repo(repo, plan, graph))
        except dag.DagError as exc:
            warnings.append(f"the task graph is inconsistent: {exc}")

    events, defects = (events_scanner or event_chain.scan)(repo.events)
    if defects:
        warnings.append(f"the audit chain has {len(defects)} defect(s)")
    task_status = state.task_status if state else {}
    attention = events_mod.open_attention(events, task_status)

    template_mode = config.template_mode if config else False
    placeholders = _is_placeholder(state.project) if state else True
    unsandboxed_profiles = config.unsandboxed_code_profiles() if config else []
    unsandboxed_build_targets = config.unsandboxed_build_targets() if config else []

    # Probe readiness for the gate the *current phase* will present — not merely the first
    # unapproved one. In `brief` and `done` no gate is in play, and in an uninitialized template
    # every gate is blocked by the initialization itself, which the recommendation already says.
    probe_gate = PHASE_GATE.get(current_phase)
    gate_blockers: list[str] | None = None
    if probe_gate is not None and state is not None and not template_mode and not placeholders:
        try:
            gate_blockers = list((readiness_probe or _default_readiness)(repo, probe_gate))
        except (OSError, models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
            warnings.append(f"cannot check gate readiness for '{probe_gate}': {exc}")

    recommendation = next_action(
        current_phase=current_phase,
        gates=gates,
        counts=counts,
        attention_count=len(attention),
        chain_defects=len(defects),
        template_mode=template_mode,
        placeholders=placeholders,
        gate_chain_broken=bool(state.gate_chain_violations()) if state else False,
        plan_missing=plan is None,
        unsandboxed_profiles=unsandboxed_profiles,
        unsandboxed_build_targets=unsandboxed_build_targets,
        # None when readiness was not probed — the table must not read that as "blocked".
        gate_ready=None if gate_blockers is None else not gate_blockers,
        open_change_requests=len(state.change_requests_for(probe_gate, "open")) if state and probe_gate else 0,
    )
    # A /-command only exists inside an agent whose surface was installed; recommending one in a
    # repo with no integration would send the user to a command their agent has never heard of.
    if recommendation.command.startswith("/") and _no_agent_surface(repo):
        recommendation = dataclasses.replace(
            recommendation,
            reason=recommendation.reason
            + " (No agent surface is installed — run `rein install <agent>`, then open a new"
            " session so the /-commands exist in your agent.)",
        )

    plan_block = _plan_block(plan) if plan is not None else None
    task_rows = tasks_block["rows"] if tasks_block else []
    pending = pending_queue(
        probe_gate=probe_gate,
        gate_blockers=gate_blockers,
        chain_defects=len(defects),
        unsandboxed_profiles=unsandboxed_profiles,
        unsandboxed_build_targets=unsandboxed_build_targets,
        attention=attention,
        task_rows=task_rows if isinstance(task_rows, list) else [],
    )

    return {
        "project": state.project if state else None,
        "cycle_id": state.cycle_id if state else None,
        "branch": config.work_branch if config else None,
        "current_phase": current_phase,
        "updated_at": state.raw.get("updated_at") if state else None,
        "phase_order": list(PHASE_ORDER),
        "gates": [
            {
                "name": g,
                "status": gates[g],
                "index": i + 1,
                "phase": GATE_PHASE[g],
                "approval_id": (state.gate_receipt(g) or {}).get("approval_id") if state else None,
            }
            for i, g in enumerate(GATE_ORDER)
        ],
        "plan": plan_block,
        "plan_status": state.plan_status if state else "draft",
        "review": _review_block(review),
        "tasks": tasks_block,
        "trace": trace_block,
        "template_mode": config.template_mode if config else False,
        "github_enabled": bool(config.github.get("enabled")) if config else False,
        "chain": {
            "root": event_chain.chain_root(events),
            "events": len(events),
            "defects": [str(d) for d in defects],
        },
        "attention": [
            {"seq": e.seq, "ts": e.ts, "event": e.event, "subject_ids": list(e.subject_ids)} for e in attention
        ],
        "next": asdict(recommendation),
        "decision": pending_decision(
            recommendation,
            next((g for g in GATE_ORDER if gates[g] != "approved"), None),
            pending=pending,
        ),
        "pending": pending,
        # False means gate readiness was not probed — see pending_queue's tri-state note.
        "pending_deep": gate_blockers is not None,
        "warnings": warnings,
        "generated_at": event_chain.now_iso(),
    }


def render_next(next_obj: dict[str, object]) -> str:
    """The recommendation as 2–3 human lines (`rein next`)."""
    lines = [f"next: {next_obj.get('command', '')}", f"  why: {next_obj.get('reason', '')}"]
    also = next_obj.get("also") or ()
    if isinstance(also, (list, tuple)) and also:
        lines.append(f"  also: {', '.join(str(a) for a in also)}")
    return "\n".join(lines)


def render_pending(status: dict[str, object], *, limit: int = 12) -> list[str]:
    """The queue as board lines — first on the screen, because it is what the reader came for.

    Truncates rather than scrolls: a board that prints forty rows is a log, and the count in the
    heading is what tells the reader whether the list they can see is the whole list.
    """
    pending = status.get("pending")
    if not isinstance(pending, list) or not pending:
        return []
    blocking = sum(1 for item in pending if item.get("severity") == "blocking")
    heading = f"### Waiting on you ({len(pending)}"
    heading += f", {blocking} blocking)" if blocking else ")"
    if not status.get("pending_deep"):
        heading += "   [gate readiness not probed]"
    lines = ["", heading]
    for item in pending[:limit]:
        action = f"  → {item['action']}" if item.get("action") else ""
        lines.append(f"- [{item['severity']}] {item['subject']}: {item['headline']}{action}")
    if len(pending) > limit:
        lines.append(f"- … {len(pending) - limit} more (`rein status --json`)")
    return lines


def render(status: dict[str, object]) -> str:
    """The human-facing board: where you are, what is approved, what is grounded, what is next."""
    lines = [
        f"project: {status.get('project')}   cycle: {status.get('cycle_id')}   "
        f"phase: {status.get('current_phase')}   plan: {status.get('plan_status')}",
    ]
    lines += render_pending(status)
    lines += ["", "### Gates"]
    gates = status.get("gates")
    if isinstance(gates, list):
        for gate in gates:
            approval = gate.get("approval_id") or "-"
            lines.append(f"- {gate['index']}. {gate['name']}: {gate['status']}  (approval: {approval})")

    plan = status.get("plan")
    if isinstance(plan, dict):
        lines += [
            "",
            "### Plan",
            f"- claims: {plan['claims']}   tasks: {plan['tasks']}   max claim risk: {plan['max_claim_risk']}",
        ]

    review = status.get("review")
    if isinstance(review, dict) and review.get("status") == "generated":
        extras = review.get("extra_behaviors")
        extras_text = "undeterminable (coverage gap)" if extras is None else str(extras)
        lines += [
            "",
            "### Review",
            f"- coverage: {review['coverage']}   extra behaviours: {extras_text}",
            f"- verdicts: {review.get('verdicts')}   human review: {review.get('human_status')}",
        ]

    tasks = status.get("tasks")
    if isinstance(tasks, dict):
        counts = tasks["counts"]
        assert isinstance(counts, dict)
        lines += ["", "### Tasks", "- " + " / ".join(f"{k}={v}" for k, v in counts.items())]

    chain = status.get("chain")
    if isinstance(chain, dict):
        defects = chain["defects"]
        assert isinstance(defects, list)
        lines += ["", f"### Audit chain\n- {chain['events']} event(s), {len(defects)} defect(s)"]

    warnings = status.get("warnings")
    if isinstance(warnings, list) and warnings:
        lines += ["", "### Warnings"] + [f"- {w}" for w in warnings]

    next_obj = status.get("next")
    if isinstance(next_obj, dict):
        lines += ["", render_next(next_obj)]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the deterministic status object and next action")
    parser.add_argument("--json", action="store_true", help="print the whole status object as JSON")
    parser.add_argument("--next", action="store_true", help="print only the next recommended command")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    status = collect_status(repo)
    if args.next:
        # --next is the narrower request, so it wins when both are given: `rein next
        # --json` is what an integration calls for one machine-readable recommendation.
        next_obj = status.get("next")
        next_obj = next_obj if isinstance(next_obj, dict) else {}
        print(json.dumps(next_obj, ensure_ascii=False, default=str) if args.json else render_next(next_obj))
        return 0
    if args.json:
        print(json.dumps(status, indent=2, ensure_ascii=False, default=str))
        return 0
    print(render(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
