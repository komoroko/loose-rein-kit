"""`rein revise --to <gate>` — rewind approval, in a chain.

Rewinding approval is a human privilege, never automatic (AGENTS.md "Roll back"). What this
command mechanizes is the part humans get wrong: an upstream gate returning to `pending` must
never leave a downstream gate `approved`, because a downstream approval standing on a
withdrawn decision is the stale-approval inconsistency the gates exist to prevent. So the reset
always runs forward from the target through the last gate.

The target is a **gate**, not a phase. It was a phase, and the mapping from one to the other was a
table here — which only existed because approving a gate also advanced a phase. What a human
withdraws is an authorization: `--to mandate` says the scope, the claims or the acceptance criteria
were wrong; `--to acceptance` says the change should not have been taken.

A roll back has three consequences beyond the gate lines themselves:

  **The plan un-freezes.** Rewinding to `mandate` sets `plan.status` back to `draft`, which is
  what makes `plan.yaml` and `config.yaml` editable again — the gate guard denies those writes
  while the plan is frozen.

  **Approvals stop applying.** A receipt binds digests; once the artifacts move, it covers
  bytes nobody will read again. The receipts are cleared with the gate — but the
  `gate_approved` events stay in the chain, because an audit record you can erase is not one.

  **The human review goes stale.** Its answers — and the freeze `approve.readiness` re-checks —
  were recorded about an implementation of a plan that no longer stands, so they return to
  `not_started`. The machine half is left alone: it is a reading of the code, regenerating it is a
  deliberate act, and clearing it here would destroy the thing those answers were answers *to*.

`--impacted` marks the named tasks **and their transitive dependents** `needs-revision`.
Missing an impacted task is the dangerous direction, so the whole closure is marked
mechanically; "this one is actually fine" is a deliberate human reclassification during the
`/tasks` reconcile, never a silent default.

**Re-decomposing the work does come here**, and only the *order* does not. The breakdown reads
like the loop's own business until you notice where it is written: `tasks[].acceptance` is in
`plan.yaml` with the claims, so a re-cut is a shape a softened criterion travels in, and the
freeze covers the document whole rather than trying to tell an honest re-cut from that. What
withdraws no authorization is consuming the DAG — the order, the parallelism, the re-runs — which
is not written down anywhere to be frozen.

**This is for a defect in the specification, and nothing else.** There used to be a
`--from-review` that derived the impacted tasks from acceptance's blocking findings, which was the
only route a machine-found *code* defect had back into the code. It marked the task and its whole
dependent closure `needs-revision` — a status about the plan — so `status_api` then demanded a
`/tasks` reconcile and a re-approval of the mandate, for a repair that changed no requirement, no claim
and no plan. Reset, salvage, re-approve, round again. The acceptance gate repairs its own findings now
(`repair.route`, `build_loop._close_gate4`), and what reaches here is what a human decided *is* a
specification defect — answering a Decision Card with `revise_design` or `revise_requirement`,
which is a different sentence from "the code is wrong".
"""

from __future__ import annotations

import argparse
import json
import logging

from rein import approve, common, dag, event_chain, models, observations
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Rewinding to this re-opens the frozen plan and its pinned toolchain (plan §16.4). Only the
#: mandate does: it is what the freeze records, and `acceptance` sits on top of it.
UNFREEZES_PLAN = frozenset({"mandate"})


class ReviseError(RuntimeError):
    """The roll back cannot be performed."""


def gates_to_reset(target_gate: str, state: models.State) -> list[str]:
    """Every currently-approved gate at or downstream of `target_gate`. Empty = nothing to do.

    Downstream is read off `State.upstream_of` rather than a position in a list, because the gates
    of a cycle are a fan and not a line: rewinding to the mandate withdraws every crossing and the
    acceptance, while rewinding to one crossing leaves the others standing — they were never
    authorized by it.
    """
    downstream = {target_gate} | {g for g in state.gate_ids if target_gate in state.upstream_of(g)}
    return [g for g in state.gate_ids if g in downstream and state.gate_status(g) == "approved"]


def impacted_closure(plan: models.Plan, state: models.State | None, seeds: list[str]) -> tuple[list[str], list[str]]:
    """(seeds that exist, their transitive dependents). Unknown seed ids are reported by the caller."""
    graph = dag.join(plan, state)
    known = {t.id for t in graph.tasks}
    valid = [s for s in seeds if s in known]
    return valid, sorted(graph.dependents_closure(valid))


def plan_revision(repo: repo_mod.Repo, target_gate: str, seeds: list[str]) -> dict[str, object]:
    """Everything the roll back would change, as data — so `--dry-run` and the real run agree.

    Computing the plan once and rendering it twice is what keeps a dry run honest; two code
    paths that "do the same thing" are two code paths that eventually do not.
    """
    if not models.gate_name_ok(target_gate):
        raise ReviseError(f"unknown target gate {target_gate!r} ({models.gate_names()})")

    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ReviseError("no .rein/state.yaml — nothing to roll back")
    plan = store.read_plan()

    resets = gates_to_reset(target_gate, state)
    unknown_seeds = list(seeds)
    marked: list[str] = []
    ripple: list[str] = []
    if seeds:
        if plan is None:
            raise ReviseError("--impacted needs a plan to resolve task ids against")
        valid, ripple = impacted_closure(plan, state, seeds)
        unknown_seeds = [s for s in seeds if s not in valid]
        marked = sorted(set(valid) | set(ripple))

    return {
        "target_gate": target_gate,
        "gates_reset": resets,
        # Withdrawn authorizations that the world will not withdraw with them. Every other line of
        # this plan describes something a roll back undoes; these describe what it cannot.
        "crossed": [g for g in resets if g in state.crossing_gates],
        "unfreezes_plan": target_gate in UNFREEZES_PLAN and state.plan_status == "frozen",
        "invalidates_review": bool(resets),
        "cleared_receipts": [g for g in resets if state.gate_receipt(g) is not None],
        "marked_tasks": marked,
        "ripple": ripple,
        "unknown_seeds": unknown_seeds,
        "previous_status": {tid: state.task_status.get(tid, "todo") for tid in marked},
    }


def render(revision: dict[str, object]) -> str:
    lines = [f"Roll back to gate '{revision['target_gate']}':"]
    resets = revision["gates_reset"]
    assert isinstance(resets, list)
    lines.append(f"- gates reset to pending (in a chain): {', '.join(resets) or '(none — already pending)'}")
    cleared = revision["cleared_receipts"]
    assert isinstance(cleared, list)
    if cleared:
        lines.append(
            f"- receipts cleared for: {', '.join(cleared)} "
            "(their gate_approved events stay in the audit chain as history)"
        )
    crossed = revision["crossed"]
    assert isinstance(crossed, list)
    if crossed:
        lines.append(
            f"- WARNING: {', '.join(crossed)} authorized work that cannot be taken back. The approval "
            "is withdrawn and the task returns to the plan; whatever it already did is still done, "
            "and undoing that is yours to do outside this tool."
        )
    if revision["unfreezes_plan"]:
        lines.append("- plan.status: frozen → draft (plan.yaml and config.yaml become editable)")
    if revision["invalidates_review"]:
        lines.append(
            "- the human review returns to not_started (its answers were about an implementation of a "
            "plan that no longer stands); the machine review is left as it is — regenerate it deliberately"
        )
    marked = revision["marked_tasks"]
    assert isinstance(marked, list)
    if marked:
        previous = revision["previous_status"]
        assert isinstance(previous, dict)
        lines.append(f"- tasks marked needs-revision ({len(marked)}):")
        ripple = revision["ripple"]
        assert isinstance(ripple, list)
        for tid in marked:
            tag = "ripple" if tid in ripple else "seed"
            lines.append(f"    {tid} [{tag}] was {previous.get(tid, 'todo')}")
    unknown = revision["unknown_seeds"]
    assert isinstance(unknown, list)
    if unknown:
        lines.append(f"- WARNING: unknown task id(s) ignored: {', '.join(unknown)}")
    return "\n".join(lines)


def apply(repo: repo_mod.Repo, revision: dict[str, object], reason: str) -> None:
    """Perform the roll back in one Central Store transaction."""
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ReviseError("no .rein/state.yaml — nothing to roll back")
    seen = store_mod.read_digest(state)

    raw = json.loads(json.dumps(state.raw))
    resets = revision["gates_reset"]
    assert isinstance(resets, list)
    for gate in resets:
        raw["gates"][gate] = {"status": "pending", "receipt": None}
    # Where the cycle now stands follows from those gates and is not written beside them
    # (`models.State.stage`). It used to be a second field this transaction set, which is how a
    # roll back could leave a phase and a gate set disagreeing.
    raw["updated_at"] = event_chain.now_iso()

    if revision["unfreezes_plan"]:
        plan_block = raw.setdefault("plan", {})
        plan_block["status"] = "draft"
        # The frozen digests described a plan that is now editable again; leaving them would
        # let a later check "verify" against a freeze that no longer holds. The key list comes
        # from the module that writes them — two copies agreeing was something to remember.
        for key in approve.FROZEN_PLAN_KEYS:
            plan_block.pop(key, None)

    marked = revision["marked_tasks"]
    assert isinstance(marked, list)
    if marked:
        tasks_block = raw.setdefault("tasks", {})
        for tid in marked:
            entry = tasks_block.get(tid)
            tasks_block[tid] = (
                {**entry, "status": "needs-revision"} if isinstance(entry, dict) else {"status": "needs-revision"}
            )

    # "The review goes stale" — the module docstring has said so since it was written, and what it
    # did was set a `state.review.status` nothing read. The human half of `review.yaml` is where
    # that sentence has to land: it holds answers recorded against an implementation of a plan that
    # no longer stands, and the freeze it may already carry is a precondition `approve.readiness`
    # re-checks. The machine half stays as it is — regenerating is a separate act, and clearing it
    # here would destroy the reading the human answers were about.
    #
    # Read only on the path that writes it. A rollback that invalidates no review has no business
    # parsing one, and parsing one is how this crashed: an `init`-written `review.yaml` scaffold
    # that an upgrade left schema-invalid — `status: not_generated`, carrying no review at all —
    # made `revise` unrunnable, and `revise` was the documented repair for the *other* document
    # the same upgrade broke. The rollback was announced to the operator and then died before the
    # state write, which is safe and unreadable at once.
    stale_review = None
    seen_review = ""
    if revision["invalidates_review"]:
        review = store.read_review()
        seen_review = store_mod.read_digest(review)
        if review is not None and review.human_status != "not_started":
            stale_review = {**review.raw, "human": {"status": "not_started"}}

    was_accepted = state.gate_status("acceptance") == "approved"
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        if stale_review is not None:
            tx.write("review", stale_review, expect_digest=seen_review)
        tx.append(
            "gate_revised",
            cycle_id=state.cycle_id,
            subject_ids=[*resets, *marked],
            detail={"target_gate": revision["target_gate"], "reason": reason},
        )
        if revision["unfreezes_plan"]:
            tx.append("plan_invalidated", cycle_id=state.cycle_id, detail={"reason": reason})

    # After the transaction, never inside it: an observation that could abort a roll back would be
    # an input to the thing it measures. An approved acceptance being rolled back is the heaviest
    # row in the store — somebody said yes to something they turned out not to have understood, and
    # the claim it tests is that comprehension comes out of deciding rather than out of reading.
    if was_accepted:
        observations.record(
            "acceptance_reopened",
            project=repo.root.name,
            cycle_id=state.cycle_id,
            subject=str(revision["target_gate"]),
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="roll back: reset gates from a target gate onward, in a chain")
    parser.add_argument("--to", required=True, metavar="GATE", help=models.gate_names())
    parser.add_argument("--reason", default="", help="why (recorded in the audit chain)")
    parser.add_argument("--impacted", default="", help="comma-separated task ids directly affected")
    parser.add_argument("--dry-run", action="store_true", help="print what would change; write nothing")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    seeds = [s.strip() for s in args.impacted.split(",") if s.strip()]
    try:
        revision = plan_revision(repo, args.to, seeds)
    except (ReviseError, dag.DagError, models.DocumentError) as exc:
        logger.error(str(exc))
        return 1

    print(render(revision))
    if args.dry_run:
        print("\n(dry run — nothing was written)")
        return 0
    if not args.reason:
        logger.error(
            "refusing to roll back with no --reason: the audit chain has to say why an approval "
            "was withdrawn, or the next reader cannot tell a correction from a mistake"
        )
        return 2

    try:
        apply(repo, revision, args.reason)
    except (ReviseError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    print("\nRolled back. Reconcile the marked tasks in /tasks before re-approving the mandate.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
