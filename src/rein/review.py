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
import tempfile
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from rein import (
    actual_extraction,
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


def _file_facts(repo: repo_mod.Repo, head: str, files: Sequence[diff_facts.DiffFile]) -> dict[str, Any]:
    """The blob and the line count of each changed path at `head`, for anchoring.

    Every code anchor a reviewer produces is validated against the committed tree: the blob has to
    be the one at that path, and the line range has to be inside the file. Both facts used to be
    the *reviewer's* to find out, which it could only do by reading the repository it was launched
    in — the same access that let a blind extractor open `.rein/plan.yaml`. Handing the two facts
    over is what makes the launch able to answer without the repository at all.

    Deliberately identity and size, not content: the bodies are what `Reviewable` replaced, and
    putting them back under another name would undo the measurement that removed them.
    """
    facts: dict[str, Any] = {}
    for file in files:
        if file.binary:
            continue
        rc, blob = repo._git_rc("rev-parse", f"{head}:{file.path}")
        if rc != 0 or not blob.strip():  # deleted at head — there is nothing to anchor in
            continue
        rc, content = repo._git_rc("show", f"{head}:{file.path}")
        if rc != 0:
            continue
        facts[file.path] = {"blob": f"git-blob:{blob.strip()}", "lines": content.count("\n") + 1}
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

    The ceiling is `max_diff_bytes_per_partition` — not a second limit invented here, but the one
    byte budget a human already approves the review against (`_refuse_over_budget`). That is the
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
            decision_cards=cards,
            statements=statements,
            gaps=gaps,
        )
    return machine


# -- generation ---------------------------------------------------------------


def _refuse_over_budget(diff_bytes: int, limits: Mapping[str, int]) -> None:
    """Refuse a review whose diff is already past the one byte-denominated budget.

    `max_diff_bytes_per_partition` was measurable only *after* the pipeline ran: `human_review`
    reads it off the finished coverage manifest, at the freeze. So a change big enough that the
    three reviewer stages cannot be run against it at all never reached the budget's own
    instruction — the operator paid three model launches to be told "the adapter exited 1", and
    the sentence that would have said what to do about it lived behind the failure.

    It is the same wall either way. A diff over this limit cannot be frozen once generated, so
    nothing is refused here that would have been allowed later; what changes is that it is refused
    before the launches rather than after them, and with the budget's own name on it. Measured
    over the whole diff, exactly as `_largest_partition_bytes` measures it, so passing here and
    blowing it at the freeze is not a thing that can happen.
    """
    ceiling = limits["max_diff_bytes_per_partition"]
    if diff_bytes <= ceiling:
        return
    raise ReviewError(
        "review budget exceeded before the pipeline ran — split the scope, do not grow the "
        f"screen: max_diff_bytes_per_partition is {ceiling} and this change's diff is "
        f"{diff_bytes} bytes. Reduce what this cycle claims through `/revise` and review the "
        "remainder in its own gate ④ round, or raise the limit in `review_policy.budgets` as a "
        "deliberate, recorded decision about how much one person can hold at once."
    )


def generate(
    repo: repo_mod.Repo,
    reviewer: review_policy.Reviewer,
    *,
    executor: Any = None,
    base: str | None = None,
    actor: str = "",
    force: bool = False,
) -> dict[str, Any]:
    """Run the whole pipeline and write `review.yaml`'s machine half; return the assembled machine.

    The deterministic piece (the coverage manifest) runs unconditionally; the reviewer stages are called and
    their *validated* outputs merged. The human half is reset to `not_started` — a fresh machine
    review is a fresh review, and no prior human answer speaks for it (plan §6.6).

    **A subject that has not moved is not re-read.** Everything the pipeline is a function of is a
    digest computed before a model is called: the committed tree, the frozen plan, the config the
    approval covers, the sandbox, the coverage manifest. When all of them match the review already
    on disk, three reviewer stages would be paid for to produce a reading of the same bytes — and,
    worse, the human half would be reset, discarding answers about a change nothing had touched.
    A field run recorded `review_generated` fifteen times in one cycle for exactly this. `force`
    is the way to say "read it again anyway", which is a deliberate act with a visible cost.

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
        if not force and _same_subject(existing, subject):
            assert existing is not None  # _same_subject is False for None
            print(
                "review: the subject has not moved since the last generation — reusing it "
                "(nothing was re-read; `rein review generate --force` to run the pipeline anyway)"
            )
            return dict(existing.machine)

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
            ceiling=limits["max_diff_bytes_per_partition"],
        )

        # What a reviewer needs to anchor a statement, since it is launched with nothing to read
        # but its request (`_adapter_reviewer`).
        anchorable = _file_facts(repo, head, facts.files)
        # What the last review found blocking *about this same base*. Taken from the copy read at
        # the top rather than re-read here, and taken before the call below, which is about to move
        # off this thread — the store is not something to touch from two. It goes into the request
        # as well as into the validator: the reviewer is refused for dropping one of these, and was
        # being refused on knowledge nobody had given it.
        prior_blocking = _prior_blocking_ids(existing, trusted_base)

        security_request = security_review.build_request(
            diff_text=reviewable.text,
            deterministic_facts={
                "signals": [h.signal for h in facts.signals],
                "context": reviewable.as_facts(),
                "files": anchorable,
            },
            trusted_base_sha=trusted_base,
            subject_head_sha=head,
            prior_blocking_ids=prior_blocking,
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
        # it failed to cancel finishes. A field run recorded an extraction failure that takes
        # seconds *61 minutes* after the command was launched, having paid for a whole security
        # review nobody would read. `shutdown(wait=False, cancel_futures=True)` does not fix it
        # either: `concurrent.futures.thread` registers an atexit hook that joins every worker, so
        # the wait moves from here to interpreter exit and the process returns no sooner. The only
        # thing that ends a launch early is killing the process it started.
        cancel = common.Cancellation()

        def run_security() -> security_review.SecurityResult:
            # Bound on this thread — the worker's — because the transport is reached through the
            # injected reviewer, whose signature is not ours to change.
            with common.cancelling(cancel):
                return security_review.run_security_review(
                    security_request,
                    reviewer,
                    repo=repo,
                    commit=head,
                    # Without it the check below — a reviewer may not clear its own block by
                    # regenerating and omitting the finding — had nothing to compare against, so
                    # the protection its own docstring describes never once applied.
                    prior_blocking_ids=prior_blocking,
                )

        with ThreadPoolExecutor(max_workers=1) as pool:
            security_future = pool.submit(run_security)
            try:
                extraction, comparison = _extract_and_compare(
                    repo,
                    reviewer,
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
        binding = {**subject, "actual_digest": extraction.actual_digest, "generated_at": event_chain.now_iso()}
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
    reviewer: review_policy.Reviewer,
    *,
    plan: models.Plan | None,
    config: models.Config | None,
    head: str,
    trusted_base: str,
    reviewable: Reviewable,
    anchorable: Mapping[str, Any],
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
        diff_text=reviewable.text,
        deterministic_facts={
            "coverage": coverage,
            "risk_floor": risk_floor,
            "context": reviewable.as_facts(),
            "files": dict(anchorable),
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


def _same_subject(existing: models.Review | None, subject: Mapping[str, str]) -> bool:
    """Would regenerating produce a review of exactly what the one on disk already reviewed?

    Every key compared is deterministic and computed before any model runs, so this is an identity
    check rather than a guess: the committed tree under review, the Expected Model, the config the
    approval covers, the sandbox, and the manifest of what could be read. An LLM stage is not a
    function of anything else, which is why nothing else needs to be in here.

    A malformed or ungenerated review is not reusable, and neither is one whose binding is missing
    any of these — "it did not say" must never read as "it agreed".
    """
    if existing is None or not existing.is_generated:
        return False
    binding = existing.binding
    return all(binding.get(key) == value for key, value in subject.items())


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


#: Guards the spend ledger the two stage threads both add to (`_adapter_reviewer`).
_SPEND_LOCK = threading.Lock()

#: How much of a failed adapter's output travels with the error. Its closing words are where a
#: CLI says what stopped it, and an error message is read on a terminal, not scrolled.
_ADAPTER_OUTPUT_TAIL = 1000


def _adapter_reviewer(
    repo: repo_mod.Repo,
    role: str = "code_reviewer",
    *,
    config: models.Config | None = None,
    spend: dict[str, int] | None = None,
) -> review_policy.Reviewer:
    """A production reviewer that hands the request as JSON to the adapter configured for `role`.

    Kept small on purpose: the request goes to the adapter on stdin and the adapter answers with
    the single JSON document the stage validators parse. Every stage revalidates the output, so
    this is a transport, not a trust boundary.

    `role` selects the adapter, so each stage gets its own: one callable serving every stage
    means the same session answers as the Actual Extractor and as the Comparator, the
    independence violation §12.4 exists to prevent. The request goes on stdin — a whole diff as
    an argv element hits E2BIG on a large change.

    The launch takes `execution.agent_timeout_sec`, the same knob the build loop launches under,
    rather than the fifteen minutes that used to be written here. Fifteen minutes was not a
    judgement about reviewing; it was a number, and a review it killed at 900 seconds cost the
    whole launch and was re-run from cold. The knob defaults to no limit for the reason its own
    docstring gives, and Ctrl-C is what stops a launch that really is stuck.

    **It runs in an empty directory, not in the repository.** This transport passed no `cwd`, so
    every stage inherited rein's — the repository root. An agent CLI reads its working directory:
    the root is where `AGENTS.md` explains the Expected Model and `.rein/plan.yaml` *is* the
    Expected Model, both a `git show` away from the one stage whose whole value is that it has
    never seen them. `actual_extraction.assert_blind` guards the payload and could never have
    caught this, because the priming did not travel in the payload.

    Cutting the directory only works because the request now carries what the answer needs: the
    stage contract (`<stage>.contract`) instead of whatever instructions the CLI picked up from a
    project, and `deterministic_facts.files` instead of the `git rev-parse` a reviewer used to
    have to run to anchor anything. What the launch can still read is the user's own global CLI
    configuration, which is theirs and not this repository's to remove.
    """
    from rein import build_loop

    if config is None:
        config = store_mod.Store(repo).read_config()
    adapter = (config.adapter(role) if config is not None else "") or "claude"
    argv = build_loop.ADAPTERS.get(adapter)
    if argv is None:
        raise ReviewError(f"agents.{role}.adapter is {adapter!r}, which this release cannot launch")
    timeout = float(config.agent_timeout_sec) if config is not None else 0.0

    def call(request: Mapping[str, Any]) -> str:
        payload = json.dumps(request, ensure_ascii=False)
        if spend is not None:
            # The stages run on two threads, and read-modify-write is not one operation on any
            # interpreter that is not holding a global lock for us.
            with _SPEND_LOCK:
                spend[role] = spend.get(role, 0) + len(payload.encode("utf-8"))
        with tempfile.TemporaryDirectory(prefix="rein-review-") as elsewhere:
            rc, out = common.run(list(argv), cwd=elsewhere, timeout=timeout or None, input_text=payload)
        if rc != 0:
            # What the adapter said, not merely that it stopped. `common.run` merges stderr into
            # `out`, so the reason was in hand and thrown away: a field report of three identical
            # `exited 1` failures was diagnosable only by wrapping the CLI in a logging shim, and
            # the message behind them — "Prompt is too long" — named its own cause exactly.
            said = out.strip()[-_ADAPTER_OUTPUT_TAIL:]
            raise review_policy.AdapterFailure(
                f"the {role} adapter exited {rc}" + (f", saying:\n{said}" if said else " and said nothing"),
                rc=rc,
                output=out,
            )
        return out

    return call


def _staged_reviewer(repo: repo_mod.Repo, *, spend: dict[str, int] | None = None) -> review_policy.Reviewer:
    """One callable that routes each stage's request to that stage's own configured adapter.

    The stage is identified by the request shape the stage builders produce — the same shapes
    the fakes in the test suite key on — so nothing has to thread a role through the pipeline.

    `spend`, when given, collects the bytes actually put in front of each stage. Measured at the
    transport because that is the only place the number is a fact rather than an estimate, and
    worth measuring because what this pipeline sends is the thing that decides whether it can run
    at all.
    """
    config = store_mod.Store(repo).read_config()
    by_role = {
        role: _adapter_reviewer(repo, role, config=config, spend=spend)
        for role in ("actual_extractor", "comparator", "security_reviewer")
    }

    def call(request: Mapping[str, Any]) -> str:
        if "expected_model" in request:
            return by_role["comparator"](request)
        facts = request.get("deterministic_facts")
        if isinstance(facts, Mapping) and "signals" in facts:
            return by_role["security_reviewer"](request)
        return by_role["actual_extractor"](request)

    return call


def spend_summary(spend: Mapping[str, int]) -> str:
    """What this generation put in front of a model, worst first. Empty when nothing was sent.

    Bytes rather than tokens, for the reason `build_loop.spend_summary` gives: a token count
    belongs to a tokenizer nobody here owns, and reporting an estimate as a measurement is the
    habit this codebase is built against.
    """
    rows = sorted(spend.items(), key=lambda item: -item[1])
    if not rows:
        return ""
    parts = ", ".join(f"{role} {sent / 1024:.0f}KiB" for role, sent in rows)
    return f"review: sent {sum(spend.values()) / 1024:.0f}KiB over {len(rows)} stage(s) — {parts}"


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
    spend: dict[str, int],
    *,
    force: bool,
    supervise: bool,
    interval_sec: int,
    make_reviewer: Callable[[], review_policy.Reviewer] | None = None,
) -> dict[str, Any]:
    """Run the pipeline, optionally waiting out a machine failure that time alone fixes.

    The same shape as `rein build --supervise`: only what :func:`_worth_waiting_for` allows is
    retried. Everything else — a reviewer whose output could not be parsed, a budget refusal, an
    unreadable SSOT — is a real answer, and sleeping on it would turn a verdict into a loop.

    A fresh reviewer per attempt, so a retry re-reads the config rather than holding whatever was
    true when the first one was built.
    """
    build_reviewer = make_reviewer or (lambda: _staged_reviewer(repo, spend=spend))
    attempt = 0
    while True:
        attempt += 1
        try:
            return generate(repo, build_reviewer(), force=force)
        except review_policy.AdapterFailure as failure:
            if not supervise or not _worth_waiting_for(failure):
                raise
            hint = faults.reset_hint(failure.output)
            logger.info(
                f"[supervise] attempt {attempt}: {failure} — sleeping {interval_sec}s"
                + (f" (the CLI said: {hint})" if hint else "")
            )
            time.sleep(interval_sec)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein review", description="the grounded machine review (gate ④)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate", help="run the review pipeline and write review.yaml")
    gen.add_argument(
        "--force",
        action="store_true",
        help="re-run the pipeline even when the subject has not moved (discards the human answers)",
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
            spend: dict[str, int] = {}
            _generate_cli(
                repo,
                spend,
                force=args.force,
                supervise=args.supervise,
                interval_sec=args.supervise_interval_sec,
            )
            if measured := spend_summary(spend):
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
        review_policy.ReviewPolicyError,
        store_mod.StoreError,
        models.DocumentError,
    ) as exc:
        logger.error(str(exc))
        return 1
    return 0
