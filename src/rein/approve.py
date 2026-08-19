"""`rein approve <gate>` — the human's command, and the only path to an approved gate.

Three steps, in this order, none skippable:

  1. **Readiness** — every mechanical precondition for the gate (:func:`readiness`), reported
     exhaustively rather than one at a time.
  2. **Confirmation** — the digests this approval covers are printed, and a human confirms:
     `[y/N]` at an interactive terminal (:func:`confirm_locally`), or in the dashboard, whose
     write session is minted only by redeeming the launch link printed to the terminal `rein ui`
     runs in. Two channels of the same kind; the receipt records which.
  3. **Receipt** — one Central Store transaction writes the gate receipt, binding those digests
     and the audit-chain root (:func:`record_approval`). Gate ③ additionally **freezes the
     plan** in that same transaction: `state.plan` gains `frozen` plus the digests the freeze
     covers, which is what `rein build` requires and what `rein guard` rule 2 protects.

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
import sys
from collections.abc import Mapping

from rein import change_request, common, dag, dag_trace, digests, event_chain, models, review_policy
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The document each gate approves, for the receipt's `artifact_digest`.
GATE_ARTIFACT: dict[str, str] = {
    "requirements": "docs/10-requirements.md",
    "design": "docs/20-design.md",
}


class ApprovalError(RuntimeError):
    """The gate cannot be approved, or an approval cannot be recorded."""


# --- readiness ------------------------------------------------------------------


def _chain_blockers(state: models.State, gate: str, *, already_approved_blocks: bool) -> list[str]:
    pending = state.pending_upstream(gate)
    if pending:
        return [
            f"gate '{pending}' is still pending — approving '{gate}' now would leave a decision "
            "standing on one that was never made (gates open in order)"
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
    if gate in {"tasks", "build", "release"} and not plan.tasks:
        blockers.append("the plan declares no tasks")
    return blockers


def _task_blockers(plan: models.Plan | None, state: models.State | None, gate: str) -> list[str]:
    if gate not in {"tasks", "build", "release"} or plan is None:
        return []
    try:
        graph = dag.join(plan, state)
    except dag.DagError as exc:
        return [str(exc)]
    blockers = [f"{cid}: no task is answerable for this claim" for cid in graph.claims_without_a_task(plan)]
    if gate in {"build", "release"}:
        unfinished = sorted(t.id for t in graph.tasks if not t.is_done)
        if unfinished:
            blockers.append(f"tasks not done: {', '.join(unfinished)}")
    return blockers


def _review_blockers(review: models.Review | None, gate: str, head: str = "") -> list[str]:
    """Gate ④/⑤ preconditions carried by the machine review (plan §16.8).

    A readiness check that passes because a stage has not been implemented yet is worse than
    no check at all, so an absent review is a blocker rather than a shrug.

    The mechanical half is `review_policy.blocking_reasons` — the module that owns the gate-④
    decision — rather than a second copy of the same rules here. Two copies had already drifted:
    this one never looked at `machine.gaps`, so a comparator could mark an actual-coverage gap
    blocking, have it written to `review.yaml`, and watch the gate open anyway.
    """
    if gate not in {"build", "release"}:
        return []
    if review is None or not review.is_generated:
        return [
            "no machine review has been generated — run `rein review generate`. "
            "Gate 4 approves a grounded review, not a green test run."
        ]
    blockers = review_policy.blocking_reasons(review, review.effective_risk)
    reviewed = review.subject_head_sha
    if head and reviewed and reviewed != head:
        # Three documents say a later commit leaves the review stale. Only the UI pane had ever
        # checked, so generate → commit → approve opened gate ④ over code no reviewer saw.
        blockers.append(
            f"the machine review was generated against {reviewed[:12]} and HEAD is now {head[:12]} — "
            "it says nothing about the commits since. Re-run `rein review generate`."
        )
    if review.human_status != "frozen":
        blockers.append(
            f"the human review is '{review.human_status}', not 'frozen' — "
            "complete it in the review UI (`rein review complete`)"
        )
    return blockers


def _change_request_blockers(state: models.State, gate: str) -> list[str]:
    """Open change requests hold the gate shut. This is what makes declining mean something.

    Without it "not yet, change R-3" was a sentence in a chat window: the gate stayed ready, the
    board kept recommending an approval, and a new session had no idea a human had already said
    no. An `addressed` request does not block — it is listed on the approval screen instead.
    """
    return [
        f"{cr.get('id')} is an open change request against {cr.get('target')}: {cr.get('reason')}"
        for cr in state.change_requests_for(gate, "open")
    ]


def readiness(repo: repo_mod.Repo, gate: str, *, already_approved_blocks: bool = True) -> list[str]:
    """Every mechanical reason `gate` cannot be approved. Empty means a request may be issued.

    Deliberately exhaustive rather than short-circuiting: being handed one blocker, fixing it,
    and being handed the next is exactly the review friction plan §2.6 budgets against.

    `already_approved_blocks=False` is for a status board asking "what stands in this gate's
    way" rather than confirming an approval (`status_api._default_readiness`, `ui.py`) — an
    already-approved gate reporting itself as its own blocker would read a healthy board as
    unready.
    """
    if gate not in models.GATE_VALUES:
        raise ApprovalError(f"unknown gate {gate!r} (one of {', '.join(models.GATE_ORDER)})")

    store = store_mod.Store(repo)
    try:
        state = store.read_state()
        plan = store.read_plan()
        review = store.read_review()
    except models.DocumentError as exc:
        return [str(exc)]

    if state is None:
        return ["no .rein/state.yaml — run `rein init` first"]

    blockers: list[str] = []
    _, defects = event_chain.scan(repo.events)
    if defects:
        blockers.append(
            f"the audit chain has {len(defects)} defect(s) — a receipt binds the chain root, so it "
            "cannot be issued against a damaged log (see `rein events --verify`)"
        )
    blockers += _chain_blockers(state, gate, already_approved_blocks=already_approved_blocks)
    blockers += _change_request_blockers(state, gate)
    blockers += _plan_blockers(repo, plan, gate)
    blockers += _task_blockers(plan, state, gate)
    blockers += _review_blockers(review, gate, _head_sha(repo))
    return blockers


def _head_sha(repo: repo_mod.Repo) -> str:
    """The commit under review, or "" outside a git repository (nothing to compare against)."""
    rc, out = repo._git_rc("rev-parse", "HEAD")
    return out.strip() if rc == 0 else ""


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
    artifact = GATE_ARTIFACT.get(gate)
    if artifact and repo.path(artifact).exists():
        subject["artifact_digest"] = digests.of_file(repo.path(artifact))
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

#: The gate whose approval freezes the Expected Model. Gate ③ approves the plan and the
#: toolchain it will be built against; everything downstream is measured against that freeze.
FREEZING_GATE = "tasks"

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
    nothing at all. A ticket edited after gate ③ changed what got built, with no record anywhere
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
    """`state.plan` as gate ③ freezes it, refusing if the documents moved since `subject`.

    Recomputed here rather than copied from `subject` because `subject` was assembled *before*
    the human read it and typed. If plan.yaml or config.yaml moved in between, freezing the
    stale digest would record a freeze of bytes nobody approved — and the chain-root guard above
    does not cover these two files. Same posture, one document further out.
    """
    store = store_mod.Store(repo)
    plan = store.read_plan()
    config = store.read_config()
    if plan is None:
        raise ApprovalError("gate 'tasks' freezes .rein/plan.yaml, and there is no plan to freeze")
    if config is None:
        raise ApprovalError("gate 'tasks' freezes .rein/config.yaml, and there is no config to freeze")

    for name, current, presented in (
        ("plan.yaml", plan.digest(), subject.get("plan_digest", "")),
        ("config.yaml", config.frozen_digest(), subject.get("config_digest", "")),
    ):
        if presented and not digests.matches(presented, current):
            raise ApprovalError(
                f"{name} changed while the confirmation was on screen — the approval would freeze "
                "bytes other than the ones it was shown. Re-run `rein approve tasks`."
            )
    return {
        "status": "frozen",
        "digest": plan.digest(),
        "config_digest": config.frozen_digest(),
        "environment_digest": config.environment_digest(),
        "sources": implementation_sources(repo, plan),
        "frozen_at": event_chain.now_iso(),
    }


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
        raw["current_phase"] = models.PHASE_AFTER_GATE[gate]
        raw["updated_at"] = event_chain.now_iso()
        # The approval is what closes the change requests it covered: the human read each note
        # beside these digests and decided they were answered. Open ones cannot be here —
        # readiness refuses while any stands.
        change_request.resolve_addressed(raw, gate)

        # Gate ③ is what freezes the plan — the write `gate_guard` rule 2 and `rein build` both
        # key off, and the only one in the codebase that sets `plan.status = "frozen"`.
        if gate == FREEZING_GATE:
            raw["plan"] = _frozen_plan_block(repo, subject)
            tx.append(
                "plan_frozen",
                cycle_id=state.cycle_id,
                actor="local-confirmation",
                subject_ids=[approval_id],
                detail={key: raw["plan"][key] for key in FROZEN_PLAN_KEYS},
            )

        tx.write("state", raw, expect_digest=seen)
    return approval_id


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
    if not sys.stdin.isatty():
        raise ApprovalError(
            f"gate '{gate}' needs a confirmation typed at a terminal, and stdin is not one. "
            "Run this in your shell — there is deliberately no flag that skips it."
        )
    print(f"gate '{gate}' is ready. This approval will cover:\n{render_subject(subject)}\n")
    addressed = addressed_requests(repo, gate)
    if addressed:
        # Read before deciding, not after. These are the changes this human asked for last time;
        # the notes are the agent's claim that they were made, and approving closes them.
        print(f"{len(addressed)} change request(s) you raised were addressed:")
        print(change_request.render(addressed) + "\n")
    print(AUTHORITY_NOTE)
    print(f"\nApprove gate '{gate}'? [y/N] ", end="", flush=True)
    if sys.stdin.readline().strip().lower() not in ("y", "yes"):
        raise ApprovalError(
            f"nothing was approved. If the deliverable needs work, record it against the gate so it "
            f"survives this session and holds the gate shut until it is answered:\n"
            f"  rein changes add {gate} --target <docs/...#R-3 | T-004> --reason <what is wrong>"
        )


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
    parser.add_argument("gate", help=f"one of: {', '.join(models.GATE_ORDER)}")
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
