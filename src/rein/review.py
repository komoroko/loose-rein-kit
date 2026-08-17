"""Assemble the grounded machine review — the artefact gate ④ actually approves (plan §12, §17).

This is the orchestration the build loop hands off to and `rein review generate` runs. It is
deliberately thin over parts that already exist and are tested on their own: the deterministic
Coverage Manifest and risk floor (diff_facts) and the three untrusted reviewer stages
(actual_extraction → conformance → security_review), each of which validates its own output
against the never-lists in review_policy. What lives *here* is the
wiring and the schema-valid assembly into ``review.yaml``'s ``machine`` half, plus the two lifecycle
verbs the human loop needs: ``complete`` (freeze the human review once every blocker is clear) and
``show``.

Two boundaries are load-bearing:

- **The reviewer is injected.** ``generate`` takes a ``review_policy.Reviewer`` — a callable that
  turns a request into JSON — so the deterministic assembly is testable with a fake, and the CLI
  supplies the real adapter-backed one. The extractor is *never* handed the plan, the expected
  claims, or the implementer's explanation (actual_extraction enforces this); the comparator gets the
  Actual read-only and digest-bound.
- **The machine half is written whole and resets the human half.** Regenerating the review moves
  ``machine`` and therefore its digest, which is exactly what must invalidate every human answer
  built on the previous one (plan §6.6, §17.5). ``complete`` only ever touches ``human``.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from rein import (
    actual_extraction,
    common,
    conformance,
    decision_cards,
    diff_facts,
    digests,
    event_chain,
    human_review,
    models,
    review_policy,
    security_review,
)
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The SSOT artefacts are bound by their own digests, so the change under review is the tree with
#: them excluded — otherwise a review that writes review.yaml would invalidate itself (plan §17.3).
_CHANGE_EXCLUDE: tuple[str, ...] = (repo_mod.SSOT_DIR,)


class ReviewError(Exception):
    """A review could not be generated or completed — carries a human-readable reason."""


# -- deterministic digests over the committed tree ----------------------------


def change_digest(repo: repo_mod.Repo, commit: str) -> str:
    """The digest of the code under review at `commit`: the committed tree minus the SSOT dir."""
    rc, out = repo._git_rc("ls-tree", "-r", "-z", commit)
    if rc != 0:
        raise ReviewError(f"cannot read the tree at {commit}: {out.strip()}")
    entries = digests.filter_tree(digests.parse_ls_tree(out), exclude_prefixes=_CHANGE_EXCLUDE)
    return digests.tree_digest(entries)


def _diff(repo: repo_mod.Repo, base: str, head: str) -> str:
    rc, out = repo._git_rc("diff", f"{base}..{head}")
    if rc != 0:
        raise ReviewError(f"cannot diff {base}..{head}: {out.strip()}")
    return out


def fold_mechanical(diff_text: str, files: Sequence[diff_facts.DiffFile]) -> tuple[str, list[str]]:
    """The diff with lockfile and generated-file *bodies* replaced by one line each.

    A lockfile's eight hundred changed lines say one thing — the dependencies moved — and they say
    it by burying the twelve lines of hand-written code in the same diff. Every reviewer here was
    handed the raw whole, twice over (the extractor and the security reviewer each get their own
    copy), and the meaningful change was somewhere in the middle of it.

    This is redaction, not summarisation and not priming: nothing is described, interpreted, or
    added. What replaces the hunks is the fact that they were there and how many lines they were,
    which is exactly what `diff_facts` already tells the Coverage Manifest — and the manifest goes
    on reporting these files as *not semantically analysed*, so the review stays `insufficient`
    for the same reason it always was. The honesty property is untouched; only the token cost of
    printing the bytes is gone.

    Returns `(text, folded_paths)`.
    """
    mechanical = {f.path for f in files if diff_facts.classify_path(f.path) in diff_facts.MECHANICAL_KINDS}
    if not mechanical:
        return diff_text, []
    kept: list[str] = []
    folded: list[str] = []
    current: str | None = None
    hunk_lines = 0
    for line in diff_text.splitlines(keepends=True):
        header = diff_facts._DIFF_GIT.match(line.rstrip("\n"))
        if header:
            if current is not None:
                kept.append(f"@@ {hunk_lines} line(s) of mechanical change, body withheld @@\n")
            path = header.group("b")
            current = path if path in mechanical else None
            hunk_lines = 0
            if current is not None:
                folded.append(current)
            kept.append(line)
            continue
        if current is None:
            kept.append(line)
        else:
            hunk_lines += 1
    if current is not None:
        kept.append(f"@@ {hunk_lines} line(s) of mechanical change, body withheld @@\n")
    return "".join(kept), folded


def _exists(repo: repo_mod.Repo, ref: str) -> bool:
    return bool(ref) and repo._git_rc("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0


def _resolve_base(repo: repo_mod.Repo, plan: models.Plan | None, base: str | None) -> str:
    """The trusted base a review is taken against: an explicit arg, the plan's base, else a fallback.

    Each candidate is verified to exist in *this* repository before it is used — a plan carrying a
    base commit that is not present here (a fork, a shallow clone) falls back rather than failing the
    whole review on a `git diff` against a missing object.

    There is deliberately no last-resort fallback to HEAD. That used to be the final branch, and
    it is the one answer that is never right: `git diff HEAD..HEAD` is empty, so every reviewer
    would be handed a change of nothing and would report, honestly and uselessly, that they found
    nothing wrong with it. A base that cannot be resolved is a review that cannot be taken.
    """
    if _exists(repo, base or ""):
        return base or ""
    if plan is not None and _exists(repo, plan.base_commit):
        return plan.base_commit
    for candidate in ("main", "master"):
        if _exists(repo, candidate):
            return repo._git_rc("rev-parse", candidate)[1].strip()
    raise ReviewError(
        "cannot resolve a base commit to review against: the plan's `cycle.base_commit` is not in "
        "this repository and neither `main` nor `master` exists here. Set `cycle.base_commit` to a "
        "commit this checkout has — reviewing HEAD against itself would report an empty change."
    )


# -- the code the reviewers are allowed to read -------------------------------

#: Per-file and whole-request ceilings on the head-side code handed to a reviewer. A diff hunk
#: without its surrounding function is often unreadable — "was this guard removed or moved?"
#: cannot be answered from the hunk alone — but a whole repository does not fit a request, so
#: the ceilings exist and, crucially, are *reported* when they bite.
RELEVANT_CODE_CHARS = 40_000
RELEVANT_CODE_TOTAL_CHARS = 240_000


@dataclass(frozen=True)
class RelevantCode:
    """The head-side code handed to a reviewer, and what did not fit.

    `truncated` and `omitted` are not diagnostics for us — they go into the request, so a
    reviewer can never read "this file is not here" as "there is nothing more to see". That is
    the same rule the Coverage Manifest applies to the diff (plan §13.3, §2.4).
    """

    files: dict[str, str]
    truncated: tuple[str, ...] = ()
    omitted: tuple[str, ...] = ()

    def as_facts(self) -> dict[str, Any]:
        return {
            "provided": sorted(self.files),
            "truncated_to_char_cap": list(self.truncated),
            "omitted_over_request_cap": list(self.omitted),
        }


def _relevant_code(repo: repo_mod.Repo, head: str, files: Sequence[diff_facts.DiffFile]) -> RelevantCode:
    """The head-side content of each changed file, under the ceilings above.

    Read from the committed tree at `head`, never the working tree: the review is of what was
    committed, and an uncommitted edit is not part of it.
    """
    provided: dict[str, str] = {}
    truncated: list[str] = []
    omitted: list[str] = []
    budget = RELEVANT_CODE_TOTAL_CHARS
    for file in files:
        if file.binary:
            continue
        if diff_facts.classify_path(file.path) in diff_facts.MECHANICAL_KINDS:
            # A lockfile's whole body, on top of its whole diff. Neither was ever going to be read
            # as code, and the manifest already reports it as not semantically analysed.
            omitted.append(file.path)
            continue
        rc, text = repo._git_rc("show", f"{head}:{file.path}")
        if rc != 0:  # deleted at head — the diff carries what it was, and there is nothing to read
            continue
        if budget <= 0:
            omitted.append(file.path)
            continue
        body = text[: min(RELEVANT_CODE_CHARS, budget)]
        if len(body) < len(text):
            truncated.append(file.path)
        provided[file.path] = body
        budget -= len(body)
    return RelevantCode(files=provided, truncated=tuple(truncated), omitted=tuple(omitted))


# -- the expected model handed to the comparator ------------------------------


def _expected_model(plan: models.Plan | None) -> dict[str, Any]:
    """The plan's claims as the comparator's Expected — the only place the plan enters the pipeline."""
    if plan is None:
        return {"claims": []}
    return {"claims": [{"id": c.id, "statement": c.raw.get("statement", ""), "risk": c.risk} for c in plan.claims]}


# -- assembly (pure, schema-valid) --------------------------------------------


def assemble(
    *,
    binding: Mapping[str, Any],
    coverage: Mapping[str, Any],
    actual_statements: Sequence[Mapping[str, Any]],
    claims: Sequence[Mapping[str, Any]],
    gaps: Sequence[Mapping[str, Any]] = (),
    extra_behaviors: Sequence[Mapping[str, Any]] = (),
    security: Mapping[str, Any] | None = None,
    effective_risk: str = "",
    plan: models.Plan | None = None,
    budget_limits: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Compose a schema-valid `machine` half from the validated pieces (plan §6.6).

    Every list is already the validated output of its own stage; this only shapes them, fills the
    summary counts, and *derives* the two sections that are restatements rather than judgements —
    the decision cards a human must answer and the budget snapshot (`decision_cards` module).
    Keeping it pure is what lets a test assert the assembled shape without a model.

    Deriving the cards here rather than asking a reviewer for them is the point: the list of
    decisions a human is answerable for must not be authored by the thing under review, and it must
    not be able to omit a finding. `plan` supplies each claim's frozen risk, domains and owed
    evidence; without it the cards still appear, at their default risk and with no domain routing.
    """
    verdicts = [str(c.get("verdict", "unknown")) for c in claims]
    summary = {
        "claims_total": len(claims),
        "aligned": verdicts.count("aligned"),
        "diverged": verdicts.count("diverged"),
        "missing": verdicts.count("missing"),
        "unverified": verdicts.count("unverified"),
        "unknown": verdicts.count("unknown"),
    }
    machine: dict[str, Any] = {
        "status": "generated",
        "binding": dict(binding),
        "summary": summary,
        "coverage": [dict(coverage)],
        "actual_extraction": [dict(a) for a in actual_statements],
        "claims": [dict(c) for c in claims],
        "security": dict(security) if security is not None else {"findings": []},
    }
    if effective_risk:
        machine["effective_risk"] = effective_risk
    if gaps:
        machine["gaps"] = [dict(g) for g in gaps]
    if extra_behaviors:
        machine["extra_behaviors"] = [dict(e) for e in extra_behaviors]

    plan_claims = {c.id: c.raw for c in plan.claims} if plan is not None else {}
    findings = list((security or {}).get("findings", ()) or ())
    statements, cards = decision_cards.derive_cards(
        claims=claims,
        gaps=gaps,
        extra_behaviors=extra_behaviors,
        security_findings=findings,
        plan_risk={cid: str(raw.get("risk", "low")) for cid, raw in plan_claims.items()},
        plan_domains={cid: tuple(str(d) for d in raw.get("domains", ()) or ()) for cid, raw in plan_claims.items()},
        first_statement=decision_cards.next_statement_index(
            (g.get("statement_id") for g in gaps),
            (e.get("statement_id") for e in extra_behaviors),
        ),
    )
    if statements:
        machine["statements"] = statements
    if cards:
        machine["decision_cards"] = cards
    if budget_limits:
        machine["review_budget"] = decision_cards.derive_review_budget(
            limits=budget_limits,
            decision_cards=cards,
            statements=statements,
            gaps=gaps,
        )
    return machine


# -- generation ---------------------------------------------------------------


def generate(
    repo: repo_mod.Repo,
    reviewer: review_policy.Reviewer,
    *,
    executor: Any = None,
    base: str | None = None,
    actor: str = "",
) -> dict[str, Any]:
    """Run the whole pipeline and write `review.yaml`'s machine half; return the assembled machine.

    The deterministic piece (the coverage manifest) runs unconditionally; the reviewer stages are called and
    their *validated* outputs merged. The human half is reset to `not_started` — a fresh machine
    review is a fresh review, and no prior human answer speaks for it (plan §6.6).

    **A failure records itself.** Every `raise` below used to leave the audit chain with nothing in
    it: `events.ATTENTION_EVENTS` listed `review_failed` and `actual_extraction_failed` as things
    needing a human decision and no code path anywhere emitted either, so a gate ④ that could not
    be produced reported "needing a human decision: 0". The whole log exists so that no state
    change goes unexplained, and the review pipeline's own failure was the state change it could
    not explain.
    """
    store = store_mod.Store(repo)
    plan = store.read_plan()
    config = store.read_config()
    state = store.read_state()
    # Captured before the pipeline runs, not inside the transaction: the whole point is to
    # refuse if review.yaml moved while the (slow, LLM-driven) stages were running.
    seen_review = store_mod.read_digest(store.read_review())

    cycle = state.cycle_id if state else (plan.cycle_id if plan else "")
    # Which stage a failure landed in, for the event that records it. Tracked as the pipeline
    # advances rather than read off the exception: each stage raises its own well-worded error and
    # wrapping those would change what a human sees at the console to say where it happened.
    stage = "coverage"

    def entered(name: str) -> None:
        nonlocal stage
        stage = name

    try:
        rc, head_out = repo._git_rc("rev-parse", "HEAD")
        if rc != 0:
            raise ReviewError("cannot resolve HEAD — is this a git repository with commits?")
        head = head_out.strip()
        trusted_base = _resolve_base(repo, plan, base)
        change = change_digest(repo, head)
        diff_text = _diff(repo, trusted_base, head)

        # The manifest reads the *whole* diff, always: what it measures is how much of the change
        # could be analysed, and folding a file before counting it would be measuring the fold.
        facts = diff_facts.analyze(diff_text)
        coverage = facts.coverage.to_manifest()
        risk_floor = facts.risk_floor
        effective = _effective_risk(facts, plan)

        # The reviewers read the folded one. `change_digest` above is over the committed tree, so
        # neither this nor `relevant`'s omissions can move what the review is bound to.
        reviewable, folded = fold_mechanical(diff_text, facts.files)
        relevant = _relevant_code(repo, head, facts.files)

        security_request = security_review.build_request(
            diff_text=reviewable,
            relevant_code=relevant.files,
            deterministic_facts={
                "signals": [h.signal for h in facts.signals],
                "relevant_code": relevant.as_facts(),
                "mechanical_bodies_withheld": folded,
            },
            trusted_base_sha=trusted_base,
            subject_head_sha=head,
        )
        # What the last review found blocking *about this same base*. Read here rather than at the
        # call below, because the call is about to move off this thread and the store is not something
        # to touch from two.
        prior_blocking = _prior_blocking_ids(store.read_review(), trusted_base)

        # The security review reads the diff and the relevant code; it consumes nothing the extractor
        # or the comparator produce, so it does not have to wait behind them. Three LLM stages at up
        # to 15 minutes each ran end to end for no reason but the order they were written in.
        # Determinism is unaffected: the results are merged, and the events appended, in a fixed
        # order below, and a failure in the extraction still surfaces ahead of one here.
        with ThreadPoolExecutor(max_workers=1) as pool:
            security_future = pool.submit(
                security_review.run_security_review,
                security_request,
                reviewer,
                repo=repo,
                commit=head,
                # Without it the check below — a reviewer may not clear its own block by
                # regenerating and omitting the finding — had nothing to compare against, so the
                # protection its own docstring describes never once applied.
                prior_blocking_ids=prior_blocking,
            )
            try:
                extraction, comparison = _extract_and_compare(
                    repo,
                    reviewer,
                    plan=plan,
                    config=config,
                    head=head,
                    trusted_base=trusted_base,
                    diff_text=reviewable,
                    relevant=relevant,
                    coverage=coverage,
                    risk_floor=risk_floor,
                    effective=effective,
                    on_stage=entered,
                )
            except BaseException:
                # The security stage's own failure is not the one to report: this one came first in
                # the pipeline's order, and reporting whichever thread lost a race would make the
                # error a reader sees depend on timing.
                security_future.cancel()
                raise
            entered("security_review")
            security = security_future.result()

        entered("assembly")
        binding = {
            "change_digest": change,
            "plan_digest": plan.digest() if plan is not None else digests.of({}),
            "config_digest": config.digest() if config is not None else digests.of({}),
            "toolchain_digest": _toolchain_digest(config),
            "coverage_digest": digests.of(coverage),
            "actual_digest": extraction.actual_digest,
            "trusted_base_sha": trusted_base,
            "subject_head_sha": head,
            "generated_at": event_chain.now_iso(),
        }
        gaps = _coverage_gaps(comparison.actual_coverage_gaps)
        machine = assemble(
            binding=binding,
            coverage=coverage,
            actual_statements=extraction.actual_statements,
            claims=comparison.claims,
            gaps=gaps,
            extra_behaviors=_extra_behaviors(comparison.extra_behaviors, gaps=gaps),
            security=security.to_section(),
            effective_risk=effective,
            plan=plan,
            # A config may set only the budgets it wants to move, so the recorded snapshot is the
            # defaults with the repository's overrides on top — the same merge human_review does.
            budget_limits={**human_review.DEFAULT_BUDGET, **(config.budgets if config is not None else {})},
        )

        entered("write")
        document = {"machine": machine, "human": {"status": "not_started"}}
        with store.transaction() as tx:
            tx.write("review", document, expect_digest=seen_review)
            tx.append("coverage_generated", cycle_id=cycle, actor=actor)
            tx.append("actual_extraction_generated", cycle_id=cycle, actor=actor)
            tx.append("comparison_generated", cycle_id=cycle, actor=actor)
            tx.append("security_review_generated", cycle_id=cycle, actor=actor)
            tx.append("review_generated", cycle_id=cycle, actor=actor, detail={"change_digest": change})
    except Exception as exc:
        # `Exception`, not `BaseException`: a Ctrl-C is a human deciding to stop, and filing that
        # as a review failure would put a decision in the log as a defect. Same line the signal
        # classifier draws (faults._EXTERNAL_SIGNALS leaves SIGINT out).
        _record_failure(store, cycle, actor, stage=stage, reason=str(exc))
        raise
    return machine


#: Reviewer stages, by the name `generate` tracks them under. `actual_extraction` is the one with
#: an event of its own: it is the stage that reads the code without the plan, so its failure means
#: there is no Actual at all — a different fact from a comparison that had one and could not use it.
_STAGE_EVENT: Mapping[str, str] = {"actual_extraction": "actual_extraction_failed"}


def _record_failure(store: store_mod.Store, cycle: str, actor: str, *, stage: str, reason: str) -> None:
    """Append the failure events for a review that could not be produced. Never raises.

    Append-only: nothing was written, so there is no document to stage, and a transaction that
    appends without writing is exactly what `store.Transaction` permits (the refusal runs the other
    way — a write with no event).

    Swallowing the store's own errors is deliberate. This runs inside an `except` block whose job
    is to re-raise the real failure; a store problem here would replace the error a human needs to
    read with one about bookkeeping, and the log being unwritable is what `rein doctor` is for.
    """
    try:
        with store.transaction() as tx:
            detail = {"stage": stage, "reason": reason[:1000]}
            if stage in _STAGE_EVENT:
                tx.append(_STAGE_EVENT[stage], cycle_id=cycle, actor=actor, detail=detail)
            tx.append("review_failed", cycle_id=cycle, actor=actor, detail=detail)
    except Exception as exc:  # the original failure must reach the reader, not this one
        logger.warning(f"could not record the review failure in the audit log: {exc}")


def _extract_and_compare(
    repo: repo_mod.Repo,
    reviewer: review_policy.Reviewer,
    *,
    plan: models.Plan | None,
    config: models.Config | None,
    head: str,
    trusted_base: str,
    diff_text: str,
    relevant: RelevantCode,
    coverage: Mapping[str, Any],
    risk_floor: str,
    effective: str,
    on_stage: Callable[[str], None] = lambda _name: None,
) -> tuple[actual_extraction.ExtractionResult, conformance.ComparatorResult]:
    """The one genuinely sequential pair: the comparator reads what the extractor produced.

    Kept together so the concurrency in `generate` reads as "one chain plus one independent
    stage" rather than as three calls someone happened to interleave.

    `on_stage` is called with each stage's name as it *begins*, so a caller can say which one
    failed without this function having to know about the store or catch anything. It is the only
    way to tell "the extractor failed" from "the comparator failed", and those are different
    facts: one means nothing was read out of the code, the other means the reading exists and
    could not be compared against the plan.
    """
    # Blind actual extraction — the plan is deliberately absent from this request (§12.2).
    on_stage("actual_extraction")
    extract_request = actual_extraction.build_request(
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
        diff_text=diff_text,
        relevant_code=relevant.files,
        deterministic_facts={
            "coverage": coverage,
            "risk_floor": risk_floor,
            "relevant_code": relevant.as_facts(),
        },
    )
    extraction = actual_extraction.run_extractor(
        extract_request, reviewer, repo=repo, commit=head, risk_floor=risk_floor
    )

    # Expected vs Actual — the Actual arrives read-only and digest-bound (§12.3).
    on_stage("comparison")
    compare_request = conformance.build_request(
        expected_model=_expected_model(plan),
        actual_statements=extraction.actual_statements,
        actual_digest=extraction.actual_digest,
    )
    known_ids = _known_ids(plan, extraction.actual_statements)
    comparison = conformance.run_comparator(
        compare_request,
        reviewer,
        repo=repo,
        commit=head,
        actual_statement_ids=[str(a.get("id")) for a in extraction.actual_statements],
        known_ids=known_ids,
        effective_risk=effective,
        independence=_independence(config),
    )

    return extraction, comparison


def _known_ids(plan: models.Plan | None, actual_statements: Sequence[Mapping[str, Any]]) -> list[str]:
    ids = [str(a.get("id")) for a in actual_statements]
    if plan is not None:
        ids += [c.id for c in plan.claims]
    return ids


def _prior_blocking_ids(review: models.Review | None, trusted_base: str) -> list[str]:
    """The blocking findings the previous review recorded **about the same base**, if any.

    The carry-over exists so a reviewer cannot clear its own block by regenerating and quietly
    omitting the finding. That is right, and it was being applied by id alone — with nothing
    anywhere saying which *change* the finding was about. So a review taken against base A kept
    blocking a regeneration against base B: a different diff, sometimes not containing the code
    the finding named, and the only way past it was for the reviewer to re-assert a finding it
    could no longer see.

    A finding is a statement about a change. Change the base and it is a statement about
    something else, so it does not carry — and `binding.trusted_base_sha` is what says so. An
    absent or unequal base means no carry-over, which is the safe direction here: the new review
    is free to find what is actually there, and the *new* base's own findings will then carry
    forward normally.
    """
    if review is None or not trusted_base:
        return []
    recorded = str(review.raw.get("machine", {}).get("binding", {}).get("trusted_base_sha", ""))
    if not recorded or recorded != trusted_base:
        return []
    return [str(f.get("id", "")) for f in review.blocking_security_findings]


def _effective_risk(facts: diff_facts.DiffFacts, plan: models.Plan | None) -> str:
    """The change's effective risk: the max of every contributor (plan §13.5).

    The detector's `risk_floor` alone would leave the frozen plan's own judgment out of it — a
    change touching a `critical` claim reading as `low` because no regex fired. So the plan
    supplies claim and task risk; everything else comes off the deterministic signals, so an AI
    cannot argue any of it down.
    """
    claim_risk = models.max_risk([c.risk for c in plan.claims]) if plan is not None else "low"
    task_risk = models.max_risk([t.risk for t in plan.tasks]) if plan is not None else "low"
    inputs = review_policy.risk_inputs_from_facts(facts, claim_risk=claim_risk, task_risk=task_risk)
    return review_policy.effective_risk(inputs)


def _independence(config: models.Config | None) -> dict[str, Any]:
    """The declared reviewer groups, read from the config that declares them.

    `independence_group` is the whole substitute for buying a second AI provider, so it is read
    from the config rather than assumed: `review_policy.independence_ok` — the Actual Extractor
    and the Comparator must not be the same opinion — has nothing to enforce otherwise. An unset
    group stays empty rather than being invented: the check refuses a critical review that cannot
    name its two groups, which is the right answer.
    """
    if config is None:
        return {"actual_extractor": {"group": ""}, "comparator": {"group": ""}}
    return {role: {"group": config.independence_group(role)} for role in ("actual_extractor", "comparator")}


def _toolchain_digest(config: models.Config | None) -> str:
    """What the review was produced in: the executor profiles that ran its steps.

    Delegates to :meth:`models.Config.toolchain_digest`, which the gate ③ freeze binds too — two
    copies of "which sandbox was this" would eventually disagree, and then a freeze and a review
    would be talking about different environments while reporting the same digest name.
    """
    return config.toolchain_digest() if config is not None else digests.of({"executors": None})


def _coverage_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Comparator-reported actual-coverage gaps, shaped as review.yaml gap records where possible."""
    out: list[dict[str, Any]] = []
    for index, gap in enumerate(gaps, start=1):
        out.append(
            {
                "id": str(gap.get("id", f"GAP-{index:03d}")),
                "kind": str(gap.get("kind", "actual_coverage_gap")),
                "statement_id": str(gap.get("statement_id", f"STMT-{index:03d}")),
                "risk": str(gap.get("risk", "medium")),
                "blocking": bool(gap.get("blocking", False)),
            }
        )
    return out


def _extra_behaviors(
    extras: Sequence[Mapping[str, Any]],
    *,
    gaps: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Comparator-reported extra behaviours, shaped as review.yaml records.

    Behaviour in the code that no claim in the plan accounts for — the section that answers "did
    it build something nobody asked for?", and the reason the summary is allowed to say "extra
    behaviours: 0". It was assembled from a parameter no call site ever passed, so that zero was
    an empty list's length rather than a reading, in exactly the place this product refuses prose
    over evidence. Now it is what the Comparator found, or nothing.

    Statement ids continue past the gaps' rather than restarting at 1: both lists become decision
    cards, and two subjects sharing a `statement_id` would put one's question against the other's
    options.
    """
    first = decision_cards.next_statement_index(g.get("statement_id") for g in gaps)
    out: list[dict[str, Any]] = []
    for offset, extra in enumerate(extras):
        record = {
            "id": str(extra.get("id", f"EXTRA-{offset + 1:03d}")),
            "statement_id": str(extra.get("statement_id", f"STMT-{first + offset:03d}")),
            "category": str(extra.get("category", "")),
            "risk": str(extra.get("risk", "medium")),
            # Both default to the answerable direction. `grounded: true` is what takes an extra
            # behaviour off the human's list, so an omitted flag must not be the thing that does it.
            "grounded": extra.get("grounded") is True,
            "blocking": extra.get("blocking") is True,
        }
        anchors = [str(a) for a in extra.get("actual_statement_ids", ()) or ()]
        if anchors:
            record["actual_statement_ids"] = anchors
        out.append(record)
    return out


def complete(repo: repo_mod.Repo, *, actor: str = "") -> None:
    """Freeze the human review, refusing while any completion blocker stands (plan §21.5)."""
    store = store_mod.Store(repo)
    review = store.read_review()
    if review is None or not review.is_generated:
        raise ReviewError("no machine review to complete — run `rein review generate` first")
    seen = store_mod.read_digest(review)
    try:
        new_human = human_review.freeze(review, dict(review.human))
    except ValueError as exc:
        raise ReviewError(str(exc)) from None
    state = store.read_state()
    with store.transaction() as tx:
        tx.write("review", {**review.raw, "human": new_human}, expect_digest=seen)
        tx.append("human_review_frozen", cycle_id=state.cycle_id if state else "", actor=actor)


# -- CLI ----------------------------------------------------------------------


def _adapter_reviewer(repo: repo_mod.Repo, role: str = "code_reviewer") -> review_policy.Reviewer:
    """A production reviewer that hands the request as JSON to the adapter configured for `role`.

    Kept small on purpose: the request goes to the adapter on stdin and the adapter answers with
    the single JSON document the stage validators parse. Every stage revalidates the output, so
    this is a transport, not a trust boundary.

    `role` selects the adapter, so each stage gets its own: one callable serving every stage
    means the same session answers as the Actual Extractor and as the Comparator, the
    independence violation §12.4 exists to prevent. The request goes on stdin — a whole diff as
    an argv element hits E2BIG on a large change.
    """
    from rein import build_loop

    config = store_mod.Store(repo).read_config()
    adapter = (config.adapter(role) if config is not None else "") or "claude"
    argv = build_loop.ADAPTERS.get(adapter)
    if argv is None:
        raise ReviewError(f"agents.{role}.adapter is {adapter!r}, which this release cannot launch")

    def call(request: Mapping[str, Any]) -> str:
        rc, out = common.run(list(argv), timeout=900, input_text=json.dumps(request, ensure_ascii=False))
        if rc != 0:
            raise review_policy.ReviewPolicyError(f"the {role} adapter exited {rc}")
        return out

    return call


def _staged_reviewer(repo: repo_mod.Repo) -> review_policy.Reviewer:
    """One callable that routes each stage's request to that stage's own configured adapter.

    The stage is identified by the request shape the stage builders produce — the same shapes
    the fakes in the test suite key on — so nothing has to thread a role through the pipeline.
    """
    by_role = {role: _adapter_reviewer(repo, role) for role in ("actual_extractor", "comparator", "security_reviewer")}

    def call(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            return by_role["comparator"](request)
        facts = request.get("deterministic_facts")
        if isinstance(facts, Mapping) and "signals" in facts:
            return by_role["security_reviewer"](request)
        return by_role["actual_extractor"](request)

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein review", description="the grounded machine review (gate ④)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="run the review pipeline and write review.yaml")
    sub.add_parser("complete", help="freeze the human review (all blockers must be clear)")
    sub.add_parser("show", help="print the current review.yaml")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(None)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    try:
        if args.cmd == "generate":
            generate(repo, _staged_reviewer(repo))
            print("review.yaml generated — review it in `rein ui`, then `rein review complete`")
            return 0
        if args.cmd == "complete":
            complete(repo)
            print("human review frozen — `rein approve build` can now be run")
            return 0
        if args.cmd == "show":
            text = repo.review.read_text(encoding="utf-8") if repo.review.exists() else "(no review.yaml yet)"
            print(text)
            return 0
    except (
        ReviewError,
        review_policy.ReviewPolicyError,
        store_mod.StoreError,
        models.DocumentError,
    ) as exc:
        logger.error(str(exc))
        return 1
    return 0
