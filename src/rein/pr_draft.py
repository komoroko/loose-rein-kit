"""`rein pr-draft` — assemble a PR body from what the cycle actually recorded.

Push and PR creation are outward-facing and stay human-run, but the *body* of an honest PR is
already in the SSOT: the gate receipts, the review coverage, the claim verdicts, the
coverage manifest, and the gate receipts. Re-typing that by hand is where a summary
starts drifting from the record, so this reads it instead. It never invokes `gh`.

The layout follows plan §28, and one line of it matters more than the rest:

    Extra behaviors: undeterminable (coverage gap)

A PR body is where a reviewer's expectations get set, and "0 blocking" next to an
unanalysable diff is the single most misleading thing this tool could print. When coverage is
insufficient the count is not rendered at all — because there is no number that honestly
describes "we could not look" (plan §2.4).
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from rein import common, dag, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

OUT_PATH = ".rein/pr-draft.md"


def _digest_line(label: str, value: str | None) -> str:
    return f"- {label}: {value or '(not recorded)'}"


def cycle_facts(
    state: models.State,
    plan: models.Plan | None,
    review: models.Review | None,
    events: Sequence[models.Event],
    defects: Sequence[event_chain.ChainDefect],
    *,
    base: str = "main",
) -> list[str]:
    """Everything the SSOT says about the cycle as a whole, as body lines.

    Split out of :func:`build_body` because a stacked pull request needs the same block under a
    slice's own section (`pr_stack`), and two renderings of the same figures are two chances for
    one of them to drift. The documents arrive already read: the caller has them, and reading
    them twice would let the two halves of one body disagree about what it is describing.
    """
    lines = [f"- Cycle: `{state.cycle_id}`   base: `{base}`"]
    lines.append(_digest_line("Plan digest", plan.digest() if plan else None))
    binding = review.machine.get("binding") if review and review.is_generated else None
    change_digest = binding.get("change_digest") if isinstance(binding, dict) else None
    actual_digest = binding.get("actual_digest") if isinstance(binding, dict) else None
    lines.append(_digest_line("Change digest", change_digest if isinstance(change_digest, str) else None))
    lines.append(_digest_line("Actual digest", actual_digest if isinstance(actual_digest, str) else None))

    if plan is not None:
        verdicts: dict[str, int] = {}
        if review is not None and review.is_generated:
            for result in review.claim_results:
                key = str(result.get("verdict", "unknown"))
                verdicts[key] = verdicts.get(key, 0) + 1
        summary = " / ".join(f"{n} {v}" for v, n in sorted(verdicts.items())) or "not reviewed"
        lines.append(f"- Claims: {len(plan.claims)} total — {summary}")

    if review is not None and review.is_generated:
        if review.coverage_sufficient:
            blocking_extras = sum(1 for e in review.extra_behaviors if e.get("blocking") is True)
            lines.append("- Coverage: sufficient")
            lines.append(f"- Extra behaviors: {blocking_extras} blocking, {len(review.extra_behaviors)} total")
        else:
            lines.append("- Coverage: **insufficient** — parts of the change could not be analysed")
            lines.append("- Extra behaviors: **undeterminable** (not zero: we could not look)")
        lines.append(f"- Security findings: {len(review.blocking_security_findings)} blocking")
        lines.append(f"- Human review: {review.human_status}")
    else:
        lines.append("- Review: not generated")

    lines.append(f"- Event chain root: {event_chain.chain_root(events)}")
    if defects:
        lines.append(f"- **Audit chain: {len(defects)} defect(s)** — this PR must not be merged as it stands")

    lines += ["", "### Gates", ""]
    for gate in models.GATE_ORDER:
        receipt = state.gate_receipt(gate) or {}
        approval = receipt.get("approval_id") or "-"
        lines.append(f"- {gate}: {state.gate_status(gate)} (approval: {approval})")

    if plan is not None:
        try:
            graph = dag.join(plan, state)
            counts = graph.counts()
            lines += ["", "### Tasks", "", "- " + " / ".join(f"{k}={v}" for k, v in counts.items() if v)]
        except dag.DagError as exc:
            lines += ["", f"- task graph inconsistent: {exc}"]

    return lines


def build_body(repo: repo_mod.Repo, base: str = "main") -> str:
    """The PR body for a cycle shipped as one pull request. Reads only; every figure is from the SSOT."""
    store = store_mod.Store(repo)
    state = store.read_state()
    plan = store.read_plan()
    review = store.read_review()
    events, defects = event_chain.scan(repo.events)

    lines = ["## Grounded implementation review", ""]
    if state is None:
        return "\n".join([*lines, "- (no .rein/state.yaml — nothing to summarize)"])
    lines += cycle_facts(state, plan, review, events, defects, base=base)
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="assemble a PR body from the SSOT (read-only; never runs gh)")
    parser.add_argument("--base", default="main", help="the base branch to name in the body (default: main)")
    parser.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    try:
        body = build_body(repo, args.base)
    except (models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1

    if args.stdout:
        print(body, end="")
        return 0
    out = repo.path(OUT_PATH)
    out.write_text(body, encoding="utf-8")
    print(
        f"wrote {OUT_PATH}\n\nReview it, then create the PR yourself:\n"
        f"  gh pr create --base {args.base} --body-file {OUT_PATH}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
