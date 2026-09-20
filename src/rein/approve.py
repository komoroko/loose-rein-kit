"""`rein approve <gate>` — the human's command, and the only path to an approved gate.

Three steps, in this order, none skippable:

  1. **Readiness** — every mechanical precondition for the gate (:func:`readiness`), reported
     exhaustively rather than one at a time.
  2. **Confirmation** — the digests this approval covers are printed, and a human confirms:
     `[y/N]` at an interactive terminal (:func:`confirm_locally`), or in the dashboard, whose
     write session is minted only by redeeming the launch link printed to the terminal `rein ui`
     runs in. Two channels of the same kind; the receipt records which.
  3. **Receipt** — one Central Store transaction writes the gate receipt, binding those digests
     and the audit-chain root (:func:`record_approval`). Approving the **mandate** additionally
     freezes it in that same transaction: `state.plan` gains `frozen` plus the digests the freeze
     covers, which is what `rein build` requires and what `rein guard` rule 2 protects.

**A gate wherever undoing gets expensive, and nowhere else.** `mandate` says what the loop may
change and what it must prove; `acceptance` says whether the change, with that evidence, is taken.
There were five — one per phase — and the same transaction that recorded an approval also advanced
`current_phase`, so authority and progress were one write and every permission question arrived as
an ordering question. Every mechanical precondition the five enforced is still here, as a readiness
check; what is gone is the claim that the *order* of the work is a human's to authorize.

Two is where that criterion lands for a change that is only code, and for a while two was also what
the tool could represent — a fixed tuple and a `gates` object that refused any other key. That is a
ceiling on how often a human is asked, which is the thing the approval-screen budget already got
wrong once. So the count is derived now: a task that freezes an `operator_surface` it cannot undo
is an irreversible point of its own, it gets a gate named for that task, and `rein build` stops
there before running it. `approve mandate` is what adds them, out of the plan it is freezing — the
act that fixes what will be built is the act that fixes how many more times this cycle stops.

There is no `--force` and no `--by`: an identity you can type is not an identity, so the
receipt records that *a* human confirmed, never which one.

**Nothing here proves a human approved, and this module never claims it does** — an agent driving
a pty can answer a prompt. What holds is the narrower claim AGENTS.md "Gate rules" 2 states: an
approval cannot happen by accident, by default, or by a configuration someone pre-authorized. The
TTY requirement below is one of the three mechanisms carrying it.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from typing import TypedDict

from rein import (
    audit,
    change_request,
    common,
    dag,
    dag_trace,
    digests,
    event_chain,
    gate_guard,
    mdlite,
    models,
    observations,
    review_policy,
    review_reading,
)
from rein import lenses as lens_lib
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The documents the mandate is written from, for the receipt's `artifact_digest` and for the
#: `[NEEDS CLARIFICATION]` sweep. They are *material*, not gates of their own: a human approving a
#: mandate is approving the scope, the claims and the acceptance criteria it states, and these are
#: where the reasoning behind them is written down.
MANDATE_SOURCES: tuple[str, ...] = ("docs/10-requirements.md", "docs/20-design.md")


#: The marker a phase agent leaves at the exact spot it would otherwise have guessed.
_CLARIFICATION_RE = re.compile(r"\[NEEDS CLARIFICATION\b")


class ApprovalError(RuntimeError):
    """The gate cannot be approved, or an approval cannot be recorded."""


# --- readiness ------------------------------------------------------------------


def _chain_blockers(state: models.State, gate: str, *, already_approved_blocks: bool) -> list[str]:
    pending = state.pending_upstream(gate)
    if pending:
        return [
            f"gate '{pending}' is still pending — approving '{gate}' now would leave a decision "
            "standing on one that was never made (a gate opens only once everything it rests on has)"
        ]
    if already_approved_blocks and state.gate_status(gate) == "approved":
        return [f"gate '{gate}' is already approved"]
    return []


def _plan_blockers(repo: repo_mod.Repo, plan: models.Plan | None, gate: str) -> list[str]:
    if plan is None:
        return [f"no plan at .rein/plan.yaml — there is nothing for gate '{gate}' to approve"]
    report = dag_trace.trace_repo(repo, plan)
    blockers: list[str] = list(report.errors)
    if not report.checked:
        blockers.append(
            "no requirement id on either side — the traceability thread is unknown, not whole. "
            "`/req` declares R-N / NFR-N headings and writes the matching claims into .rein/plan.yaml"
        )

    # A plan with nothing in it passes every consistency check trivially. Requiring content is
    # the difference between "no contradictions found" and "there is something here to approve".
    if not plan.claims:
        blockers.append(
            "the plan states no claims — there is nothing to approve. `/req` turns each `R-N` / "
            "`NFR-N` heading into a claim in .rein/plan.yaml."
        )
    if not plan.tasks:
        blockers.append("the plan declares no tasks")
    return blockers


def _task_blockers(plan: models.Plan | None, state: models.State | None, gate: str) -> list[str]:
    """Every claim has somewhere to be answered, and — at acceptance — every task is finished.

    The claim check belongs to the mandate: a claim no task answers is a thing the loop has not
    been given a way to prove, and that is a defect in what is being authorized rather than in the
    work. The task check belongs to acceptance, where "is it done" is the question.
    """
    if plan is None:
        return []
    try:
        graph = dag.join(plan, state)
    except dag.DagError as exc:
        return [str(exc)]
    blockers = [f"{cid}: no task is answerable for this claim" for cid in graph.claims_without_a_task(plan)]
    if gate == "acceptance":
        unfinished = sorted(t.id for t in graph.tasks if not t.is_done)
        if unfinished:
            blockers.append(f"tasks not done: {', '.join(unfinished)}")
    return blockers


def _review_blockers(
    repo: repo_mod.Repo, review: models.Review | None, state: models.State | None, gate: str
) -> list[str]:
    """The acceptance gate's preconditions carried by the machine review (plan §16.8).

    A readiness check that passes because a stage has not been implemented yet is worse than
    no check at all, so an absent review is a blocker rather than a shrug.

    The mechanical half is `review_policy.blocking_reasons` — the module that owns the acceptance
    decision — rather than a second copy of the same rules here. Two copies had already drifted:
    this one never looked at `machine.gaps`, so a comparator could mark an actual-coverage gap
    blocking, have it written to `review.yaml`, and watch the gate open anyway.
    """
    if gate != "acceptance":
        return []
    if review is None or not review.is_generated:
        return [
            "no machine review has been generated — run `rein review generate`. "
            "Acceptance rests on a grounded review, not on a green test run."
        ]
    blockers = review_policy.blocking_reasons(review, review.effective_risk)
    # Three documents say a later commit leaves the review stale. Only the UI pane had ever
    # checked, so generate → commit → approve opened acceptance over code no reviewer saw. Asked on
    # the product's content rather than on HEAD's id, because the workflow's own
    # `review.yaml` commit is a later commit and must not invalidate the thing it records
    # (`review_reading.freshness`).
    if reason := review_reading.freshness(repo, review, state).reason:
        blockers.append(reason)
    if review.human_status != "frozen":
        blockers.append(
            f"the human review is '{review.human_status}', not 'frozen' — "
            "complete it in the review UI (`rein review complete`)"
        )
    return blockers


def _audit_blockers(repo: repo_mod.Repo, state: models.State, config: models.Config | None, gate: str) -> list[str]:
    """The one security answer acceptance cannot read off the grounded review.

    Everything else it needs about security the review already holds, bound to the reviewed HEAD.
    A dependency audit is different in kind: the same commit audited last month and today can
    differ, because the database moved while the code did not. `verify.md` has said so since it
    existed and nothing ran it — the instruction lived in a prompt, no document held the answer,
    and no readiness check asked for one, so a release could be signed with the audit having been
    "done" in a chat window.

    A project that declares no audit command is told so rather than waved through: "we have no way
    to ask" is not "there is nothing wrong".
    """
    if gate != "acceptance":
        return []
    if not audit.configured(config):
        return [
            "no `security.dependency_audit.command` is configured, so this release has no "
            "dependency answer at all. It is the one security question a review cannot answer once "
            "— add the command (pip-audit, npm audit, cargo audit, `make audit`) and run "
            "`rein audit run`."
        ]
    reason = audit.staleness(
        state.raw.get("dependency_audit"),
        dependencies=audit.dependency_digest(repo),
        now=datetime.now(timezone.utc),
        max_age=audit.max_age_days(config),
    )
    return [reason] if reason else []


#: How many out-of-mandate paths one blocker names before it says how many more there are. A cut
#: that did not say it was a cut would make "these are the paths" and "these are some of them"
#: read the same, which is the distinction the Coverage Manifest exists for (plan §2.4).
_NAMED_PATHS = 10


def _boundary_blockers(
    repo: repo_mod.Repo,
    plan: models.Plan | None,
    state: models.State | None,
    review: models.Review | None,
    gate: str,
) -> list[str]:
    """Is what acceptance is about to ratify inside the mandate that authorized it?

    **Rule 3 has three checkpoints and all three sit on the path a change takes through the loop**
    (`gate_guard`): the editor hook, which is a host capability; the commit-stage check, which is
    a repository's own `.pre-commit-config.yaml`; and `build_loop._gate_violations`, which is
    inside `rein build`. A change that never went through `rein build` — a human's own commit, an
    agent on a host with no hook, `git commit -n` — passes none of them. The guard's module
    docstring has said so; what it does not say is where that is caught instead, and the answer
    was nowhere. Acceptance is the one point every change in the cycle reaches, and everything it
    asked was about records: the chain, the review, the requests. Never about the tree.

    So: the same rule, read once over the cycle's committed diff, at the moment a human is about
    to take it. **Not a fourth layer of enforcement** — nothing here denies a write, and a cycle
    whose work went through `rein build` produces no finding, because merge-stage already refused
    those paths one at a time.

    It blocks rather than merely naming, and the approval-screen budget is the reason that is
    allowed: a limit whose remedy does not exist at the point it fires gets raised instead of
    obeyed. Both of this one's remedies exist here — `rein revise --to mandate` widens the scope a
    human approved, or the change comes out of the branch.

    **The subject is the review's own, not one resolved a second time here.**
    `binding.trusted_base_sha..subject_head_sha` is the change the reviewers read and the change
    this approval takes; re-deriving a base would let the boundary be checked over a different
    span from the one being accepted. It follows that an absent or ungenerated review produces
    nothing here — `_review_blockers` already holds the gate shut, and there is no subject to
    measure until it does.

    That span is two trees compared, so it cannot say *who* wrote a path, and this does not
    claim it can. A branch that took in history from elsewhere — a mainline merged back in
    mid-cycle — carries those paths in the span too, and the reviewers were shown that code as
    part of this change for the same reason. The finding is true of the change being accepted
    either way, and the blocker names the third repair that case needs (`cycle.base_commit`)
    rather than asserting the cycle wrote what it may not have.

    Fails closed on every question it cannot answer, for the reason the guard does: a boundary
    that cannot be determined must not be reported as held. A config this cannot read is not one
    of those questions — `readiness` reads the document before it calls anything, and an
    unreadable one is already the whole answer.
    """
    if gate != "acceptance" or plan is None or state is None or state.plan_status != "frozen":
        return []
    if review is None or not review.is_generated:
        return []
    base, head = str(review.binding.get("trusted_base_sha") or ""), review.subject_head_sha
    if not base or not head:
        return [
            "the review does not say which commits it read (`binding.trusted_base_sha` / "
            "`subject_head_sha`), so acceptance cannot tell whether the change stayed inside the "
            "mandate. Regenerate it with `rein review generate`."
        ]
    settings = gate_guard.guard_settings(repo)
    if settings.template_mode:
        return []
    # `-z`: NUL-separated and never quoted, whatever `core.quotePath` says and whatever bytes the
    # filename holds. Read as lines, a path with a non-ASCII byte comes back as `"src/\346\227\245.py"`
    # — a string no prefix in the mandate matches, so the check reports "inside" about a file it
    # never recognised, while the editor hook, handed the real path, blocks it. One rule answering
    # two ways is exactly what `gate_guard.outside_the_mandate` exists to prevent.
    rc, out = repo._git_rc("diff", "-z", "--name-only", f"{base}..{head}")
    if rc != 0:
        return [
            f"`git diff --name-only {base[:12]}..{head[:12]}` failed, so the paths this change "
            "carries are unknown and acceptance cannot tell whether they are inside the mandate. "
            "Fetch the commits the review was taken on, then ask again."
        ]
    include, exclude = plan.scope
    outside = [
        (path, why)
        for path in sorted({entry for entry in out.split("\0") if entry})
        if (why := gate_guard.outside_the_mandate(path, include=include, exclude=exclude, guarded=settings.paths))
    ]
    if not outside:
        return []
    named = ", ".join(path for path, _ in outside[:_NAMED_PATHS])
    more = f" (and {len(outside) - _NAMED_PATHS} more)" if len(outside) > _NAMED_PATHS else ""
    reasons = sorted({why for _, why in outside})
    return [
        f"{len(outside)} path(s) in the change this review read are {' and '.join(reasons)}: "
        f"{named}{more}. The mandate is what authorizes a change to the product, and these were "
        "not covered by the one that was approved — a change that never went through `rein build` "
        "meets no other checkpoint. Either widen the scope a human approved "
        "(`rein revise --to mandate`, then re-approve), or take the change out of the branch. If "
        "a path is here because the branch took in history from elsewhere, it is "
        f"`cycle.base_commit` that is wrong: the reviewers read {base[:12]}..{head[:12]} as this "
        "cycle's change and were shown that code too."
    ]


def _baseline_blockers(state: models.State, gate: str) -> list[str]:
    """The mandate decides that this plan is implementable against this tree. It has to know the tree.

    The measurement used to live inside `rein build`, taken just before the first batch — which is
    after this approval. So a cycle could be approved and started on a work branch whose `check`
    had been red for weeks, and the discovery was the first task's to make: three implementer
    launches spent on a failure it had not caused, in a scope that did not contain it, and three
    `task_failed` verdicts in a chain that never rotates.

    A red baseline is not refused. It is required to be a *decision*: `rein baseline measure
    --freeze` says a human looked at it and started anyway, and the loop then stops a task that
    hits one of those steps rather than sending it back.
    """
    if gate != "mandate":
        return []
    baseline = state.baseline
    if not baseline:
        return [
            "no baseline is recorded — approving a mandate says this plan is implementable against "
            "this tree, and nothing has asked the tree. Run `rein baseline measure`."
        ]
    red = sorted(state.baseline_red())
    if red and baseline.get("frozen") is not True:
        return [
            f"the work branch is already red on {', '.join(red)} and nobody has said so on the record. "
            "Fix it, or `rein baseline measure --freeze` to approve it as known — a task that fails one "
            "of these is then stopped rather than sent back to an implementer who cannot fix it."
        ]
    return []


def _change_request_blockers(plan: models.Plan | None, state: models.State, gate: str) -> list[str]:
    """Open change requests hold the gate shut. This is what makes declining mean something.

    Without it "not yet, change R-3" was a sentence in a chat window: the gate stayed ready, the
    board kept recommending an approval, and a new session had no idea a human had already said
    no. An `addressed` request does not block — it is listed on the approval screen instead.

    The mandate answers for **two** sets of requests, because it is the one approval that can end
    another gate's existence: freezing the plan re-derives the crossing gates from it, so a
    crossing the new cut does not declare irreversible is gone in that same write. An open request
    standing against such a gate would survive the write pointing at a gate the cycle no longer
    has — recorded, holding nothing shut, invisible to every `--gate` this cycle can name. So it
    blocks here instead: either the request is addressed (and the approval closes it, the way every
    other addressed request is closed), or the plan keeps the task irreversible and the gate with
    it. Nothing is dropped, and nothing is dropped silently.
    """
    blockers = [
        f"{cr.get('id')} is an open change request against {cr.get('target')}: {cr.get('reason')}"
        for cr in state.change_requests_for(gate, "open")
    ]
    if gate != FREEZING_GATE or plan is None:
        return blockers
    for cr in change_request.open_against(state, gates_dropped_by(plan, state)):
        blockers.append(
            f"{cr.get('id')} is an open change request against gate {cr.get('gate')}, which this "
            f"plan no longer declares irreversible — approving would delete the gate it is holding "
            f"shut. Answer it (`rein changes address {cr.get('id')} --note …`), or keep the task's "
            "`operator_surface` declaration at `reversible: false`."
        )
    return blockers


def gates_dropped_by(plan: models.Plan, state: models.State) -> tuple[str, ...]:
    """The crossing gates this cycle has that freezing `plan` would remove.

    Takes the plan rather than the repository, because "the plan that is about to be frozen" is
    something both callers already hold and re-reading it would let them disagree about it. The
    difference matters: `plan.crossing_task_ids` is `()` both for a plan that declares nothing
    irreversible and for one nobody could read, and subtracting the second from this cycle's gates
    says every crossing is about to be deleted. There is no plan to pass in that case, so the
    question is not asked.
    """
    keeping = set(plan.crossing_task_ids)
    return tuple(g for g in state.crossing_gates if g not in keeping)


def _clarification_blockers(repo: repo_mod.Repo, gate: str) -> list[str]:
    """Unresolved `[NEEDS CLARIFICATION]` markers in the documents the mandate is written from.

    Three documents told the reader this check existed; nothing ran it, so a marker left standing
    opened the gate anyway and the question it named was answered by whatever default the draft
    had already been written against.

    HTML comments are dropped first: the scaffold explains the marker convention *using* the
    marker, and a check that cannot tell guidance from an open question is one nobody can leave
    switched on.
    """
    if gate != "mandate":
        return []
    blockers: list[str] = []
    for artifact in MANDATE_SOURCES:
        path = repo.path(artifact)
        if not path.exists():
            # Absence is a different failure, and not this function's to report: `_plan_blockers`
            # already refuses a gate with nothing behind it.
            continue
        try:
            body = path.read_text(encoding="utf-8")
        except OSError as exc:
            blockers.append(f"cannot read {artifact}: {exc}")
            continue
        lines = [
            str(n)
            for n, line in enumerate(mdlite.strip_comments(body).splitlines(), 1)
            if _CLARIFICATION_RE.search(line)
        ]
        if lines:
            blockers.append(
                f"{artifact} still carries {len(lines)} unresolved `[NEEDS CLARIFICATION]` marker(s) "
                f"(line {', '.join(lines)}) — ask the human and record the answer under `## Clarifications`, "
                "or demote it to `## Open questions` with the assumption you wrote the text under."
            )
    return blockers


def _unknowns_admitted(repo: repo_mod.Repo) -> int:
    """How many decisions this mandate admits it has no answer to.

    Counted because the claim it tests is one this harness makes loudly: that saying `unknown` at
    the mandate is what buys fewer interventions during the build. The other half of that pair is
    `judgement_raised`. A mandate that admitted nothing and then raised a dozen judgements is the
    shape that would falsify it.
    """
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return 0
    return sum(1 for d in plan.decisions if d.status == "unknown") if plan is not None else 0


def _decision_blockers(plan: models.Plan | None, gate: str) -> list[str]:
    """A `mandate`-reach decision the gate cannot be opened over. Two of them.

    `plan.decisions` records how each thing the drafting phase met was settled, and by whom. The
    ones that reach the mandate are the ones whose reversal would collapse the scope; the rest the
    loop settled itself, on the reading that undoing them later costs one task. A `mandate`
    decision still `unknown` is one state the gate cannot be opened over: the loop would be told
    what it may change while what it may change is the undecided thing. A `mandate` decision the
    loop *settled* is the other, and it is the same defect read from the opposite side — the
    record classified it as a human's to make and then made it anyway. Neither is left to the gate
    screen, where the only way to object is to notice a line and speak up.

    Deliberately not a count. Any number of `unknown` decisions the loop owns is fine and is
    recorded rather than guessed at; one that the mandate rests on is not, however few there are.
    The exits are both `/req`'s: narrow the mandate so it does not cover the undecided thing, or
    make finding the answer this cycle's scope, with claims about what will be established rather
    than what will be built.
    """
    if gate != "mandate" or plan is None:
        return []
    out: list[str] = []
    unknown = [d for d in plan.decisions if d.reach == "mandate" and d.status == "unknown"]
    if unknown:
        listed = "; ".join(f"{d.id} ({d.subject})" for d in unknown)
        out.append(
            f"{len(unknown)} decision(s) the mandate rests on are still `unknown`: {listed}. "
            "A scope cannot be delegated while what it covers is the undecided thing. Either narrow "
            "the mandate so it does not reach them, or make answering them this cycle's scope."
        )
    unasked = [d for d in plan.decisions if d.settled_without_asking]
    if unasked:
        listed = "; ".join(f"{d.id} ({d.subject})" for d in unasked)
        out.append(
            f"{len(unasked)} decision(s) reach the mandate and the loop settled them itself: {listed}. "
            "`reach: mandate` is the record saying a human settles this one; answer each and set "
            "`settled_by: human`, or change the reach and say in `rationale` why undoing it stays local."
        )
    return out


def readiness(repo: repo_mod.Repo, gate: str, *, already_approved_blocks: bool = True) -> list[str]:
    """Every mechanical reason `gate` cannot be approved. Empty means a request may be issued.

    Deliberately exhaustive rather than short-circuiting: being handed one blocker, fixing it,
    and being handed the next is exactly the review friction plan §2.6 budgets against.

    `already_approved_blocks=False` is for a status board asking "what stands in this gate's
    way" rather than confirming an approval (`status_api._default_readiness`, `ui.py`) — an
    already-approved gate reporting itself as its own blocker would read a healthy board as
    unready.
    """
    if not models.gate_name_ok(gate):
        raise ApprovalError(f"unknown gate {gate!r} ({models.gate_names()})")

    store = store_mod.Store(repo)
    # Before the documents are read, not after. A newer release widens a schema, so the very
    # symptom of being behind is that a read raises — and a check that ran afterwards was a check
    # that never ran in the case it was written for. Reported rather than raised, because "you are
    # behind" is a thing a board can show beside every other blocker, and an exception is not.
    behind = store.behind()
    if behind is not None:
        return [
            f"{behind}. A gate receipt binds digests this process computed, and `confirmed_via` "
            "records which channel confirmed rather than which release wrote it — so a receipt "
            "written from here would be indistinguishable afterwards from a sound one. Upgrade "
            "first, and restart any running `rein ui`."
        ]

    try:
        state = store.read_state()
        plan = store.read_plan()
        review = store.read_review()
        config = store.read_config()
    except models.DocumentError as exc:
        return [str(exc)]

    if state is None:
        return ["no .rein/state.yaml — run `rein init` first"]
    if absent := state.gate_absence_reason(gate):
        raise ApprovalError(absent)

    blockers: list[str] = []
    _, defects = event_chain.scan(repo.events)
    if defects:
        blockers.append(
            f"the audit chain has {len(defects)} defect(s) — a receipt binds the chain root, so it "
            "cannot be issued against a damaged log (see `rein events --verify`)"
        )
    blockers += _chain_blockers(state, gate, already_approved_blocks=already_approved_blocks)
    blockers += _baseline_blockers(state, gate)
    blockers += _boundary_blockers(repo, plan, state, review, gate)
    blockers += _audit_blockers(repo, state, config, gate)
    blockers += _change_request_blockers(plan, state, gate)
    blockers += _clarification_blockers(repo, gate)
    blockers += _decision_blockers(plan, gate)
    blockers += _plan_blockers(repo, plan, gate)
    blockers += _task_blockers(plan, state, gate)
    blockers += _review_blockers(repo, review, state, gate)
    return blockers


# --- what an approval covers -------------------------------------------------------


def approval_subject(repo: repo_mod.Repo, gate: str) -> dict[str, str]:
    """Every digest this approval would cover, including the audit-chain root at this moment.

    The human reads this before confirming, and :func:`record_approval` writes it into the
    receipt unchanged — so an approval can never be presented for a plan, a review, or a log
    other than the one that was on screen. If any of these move afterwards, the approval stops
    applying to what moved, which is what makes a stale review a blocker rather than a note.
    """
    store = store_mod.Store(repo)
    state = store.read_state()
    plan = store.read_plan()
    review = store.read_review()
    config = store.read_config()
    events, _ = event_chain.scan(repo.events)

    subject: dict[str, str] = {
        "repository_id": repo.repository_id,
        "cycle_id": state.cycle_id if state else "",
        "attested_chain_root": event_chain.chain_root(events),
    }
    if plan is not None:
        subject["plan_digest"] = plan.digest()
    if config is not None:
        subject["config_digest"] = config.frozen_digest()
        # Recorded, never compared against the freeze. The pin is allowed to move within a cycle
        # (a task adds a dependency, the image is rebuilt), so what a receipt can honestly say is
        # *which* environment the approval was taken over — which is what makes a later "the
        # evidence was produced somewhere else" answerable at all. The schema declared this slot
        # and nothing ever filled it.
        subject["environment_digest"] = config.environment_digest()
    if review is not None and review.is_generated:
        subject["machine_digest"] = review.machine_digest()
        subject["human_digest"] = review.human_digest()
    if gate == "mandate":
        # One digest over the documents the mandate is written from, in a fixed order, so the
        # receipt binds the prose a human read and not only the machine-readable plan. Present-only
        # — a repository with no design document has one fewer source, not a missing one.
        present = {path: digests.of_file(repo.path(path)) for path in MANDATE_SOURCES if repo.path(path).is_file()}
        if present:
            subject["artifact_digest"] = digests.of(present)
    subject["validation_digest"] = digests.of({"gate": gate, "readiness": "clear"})
    return subject


# --- recording an approval ---------------------------------------------------------

#: Receipt keys carried straight from the subject. `repository_id` and `cycle_id` are not
#: digests and already live in state.yaml, so they stay out of the receipt.
_RECEIPT_DIGESTS = (
    "plan_digest",
    "config_digest",
    "environment_digest",
    "machine_digest",
    "human_digest",
    "artifact_digest",
    "validation_digest",
    "attested_chain_root",
)

#: The gate whose approval freezes the Expected Model. The mandate says what the loop may change,
#: what must become true, and what it will be built against; everything after is measured against
#: that freeze.
FREEZING_GATE = "mandate"

#: The keys `state.plan` carries once frozen — exactly the set `revise.apply` clears on a roll
#: back. A key written here and not cleared there would survive an un-freeze and let a later
#: check "verify" against a freeze that no longer holds. `revise` imports this rather than
#: repeating it: the two lists agreeing was, until now, a thing somebody had to remember.
FROZEN_PLAN_KEYS = ("digest", "config_digest", "environment_digest", "sources", "frozen_at")

#: Documents outside `plan.yaml` that the implementation phase reads, other than the task tickets
#: (which come from the plan's own task ids). Present-only: a repository without a baseline
#: document simply has one fewer source, not a missing one.
_SOURCE_DOCS = ("docs/10-requirements.md", "docs/20-design.md", "docs/05-current-state.md")


def implementation_sources(repo: repo_mod.Repo, plan: models.Plan) -> dict[str, str]:
    """Every prose document the build will read, digested, keyed by repo-relative path.

    The gap this closes: `plan.yaml` is frozen by digest, and the documents an implementer is
    actually pointed at — its ticket, the design section covering its claims — were bound to
    nothing at all. A ticket edited after the mandate changed what got built, with no record anywhere
    that the thing built was not the thing approved.

    Digested over the file's bytes as they sit in the working tree, because that is what a human
    reading the repository sees. Whether those bytes have also been *committed* is a separate
    question, and a separate check: a parallel leaf is cut from the work branch's tip and can only
    read what is committed there.
    """
    paths = [*_SOURCE_DOCS, *(f"docs/tasks/{task.id}.md" for task in plan.tasks)]
    artifacts = plan.raw.get("cycle", {}).get("artifacts")
    if isinstance(artifacts, dict):
        paths += [str(ref.get("path", "")) for ref in artifacts.values() if isinstance(ref, dict)]
    found: dict[str, str] = {}
    for path in sorted(set(p for p in paths if p)):
        candidate = repo.path(path)
        if candidate.is_file():
            found[path] = digests.of_file(candidate)
    return found


def _frozen_plan_block(repo: repo_mod.Repo, subject: Mapping[str, str]) -> dict[str, object]:
    """`state.plan` as the mandate approval freezes it, refusing if the documents moved since `subject`.

    Recomputed here rather than copied from `subject` because `subject` was assembled *before*
    the human read it and typed. If plan.yaml or config.yaml moved in between, freezing the
    stale digest would record a freeze of bytes nobody approved — and the chain-root guard above
    does not cover these two files. Same posture, one document further out.
    """
    store = store_mod.Store(repo)
    plan = store.read_plan()
    config = store.read_config()
    if plan is None:
        raise ApprovalError("approving a mandate freezes .rein/plan.yaml, and there is no plan to freeze")
    if config is None:
        raise ApprovalError("approving a mandate freezes .rein/config.yaml, and there is no config to freeze")

    for name, current, presented in (
        ("plan.yaml", plan.digest(), subject.get("plan_digest", "")),
        ("config.yaml", config.frozen_digest(), subject.get("config_digest", "")),
    ):
        if presented and not digests.matches(presented, current):
            raise ApprovalError(
                f"{name} changed while the confirmation was on screen — the approval would freeze "
                "bytes other than the ones it was shown. Re-run `rein approve mandate`."
            )
    return {
        "status": "frozen",
        "digest": plan.digest(),
        "config_digest": config.frozen_digest(),
        "environment_digest": config.environment_digest(),
        "sources": implementation_sources(repo, plan),
        "frozen_at": event_chain.now_iso(),
    }


def _crossing_task_ids(repo: repo_mod.Repo) -> tuple[str, ...]:
    """The tasks the draft plan declares irreversible. `()` when there is no readable plan."""
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return ()
    return plan.crossing_task_ids if plan is not None else ()


def record_approval(
    repo: repo_mod.Repo, gate: str, subject: Mapping[str, str], *, confirmed_via: str = "terminal"
) -> str:
    """Write the gate receipt in one Central Store transaction, and return its id.

    **Two confirmation paths, one recording path.** A human confirms at a terminal
    (:func:`confirm_locally`) or in the dashboard, whose write session is minted only by
    redeeming the launch secret printed on the server's controlling terminal — the same kind of
    claim over a different channel. Both end here: a second way to reach an approved gate is the
    failure mode this module exists to prevent, so the receipt records *which* channel confirmed
    rather than flattening them into an unqualified "approved".

    The channel is checked against the vocabulary here rather than only by the schema at write time.
    `models.CONFIRMATION_CHANNELS` was declared, named in a comment, and never once consulted — so
    the argument accepted any string and the refusal, when it came, read as a schema enum violation
    several layers out instead of naming the one thing that was wrong.
    """
    if confirmed_via not in models.CONFIRMATION_CHANNEL_VALUES:
        raise ApprovalError(
            f"unknown confirmation channel {confirmed_via!r} — one of {', '.join(models.CONFIRMATION_CHANNELS)}"
        )
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ApprovalError("no .rein/state.yaml to record the approval in")
    seen = store_mod.read_digest(state)
    approval_id = f"GA-{gate.upper()}-{event_chain.new_id()[:8].upper()}"
    # Read before the transaction, recorded after it: an observation must never be able to fail an
    # approval, and the plan it counts is the one this approval is about to freeze.
    admitted = _unknowns_admitted(repo) if gate == FREEZING_GATE else 0
    demoted, reaches = _reach_movement(repo) if gate == FREEZING_GATE else ([], None)
    # The tasks this mandate is about to make into gates of their own. Read here, out of the plan
    # this approval freezes, because after the freeze it is the same bytes and before it there is
    # nothing binding to read.
    crossings = _crossing_task_ids(repo) if gate == FREEZING_GATE else ()

    # Everything below runs under the store lock. The chain-root binding is only meaningful if
    # nothing can append between the check and the receipt that pins it, and a gate approval
    # that lost a race with a concurrent write is refused outright rather than retried.
    with store.transaction() as tx:
        events, defects = event_chain.scan(repo.events)
        if defects:
            raise ApprovalError("refusing to record an approval against a damaged audit chain")
        root_before = event_chain.chain_root(events)
        # `verify_root` is the named form of exactly this check and was written for it, then never
        # called from anywhere while this line spelled it out by hand. One of the two would have
        # drifted eventually, and the one with a test is the one to keep.
        if not event_chain.verify_root(events, str(subject.get("attested_chain_root", ""))):
            raise ApprovalError(
                "the audit chain moved while the confirmation was on screen — events were appended, "
                f"removed, or regenerated. Re-run `rein approve {gate}`."
            )

        tx.append(
            "gate_approved",
            cycle_id=state.cycle_id,
            actor="local-confirmation",
            subject_ids=[gate, approval_id],
            detail={"attested_chain_root": root_before, "subject_digest": digests.of(dict(subject))},
        )

        raw = json.loads(json.dumps(state.raw))  # plain deep copy; state.raw stays untouched
        receipt: dict[str, object] = {
            "approval_id": approval_id,
            "confirmed_at": event_chain.now_iso(),
            # Which channel carried the confirmation, never who (models.CONFIRMATION_CHANNELS).
            "confirmed_via": confirmed_via,
            # The root the approval *lands* on, not the one it was confirmed against: this very
            # transaction appends `gate_approved`, so the chain necessarily moves.
            "result_chain_root": tx.projected_chain_root(),
        }
        receipt.update({key: subject[key] for key in _RECEIPT_DIGESTS if subject.get(key)})

        raw["gates"][gate] = {"status": "approved", "receipt": receipt}
        # And nothing else. This used to write `current_phase` in the same breath, which made one
        # transaction the author of two facts — what has been permitted, and how far the work has
        # got — that could then disagree. Where the cycle stands is read off the gates
        # (`models.State.stage`).
        raw["updated_at"] = event_chain.now_iso()
        # The approval is what closes the change requests it covered: the human read each note
        # beside these digests and decided they were answered. Open ones cannot be here —
        # readiness refuses while any stands, for this gate and for the gates the line below is
        # about to delete.
        #
        # `dropped` is the second set. Freezing the plan re-derives the crossing gates from it, so
        # a crossing this cut does not declare irreversible ceases to exist in this write; an
        # addressed request standing against it would otherwise be left pointing at a gate the
        # cycle no longer has, blocking nothing and listed under no gate name anybody can type.
        # The approval that removes the gate is what closes it, and this receipt is what the
        # closure points back to.
        # Read off `crossings`, which is the gate set this transaction is about to write, so the
        # gates being deleted and the gates being kept cannot come from two readings of one plan.
        # Only the mandate deletes any: `crossings` is empty at every other gate because none is
        # being derived there, and subtracting that from this cycle's would name all of them.
        dropped = tuple(g for g in state.crossing_gates if g not in set(crossings)) if gate == FREEZING_GATE else ()
        change_request.resolve_addressed(raw, (gate, *dropped))

        # Approving the mandate is what freezes the plan — the write `gate_guard` rule 2 and
        # `rein build` both key off, and the only one in the codebase that sets
        # `plan.status = "frozen"`.
        if gate == FREEZING_GATE:
            # The cycle's gates, recomputed from the plan being frozen rather than added to. A roll
            # back to the mandate un-freezes the plan and the next mandate may cut different tasks,
            # so a crossing gate that no longer has a task behind it has to go — otherwise
            # acceptance waits on a point nothing will ever reach. Nothing approved is lost: the
            # roll back that made this re-approval possible already reset everything downstream.
            raw["gates"] = {g: v for g, v in raw["gates"].items() if g in models.GATE_ENDS} | {
                task_id: {"status": "pending", "receipt": None} for task_id in crossings
            }
            raw["plan"] = _frozen_plan_block(repo, subject)
            tx.append(
                "plan_frozen",
                cycle_id=state.cycle_id,
                actor="local-confirmation",
                subject_ids=[approval_id],
                detail={key: raw["plan"][key] for key in FROZEN_PLAN_KEYS},
            )
            # The baseline the next comparison runs against, advanced by the thing that consumes
            # it. `/revise` puts this gate back to `pending`, so without this the same demotion is
            # re-read and re-recorded at every re-approval and the figure counts revisions rather
            # than misjudged reaches. `events` is the listing this transaction already read under
            # the lock, so the comparison and the append cannot disagree.
            if reaches is not None and reaches != event_chain.derived_reaches(events):
                tx.append(
                    "decisions_derived",
                    cycle_id=state.cycle_id,
                    actor="local-confirmation",
                    subject_ids=sorted(reaches),
                    detail={"reaches": reaches},
                )

        tx.write("state", raw, expect_digest=seen)
    if gate == FREEZING_GATE:
        observations.record(
            "unknown_at_mandate", project=repo.root.name, cycle_id=state.cycle_id, value=admitted, subject=approval_id
        )
        for decision_id in demoted:
            observations.record(
                "reach_overruled",
                project=repo.root.name,
                cycle_id=state.cycle_id,
                subject=decision_id,
                arm=observations.ARM_TOO_MANDATE,
            )
    return approval_id


def _reach_movement(repo: repo_mod.Repo) -> tuple[list[str], dict[str, str] | None]:
    """`(demoted ids, every id's reach)` — what moved down since rein last read this draft, and now.

    `None` for the second is a plan nobody could read, which is not the same answer as a plan with
    no decisions in it: one says nothing about the reaches, and the other says there are none. A
    caller that flattened them would erase the baseline on the way past a plan it could not parse.

    The other half of selection-by-reach's falsifier, and the mirror of the one `change_request`
    files. `too_local` is a decision the loop settled on its own reading that somebody then paid
    for; this is one the loop routed *to* a human that ended up settled as `local` — the loop asked
    about something whose reversal stays inside a task. `rein approve mandate` names the move
    itself when it refuses a `mandate` decision the loop settled: answer it, or change the reach
    and say in `rationale` why undoing it stays local. The schema requires that rationale on every
    `local`, so the move cannot be made silently.

    **The reading is about the criterion, not about who moved it.** What is measured is that a
    reach the loop derived did not survive to the freeze, and the rationale requirement makes the
    change deliberate whoever made it. An agent revising its own draft is not counted anyway, but
    not because this can tell: the baseline is re-read by every drafting command (`rein lens
    --select`), so an ordinary redraft moves the baseline with it, and what is left to compare
    against is the pass that ran up to the gate. Attributing it further than that would be a claim
    with no measurement behind it.

    Neither arm is an unbiased estimate and this one is not either: somebody who answers the
    question because answering is faster than arguing about the reach leaves nothing behind. Both
    sides are lower bounds, which is the point — the figure exists to show that pressure runs in
    both directions, not to measure how much.

    Only decisions present in both readings count. One that first appeared after the last pre-freeze
    pass has no "before" to have moved from, and a demotion inferred from a missing snapshot would
    be a reading with no measurement behind it.
    """
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return [], None  # a plan that does not parse is `_plan_blockers`' to report, not this figure's
    if plan is None:
        return [], None
    events, _ = event_chain.scan(repo.events)
    before = event_chain.derived_reaches(events)
    reaches = {d.id: d.reach for d in plan.decisions}
    demoted = [d.id for d in plan.decisions if before.get(d.id) == "mandate" and d.reach == "local"]
    return demoted, reaches


# --- the human confirmation -------------------------------------------------------


#: What an approval does and does not establish, said in full every time one is taken. It is not
#: a disclaimer to be skimmed: a reader of `state.yaml` months later has to know that the receipt
#: records that *a* human approved at a terminal, and never *which*.
AUTHORITY_NOTE = (
    "This records that someone with access to this terminal approved it — not which human, and\n"
    "not, provably, a human at all. What it does establish: an approval cannot happen by accident,\n"
    "by default, or by a configuration anyone pre-authorized. That is carried by the terminal this\n"
    'prompt needs and by `rein approve` never being pre-authorizable (AGENTS.md "Gate rules" 2).'
)


def addressed_requests(repo: repo_mod.Repo, gate: str) -> list[Mapping[str, object]]:
    """The change requests this approval would close — shown on both approval screens."""
    state = store_mod.Store(repo).read_state()
    return state.change_requests_for(gate, "addressed") if state else []


def render_subject(subject: Mapping[str, str]) -> str:
    """The digests this approval would cover, in a form a human can read before typing.

    The point of the pause is that there is something specific to read. A prompt that only says
    "approve? [y/N]" is a fumble guard; naming what moves if any of these digests move is the
    thing that makes the confirmation about this approval rather than about approving in general.
    """
    width = max((len(k) for k in subject), default=0)
    return "\n".join(f"  {key.ljust(width)}  {value}" for key, value in subject.items())


def confirm_locally(repo: repo_mod.Repo, gate: str, subject: Mapping[str, str]) -> None:
    """Confirm at an interactive terminal. Raises unless the answer is yes.

    An interactive TTY is required and there is no flag to skip it. What that adds is not proof
    of a human but that a piped stdin, a CI job, or an agent's captured subprocess cannot approve
    by accident — the failure that would otherwise happen silently.

    The answer is `[y/N]`, not the gate name typed back: retyping a word that is already on the
    command line establishes nothing, since someone who would reflexively press `y` would as
    reflexively type `tasks`. What is load-bearing is the pause with the digests above it, the
    TTY, and **the default being no** — a stray Enter must never approve anything.
    """
    if not common.stdin_is_terminal():
        raise ApprovalError(
            f"gate '{gate}' needs a confirmation typed at a terminal, and stdin is not one. "
            "Run this in your shell — there is deliberately no flag that skips it."
        )
    print(f"gate '{gate}' is ready. This approval will cover:\n{render_subject(subject)}\n")
    # One source for both routes. This function used to call `crossing_declarations` and
    # `_unasked_decisions` itself and never learned about the third list, which is how the lens
    # selection came to exist on one screen only: two screens assembling the same panel from
    # different parts is a panel that can differ, and it did.
    named = naming(repo, gate)
    crossing = named["crossing"]
    if crossing:
        # At the mandate this is the count of stops still to come; at a crossing gate it is the
        # thing about to become permanent. Either way it is the one item on the screen that no
        # later gate can reconsider.
        if gate == FREEZING_GATE:
            tasks = sorted({row["task_id"] for row in crossing})
            print(
                f"{len(tasks)} further stop(s) this mandate creates — one before each task that "
                "declares work it cannot take back:"
            )
        else:
            print("This approval lets the loop do something it cannot undo:")
        print(render_crossing(crossing) + "\n")
    unasked = named["unasked"]
    if unasked:
        # The one thing on this screen that is not a digest. Everything else says what was decided
        # with this human; this says what was decided without them, which is the part an approval
        # silently ratifies unless it is put in front of somebody.
        print(f"{len(unasked)} decision(s) the loop settled without asking you:")
        print(render_unasked(unasked) + "")
        print(f"  {named['overrule_cost']}\n")
    lens_rows = named["lenses"]
    if lens_rows:
        # Not folded away. `proposed` is the half a human is being *asked* about, and a list that
        # has to be opened before anything can be dropped is a list whose default is "keep them
        # all" — the always-on set the class system replaced, re-entering through a closed
        # disclosure rather than through an empty `when:`.
        droppable = sum(1 for row in lens_rows if row["status"] == lens_lib.SELECTION_PROPOSED)
        asked = f" — {droppable} of them yours to keep or drop" if droppable else ""
        print(f"{len(lens_rows)} review lens(es) this mandate would freeze{asked}:")
        print(render_lenses(lens_rows) + "")
    addressed = addressed_requests(repo, gate)
    if addressed:
        # Read before deciding, not after. These are the changes this human asked for last time;
        # the notes are the agent's claim that they were made, and approving closes them.
        print(f"{len(addressed)} change request(s) you raised were addressed:")
        print(change_request.render(addressed) + "\n")
    print(AUTHORITY_NOTE)
    if not common.ask_yes_no(f"Approve gate '{gate}'?"):
        raise ApprovalError(
            f"nothing was approved. If the deliverable needs work, record it against the gate so it "
            f"survives this session and holds the gate shut until it is answered:\n"
            f"  rein changes add {gate} --target <docs/...#R-3 | T-004> --reason <what is wrong>"
        )


#: What overruling one costs, and where that cost changes. Kept as one string because two screens
#: say it, and a paraphrase on one of them would be a second claim about the same mechanism.
OVERRULE_COST = "Overruling one now costs a task. After the mandate it costs `/revise`."


class Naming(TypedDict):
    """What the approval panel owes a human besides the digests, in the shape both screens read."""

    unasked: list[dict[str, str]]
    overrule_cost: str
    lenses: list[dict[str, str]]
    crossing: list[dict[str, str]]


def naming(repo: repo_mod.Repo, gate: str, *, include_library: bool = True) -> Naming:
    """What an approval would ratify without being asked, in a shape either screen can render.

    The *material* was never missing from the dashboard: `.rein/plan.yaml` is the first deliverable
    in its mandate pane, and `decisions` and `lenses` are both in it. What only the terminal had is
    the **selection** — which of a few hundred lines deserve a human's eye at the moment of
    approving. Asking somebody to find three `unasked` decisions inside a plan document is the same
    failure as sending a reviewer through a lens that cannot fire here: what matters competes for
    attention with what does not, and loses.

    The rule this restores: **whatever a gate requires on screen belongs on every route that can
    open that gate.** The dashboard grew a second approval route and this did not follow it — and
    then the lens list, added here in the same change, went the other way: built by this function
    and rendered only by the dashboard. Both routes now render everything this returns, which is
    what makes the rule checkable rather than remembered.

    The lens list is the plan's whole selection, not one task's. At the moment of approving nothing
    has been narrowed yet (`lenses.for_task` runs at the hand-off to a reviewer), and what the
    approval can overrule is the selection itself.

    `include_library` is about where the text comes from, not about how sensitive it is. The ids,
    stages and statuses are in `.rein/plan.yaml`, which this cycle's own record already carries;
    `attack` and `applies_when` are read out of the **user-global** library, which belongs to the
    person and not to this repository — other projects' failures are written in it. The dashboard
    serves a gate's readiness to any reader, by design, so it asks for the library only for a
    reader holding the write session: the one who would be doing the approving has it, and nobody
    else gets a file from outside the repository because a page was left open.
    """
    out: Naming = {"unasked": [], "overrule_cost": OVERRULE_COST, "lenses": [], "crossing": []}
    out["crossing"] = crossing_declarations(repo, gate)
    unasked = _unasked_decisions(repo, gate)
    out["unasked"] = [
        {
            "id": d.id,
            "subject": d.subject,
            "answer": d.answer,
            # Required for `local` by the schema, so "" can only appear under a plan nothing
            # validated. Carried rather than dropped: the reach claim with no reasoning behind it
            # is the one most worth looking at.
            "rationale": d.rationale,
        }
        for d in unasked
    ]
    if gate != FREEZING_GATE:
        return out
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return out  # a plan that does not parse is `_plan_blockers`' to report, not this screen's
    if plan is None:
        return out
    known = {lens.id: lens for lens in lens_lib.library()} if include_library else {}
    out["lenses"] = [
        {
            "id": entry.id,
            "stage": entry.stage,
            "status": entry.status,
            "attack": known[entry.id].attack if entry.id in known else "",
            # Named rather than skipped: an id the library no longer holds is exactly the drift the
            # freeze exists to expose, and it should be visible while it can still be acted on. A
            # reader who was not given the library is told that, rather than being shown the same
            # blank and left to read it as drift.
            "applies_when": (
                known[entry.id].applies_when
                if entry.id in known
                else ("(no longer in the library)" if include_library else "")
            ),
        }
        for entry in plan.lenses
    ]
    return out


def crossing_declarations(repo: repo_mod.Repo, gate: str) -> list[dict[str, str]]:
    """The irreversible declarations this gate is about.

    At a crossing gate: the ones on the task about to run, which is what the approval authorizes.
    At the mandate: *every* one in the plan, because the mandate is where they become gates and a
    human approving it is entitled to see how many more times this cycle will stop. That is the
    difference between a count that follows from the change and a count somebody has to discover
    later, one stop at a time.

    Empty everywhere else, including at acceptance: by then every crossing has been approved or
    the chain check has already refused.
    """
    if gate == models.GATE_LAST:
        return []
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return []  # a plan that does not parse is `_plan_blockers`' to report, not this screen's
    if plan is None:
        return []
    wanted = plan.crossing_task_ids if gate == FREEZING_GATE else (gate,)
    rows: list[dict[str, str]] = []
    for task in plan.tasks:
        if task.id not in wanted:
            continue
        for entry in task.irreversible_surfaces:
            rows.append(
                {
                    "task_id": task.id,
                    "title": task.title,
                    "kind": str(entry.get("kind", "")),
                    "name": str(entry.get("name", "")),
                    # Where the decision and its reversibility were argued. Optional in the schema,
                    # so an empty string here means the plan pointed at nothing, not that this
                    # screen dropped it.
                    "adr": str(entry.get("adr", "")),
                }
            )
    return rows


def render_crossing(rows: Sequence[Mapping[str, str]]) -> str:
    lines: list[str] = []
    for row in rows:
        lines.append(f"  - {row['task_id']} {row['title']}")
        lines.append(f"      cannot be undone: {row['name']} ({row['kind']})")
        lines.append(f"      decided in: {row['adr'] or '(no ADR recorded — the reversibility claim is unsupported)'}")
    return "\n".join(lines)


def _unasked_decisions(repo: repo_mod.Repo, gate: str) -> list[models.Decision]:
    """What the loop settled on its own reading that undoing it later stays local.

    Only at the mandate: it is the gate that freezes the plan, so it is the last moment at which
    disagreeing with that reading is a cheap edit rather than a `/revise`.
    """
    if gate != FREEZING_GATE:
        return []
    try:
        plan = store_mod.Store(repo).read_plan()
    except models.DocumentError:
        return []  # a plan that does not parse is `_plan_blockers`' to report, not this screen's
    # `local` only. A `mandate`-reach decision the loop settled by itself is not a line on this
    # screen to be overruled — it is `_decision_blockers`' business, because the reach already
    # said a human settles it.
    return [d for d in plan.decisions if d.unasked and d.is_local] if plan is not None else []


def render_unasked(rows: Sequence[Mapping[str, str]]) -> str:
    """The rows `naming` carries, not `models.Decision` — so the terminal renders what the
    dashboard renders rather than a second reading of the same plan."""
    lines: list[str] = []
    for row in rows:
        lines.append(f"  - {row['id']} {row['subject']}")
        lines.append(f"      settled: {row['answer'] or '(no answer recorded)'}")
        # `rationale` is required for `local` by the schema, so "(none recorded)" can only appear
        # under a plan nothing validated. Printed rather than skipped: the reach claim with no
        # reasoning behind it is the one most worth looking at, not the one to leave off the list.
        lines.append(f"      local because: {row['rationale'] or '(none recorded — the reach claim is unsupported)'}")
    return "\n".join(lines)


def render_lenses(rows: Sequence[Mapping[str, str]]) -> str:
    """What this mandate would freeze a reviewer to look for, in the words the library uses.

    `applies_when` is the lens's own account of when it is worth applying, and for a `proposed`
    one it is the whole of what the human is deciding against: its `when:` block is what a machine
    could settle, and for these lenses that is not the condition — the condition is in the prose,
    and settling it takes reading the deliverable.
    """
    lines: list[str] = []
    for row in rows:
        lines.append(f"  - {row['id']} [{row['stage']}] {row['attack']}")
        prefix = "proposed — yours to keep or drop" if row["status"] == lens_lib.SELECTION_PROPOSED else "applies when"
        lines.append(f"      {prefix}: {row['applies_when']}")
    return "\n".join(lines)


def approve_locally(repo: repo_mod.Repo, gate: str, subject: Mapping[str, str]) -> int:
    """Confirm, re-check, record, report."""
    confirm_locally(repo, gate, subject)
    # Re-checked after the pause: the repository may have moved while the prompt waited, and
    # recording a second receipt over a gate something else opened is not a no-op.
    blockers = readiness(repo, gate)
    if blockers:
        logger.error(render_blockers(gate, blockers))
        return 1
    approval_id = record_approval(repo, gate, subject)
    print(f"gate '{gate}' opened ({approval_id})")
    return 0


# --- CLI -------------------------------------------------------------------------


def render_blockers(gate: str, blockers: list[str]) -> str:
    body = "\n".join(f"  - {b}" for b in blockers)
    return f"gate '{gate}' is not ready ({len(blockers)} blocker(s)):\n{body}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="check a gate's readiness, then take the human's confirmation")
    parser.add_argument("gate", help=models.gate_names())
    parser.add_argument("--check", action="store_true", help="readiness only; ask for nothing, open nothing")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    try:
        blockers = readiness(repo, args.gate)
    except ApprovalError as exc:
        logger.error(str(exc))
        return 2
    if blockers:
        logger.error(render_blockers(args.gate, blockers))
        return 1
    if args.check:
        print(f"gate '{args.gate}' is ready for a confirmation")
        return 0

    try:
        return approve_locally(repo, args.gate, approval_subject(repo, args.gate))
    except ApprovalError as exc:
        logger.error(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
