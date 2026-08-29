"""`rein revise --to <phase>` — rewind approval, in a chain.

Rewinding approval is a human privilege, never automatic (AGENTS.md "Roll back"). What this
command mechanizes is the part humans get wrong: an upstream gate returning to `pending` must
never leave a downstream gate `approved`, because a downstream approval standing on a
withdrawn decision is the stale-approval inconsistency the whole gate ladder exists to
prevent. So the reset always runs forward from the target through gate ⑤.

A roll back has three consequences beyond the gate lines themselves:

  **The plan un-freezes.** Rewinding to `tasks` or earlier sets `plan.status` back to `draft`,
  which is what makes `plan.yaml` and `config.yaml` editable again — the gate guard denies
  those writes while the plan is frozen.

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
"""

from __future__ import annotations

import argparse
import json
import logging

from rein import approve, common, dag, event_chain, findings, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Roll-back target phase → the gate the chain reset starts at. `verify` is not a target: it
#: precedes gate ⑤, so rewinding "to verify" would reset nothing.
PHASE_GATE: dict[str, str] = {
    "requirements": "requirements",
    "design": "design",
    "tasks": "tasks",
    "build": "build",
}

#: Rewinding to these re-opens the frozen plan and its pinned toolchain (plan §16.4).
UNFREEZES_PLAN = frozenset({"requirements", "design", "tasks"})


class ReviseError(RuntimeError):
    """The roll back cannot be performed."""


def gates_to_reset(target_phase: str, state: models.State) -> list[str]:
    """Every currently-approved gate from `target_phase`'s gate onward. Empty = nothing to do."""
    start = models.GATE_ORDER.index(PHASE_GATE[target_phase])
    return [g for g in models.GATE_ORDER[start:] if state.gate_status(g) == "approved"]


def impacted_closure(plan: models.Plan, state: models.State | None, seeds: list[str]) -> tuple[list[str], list[str]]:
    """(seeds that exist, their transitive dependents). Unknown seed ids are reported by the caller."""
    graph = dag.join(plan, state)
    known = {t.id for t in graph.tasks}
    valid = [s for s in seeds if s in known]
    return valid, sorted(graph.dependents_closure(valid))


def plan_revision(repo: repo_mod.Repo, target_phase: str, seeds: list[str]) -> dict[str, object]:
    """Everything the roll back would change, as data — so `--dry-run` and the real run agree.

    Computing the plan once and rendering it twice is what keeps a dry run honest; two code
    paths that "do the same thing" are two code paths that eventually do not.
    """
    if target_phase not in PHASE_GATE:
        raise ReviseError(f"unknown target phase {target_phase!r} (one of {', '.join(PHASE_GATE)})")

    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ReviseError("no .rein/state.yaml — nothing to roll back")
    plan = store.read_plan()

    resets = gates_to_reset(target_phase, state)
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
        "target_phase": target_phase,
        "gates_reset": resets,
        "unfreezes_plan": target_phase in UNFREEZES_PLAN and state.plan_status == "frozen",
        "invalidates_review": bool(resets),
        "cleared_receipts": [g for g in resets if state.gate_receipt(g) is not None],
        "marked_tasks": marked,
        "ripple": ripple,
        "unknown_seeds": unknown_seeds,
        "previous_status": {tid: state.task_status.get(tid, "todo") for tid in marked},
    }


def render(revision: dict[str, object]) -> str:
    lines = [f"Roll back to phase '{revision['target_phase']}':"]
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
    raw["current_phase"] = revision["target_phase"]
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

    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        if stale_review is not None:
            tx.write("review", stale_review, expect_digest=seen_review)
        tx.append(
            "gate_revised",
            cycle_id=state.cycle_id,
            subject_ids=[*resets, *marked],
            detail={"target_phase": revision["target_phase"], "reason": reason},
        )
        if revision["unfreezes_plan"]:
            tx.append("plan_invalidated", cycle_id=state.cycle_id, detail={"reason": reason})


def _seeds_from_review(repo: repo_mod.Repo) -> list[str]:
    """The tasks the machine review's blocking findings belong to, derived rather than typed.

    Refused once gate ④ is approved. Marking tasks then is the front half of rewinding an
    approval, and AGENTS.md keeps that a human's privilege — they may still name the ids
    themselves with `--impacted`, which is the point: the decision stays theirs, only the
    clerical part is automated.
    """
    store = store_mod.Store(repo)
    state, plan = store.read_state(), store.read_plan()
    if state is None or plan is None:
        raise ReviseError("--from-review needs state.yaml and plan.yaml")
    if state.gate_status("build") == "approved":
        raise ReviseError(
            "gate 4 (build) is approved, so deriving the impacted tasks from its review would begin "
            "rewinding an approval on its own. Name the tasks with --impacted: the decision is yours."
        )
    attributions = findings.attribute(plan, store.read_review())
    print(findings.render(attributions))
    derived = findings.seeds(attributions)
    if not derived:
        raise ReviseError(
            "no blocking finding maps to a task with a declared scope — there is nothing to derive. "
            "Name the tasks with --impacted."
        )
    return derived


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="roll back: reset gates from a target phase onward, in a chain")
    parser.add_argument("--to", required=True, metavar="PHASE", help=f"one of: {', '.join(PHASE_GATE)}")
    parser.add_argument("--reason", default="", help="why (recorded in the audit chain)")
    parser.add_argument("--impacted", default="", help="comma-separated task ids directly affected")
    parser.add_argument(
        "--from-review",
        action="store_true",
        help="derive the impacted tasks from the machine review's blocking findings (gate 4 must be pending)",
    )
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
    if args.from_review:
        try:
            derived = _seeds_from_review(repo)
        except ReviseError as exc:
            logger.error(str(exc))
            return 2
        seeds = sorted(set(seeds) | set(derived))
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
    print("\nRolled back. Reconcile the marked tasks in /tasks before re-approving.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
