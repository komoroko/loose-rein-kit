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

- **The reviewers are injected.** ``generate`` takes a ``review_policy.Reviewers`` — the reviewer
  for each stage's role, plus what launching them has cost — so the deterministic assembly is
  testable with a fake, and the CLI supplies the real adapter-backed one (``review_transport``).
  The extractor is *never* handed the plan, the expected claims, or the implementer's explanation
  (actual_extraction enforces this); the comparator gets the Actual read-only and digest-bound.
- **The machine half is written whole and resets the human half.** Regenerating the review moves
  ``machine`` and therefore its digest, which is exactly what must invalidate every human answer
  built on the previous one (plan §6.6, §17.5). ``complete`` only ever touches ``human``.
"""

from __future__ import annotations

import argparse
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor
from concurrent.futures import wait as futures_wait
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from rein import (
    adapters,
    brief,
    common,
    conformance,
    decision_cards,
    diff_facts,
    digests,
    event_chain,
    faults,
    human_review,
    models,
    review_cache,
    review_policy,
    review_reading,
    review_transport,
    run_progress,
    run_record,
    security_review,
)
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: The reading half of the pipeline — what a reviewer is allowed to see, and the budget that
#: decides whether it can be seen at all. Re-exported under the names this module has always used
#: so that callers and tests keep one address for them.
ReviewError = review_reading.ReviewError
not_the_product = review_reading.not_the_product
change_digest = review_reading.change_digest
fold_bodies = review_reading.fold_bodies
split_tests = review_reading.split_tests
bytes_by_kind = review_reading.bytes_by_kind
Reviewable = review_reading.Reviewable
CONTEXT_LADDER = review_reading.CONTEXT_LADDER
PLAIN_CONTEXT = review_reading.PLAIN_CONTEXT
_diff = review_reading.diff_of
_file_facts = review_reading.file_facts
_reviewable = review_reading.reviewable_of
_cached_stage = review_reading.cached_stage
_reviewer_identity = review_reading.reviewer_identity
_stage_keys = review_reading.reading_keys
_exists = review_reading.commit_exists
_resolve_base = review_reading.resolve_base


def _blob_facts(repo: repo_mod.Repo, head: str) -> brief.BlobFacts:
    """How the brief names a declared path *as it ends up*: its blob at `head`, and its size.

    Bound to the reviewed commit rather than the working tree, for the same reason
    the reviewable diff is: the review is of what was committed, and showing an approver a file
    that has moved since would put a different tree beside the findings about this one.

    Identity and size only. The body is fetched from the same commit when a reader asks, so
    review.yaml never becomes a second copy of the repository.
    """

    def read(path: str) -> dict[str, Any] | None:
        if not models.is_repo_path(path):
            return None
        rc, blob = repo._git_rc("rev-parse", f"{head}:{path}")
        blob = blob.strip()
        if rc != 0 or not blob:
            return None
        rc, size = repo._git_rc("cat-file", "-s", blob)
        if rc != 0 or not size.strip().isdigit():
            return None
        return {"blob": blob, "bytes": int(size.strip())}

    return read


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
    unanswered: Sequence[str] = (),
    gaps: Sequence[Mapping[str, Any]] = (),
    extra_behaviors: Sequence[Mapping[str, Any]] = (),
    security: Mapping[str, Any] | None = None,
    effective_risk: str = "",
    plan: models.Plan | None = None,
    budget_limits: Mapping[str, int] | None = None,
    brief_sections: Mapping[str, Any] | None = None,
    residual_findings: Sequence[Mapping[str, Any]] = (),
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

    `brief_sections` and `residual_findings` arrive already derived (`brief.derive`,
    `brief.residual_findings`) rather than being built here, because they read config.yaml and
    state.yaml — documents this function deliberately never takes, so that assembling a machine
    half stays a pure shaping step a test can drive without a repository on disk.
    """
    verdicts = [str(c.get("verdict", "unknown")) for c in claims]
    summary: dict[str, Any] = {
        "claims_total": len(claims),
        "aligned": verdicts.count("aligned"),
        "diverged": verdicts.count("diverged"),
        "missing": verdicts.count("missing"),
        "unverified": verdicts.count("unverified"),
        "unknown": verdicts.count("unknown"),
    }
    if unanswered:
        summary["unanswered"] = list(unanswered)
    machine: dict[str, Any] = {
        "status": "generated",
        "binding": dict(binding),
        "summary": summary,
        "coverage": dict(coverage),
        "actual_extraction": [dict(a) for a in actual_statements],
        "claims": [dict(c) for c in claims],
        "security": dict(security) if security is not None else {"findings": []},
    }
    if effective_risk:
        machine["effective_risk"] = effective_risk
    if brief_sections:
        machine["brief"] = dict(brief_sections)
    if residual_findings:
        machine["residual_findings"] = [dict(f) for f in residual_findings]
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
            diff_bytes=review_reading.largest_reading_bytes(coverage),
            decision_cards=cards,
            statements=statements,
            gaps=gaps,
        )
    return machine


# -- generation ---------------------------------------------------------------


@dataclass(frozen=True)
class ChangeOutlook:
    """Whether this cycle's change can be reviewed at all — derivable at any moment, from git.

    Two constraints refuse a review, and both were being enforced at gate ④, where the only move
    left is to raise the number the tool's own documentation calls the wrong answer:

    - **`max_diff_bytes`.** It bounds what one reviewer is asked to read in **one launch**, and one
      launch holds one reading — so what it is measured against here is the largest reading, named.
      Measured over the whole change instead, it was a wall in front of a quantity nobody reads:
      two consecutive release cycles of this repository came to 662 KB and 754 KB against a 512 KiB
      ceiling, and one field cycle met it at 2,141,194 bytes, whose only exit was a gate-③ rollback
      to raise the limit to 1,584,559. Per reading the lever is a real one and it is upstream —
      a task whose scope is too broad to read is a task to split at gate ③, while splitting is
      still a move that exists.
    - **Coverage.** A single tracked binary — smoke-test output somebody committed months ago —
      makes the Coverage Manifest `insufficient`, which at `high` risk blocks gate ④. Nothing said
      so: not `doctor`, not `status`, and not `build` while it landed seventeen tasks.

    Neither needs a model, a launch, or a review: both are `git diff` plus the same `diff_facts`
    the manifest uses. So they are derived here, once, and read by `doctor`, `build` and `status` —
    three readers of one answer rather than three spellings of it.
    """

    #: The largest single reading — what one launch would be asked to hold, and what `ceiling`
    #: bounds. For an uncomposed review this is the whole change, which is what it has always been.
    diff_bytes: int
    #: The whole change, which is what `composition` is a breakdown of and what a reader compares
    #: the largest reading against. Required, and not defaulted to `diff_bytes`: an outlook that
    #: does not know how big the change is has nothing to say about what to remove from it.
    total_bytes: int
    ceiling: int
    unreadable: tuple[str, ...]
    effective_risk: str
    #: What the payload is made of, by `diff_facts` kind, largest first. The number alone says a
    #: review cannot be run; this says which lever to reach for.
    composition: tuple[tuple[str, int], ...] = ()
    #: Which reading `diff_bytes` is, and how many there are. `unit` is `WHOLE` when the change is
    #: read whole, and then `readings` is 1 — the shape every outlook had before composition.
    unit: str = review_reading.WHOLE
    readings: int = 1

    @property
    def over_budget(self) -> bool:
        return self.diff_bytes > self.ceiling

    @property
    def coverage_blocks_gate(self) -> bool:
        """Would this coverage block gate ④? Insufficient coverage only blocks at high/critical."""
        return bool(self.unreadable) and models.risk_at_least(self.effective_risk, "high")

    def line(self) -> str:
        """One line for a status board: what the reading stages would be asked to hold."""
        # An uncomposed change *is* the one reading, so naming it and printing its size beside the
        # change's own would be one number twice under two labels — and that shape, `N MB / ceiling`,
        # is the line this has always printed.
        composed = self.unit != review_reading.WHOLE
        over = f" — OVER, narrow {self.unit}'s scope at gate ③" if self.over_budget else ""
        ceiling = f"{self.ceiling / 1_000_000:.2f} MB"
        whole = f"{self.total_bytes / 1_000_000:.2f} MB"
        if composed:
            largest = f"{self.diff_bytes / 1_000_000:.2f} MB"
            text = f"change under review: {whole} in {self.readings} readings; "
            text += f"largest reading {self.unit} {largest} / {ceiling}{over}"
        else:
            text = f"change under review: {whole} / {ceiling}{over}"
        if self.unreadable:
            verdict = "blocks gate ④" if self.coverage_blocks_gate else "recorded, not blocking below high risk"
            text += f"; {len(self.unreadable)} unreadable file(s) make coverage insufficient ({verdict})"
        return text

    def made_of(self) -> str:
        """What the payload is made of, largest first. Empty only when there is nothing to say.

        Kept off `line()` on purpose: the board's line answers "can this be reviewed", and this
        answers "what would I remove".

        The silent case is *one kind, and that kind is `source`* — "it is all product code" is the
        null answer to "what would I remove". Silence on any single non-source kind was a bug with
        the sign reversed: a change that is 900 KB of lockfile and nothing else is the case where
        the answer is most obvious and most worth printing, and it was the one case that printed
        nothing.
        """
        if not self.composition or not self.total_bytes:
            return ""
        if len(self.composition) == 1 and self.composition[0][0] == "source":
            return ""  # "it is all product code" is the null answer: there is nothing to remove.
        parts = [
            f"{kind} {size / 1000:.1f} KB ({round(size * 100 / self.total_bytes)}%)" for kind, size in self.composition
        ]
        return "made of: " + ", ".join(parts)


def outlook(repo: repo_mod.Repo, *, base: str | None = None) -> ChangeOutlook | None:
    """What gate ④ would be asked to read, right now. None when the repo cannot answer yet.

    Cheap enough to run from `status`: **one** `git diff` and the deterministic analysis, no
    launches. The readings are derived from the plan and attributed out of that one diff
    (`review_reading.bytes_by_reading`) rather than each taking a `git diff` of its own — this is
    read on every stream tick, and eighteen `git diff` calls on a WSL mount is the cost the
    fingerprint exists to avoid.
    """
    store = store_mod.Store(repo)
    try:
        state, plan, config = store.read_state(), store.read_plan(), store.read_config()
    except common.ReinError:
        return None
    try:
        trusted_base = _resolve_base(repo, plan, base)
        diff_text = _diff(repo, trusted_base, "HEAD", not_the_product(repo, state))
    except ReviewError:
        return None
    facts = diff_facts.analyze(diff_text)
    limits = {**human_review.DEFAULT_BUDGET, **(config.budgets if config is not None else {})}
    unreadable = [
        str(entry.get("path", "")) for entry in (*facts.coverage.unsupported_files, *facts.coverage.generated_files)
    ]
    effective = review_reading.effective_risk(facts, plan)
    # The same readings gate ④ will take, decided by the same function on the same inputs — a board
    # that showed a different split from the one the pipeline runs would be reporting on a review
    # nobody is going to generate.
    readings = review_reading.plan_readings(
        plan,
        [f.path for f in facts.files],
        mode=config.composition if config is not None else "auto",
        risk=effective,
    )
    # Minus the slices this cycle has not touched, which is the same subtraction `take_readings`
    # makes before it launches anything: a plan scopes every task, and at task 3 of 18 the other
    # fifteen readings have nothing in them to read. Counting them would put "in 18 readings" on
    # the board for a review that is going to take four.
    sizes = review_reading.bytes_by_reading(diff_text, readings)
    taken = {r.unit: sizes.get(r.unit, 0) for r in readings if r.whole or sizes.get(r.unit)}
    unit, largest = max(taken.items(), key=lambda item: item[1]) if taken else (review_reading.WHOLE, 0)
    return ChangeOutlook(
        diff_bytes=largest,
        total_bytes=facts.coverage.analyzed_bytes,
        ceiling=int(limits["max_diff_bytes"]),
        unreadable=tuple(sorted(p for p in unreadable if p)),
        effective_risk=effective,
        composition=tuple(bytes_by_kind(diff_text).items()),
        unit=unit,
        readings=len(taken) or 1,
    )


def generate(
    repo: repo_mod.Repo,
    reviewers: review_policy.Reviewers,
    *,
    base: str | None = None,
    actor: str = "",
    force: bool = False,
    readers: int = 1,
) -> dict[str, Any]:
    """Run the whole pipeline and write `review.yaml`'s machine half; return the assembled machine.

    `reviewers` is asked for the reviewer of each stage's own role and holds the ledger of what
    launching them cost — read here, never written. The pipeline knows which stage it is running,
    so the role is given rather than recovered from the request's shape.

    The deterministic pieces (the coverage manifest, the assembly, the orientation brief) run
    unconditionally; the three reviewer stages are reused from `review_cache` when their own
    inputs have not moved, and their *validated* outputs merged.

    **Each stage is reused on its own inputs, not on the pipeline's.** One `subject` digest used to
    decide whether all three ran, which meant editing `plan.yaml` re-read the code with an
    extractor that has never seen a plan, and promoting a task to `done` re-ran every stage to
    refresh an orientation brief no model produces. `_stage_keys` names what each stage is actually
    a function of; `force` ignores the cache, which is a deliberate act with a visible cost.

    **The human half is reset only when the machine half moves.** A fresh reading is a fresh
    review and no prior human answer speaks for it (plan §6.6) — but an assembly that comes out
    byte-identical is not a fresh reading, and resetting over it discards answers about a change
    nothing touched. A field run recorded `review_generated` fifteen times in one cycle for
    exactly that. Nothing is written and no event is appended when the machine half is unchanged.

    `readers` is how many readings are taken at once (:func:`_read_all`). It buys wall-clock and
    nothing else — the readings are the same readings and cost the same tokens — and it defaults to
    one because concurrent launches spend a provider's session and rate limits, which is an
    operator's judgement about their account rather than a property of the change.

    **A failure records itself.** Every `raise` below used to leave the audit chain with nothing in
    it: `events.ATTENTION_EVENTS` listed `review_failed` and `actual_extraction_failed` as things
    needing a human decision and no code path anywhere emitted either, so a gate ④ that could not
    be produced reported "needing a human decision: 0". The whole log exists so that no state
    change goes unexplained, and the review pipeline's own failure was the state change it could
    not explain.
    """
    store = store_mod.Store(repo)
    # Which stage a failure landed in, for the event that records it. Tracked as the pipeline
    # advances rather than read off the exception: each stage raises its own well-worded error and
    # wrapping those would change what a human sees at the console to say where it happened.
    #
    # It starts at `inputs`, not `coverage`, because reading the SSOT is a stage that fails: a
    # plan.yaml that does not parse means gate ④ cannot be produced, and with these four reads
    # outside the recording block that failure was the one kind still going unrecorded. `cycle` is
    # read *from* those documents, so it is bound before them and stays "" when they are what broke.
    stage = "inputs"
    #: Which reading the failed stage belonged to, when it belonged to one. Empty for the phases
    #: that are not per-reading (inputs, coverage, comparison, assembly, write) and for a review
    #: read whole, which is the only shape a failure could name before composition existed.
    unit = ""
    cycle = ""
    #: How this run ended, for the measurement below. It starts at the pessimistic value so that a
    #: raise anywhere — including one from a line that has not been written yet — is recorded as
    #: what it was rather than as nothing.
    outcome = "failed"
    run_id = str(uuid.uuid4())
    reused = usage_mod.Ledger()
    plan_of_run: dict[str, Any] = {}
    live: run_progress.Writer | None = None

    def entered(name: str) -> None:
        nonlocal stage
        stage = name

    try:
        # state.yaml first, and its cycle taken immediately: it is the document that *names* the
        # cycle, so reading it ahead of the ones that can fail is what lets their failure be
        # recorded under the right cycle instead of nowhere.
        state = store.read_state()
        cycle = state.cycle_id if state else ""
        plan = store.read_plan()
        config = store.read_config()
        # Read once and used three times over — the staleness digest, the reuse check, and the
        # carried-over blocking findings all ask about the same document, and parsing it once per
        # question meant three YAML loads of a file that can hold a whole review.
        #
        # Captured before the pipeline runs, not inside the transaction: the whole point is to
        # refuse if review.yaml moved while the (slow, LLM-driven) stages were running.
        existing = store.read_review()
        seen_review = store_mod.read_digest(existing)

        cycle = cycle or (plan.cycle_id if plan else "")
        if not cycle:
            raise ReviewError("no cycle to record this review under — .rein/state.yaml names none; run `rein doctor`")
        entered("coverage")
        rc, head_out = repo._git_rc("rev-parse", "HEAD")
        if rc != 0:
            raise ReviewError("cannot resolve HEAD — is this a git repository with commits?")
        head = head_out.strip()
        trusted_base = _resolve_base(repo, plan, base)
        exclude = not_the_product(repo, state)
        change = change_digest(repo, head, exclude)

        # A config may set only the budgets it wants to move, so the effective ceilings are the
        # defaults with the repository's overrides on top — the same merge `human_review` does at
        # the freeze, and the snapshot recorded on the assembled review below.
        limits = {**human_review.DEFAULT_BUDGET, **(config.budgets if config is not None else {})}

        # The manifest is about the **whole** change, whatever readings it is then taken in:
        # measuring it over the readings would make the measure a function of how the reading
        # happened to be split. It reads the whole diff, always — folding a file before counting it
        # would be measuring the fold — and `change_digest` above is over the committed tree, so
        # neither the widening nor the folding can move what the review is bound to.
        #
        # The byte budget is **not** measured here. `max_diff_bytes` bounds what one reviewer is
        # asked to read in one launch, and under a composed review no reviewer ever reads the whole
        # change; `read_facts` refuses each reading against it as that reading is measured. Checking
        # the whole here was a wall in front of a quantity nobody reads, and its own instruction —
        # split the scope — is not a move that exists at gate ④.
        whole_diff = review_reading.diff_of(repo, trusted_base, head, exclude)
        facts = diff_facts.analyze(whole_diff)
        effective = review_reading.effective_risk(facts, plan)
        changed = [f.path for f in facts.files]

        # What the last review found blocking *about this same base*. Taken from the copy read at
        # the top rather than re-read here, and before the calls below, which are about to move off
        # this thread — the store is not something to touch from two. It is needed this early
        # because it decides how the change may be read at all: see the re-take below.
        prior_blocking = _prior_blocking(repo, existing, trusted_base, state)

        # How the change is read: one reading of everything, or one per task the plan scopes plus
        # the seam between them. Each is measured and widened on its own, so one launch holds one
        # task's slice rather than a whole cycle, and a slice nobody read is named rather than
        # counted as read (`compose_coverage`).
        readings = review_reading.plan_readings(
            plan, changed, mode=config.composition if config is not None else "auto", risk=effective
        )
        measures = review_reading.take_readings(
            repo, readings, base=trusted_base, head=head, exclude=exclude, limits=limits
        )
        # A composition is a way of reading *this* change only if every finding carried into it
        # lands on a reading that can see the code it names. One that does not is not a finding to
        # be assigned somewhere anyway — the reviewer that got it could neither re-state nor
        # resolve it, and the refusal for dropping a carried finding is not passable from there.
        # Asked after the empty slices are dropped, because a dropped reading owns nothing.
        if orphaned := review_reading.unowned_priors([m.reading for m in measures], prior_blocking):
            logger.warning(
                f"carried blocking finding(s) {', '.join(orphaned)} are anchored in code no task "
                "scope covers, so no slice of this change can answer for them — reading the change "
                "whole instead of composing it"
            )
            measures = review_reading.take_readings(
                repo, [review_reading.WHOLE_READING], base=trusted_base, head=head, exclude=exclude, limits=limits
            )
        # **Highest-risk reading first.** Every reading is taken either way and none is priced
        # differently for it — what the order decides is which answers exist when a run does not
        # finish. A session limit, a capacity refusal or a Ctrl-C leaves the cache holding whatever
        # landed, and plan order made that "the tasks with the lowest ids". The reading's own
        # `risk_floor` is the deterministic detector's, so this is not a model's opinion about what
        # matters, and it is the one use of a per-reading risk that cannot lower anything: the
        # floor every request still carries is the whole change's (`extraction_request`).
        in_plan_order = {r.unit: i for i, r in enumerate(readings)}
        measures.sort(
            key=lambda m: (
                -models.RISK_ORDER.index(m.facts.risk_floor),
                in_plan_order.get(m.reading.unit, len(in_plan_order)),
            )
        )
        coverage = review_reading.compose_coverage(facts.coverage.to_manifest(), measures, changed_paths=changed)

        subject = {
            "change_digest": change,
            "plan_digest": plan.digest() if plan is not None else digests.of({}),
            "config_digest": config.frozen_digest() if config is not None else digests.of({}),
            "environment_digest": _environment_digest(config),
            "coverage_digest": digests.of(coverage),
            "tasks_digest": _tasks_digest(state),
            "trusted_base_sha": trusted_base,
            "subject_head_sha": head,
        }

        # The carried findings go *into the request*, which is both where the reviewer reads them
        # and where the validator now takes them from: the reviewer is refused for dropping one,
        # and was being refused on knowledge nobody had given it. `priors_by_reading` splits them,
        # because a reading can only be held to findings anchored in code it was actually sent.
        prior_by_unit = review_reading.priors_by_reading([m.reading for m in measures], prior_blocking)

        cancel = common.Cancellation()
        cache = review_cache.StageCache(repo.root, enabled=not force)
        # Keyed on the reading, not on the review: `content_digest` narrows to the paths that
        # reading covers, which for the whole-change reading is the whole change.
        keys_by_unit = {
            m.reading.unit: review_reading.keys_for(
                m,
                config=config,
                trusted_base=trusted_base,
                ceiling=limits["max_diff_bytes"],
                # The floor the *whole* change carries, never the slice's own: a reading that
                # happens to hold no signal must not be the place a risk drops (§13.5). It is the
                # same number `build_loop` resolves when it warms a reading, so both look the
                # question up under one key.
                risk_floor=facts.risk_floor,
                prior_blocking=prior_by_unit[m.reading.unit],
            )
            for m in measures
        }
        ran: set[str] = set()
        risk_by_unit = {m.reading.unit: m.facts.risk_floor for m in measures}
        plan_of_run = _execution_plan(config, cache, keys_by_unit, risk_by_unit)
        print(_render_execution_plan(plan_of_run))
        # The same plan the console prints, in the one place the dashboard can see it. Opened here
        # rather than at the top of the run because this is the first moment there is a figure to
        # report: before it, the run has read documents and taken a diff, and "0 of unknown" is not
        # progress. `outcome` is settled in the `finally` below, whatever the ending.
        live = run_progress.Writer(
            repo.root,
            run_id=run_id,
            total=sum(len(k) for k in keys_by_unit.values()) + 1,
            stages=plan_of_run.get("stages", ()),
        )

        # Every reading's two stages, plus the one comparison over the merged Actual. `keys_by_unit`
        # holds only the two — `reading_keys` mints no comparison key, because the Actual it takes
        # as an input does not exist yet — so counting it alone would end the run at "19/18".
        progress = _Progress(reviewers, total=sum(len(k) for k in keys_by_unit.values()) + 1, live=live)
        discipline = review_reading.security_discipline(config)

        def read(m: review_reading.ReadingFacts) -> review_reading.ReadOut:
            # This reading's own stage cell, never the run's: with more than one reading in flight a
            # shared one would name whichever stage some *other* reading had just entered, and the
            # failure event would be filed against a stage that did not fail.
            here = _Stage(unit=m.reading.unit)
            try:
                return review_reading.read_one(
                    repo,
                    reviewers,
                    measured=m,
                    trusted_base=trusted_base,
                    head=head,
                    risk_floor=facts.risk_floor,
                    prior_blocking=prior_by_unit[m.reading.unit],
                    discipline=discipline,
                    on_stage=here.entered,
                    cache=cache,
                    keys=keys_by_unit[m.reading.unit],
                    ran=ran,
                    reused=reused,
                    cancel=cancel,
                    on_progress=progress.landed,
                )
            except BaseException:
                failed.append(here)
                raise

        failed: list[_Stage] = []
        # One heartbeat around the whole reading phase rather than one per reading: what the host's
        # inactivity timeout measures is the gap between two lines, and with several readings in
        # flight one line per reading is several timers saying the same thing. `_Progress` closes
        # each stage; this fills the silence between them.
        with common.Heartbeat(_reading_phase(measures, readers)):
            try:
                readouts = _read_all(measures, readers=readers, cancel=cancel, read=read)
            except BaseException:
                # The earliest failure in reading order, so which stage a reader is told about does
                # not depend on which thread lost a race.
                if failed:
                    order = {m.reading.unit: i for i, m in enumerate(measures)}
                    first = min(failed, key=lambda w: order.get(w.unit, 0))
                    stage, unit = first.stage, first.unit
                raise
        composed = review_reading.merge(readouts, coverage=coverage)
        with common.Heartbeat("comparison"):
            comparison = _compare(
                repo,
                reviewers,
                plan=plan,
                config=config,
                head=head,
                composed=composed,
                effective=effective,
                on_stage=entered,
                cache=cache,
                ran=ran,
                reused=reused,
            )
        progress.landed(review_reading.WHOLE, "comparison", "comparison" not in ran)

        entered("assembly")
        binding: dict[str, Any] = {
            **subject,
            "actual_digest": composed.actual_digest,
            "generated_at": event_chain.now_iso(),
        }
        if seen_models := _independence_record(config, reviewers.spend(), reused.totals()):
            binding["independence"] = seen_models
        gaps = _coverage_gaps(comparison.actual_coverage_gaps)
        machine = assemble(
            binding=binding,
            coverage=coverage,
            actual_statements=composed.statements,
            claims=comparison.claims,
            unanswered=comparison.unanswered,
            gaps=gaps,
            extra_behaviors=_extra_behaviors(comparison.extra_behaviors, gaps=gaps),
            # Disputes re-applied here rather than trusted to survive in the human half: the
            # reviewer has no memory of the last review, so a deterministic false positive is
            # found again, and a regeneration discards the human answers that had settled it.
            security={"findings": security_review.apply_disputes(repo, state, composed.findings)},
            effective_risk=effective,
            plan=plan,
            # The orientation stage, derived here rather than inside `assemble` because it reads
            # config.yaml and state.yaml — documents `assemble` deliberately never takes, so that
            # composing a machine half stays testable without a repository.
            brief_sections=brief.derive(
                plan=plan,
                state=state,
                config=config,
                actual_statements=composed.statements,
                changed_paths=changed,
                blob_facts=_blob_facts(repo, head),
            ),
            residual_findings=brief.residual_findings(state),
            budget_limits=limits,
        )

        entered("write")
        if _same_machine(existing, machine):
            assert existing is not None  # _same_machine is False for None
            print(
                "review: nothing this review is made of has moved — the machine half stands and "
                f"the human answers with it ({len(ran)} stage(s) re-read)"
            )
            cache.prune()
            outcome = "unchanged"
            return dict(existing.machine)

        document = {"machine": machine, "human": {"status": "not_started"}}
        with store.transaction() as tx:
            tx.write("review", document, expect_digest=seen_review)
            tx.append("coverage_generated", cycle_id=cycle, actor=actor)
            # Only the stages that actually ran. A log recording commands issued rather than
            # changes made is a log nobody can aggregate, and a reused answer is not a new reading.
            for stage_name in _STAGE_ORDER:
                if stage_name in ran:
                    tx.append(_STAGE_RAN_EVENT[stage_name], cycle_id=cycle, actor=actor)
            # A blocking finding that stopped blocking is a state change, and the document is not
            # where it survives: the next generation re-derives its findings from a reviewer with
            # no memory of this one, so the resolved row is gone from `review.yaml` the moment the
            # review is regenerated. The chain never rotates, and ids there need no uniqueness
            # across time — nothing resolves a reference by them.
            for closed in composed.resolved:
                tx.append(
                    "security_finding_resolved",
                    cycle_id=cycle,
                    actor=actor,
                    subject_ids=[str(closed.get("id", ""))],
                    detail={
                        "severity": str(closed.get("severity", "")),
                        "resolved_at": dict(closed.get("resolved_at") or {}),
                    },
                )
            tx.append("review_generated", cycle_id=cycle, actor=actor, detail={"change_digest": change})
        cache.prune()
        outcome = "generated"
    except KeyboardInterrupt:
        # A human deciding to stop is not a failed run, and the measurement says so. Same line the
        # `except` below draws, and the same one the signal classifier draws.
        outcome = "interrupted"
        raise
    except Exception as exc:
        # `Exception`, not `BaseException`: a Ctrl-C is a human deciding to stop, and filing that
        # as a review failure would put a decision in the log as a defect. Same line the signal
        # classifier draws (faults._EXTERNAL_SIGNALS leaves SIGINT out).
        _record_failure(store, cycle, actor, stage=stage, unit=unit, failure=exc)
        raise
    finally:
        if live is not None:
            live.ended(outcome)
        run_record.record(
            store,
            kind="review",
            cycle=cycle,
            actor=actor,
            run_id=run_id,
            outcome=outcome,
            plan=plan_of_run,
            billed=reviewers.spend(),
            reused=reused.totals(),
        )
    return machine


@dataclass
class _Stage:
    """Which stage of one reading is in flight — the failure's own address, not the run's."""

    unit: str
    stage: str = "reading"

    def entered(self, name: str) -> None:
        self.stage = name


def _reading_phase(measures: Sequence[review_reading.ReadingFacts], readers: int) -> str:
    """What the heartbeat calls the phase: one reading names itself, several say how many."""
    if len(measures) == 1:
        return f"reading{_named(measures[0].reading.unit)}"
    at_once = min(readers, len(measures))
    return f"{len(measures)} readings, {at_once} at a time" if at_once > 1 else f"{len(measures)} readings"


def _read_all(
    measures: Sequence[review_reading.ReadingFacts],
    *,
    readers: int,
    cancel: common.Cancellation,
    read: Callable[[review_reading.ReadingFacts], review_reading.ReadOut],
) -> list[review_reading.ReadOut]:
    """Every reading, `readers` at a time. Buys wall-clock and nothing else.

    The readings are independent by construction: each is a different slice of the change, each is
    primed into its own session keyed by its own bytes (`review_transport.SharedReading`), and none
    consumes what another produced. So this costs exactly the same tokens as running them one after
    another — what it changes is that a composed review measured at thirteen hours on one run stops
    being thirteen sequential hours.

    **A serial run is the serial code path**, not a pool of one: submitting every reading to a
    single worker would queue them all, so a failure in the first would still be followed by the
    rest starting. One reading at a time means one reading at a time.

    In parallel, the first failure trips `cancel`, which kills every launch in flight *and* refuses
    every launch bound to it afterwards (`common.Cancellation`) — so the readings still queued die
    on arrival rather than each paying for a full pair of stages nobody will read. The results are
    then collected in submission order, so which failure a reader is shown does not depend on which
    thread lost a race. `pool.shutdown` still joins its workers on the way out; what makes that
    quick is the killing, which is the only thing that ends a launch early.
    """
    if readers <= 1:
        return [read(m) for m in measures]

    def bound(m: review_reading.ReadingFacts) -> review_reading.ReadOut:
        # Bound on the worker, because that is the thread whose launches have to be killable from
        # another reading's failure. `read_one` binds the same token again around its own security
        # stage; `cancelling` saves and restores, so the two nest without fighting.
        with common.cancelling(cancel):
            return read(m)

    with ThreadPoolExecutor(max_workers=readers) as pool:
        futures = [pool.submit(bound, m) for m in measures]
        futures_wait(futures, return_when=FIRST_EXCEPTION)
        if any(f.done() and f.exception() is not None for f in futures):
            cancel.cancel()
        return [f.result() for f in futures]


#: Reviewer stages, by the name `generate` tracks them under. `actual_extraction` is the one with
#: an event of its own: it is the stage that reads the code without the plan, so its failure means
#: there is no Actual at all — a different fact from a comparison that had one and could not use it.
_STAGE_EVENT: Mapping[str, str] = {"actual_extraction": "actual_extraction_failed"}

#: The reviewer stages in the order their events are appended, so a log reads the same whichever
#: order two threads happened to finish in.
_STAGE_ORDER: tuple[str, ...] = ("actual_extraction", "comparison", "security_review")

#: What a stage that really ran records. A stage reused from the cache records nothing: it produced
#: no new reading, and an event for it would be a command issued rather than a change made.
_STAGE_RAN_EVENT: Mapping[str, str] = {
    "actual_extraction": "actual_extraction_generated",
    "comparison": "comparison_generated",
    "security_review": "security_review_generated",
}


def _comparison_key(
    *,
    config: models.Config | None,
    plan_digest: str,
    actual_digest: str,
    effective: str,
    independence: Mapping[str, Any],
) -> str:
    """The comparator's key. It reads the Expected and the Actual, and nothing else."""
    return review_cache.stage_key(
        "comparison",
        {
            **_reviewer_identity(config, "comparator"),
            "plan_digest": plan_digest,
            "actual_digest": actual_digest,
            "effective_risk": effective,
            "independence": independence,
        },
    )


def _named(unit: str) -> str:
    """`[T-016]` for a composed review's reading, and nothing at all otherwise.

    A review that was not composed has exactly one reading, so naming it says nothing; and the
    comparison is not a reading at all — it is handed statements, never code — so a unit beside it
    would name something it never read.
    """
    return "" if unit in ("", review_reading.WHOLE) else f"[{unit}]"


class _Progress:
    """Prints one line per stage as it lands: what finished, out of how many, and what it cost.

    A composed review's console output was its execution plan and then nothing at all — four lines
    in eight hours on one measured run, while nine of eighteen readings were in fact being served
    from cache and nine were being read. The number existed only in `events.ndjson`, recoverable by
    pulling the last `run_measured` and counting `decision` fields in `detail.plan.stages`. There
    is no reason a run cannot say it as it happens.

    The cost is taken here because here is where it is attributable: the ledger is per role, each
    stage has exactly one role (`review_policy.STAGE_ROLE`), and the delta across one stage is
    therefore that stage's bill. Otherwise it surfaces only in `run_measured`, after the fact, for
    the whole run at once.

    Written from two threads — `read_one` runs its security stage on a worker — so the counter and
    the ledger snapshot are taken under a lock. A miscounted line is a small thing; a torn read of
    the totals would put a wrong number in front of somebody deciding whether to keep waiting.
    """

    def __init__(
        self,
        reviewers: review_policy.Reviewers,
        *,
        total: int,
        live: run_progress.Writer | None = None,
    ) -> None:
        self._reviewers = reviewers
        self._total = total
        self._done = 0
        self._lock = threading.Lock()
        self._seen: dict[str, usage_mod.Usage] = {}
        #: The same figure, written where something other than this terminal can read it.
        self._live = live

    def landed(self, unit: str, stage: str, was_reused: bool) -> None:
        role = review_policy.STAGE_ROLE[stage]
        with self._lock:
            self._done += 1
            done = self._done
            spent = self._reviewers.spend().get(role, usage_mod.Usage())
            before = self._seen.get(role, usage_mod.Usage())
            self._seen[role] = spent
        what = "reuse" if was_reused else "run"
        bill = "" if was_reused else _billed(before, spent)
        print(f"    [review] {done}/{self._total} {stage}{_named(unit)}: {what}{bill}", flush=True)
        if self._live is not None:
            billed = {name: row.to_detail() for name, row in self._reviewers.spend().items() if row.launches}
            self._live.landed(unit, stage, reused=was_reused, billed=billed)


def _billed(before: usage_mod.Usage, after: usage_mod.Usage) -> str:
    """What one stage's launch cost — the ledger's movement across it.

    Three fields differenced rather than a `Usage.__sub__`: the ledger's totals also carry
    `launches` and the set of `models` that answered, and neither has a meaningful difference. A
    subtraction operator would have to invent one, and an invented number in a cost report is
    exactly the thing this codebase refuses elsewhere.

    A zero is not printed as a price: an adapter that reports no usage is unmeasured, and
    `USD 0.00` beside a launch that was certainly paid for is the one reading this must not allow.

    It is this stage's launch and not the run's share of everything: a `SharedReading`'s priming
    turn is billed to its own role (`review_transport._SHARED_READING_ROLE`), so these lines do not
    sum to the total. The total is the one `usage.summarize` prints when the run ends, and the
    breakdown by role is in `run_measured`.
    """
    if not after.available:
        return " (cost not reported)"
    read = after.total_input_tokens - before.total_input_tokens
    wrote = after.output_tokens - before.output_tokens
    return f" ({read:,} in / {wrote:,} out, USD {after.cost_usd - before.cost_usd:.2f})"


def _execution_plan(
    config: models.Config | None,
    cache: review_cache.StageCache,
    keys_by_unit: Mapping[str, Mapping[str, str]],
    risk_by_unit: Mapping[str, str],
) -> dict[str, Any]:
    """What this run intends to do, settled before it does any of it.

    The run/reuse decision, which role answers each stage and on which model, and whether the two
    reading stages will share one reading of the change — all of it existed only as local variables
    and a `cache.has` call somewhere inside the pipeline, so the only way to know what a review was
    about to spend was to watch it spend it. Deciding it up front costs nothing (`_stage_keys` and
    `cache.has` are already in hand) and makes the intention a thing that can be printed, recorded
    beside what actually happened, and disagreed with.

    `comparison` is `undecided` rather than `run`: its key takes the Actual as an input and the
    Actual does not exist yet (`_comparison_key`). Saying "run" would be a guess, and the whole
    point of writing the plan down is that it does not contain any. It is listed once, not once
    per reading, because it is run once over the merged Actual (`_compare`).

    A row per reading per stage, because that is the unit that is decided: on a composed review one
    task's slice can be reused from the last generation while the task next to it has to be read
    again, and a plan that named only the stages would report that as one decision it does not have.

    Each row carries the reading's own `risk`, which is what `generate` ordered them by. The rows
    are in reading order, so the plan recorded on `run_measured` says both what the run intended to
    do and why it intended to do it in that sequence — the ordering was a judgement nothing wrote
    down. The comparison row's risk is the empty string: it reads the Actual, not a reading.
    """
    stages: list[dict[str, Any]] = []
    for stage in _STAGE_ORDER:
        role = review_policy.STAGE_ROLE[stage]
        for unit, keys in keys_by_unit.items():
            key = keys.get(stage, "")
            stages.append(
                {
                    "stage": stage,
                    "unit": unit,
                    "role": role,
                    "key": key,
                    "decision": ("reuse" if cache.has(stage, key) else "run") if key else "undecided",
                    "risk": risk_by_unit.get(unit, "") if stage != "comparison" else "",
                    "adapter": config.adapter(role) if config is not None else "",
                    "model": config.model(role) if config is not None else "",
                }
            )
            if stage == "comparison":
                break  # one comparison over the merged Actual, whatever it was read in
    plan: dict[str, Any] = {"stages": stages}
    shared = _shares_reading(config)
    if shared is not None:
        plan["shared_reading"] = shared
    return plan


def _shares_reading(config: models.Config | None) -> bool | None:
    """Will the extractor and the security reviewer branch one reading? None when it cannot be said.

    The transport refuses a role this release cannot launch, which is a real answer in the
    production path and no answer at all here — the reviewers are injected, so `generate` runs
    against transports that were never going to launch a CLI. "Cannot say" is left out of the plan
    rather than rendered as `false`.
    """
    try:
        return review_transport.shares_reading(config)
    except adapters.LaunchRefused:
        return None


def _render_execution_plan(plan: dict[str, Any]) -> str:
    """What this run is about to do — the counts, and the units it will actually read.

    It was every row on one line: 36 stage decisions on a composed review, reused ones included,
    ~3,600 characters printed once per attempt. Nothing about a terminal makes that readable, and
    it named the reuses — the decisions that cost nothing and change nothing — at the same weight
    as the launches.

    The full listing is not lost: `run_measured.detail.plan` records every row, which is where a
    question about a past run is answered anyway. This is the console's version, and the console's
    question is "how much of this run is going to be paid for, and against what".
    """
    rows = list(plan.get("stages", []))
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["decision"])] = counts.get(str(row["decision"]), 0) + 1
    readings = len({row.get("unit", "") for row in rows})
    shared = plan.get("shared_reading")
    tail = "" if shared is None else f"; shared reading: {'yes' if shared else 'no'}"
    head = (
        f"review plan: {readings} reading(s){', highest-risk first' if readings > 1 else ''}, "
        f"{len(rows)} stage decision(s) — "
        + ", ".join(f"{n} {decision}" for decision, n in sorted(counts.items()))
        + tail
    )
    return "\n".join([head, *_to_run_lines(rows)])


def _to_run_lines(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """One line per stage that will launch, naming the units and where they go.

    Grouped by stage rather than listed per row: a reader deciding whether to wait wants "the
    extractor has nine readings left, on opus", and the per-unit rows say that nine times.
    """
    by_stage: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        if row["decision"] != "run":
            continue
        where = str(row["model"] or row["adapter"] or "cli default")
        unit = str(row.get("unit", ""))
        by_stage.setdefault((str(row["stage"]), where), []).append("" if unit in ("", review_reading.WHOLE) else unit)
    lines = []
    for (stage, where), units in by_stage.items():
        named = ", ".join(u for u in units if u)
        lines.append(f"  to run: {stage} ×{len(units)} ({where})" + (f" — {named}" if named else ""))
    return lines


def _worth_waiting_for(failure: review_policy.AdapterFailure) -> bool:
    """Would running this again, unchanged, plausibly do better?

    Only a *launch* the machine failed and time can fix — the same narrow licence `rein build
    --supervise` takes. And not a request that did not fit: that classifies as transient (a
    resumed session which outgrew its window is fixed by relaunching cold, so the classifier is
    right to), but this pipeline has no session to reset. The same request will be the same size
    in fifteen minutes, and a supervisor spinning on it burns the quota that would have paid for
    the smaller review.
    """
    if faults.is_context_overflow(failure.output):
        return False
    # The CLI's own status code when it named one: 429 is capacity and 401/403 is a credential,
    # and both used to be decided by matching English inside a byte slice of stream-JSON.
    status = faults.status_code(failure.output)
    if status is not None:
        return status == 429 or status >= 500
    return faults.classify_launch(failure.rc, failure.output) is faults.Fault.ENV_TRANSIENT


def _record_failure(
    store: store_mod.Store, cycle: str, actor: str, *, stage: str, failure: BaseException, unit: str = ""
) -> None:
    """Append the events for a review that could not be produced. Never raises.

    Append-only: nothing was written, so there is no document to stage, and a transaction that
    appends without writing is exactly what `store.Transaction` permits (the refusal runs the other
    way — a write with no event).

    **Which events depends on what failed, and it did not.** Every failure recorded
    `review_failed` plus the stage's own `*_failed`, both of them in `events.ATTENTION_EVENTS`, so
    a supervised run waiting out a session limit filed two "awaits a human decision" rows per
    attempt against a condition the machine had already answered by retrying. Eight attempts on
    one cycle left sixteen of them on a board beside the one blocker that mattered, with no verb
    that could clear one.

    :func:`_worth_waiting_for` is the code that already knows the difference: a launch that failed
    for a machine reason time alone fixes asks for a re-run, not a judgement. That is
    `review_aborted`, outside `ATTENTION_EVENTS` — the same distinction `run_aborted` draws in the
    build loop. Everything else is unchanged: an unparseable answer, a coverage gap, a budget
    refusal, an unreadable SSOT all still sit on the board until somebody answers them.

    Classified from the failure itself rather than from whether `--supervise` was passed. What
    makes a capacity refusal not-a-decision is the refusal, not the flag: without the flag the run
    stops, and what is then waiting on the human is "there is no machine review", which the board
    already says as a blocker.

    Swallowing the store's own errors is deliberate. This runs inside an `except` block whose job
    is to re-raise the real failure; a store problem here would replace the error a human needs to
    read with one about bookkeeping, and the log being unwritable is what `rein doctor` is for.
    """
    reason = str(failure)
    transient = isinstance(failure, review_policy.AdapterFailure) and _worth_waiting_for(failure)
    if not cycle:
        # There is no cycle to file it under, and an event that cannot name one is refused by
        # `event_chain.make`. Say where the account went instead of leaving the reader to notice
        # the log is silent — this is reachable only in the `inputs` stage, where the cycle is read
        # out of the very documents that failed.
        logger.warning(
            f"the review failed at stage '{stage}'{_named(unit)} and no cycle is established to "
            f"record it under: {reason}"
        )
        return
    try:
        with store.transaction() as tx:
            detail: dict[str, Any] = {"stage": stage, "reason": reason[:1000]}
            if unit:
                # Which reading it was. A composed review runs the same stage once per slice, so
                # "actual_extraction failed" without it names three of eighteen launches at once.
                detail["unit"] = unit
            if transient:
                tx.append("review_aborted", cycle_id=cycle, actor=actor, detail=detail)
                return
            if stage in _STAGE_EVENT:
                tx.append(_STAGE_EVENT[stage], cycle_id=cycle, actor=actor, detail=detail)
            tx.append("review_failed", cycle_id=cycle, actor=actor, detail=detail)
    except Exception as exc:  # the original failure must reach the reader, not this one
        logger.warning(f"could not record the review failure in the audit log: {exc}")


def _compare(
    repo: repo_mod.Repo,
    reviewers: review_policy.Reviewers,
    *,
    plan: models.Plan | None,
    config: models.Config | None,
    head: str,
    composed: review_reading.Composition,
    effective: str,
    on_stage: Callable[[str], None] = lambda _name: None,
    cache: review_cache.StageCache,
    ran: set[str],
    reused: usage_mod.Ledger,
) -> conformance.ComparatorResult:
    """Expected vs Actual — the Actual arrives read-only and digest-bound (§12.3).

    Run once over the merged Actual however many readings produced it: a claim can be answered by
    code from two tasks, and a comparator shown one slice at a time could only ever say `unknown`
    about the half it was not holding. This is the stage that has to see the whole thing, and it is
    also the one that can — it is handed statements, never the code.

    Its key is minted here rather than passed in because it takes the Actual as an input, and the
    Actual is what the readings above produce. Reusing every reading therefore reuses the
    comparison too, since an identical Actual keys the same question — and moving `plan.yaml` alone
    re-runs only this stage.
    """
    on_stage("comparison")
    compare_request = conformance.build_request(
        expected_model=_expected_model(plan),
        actual_statements=composed.statements,
        actual_digest=composed.actual_digest,
    )
    known_ids = _known_ids(plan, composed.statements)
    independence = _independence(config)
    return _cached_stage(
        cache,
        "comparison",
        _comparison_key(
            config=config,
            plan_digest=plan.digest() if plan is not None else digests.of({}),
            actual_digest=composed.actual_digest,
            effective=effective,
            independence=independence,
        ),
        ran,
        lambda ask: conformance.run_comparator(
            compare_request,
            ask,
            repo=repo,
            commit=head,
            actual_statements=composed.statements,
            known_ids=known_ids,
            expected_claim_ids=[c.id for c in plan.claims] if plan is not None else [],
            effective_risk=effective,
            independence=independence,
        ),
        reviewers,
        reused=reused,
    )


def _known_ids(plan: models.Plan | None, actual_statements: Sequence[Mapping[str, Any]]) -> list[str]:
    ids = [str(a.get("id")) for a in actual_statements]
    if plan is not None:
        ids += [c.id for c in plan.claims]
    return ids


def _prior_blocking(
    repo: repo_mod.Repo, review: models.Review | None, trusted_base: str, state: models.State | None
) -> list[dict[str, Any]]:
    """The blocking findings the previous review recorded **about the same base**, if any.

    Whole findings, anchors included: `security_review.resolution_of` decides whether a dropped one
    was fixed or forgotten by re-reading the code it named, and an id says nothing about that.

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

    **A finding a human has contradicted does not carry.** Holding the reviewer to a finding it
    was right not to re-emit is what made a false positive unescapable: the human disputed it, the
    regeneration discarded the human review that held the dispute, the reviewer honestly left the
    finding out, and `resolution_of` could not close it because the code it named was correct and
    still there — so the gate refused the drop as "a reviewer cannot clear its own block", every
    time, for the rest of the cycle. Two responsibilities had been folded into one list: carrying
    a blocker forward so a reviewer cannot quietly retract it, and re-deciding whether it is true.
    The second is not the reviewer's to answer here, and the durable record of the human's answer
    is `state.disputed_findings` — bound to the anchored text, so it retires if that code moves.
    """
    if review is None or not trusted_base:
        return []
    recorded = str(review.raw.get("machine", {}).get("binding", {}).get("trusted_base_sha", ""))
    if not recorded or recorded != trusted_base:
        return []
    findings = [dict(f) for f in review.blocking_security_findings]
    disputed = security_review.live_disputes(repo, state, findings)
    return [f for f in findings if str(f.get("id", "")) not in disputed]


def _independence_record(
    config: models.Config | None,
    spend: Mapping[str, usage_mod.Usage] | None,
    reused: Mapping[str, usage_mod.Usage] | None = None,
) -> dict[str, Any]:
    """Which model each reviewer was *asked* for and which one *answered*, for the binding.

    `binding.independence` was declared in the schema, rendered by the dashboard, and written by
    nobody — so a gate receipt bound no record of who produced either half of the review, at the
    one gate where the plan requires them to differ. It is written here from two sources that
    cannot be the same mistake: `group` is what the config asked for, `model` is the id the launch
    reported having used (`usage.Usage.models`).

    A role whose adapter reports no usage carries no `model`, which is the honest record: nobody
    measured, and `review_policy.independence_observed` stays silent rather than reading an absent
    observation as agreement.

    `reused` is where a replayed stage's observation comes from. A stage served from
    `review_cache` makes no launch, so it contributed nothing to `spend` and its `model` went
    missing from the binding — which quietly stood the critical-independence check down on exactly
    the runs a cache hit makes cheap. The model that answered is a property of the answer, not of
    who paid for it.
    """
    record: dict[str, Any] = {}
    for role in ("actual_extractor", "comparator", "security_reviewer"):
        entry: dict[str, Any] = {}
        group = config.independence_group(role) if config is not None else ""
        if group:
            entry["group"] = group
        observed = (spend or {}).get(role) or (reused or {}).get(role)
        # One id, or nothing. Two would mean the launch itself switched models part-way, which is
        # not a fact about this role's opinion and must not be recorded as one.
        if observed is not None and len(observed.models) == 1:
            entry["model"] = observed.models[0]
        if entry:
            record[role] = entry
    return record


def _independence(config: models.Config | None) -> dict[str, Any]:
    """The declared reviewer groups, read from the config that declares them.

    The group is `<adapter>/<model>` and is derived from what the role is launched with, so it is
    read from the config rather than assumed: `review_policy.independence_ok` — the Actual
    Extractor and the Comparator must not be the same opinion — has nothing to enforce otherwise.
    An unnamed model leaves the group empty rather than inventing one: two roles on the CLI's
    default are one launch twice, and the check refuses that at critical, which is the right answer.
    """
    if config is None:
        return {"actual_extractor": {"group": ""}, "comparator": {"group": ""}}
    return {role: {"group": config.independence_group(role)} for role in ("actual_extractor", "comparator")}


def _tasks_digest(state: models.State | None) -> str:
    """The task facts the orientation is derived from, as one digest.

    `change_digest` covers the committed tree *minus* `.rein/`, which is right for a review of the
    code and wrong as the whole reuse key: the orientation brief and the residual findings are
    derived from `state.yaml`, and a task promoted from `awaiting-evidence` to `done` after a human
    recorded what they saw moves none of the other digests. Reusing across that served a brief the
    repository had since contradicted, at the gate where the whole point is that it has not.

    Only `tasks`, because that is what `brief.derive` and `brief.residual_findings` read. The gate
    lines and the freeze record move for reasons that say nothing about this document.
    """
    tasks = state.raw.get("tasks") if state is not None else None
    return digests.of(tasks if isinstance(tasks, dict) else {}, drop=digests.VOLATILE_TIMESTAMP_KEYS)


def _same_machine(existing: models.Review | None, machine: Mapping[str, Any]) -> bool:
    """Is the assembled machine half the one already on disk, word for word?

    This is what decides whether the human answers survive, and it asks about the *product* rather
    than about the inputs. The old check compared a `subject` digest of the inputs, which is a
    weaker question in both directions: two runs of the same inputs can differ (a model is not a
    function), and — the case that cost real work — a run whose inputs moved for reasons that
    change nothing in the document still reset every answer a reviewer had recorded.

    `binding.generated_at` is excluded because it is the one field that moves on every run by
    construction. Nothing else here is a timestamp, so nothing else needs excluding, and leaving it
    in would make this check answer "no" always.

    A malformed or ungenerated review is not reusable — "it did not say" must never read as
    "it agreed".
    """
    if existing is None or not existing.is_generated:
        return False
    return digests.of(_without_generated_at(existing.machine)) == digests.of(_without_generated_at(machine))


def _without_generated_at(machine: Mapping[str, Any]) -> dict[str, Any]:
    binding = {key: value for key, value in dict(machine.get("binding", {}) or {}).items() if key != "generated_at"}
    return {**dict(machine), "binding": binding}


def _environment_digest(config: models.Config | None) -> str:
    """What the review was produced in: the executor profiles that ran its steps, pins included.

    Delegates to :meth:`models.Config.environment_digest`, which the gate ③ freeze binds too — two
    copies of "which sandbox was this" would eventually disagree, and then a freeze and a review
    would be talking about different environments while reporting the same digest name.

    This is the digest that is *allowed* to move within a cycle (a dependency was added, the image
    was rebuilt). Which is exactly why it is recorded here: gate ④ shows the human that the
    environment its evidence was produced in is not the one gate ③ saw.
    """
    return config.environment_digest() if config is not None else digests.of({"executors": None})


def _coverage_gaps(gaps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Comparator-reported actual-coverage gaps, shaped as review.yaml gap records where possible.

    `blocking` is derived from the risk rather than read from the comparator: it is the gate's
    price for the gap, and pricing is the policy's (`review_policy.blocks`).
    """
    out: list[dict[str, Any]] = []
    for index, gap in enumerate(gaps, start=1):
        risk = str(gap.get("risk", "medium"))
        out.append(
            {
                "id": str(gap.get("id", f"GAP-{index:03d}")),
                "kind": str(gap.get("kind", "actual_coverage_gap")),
                "statement_id": str(gap.get("statement_id", f"STMT-{index:03d}")),
                "risk": risk,
                "blocking": review_policy.blocks(risk),
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
        risk = str(extra.get("risk", "medium"))
        # `grounded` defaults to the answerable direction: it is what takes an extra behaviour off
        # the human's list, so an omitted flag must not be the thing that does it. `blocking` is
        # not the comparator's at all — the policy prices the risk it stated.
        grounded = extra.get("grounded") is True
        record = {
            "id": str(extra.get("id", f"EXTRA-{offset + 1:03d}")),
            "statement_id": str(extra.get("statement_id", f"STMT-{first + offset:03d}")),
            "category": str(extra.get("category", "")),
            "risk": risk,
            "grounded": grounded,
            "blocking": review_policy.blocks(risk, grounded=grounded),
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
    if state is None or not state.cycle_id:
        raise ReviewError("cannot record the freeze — .rein/state.yaml names no cycle; run `rein doctor`")
    with store.transaction() as tx:
        tx.write("review", {**review.raw, "human": new_human}, expect_digest=seen)
        tx.append("human_review_frozen", cycle_id=state.cycle_id, actor=actor)


# -- CLI ----------------------------------------------------------------------


#: The longest `--supervise` will wait on one refusal, however far off the reset it was told about.
#: A session limit that lifts tomorrow is not something to hold a terminal open for, and a CLI that
#: names a time this far out is likelier to have been misread than to be right.
MAX_SUPERVISE_SLEEP_SEC = 6 * 3600


def _supervise_delay(failure: review_policy.AdapterFailure, interval_sec: int) -> tuple[int, str]:
    """`(seconds to sleep, the CLI's own words)` before trying this stage again.

    The reset time was being extracted, printed, and thrown away: seven attempts fifteen minutes
    apart, each refused in under half a second against a limit that lifted at 06:50, and the host
    session ended before it did. Nothing was learned by any of them.

    So when the refusal names a time, wait until it (plus `faults.RESET_MARGIN_SEC`) instead of a
    fixed interval — capped, and never shorter than one interval, because a reset that has already
    passed by the time this reads it would otherwise turn the supervisor into a spin.
    """
    hint = faults.reset_hint(failure.output)
    when = faults.reset_at(failure.output)
    if when is None:
        return interval_sec, hint
    seconds = int((when - datetime.now(timezone.utc)).total_seconds()) + faults.RESET_MARGIN_SEC
    return max(interval_sec, min(seconds, MAX_SUPERVISE_SLEEP_SEC)), hint


def _generate_cli(
    repo: repo_mod.Repo,
    *,
    force: bool,
    supervise: bool,
    interval_sec: int,
    readers: int = 1,
    make_reviewers: Callable[[], review_policy.Reviewers] | None = None,
) -> tuple[dict[str, Any], dict[str, usage_mod.Usage]]:
    """`(the machine review, what the attempts cost)`, waiting out a machine failure time can fix.

    Only a **launch** that failed for a machine reason (:func:`_worth_waiting_for`) is waited out
    — the same narrow licence `rein build --supervise` takes. Capacity comes back; the only cost
    of waiting is the wait. Everything else — a budget refusal, an unreadable SSOT, a coverage
    gap — is a real answer, and sleeping on it would turn a verdict into a loop.

    **An answer the validator refuses is not retried here, and deliberately so.** It is retried
    where it is judged (`review_reading._run_once_more_if_refused`), which is what makes the
    budget the stage's rather than the run's: a second malformed answer eighteen readings later
    must not find the allowance spent by the first, and one bad answer must not re-enter the whole
    pipeline. By the time a `ReviewPolicyError` reaches here, the stage has already had its extra
    launch, so this is the verdict.

    A retry costs only the stages that have not answered yet: `review_cache` keeps each stage's
    answer as it validates, so waiting out a capacity stop no longer re-reads the whole change.

    A fresh transport per attempt, so a retry re-reads the config rather than holding whatever was
    true when the first one was built — and the bill is returned rather than filled in through a
    dict the caller passed down, because a supervised run's bill is every launch it made and the
    per-attempt transports are the only things that know.
    """
    build_reviewers = make_reviewers or (lambda: review_transport.StagedReviewers(repo))
    spend: dict[str, usage_mod.Usage] = {}
    attempt = 0
    while True:
        attempt += 1
        reviewers = build_reviewers()
        try:
            return generate(repo, reviewers, force=force, readers=readers), spend
        except review_policy.AdapterFailure as failure:
            if not supervise or not _worth_waiting_for(failure):
                raise
            delay, hint = _supervise_delay(failure, interval_sec)
            logger.info(
                f"[supervise] attempt {attempt}: {failure} — sleeping {delay}s"
                + (f" (the CLI said: {hint})" if hint else "")
            )
            time.sleep(delay)
        finally:
            for role, row in reviewers.spend().items():
                usage_mod.merged(spend, role, row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein review", description="the grounded machine review (gate ④)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="run the review pipeline and write review.yaml")
    gen.add_argument(
        "--force",
        action="store_true",
        help=(
            "ignore the stored stage answers and read the change again (a re-reading that says "
            "the same thing leaves the human answers standing)"
        ),
    )
    gen.add_argument(
        "--readers",
        type=int,
        default=1,
        metavar="N",
        help=(
            "take up to N of the change's readings at once (default: 1). A composed review is one "
            "reading per scoped task, each independent of the others, so this buys wall-clock and "
            "costs the same tokens — but concurrent launches spend your provider's session and "
            "rate limits, which is why it is a flag and not a setting: it is a judgement about "
            "the account, not about the change, and gate ③'s frozen config is no place for it"
        ),
    )
    gen.add_argument(
        "--supervise",
        action="store_true",
        help=(
            "when a stage's launch fails for a machine reason time alone fixes (capacity "
            "exhausted, a signal), sleep and run it again instead of exiting"
        ),
    )
    gen.add_argument(
        "--supervise-interval-sec",
        type=int,
        default=900,
        help="seconds to sleep between retries under --supervise (default: 900, the build loop's interval)",
    )
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
            if args.supervise and args.supervise_interval_sec < 1:
                logger.error("--supervise-interval-sec must be at least 1")
                return 2
            if args.readers < 1:
                logger.error("--readers must be at least 1")
                return 2
            _, spend = _generate_cli(
                repo,
                force=args.force,
                supervise=args.supervise,
                interval_sec=args.supervise_interval_sec,
                readers=args.readers,
            )
            if measured := usage_mod.summarize(spend, what="review"):
                print(measured)
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
        review_transport.TransportError,
        adapters.LaunchRefused,
        review_policy.ReviewPolicyError,
        store_mod.StoreError,
        models.DocumentError,
    ) as exc:
        logger.error(str(exc))
        return 1
    return 0
