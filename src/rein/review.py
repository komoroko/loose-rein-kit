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
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from rein import (
    actual_extraction,
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
    review_transport,
    run_record,
    security_review,
)
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: One stage's validated result — whatever `_cached_stage` was handed a runner for.
T = TypeVar("T")

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


def _diff(repo: repo_mod.Repo, base: str, head: str, *, context: int | None = None) -> str:
    """The change under review, which is the *product* — `.rein/` is not part of it.

    The same exclusion `change_digest` above takes, through the same constant, because the two
    have to be answers about one subject. They were not: the digest a review binds itself to left
    the SSOT out, and the diff every reviewer read put it back in — schema payloads, the frozen
    plan, task state, the event log, all of it handed over as if it were code somebody wrote. A
    field report measured it at 27% of a normal cycle's diff, and the extractor's request went past
    the model's hard context ceiling on the strength of it, which is a gate ④ that cannot be
    produced at all.

    Not a fold, which is what a lockfile gets: a folded file is still *in* the change and the
    Coverage Manifest goes on reporting its body as unread. `.rein/` is not in the change, so
    reporting it unread would be a coverage gap invented out of something nobody was ever meant
    to review — and `_default_status` turns any generated file into `insufficient`, which at
    high risk is a gate ④ block whose instruction ("split the unreadable part out of this scope")
    cannot be carried out on the orchestration state itself.
    """
    width = () if context is None else (f"-U{context}",)
    rc, out = repo._git_rc("diff", *width, f"{base}..{head}", "--", *repo_mod.SSOT_PATHSPEC)
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


# -- the change the reviewers are allowed to read -----------------------------

#: Git's own default context width, and the floor of the ladder below: the plain diff is what the
#: Coverage Manifest and the byte budget are measured on, so no request is ever narrower than the
#: change itself.
PLAIN_CONTEXT = 3

#: How much context around each hunk to ask git for, widest first. A hunk without its surroundings
#: is often unreadable — "was this guard removed, or moved?" cannot be answered from the hunk alone
#: — so the request buys as much of them as the budget allows and says which rung it landed on.
CONTEXT_LADDER: tuple[int, ...] = (30, 15, 10)


@dataclass(frozen=True)
class Reviewable:
    """The change as a reviewer reads it: the diff, widened around each hunk, and what that cost.

    This replaces what used to be sent beside the diff — the whole head-side body of every changed
    file, under a per-file and a whole-request character cap. That was the wrong shape twice over,
    and measuring one cycle of this repository (17 files) says so:

      * The bodies came to 776 KB against a 240 KB request cap, so **69% of them were dropped** —
        by position in the diff, which is nobody's idea of what matters.
      * What survived was `text[:40000]`, the *first* 40 KB of each file. For a 145 KB module that
        is its docstring and its imports. The functions that actually changed were not in it.

    So the request spent 240 KB on the parts of the changed files the change did not touch, dropped
    two thirds of what it meant to send, and left the reviewer to work from the bare hunks anyway —
    while pushing the whole request towards the context ceiling the adapter then failed on
    ("Prompt is too long", a failure no retry can fix and every retry pays full price for).

    Widening the diff answers the question the bodies were there to answer, and every byte of it is
    adjacent to a change. `--function-context` was measured and rejected: with no funcname pattern
    to anchor on it expands without bound, taking one 1.9 KB diff to 110 KB and a JSON schema's
    1.4 KB to 50 KB.

    `context_lines` and `narrowed_from` go *into* the request, so a reviewer can never read "the
    rest of this function is not here" as "there is nothing more to see" — the same rule the
    Coverage Manifest applies to the diff (plan §13.3, §2.4).
    """

    text: str
    context_lines: int
    folded: tuple[str, ...] = ()

    def as_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"unit": "diff", "context_lines": self.context_lines}
        if self.context_lines < CONTEXT_LADDER[0]:
            facts["narrowed_from"] = CONTEXT_LADDER[0]
        if self.folded:
            facts["mechanical_bodies_withheld"] = list(self.folded)
        return facts


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


def _file_facts(repo: repo_mod.Repo, head: str, files: Sequence[diff_facts.DiffFile]) -> list[dict[str, Any]]:
    """The blob and the line count of each changed path at `head`, for anchoring.

    Every code anchor a reviewer produces is validated against the committed tree: the blob has to
    be the one at that path, and the line range has to be inside the file. Both facts used to be
    the *reviewer's* to find out, which it could only do by reading the repository it was launched
    in — the same access that let a blind extractor open `.rein/plan.yaml`. Handing the two facts
    over is what makes the launch able to answer without the repository at all.

    **A list of records, never a mapping keyed by path.** `actual_extraction.assert_blind` walks
    the request for Expected-Model *keys*, so a path used as a key is a filename being read as
    structure: a product with a root-level file called `plan` — or `claims`, `solution`,
    `rationale` — made every review fail with "the extractor request carries Expected-Model keys
    ['plan']", a sentence about priming describing a file nobody had primed anything with. The
    payload this replaced (`relevant_code`) was keyed by path too and had the same hole.

    Deliberately identity and size, not content: the bodies are what `Reviewable` replaced, and
    putting them back under another name would undo the measurement that removed them.

    Every non-binary changed path is listed, mechanical ones included, even though their bodies are
    withheld from the diff. The tempting symmetry — withhold the body, withhold the blob — sets a
    trap instead of closing one: `review.schema.json` requires `blob` on every code anchor, so a
    reviewer that anchors a path this list omits produces a statement the *write* rejects, and the
    whole review fails on a schema error several steps from the cause. The contract tells it to say
    less rather than reach past what it can anchor; that is the place for that rule, not here.
    """
    facts: list[dict[str, Any]] = []
    for file in files:
        if file.binary:
            continue
        rc, blob = repo._git_rc("rev-parse", f"{head}:{file.path}")
        if rc != 0 or not blob.strip():  # deleted at head — there is nothing to anchor in
            continue
        rc, content = repo._git_rc("show", f"{head}:{file.path}")
        if rc != 0:
            continue
        facts.append({"path": file.path, "blob": f"git-blob:{blob.strip()}", "lines": content.count("\n") + 1})
    return facts


def _reviewable(
    repo: repo_mod.Repo,
    base: str,
    head: str,
    files: Sequence[diff_facts.DiffFile],
    *,
    plain: str,
    ceiling: int,
) -> Reviewable:
    """The widest context that fits `ceiling`, falling back to the plain diff already in hand.

    The ceiling is `max_diff_bytes` — not a second limit invented here, but the one byte budget a
    human already approves the review against (`_refuse_over_budget`). That is the
    property worth having: **what a reviewer is sent cannot exceed what was approved**, where
    before it was the approved diff *plus* an unbounded-by-anyone 240 KB of file bodies.

    Ordered widest-first so the common case costs one `git diff`; a change large enough to need the
    ladder pays a few more, which is cheap next to the model launch it is sizing.
    """
    for lines in CONTEXT_LADDER:
        text, folded = fold_mechanical(_diff(repo, base, head, context=lines), files)
        if len(text.encode("utf-8")) <= ceiling:
            return Reviewable(text=text, context_lines=lines, folded=tuple(folded))
    # Over the ceiling even at git's default width. Refusing here would be a second budget nobody
    # approved: `_refuse_over_budget` has already passed on this diff, and the answer to a change
    # too big to review is `/revise`, not a narrower window onto it.
    text, folded = fold_mechanical(plain, files)
    return Reviewable(text=text, context_lines=PLAIN_CONTEXT, folded=tuple(folded))


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
            diff_bytes=int(coverage["analyzed_bytes"]),
            decision_cards=cards,
            statements=statements,
            gaps=gaps,
        )
    return machine


# -- generation ---------------------------------------------------------------


def _refuse_over_budget(diff_bytes: int, limits: Mapping[str, int]) -> None:
    """Refuse a review whose diff is already past the one byte-denominated budget.

    `max_diff_bytes` was measurable only *after* the pipeline ran: `human_review` reads it off the
    finished coverage manifest, at the freeze. So a change big enough that the
    three reviewer stages cannot be run against it at all never reached the budget's own
    instruction — the operator paid three model launches to be told "the adapter exited 1", and
    the sentence that would have said what to do about it lived behind the failure.

    It is the same wall either way. A diff over this limit cannot be frozen once generated, so
    nothing is refused here that would have been allowed later; what changes is that it is refused
    before the launches rather than after them, and with the budget's own name on it. Measured
    over the whole diff, exactly as `human_review.budget_actuals` measures it, so passing here and
    blowing it at the freeze is not a thing that can happen.
    """
    ceiling = limits["max_diff_bytes"]
    if diff_bytes <= ceiling:
        return
    raise ReviewError(
        "review budget exceeded before the pipeline ran — split the scope, do not grow the "
        f"screen: max_diff_bytes is {ceiling} and this change's diff is "
        f"{diff_bytes} bytes. Reduce what this cycle claims through `/revise` and review the "
        "remainder in its own gate ④ round, or raise the limit in `review_policy.budgets` as a "
        "deliberate, recorded decision about how much one person can hold at once."
    )


def generate(
    repo: repo_mod.Repo,
    reviewers: review_policy.Reviewers,
    *,
    base: str | None = None,
    actor: str = "",
    force: bool = False,
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
    cycle = ""
    #: How this run ended, for the measurement below. It starts at the pessimistic value so that a
    #: raise anywhere — including one from a line that has not been written yet — is recorded as
    #: what it was rather than as nothing.
    outcome = "failed"
    run_id = str(uuid.uuid4())
    reused = usage_mod.Ledger()
    plan_of_run: dict[str, Any] = {}

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
        change = change_digest(repo, head)
        diff_text = _diff(repo, trusted_base, head)

        # The manifest reads the *whole* diff, always: what it measures is how much of the change
        # could be analysed, and folding a file before counting it would be measuring the fold.
        facts = diff_facts.analyze(diff_text)
        coverage = facts.coverage.to_manifest()
        risk_floor = facts.risk_floor
        effective = _effective_risk(facts, plan)

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

        # A config may set only the budgets it wants to move, so the effective ceilings are the
        # defaults with the repository's overrides on top — the same merge `human_review` does at
        # the freeze, and the snapshot recorded on the assembled review below.
        limits = {**human_review.DEFAULT_BUDGET, **(config.budgets if config is not None else {})}
        _refuse_over_budget(facts.coverage.analyzed_bytes, limits)

        # The reviewers read a widened, folded diff. `change_digest` above is over the committed
        # tree, so neither the widening nor the folding can move what the review is bound to.
        reviewable = _reviewable(
            repo,
            trusted_base,
            head,
            facts.files,
            plain=diff_text,
            ceiling=limits["max_diff_bytes"],
        )

        # What a reviewer needs to anchor a statement, since it is launched with nothing to read
        # but its request (`review_transport`).
        anchorable = _file_facts(repo, head, facts.files)
        # What the last review found blocking *about this same base*. Taken from the copy read at
        # the top rather than re-read here, and taken before the call below, which is about to move
        # off this thread — the store is not something to touch from two. It goes *into the
        # request*, which is both where the reviewer reads it and where the validator now takes it
        # from: the reviewer is refused for dropping one of these, and was being refused on
        # knowledge nobody had given it.
        prior_blocking = _prior_blocking(existing, trusted_base)

        security_request = security_review.build_request(
            diff_text=reviewable.text,
            deterministic_facts={
                "signals": [h.signal for h in facts.signals],
                "context": reviewable.as_facts(),
                "files": anchorable,
            },
            trusted_base_sha=trusted_base,
            subject_head_sha=head,
            prior_blocking=prior_blocking,
        )

        # The security review reads the same change the extractor does and consumes nothing the
        # extractor or the comparator produce, so it does not have to wait behind them. All three
        # LLM stages ran end to end for no reason but the order they were written in. Determinism
        # is unaffected: the results are merged, and the events appended, in a fixed order below,
        # and a failure in the extraction still surfaces ahead of one here.
        #
        # `cancel` is what makes the failure path honest. `Future.cancel()` cannot stop a task that
        # has started — with one worker and nothing competing for it, this one always has — and
        # `ThreadPoolExecutor.__exit__` then blocks in `shutdown(wait=True)` until the adapter call
        # it failed to cancel finishes. So a failure is reported when the *discarded* call ends
        # rather than when it happens — measured across two runs of one cycle, the extraction
        # failure surfaced 1m36s and 3m54s late, each run having paid in full for a security review
        # nobody would read. `shutdown(wait=False, cancel_futures=True)` does not fix it
        # either: `concurrent.futures.thread` registers an atexit hook that joins every worker, so
        # the wait moves from here to interpreter exit and the process returns no sooner. The only
        # thing that ends a launch early is killing the process it started.
        cancel = common.Cancellation()
        cache = review_cache.StageCache(repo.root, enabled=not force)
        keys = _stage_keys(
            config=config,
            change=change,
            coverage_digest=subject["coverage_digest"],
            trusted_base=trusted_base,
            head=head,
            ceiling=limits["max_diff_bytes"],
            risk_floor=risk_floor,
            prior_blocking=[str(f.get("id", "")) for f in prior_blocking],
        )
        ran: set[str] = set()
        plan_of_run = _execution_plan(config, cache, keys)
        print(_render_execution_plan(plan_of_run))

        def run_security() -> security_review.SecurityResult:
            # Bound on this thread — the worker's — because the transport is reached through the
            # injected reviewers, whose signature is not ours to change.
            with common.cancelling(cancel):
                return _cached_stage(
                    cache,
                    "security_review",
                    keys["security_review"],
                    ran,
                    lambda ask: security_review.run_security_review(
                        security_request,
                        ask,
                        repo=repo,
                        commit=head,
                    ),
                    reviewers,
                    reused=reused,
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            security_future = pool.submit(run_security)
            try:
                extraction, comparison = _extract_and_compare(
                    repo,
                    reviewers,
                    plan=plan,
                    config=config,
                    head=head,
                    trusted_base=trusted_base,
                    reviewable=reviewable,
                    anchorable=anchorable,
                    coverage=coverage,
                    risk_floor=risk_floor,
                    effective=effective,
                    on_stage=entered,
                    cache=cache,
                    keys=keys,
                    ran=ran,
                    reused=reused,
                )
            except BaseException:
                # The security stage's own failure is not the one to report: this one came first in
                # the pipeline's order, and reporting whichever thread lost a race would make the
                # error a reader sees depend on timing. So the sibling is killed rather than
                # awaited — the `with` above still joins the worker, but there is nothing left for
                # it to wait on.
                cancel.cancel()
                raise
            entered("security_review")
            security = security_future.result()

        entered("assembly")
        binding: dict[str, Any] = {
            **subject,
            "actual_digest": extraction.actual_digest,
            "generated_at": event_chain.now_iso(),
        }
        if seen_models := _independence_record(config, reviewers.spend(), reused.totals()):
            binding["independence"] = seen_models
        gaps = _coverage_gaps(comparison.actual_coverage_gaps)
        machine = assemble(
            binding=binding,
            coverage=coverage,
            actual_statements=extraction.actual_statements,
            claims=comparison.claims,
            unanswered=comparison.unanswered,
            gaps=gaps,
            extra_behaviors=_extra_behaviors(comparison.extra_behaviors, gaps=gaps),
            security=security.to_section(),
            effective_risk=effective,
            plan=plan,
            # The orientation stage, derived here rather than inside `assemble` because it reads
            # config.yaml and state.yaml — documents `assemble` deliberately never takes, so that
            # composing a machine half stays testable without a repository.
            brief_sections=brief.derive(
                plan=plan,
                state=state,
                config=config,
                actual_statements=extraction.actual_statements,
                changed_paths=[f.path for f in facts.files],
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
            for closed in security.resolved:
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
        _record_failure(store, cycle, actor, stage=stage, reason=str(exc))
        raise
    finally:
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


def _reviewer_identity(config: models.Config | None, role: str) -> dict[str, str]:
    """Which model answers for `role`. Part of a stage key: a different model is a different answer."""
    if config is None:
        return {"adapter": "", "independence_group": ""}
    return {"adapter": config.adapter(role), "independence_group": config.independence_group(role)}


def _stage_keys(
    *,
    config: models.Config | None,
    change: str,
    coverage_digest: str,
    trusted_base: str,
    head: str,
    ceiling: int,
    risk_floor: str,
    prior_blocking: Sequence[str],
) -> dict[str, str]:
    """What each reviewer stage is a function of, one key per stage (`review_cache`).

    Written out rather than folded into one `subject` digest, because that is the whole repair:
    the extractor and the security reviewer are not functions of `plan.yaml`, and none of the three
    is a function of `state.yaml`'s task statuses — the orientation brief is, and no model produces
    it. `comparison` is missing from here because it takes the Actual as an input and the Actual
    does not exist yet; `_comparison_key` mints it once the extractor has answered.

    Two digests that *are* in the binding are deliberately not in any key. `config_digest` covers
    the whole frozen config — the quality gate, the guard paths, limits no reviewer reads — so
    keying on it would re-run three models over a changed test command; the parts a stage really
    depends on (its adapter, its group, the byte ceiling that decides how wide a diff it is sent)
    are named instead. `environment_digest` describes the OCI sandbox, and a reviewer stage does
    not run in one: `review_transport` launches the CLI on the host, in an empty directory.
    """
    diff_inputs = {
        "trusted_base_sha": trusted_base,
        "subject_head_sha": head,
        "change_digest": change,
        # The ceiling picks the rung of the context ladder, so it decides the exact bytes sent.
        "max_diff_bytes": ceiling,
    }
    return {
        "actual_extraction": review_cache.stage_key(
            "actual_extraction",
            {
                **diff_inputs,
                **_reviewer_identity(config, "actual_extractor"),
                "coverage_digest": coverage_digest,
                "risk_floor": risk_floor,
            },
        ),
        "security_review": review_cache.stage_key(
            "security_review",
            {
                **diff_inputs,
                **_reviewer_identity(config, "security_reviewer"),
                # A finding the previous review left blocking is in the request, and the validator
                # refuses an answer that drops one — so it changes what a valid answer is.
                "prior_blocking_ids": sorted(prior_blocking),
            },
        ),
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


def _cached_stage(
    cache: review_cache.StageCache,
    stage: str,
    key: str,
    ran: set[str],
    run: Callable[[review_policy.Reviewer], T],
    reviewers: review_policy.Reviewers,
    *,
    reused: usage_mod.Ledger,
) -> T:
    """`run` the stage, reusing the stored answer to this exact question when there is one.

    A hit is put back through `run`, so it is validated exactly as a fresh answer would be —
    anchors re-checked against the commit, never-lists applied. A miss records the raw answer, but
    only after `run` returned, so a malformed answer is never stored.

    A stored answer that no longer validates is dropped and the stage runs for real. That is
    recovery with a stated scope: the only way it happens is a rein release tightening a validator
    under an entry taken before it, and leaving the entry in place would wedge the review behind
    bytes that can never pass again. The re-run's own failure, if there is one, is what raises.

    **A replay puts the original launch's provenance back too**, into `reused` — which model
    answered, and what that reading cost when it was taken. Kept apart from the transport's ledger
    because they answer different questions: the ledger is this run's bill and must not be inflated
    by a launch it did not make, while `binding.independence` asks who produced each half of the
    review and gets the same answer whether the bytes came from a provider or from disk. Without
    this, reusing an extraction silently disarmed `review_policy.independence_observed` on a
    critical change.
    """
    role = review_policy.STAGE_ROLE[stage]
    stored = cache.read(stage, key)
    if stored is not None:
        try:
            result = run(review_cache.replay(stored.answer))
        except Exception as exc:
            logger.warning(f"the stored {stage} answer no longer validates ({exc}) — re-reading")
            cache.drop(stage, key)
        else:
            reused.add(role, stored.usage)
            return result
    recorder = review_cache.Recorder(reviewers.for_role(role))
    result = run(recorder)
    ran.add(stage)
    if recorder.reply is not None:
        cache.write(stage, key, recorder.reply.text, recorder.reply.usage)
    return result


def _execution_plan(
    config: models.Config | None,
    cache: review_cache.StageCache,
    keys: Mapping[str, str],
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
    point of writing the plan down is that it does not contain any.
    """
    stages: list[dict[str, Any]] = []
    for stage in _STAGE_ORDER:
        role = review_policy.STAGE_ROLE[stage]
        key = keys.get(stage, "")
        stages.append(
            {
                "stage": stage,
                "role": role,
                "key": key,
                "decision": ("reuse" if cache.has(stage, key) else "run") if key else "undecided",
                "adapter": config.adapter(role) if config is not None else "",
                "model": config.model(role) if config is not None else "",
            }
        )
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
    """The plan, in one line, before the launches it describes."""
    parts = []
    for row in plan.get("stages", []):
        where = row["model"] or row["adapter"] or "cli default"
        parts.append(f"{row['stage']}={row['decision']} ({where})")
    shared = plan.get("shared_reading")
    tail = "" if shared is None else f"; shared reading: {'yes' if shared else 'no'}"
    return "review plan: " + ", ".join(parts) + tail


def _record_failure(store: store_mod.Store, cycle: str, actor: str, *, stage: str, reason: str) -> None:
    """Append the failure events for a review that could not be produced. Never raises.

    Append-only: nothing was written, so there is no document to stage, and a transaction that
    appends without writing is exactly what `store.Transaction` permits (the refusal runs the other
    way — a write with no event).

    Swallowing the store's own errors is deliberate. This runs inside an `except` block whose job
    is to re-raise the real failure; a store problem here would replace the error a human needs to
    read with one about bookkeeping, and the log being unwritable is what `rein doctor` is for.
    """
    if not cycle:
        # There is no cycle to file it under, and an event that cannot name one is refused by
        # `event_chain.make`. Say where the account went instead of leaving the reader to notice
        # the log is silent — this is reachable only in the `inputs` stage, where the cycle is read
        # out of the very documents that failed.
        logger.warning(f"the review failed at stage '{stage}' and no cycle is established to record it under: {reason}")
        return
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
    reviewers: review_policy.Reviewers,
    *,
    plan: models.Plan | None,
    config: models.Config | None,
    head: str,
    trusted_base: str,
    reviewable: Reviewable,
    anchorable: Sequence[Mapping[str, Any]],
    coverage: Mapping[str, Any],
    risk_floor: str,
    effective: str,
    on_stage: Callable[[str], None] = lambda _name: None,
    cache: review_cache.StageCache,
    keys: Mapping[str, str],
    ran: set[str],
    reused: usage_mod.Ledger,
) -> tuple[actual_extraction.ExtractionResult, conformance.ComparatorResult]:
    """The one genuinely sequential pair: the comparator reads what the extractor produced.

    Kept together so the concurrency in `generate` reads as "one chain plus one independent
    stage" rather than as three calls someone happened to interleave.

    `on_stage` is called with each stage's name as it *begins*, so a caller can say which one
    failed without this function having to know about the store or catch anything. It is the only
    way to tell "the extractor failed" from "the comparator failed", and those are different
    facts: one means nothing was read out of the code, the other means the reading exists and
    could not be compared against the plan.

    Each half is reused on its own key, which is why the comparator's is minted here rather than
    passed in: it takes the Actual as an input, and the Actual is what the line above produces.
    Reusing the extraction therefore reuses the comparison too, since an identical Actual keys the
    same question — and moving `plan.yaml` alone re-runs only this second half.
    """
    # Blind actual extraction — the plan is deliberately absent from this request (§12.2).
    on_stage("actual_extraction")
    extract_request = actual_extraction.build_request(
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
        diff_text=reviewable.text,
        deterministic_facts={
            "coverage": coverage,
            "risk_floor": risk_floor,
            "context": reviewable.as_facts(),
            "files": [dict(entry) for entry in anchorable],
        },
    )
    extraction = _cached_stage(
        cache,
        "actual_extraction",
        keys["actual_extraction"],
        ran,
        lambda ask: actual_extraction.run_extractor(
            extract_request, ask, repo=repo, commit=head, risk_floor=risk_floor
        ),
        reviewers,
        reused=reused,
    )

    # Expected vs Actual — the Actual arrives read-only and digest-bound (§12.3).
    on_stage("comparison")
    compare_request = conformance.build_request(
        expected_model=_expected_model(plan),
        actual_statements=extraction.actual_statements,
        actual_digest=extraction.actual_digest,
    )
    known_ids = _known_ids(plan, extraction.actual_statements)
    independence = _independence(config)
    comparison = _cached_stage(
        cache,
        "comparison",
        _comparison_key(
            config=config,
            plan_digest=plan.digest() if plan is not None else digests.of({}),
            actual_digest=extraction.actual_digest,
            effective=effective,
            independence=independence,
        ),
        ran,
        lambda ask: conformance.run_comparator(
            compare_request,
            ask,
            repo=repo,
            commit=head,
            actual_statement_ids=[str(a.get("id")) for a in extraction.actual_statements],
            known_ids=known_ids,
            expected_claim_ids=[c.id for c in plan.claims] if plan is not None else [],
            effective_risk=effective,
            independence=independence,
        ),
        reviewers,
        reused=reused,
    )

    return extraction, comparison


def _known_ids(plan: models.Plan | None, actual_statements: Sequence[Mapping[str, Any]]) -> list[str]:
    ids = [str(a.get("id")) for a in actual_statements]
    if plan is not None:
        ids += [c.id for c in plan.claims]
    return ids


def _prior_blocking(review: models.Review | None, trusted_base: str) -> list[dict[str, Any]]:
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
    """
    if review is None or not trusted_base:
        return []
    recorded = str(review.raw.get("machine", {}).get("binding", {}).get("trusted_base_sha", ""))
    if not recorded or recorded != trusted_base:
        return []
    return [dict(f) for f in review.blocking_security_findings]


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
    if state is None or not state.cycle_id:
        raise ReviewError("cannot record the freeze — .rein/state.yaml names no cycle; run `rein doctor`")
    with store.transaction() as tx:
        tx.write("review", {**review.raw, "human": new_human}, expect_digest=seen)
        tx.append("human_review_frozen", cycle_id=state.cycle_id, actor=actor)


# -- CLI ----------------------------------------------------------------------


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
    return faults.classify_launch(failure.rc, failure.output) is faults.Fault.ENV_TRANSIENT


def _generate_cli(
    repo: repo_mod.Repo,
    *,
    force: bool,
    supervise: bool,
    interval_sec: int,
    make_reviewers: Callable[[], review_policy.Reviewers] | None = None,
) -> tuple[dict[str, Any], dict[str, usage_mod.Usage]]:
    """`(the machine review, what the attempts cost)`, waiting out a machine failure time can fix.

    The same shape as `rein build --supervise`: only what :func:`_worth_waiting_for` allows is
    retried. Everything else — a reviewer whose output could not be parsed, a budget refusal, an
    unreadable SSOT — is a real answer, and sleeping on it would turn a verdict into a loop.

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
            return generate(repo, reviewers, force=force), spend
        except review_policy.AdapterFailure as failure:
            if not supervise or not _worth_waiting_for(failure):
                raise
            hint = faults.reset_hint(failure.output)
            logger.info(
                f"[supervise] attempt {attempt}: {failure} — sleeping {interval_sec}s"
                + (f" (the CLI said: {hint})" if hint else "")
            )
            time.sleep(interval_sec)
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
            _, spend = _generate_cli(
                repo,
                force=args.force,
                supervise=args.supervise,
                interval_sec=args.supervise_interval_sec,
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
