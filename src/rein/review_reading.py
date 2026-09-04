"""What a reviewer is allowed to read, and the reading of one unit of the change.

Split out of ``review.py`` because two callers need it and only one of them assembles a review.
``review.generate`` composes a gate-④ document out of readings; ``build_loop`` takes a reading the
moment a task lands, while its diff is small and the tree that produced it is the one in front of
it. Keeping the reading here is what lets the second caller exist without importing the first.

What lives here is everything that decides **which bytes a reviewer sees**: the exclusion of what
is not the product, the diff and its widening ladder, the folding of mechanical bodies, the split
that keeps tests away from the blind extractor, and the byte budget that refuses a change too big
to read. Plus :class:`Reading` — one unit of the change, run through the two stages that read code.

The invariants the pipeline rests on are enforced where they were before, one layer down:
``actual_extraction.assert_blind`` walks every request built here, and ``security_review`` refuses
an answer that drops a carried-forward blocking finding. A reading is a smaller subject, never a
weaker one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, TypeVar

from rein import (
    actual_extraction,
    adapters,
    common,
    diff_facts,
    digests,
    models,
    review_cache,
    review_policy,
    security_review,
)
from rein import install as install_mod
from rein import lock as lock_mod
from rein import repo as repo_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: One stage's validated result — whatever `cached_stage` was handed a runner for.
T = TypeVar("T")

#: The unit name of a reading that covers the whole change. The only shape gate ④ had before a
#: review could be composed, and still the shape it takes when nothing asks for another. Read from
#: `models` so the vocabulary and the schema that enforces it cannot drift.
WHOLE = models.COMPOSITION_WHOLE

#: The plan's own prose that lives outside `plan.sources`: the task tickets and the ADRs. Named as
#: directories as well as read from the freeze because a ticket written or amended after gate ③ is
#: still the answer sheet, and `plan.sources` only knows the ones the freeze hashed.
_PLAN_PROSE_DIRS: tuple[str, ...] = ("docs/tasks/", "docs/decisions/")


def not_the_product(repo: repo_mod.Repo, state: models.State | None) -> tuple[str, ...]:
    """Every path in this repository that is not the thing under review.

    `.rein/` was the whole list, and it was the whole list because it was written down rather than
    derived. What is actually *not the product* is larger and this repository already knows it
    exactly:

    - **`.rein/`**, which is bound by its own digests — a review that wrote `review.yaml` would
      otherwise invalidate itself (plan §17.3).
    - **The Expected Model's prose.** `state.plan.sources` names the documents gate ③ froze as
      "the prose the build reads": the requirements, the design, the ADRs, every task ticket. The
      blind extractor was being handed all of it. `actual_extraction.assert_blind` guards against
      the plan being *given* to it and cannot notice the plan arriving inside the diff, so on any
      cycle that touched a requirement or a ticket the Expected/Actual independence gate ④ rests on
      was not established — and "extra behaviours: 0" means very little from a reader that has read
      the tickets. Measured on one cycle: `docs/` was 658,850 of 2,141,194 bytes, 31%, of which
      422 KB was the Expected side verbatim.
    - **The surfaces `rein install <agent>` wrote.** The phase command bodies describe what each
      gate expects; the role prompts describe how each stage is meant to answer. They are this
      tool's own orchestration text, handed to the reviewer as if somebody had written it as code.
      The exact paths are in `rein.lock`, because `install` recorded every one of them.

    The rest of `docs/` stays in: a README and an operator guide are deliverables, and a blanket
    `docs` exclusion would hide user-facing documentation from the only reader it gets.

    A directory keeps its trailing slash and a file does not — `digests.filter_tree` and
    `repo.pathspec_excluding` both read the difference.
    """
    paths: set[str] = {repo_mod.SSOT_DIR, *_PLAN_PROSE_DIRS}
    if state is not None:
        paths |= set(state.frozen_sources)
    lock_data = lock_mod.read(repo.lock) or {}
    integrations = lock_data.get("integrations")
    if isinstance(integrations, dict):
        for name, record in integrations.items():
            if not isinstance(record, dict):
                continue
            paths |= {str(path) for path in (record.get("files") or {})}
            # The settings file is recorded apart from `files` because install *merges* into it
            # rather than owning it. It is still a surface this tool wrote, so it is still not the
            # product. Which file that is comes off the host's own record: this named
            # `.claude/settings.json` for every integration, which was right only while claude was
            # the only host that had one.
            spec = install_mod.INTEGRATIONS.get(str(name))
            if isinstance(record.get("settings"), dict) and spec is not None and spec.settings:
                paths.add(spec.settings)
    return tuple(sorted(paths))


class ReviewError(common.ReinError):
    """A review could not be generated or completed — carries a human-readable reason."""


# -- deterministic digests over the committed tree ----------------------------


def change_digest(repo: repo_mod.Repo, commit: str, exclude: Sequence[str], *, include: Sequence[str] = ()) -> str:
    """The digest of the code under review at `commit`: the committed tree minus `exclude`.

    `exclude` is `not_the_product`'s answer, passed in rather than read here so that the digest and
    the diff below cannot be given two different ones. `include` narrows it to one reading's own
    paths, the same narrowing `diff_of` takes, so a reading's key identifies that reading's content
    and nothing else — which is what lets a reading taken while one task landed survive every task
    that lands after it.
    """
    rc, out = repo._git_rc("ls-tree", "-r", "-z", commit)
    if rc != 0:
        raise ReviewError(f"cannot read the tree at {commit}: {out.strip()}")
    entries = digests.filter_tree(digests.parse_ls_tree(out), exclude_prefixes=exclude, include_prefixes=include)
    return digests.tree_digest(entries)


def commit_exists(repo: repo_mod.Repo, ref: str) -> bool:
    return bool(ref) and repo._git_rc("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")[0] == 0


def resolve_base(repo: repo_mod.Repo, plan: models.Plan | None, base: str | None) -> str:
    """The trusted base a review is taken against: an explicit arg, the plan's base, else a fallback.

    Each candidate is verified to exist in *this* repository before it is used — a plan carrying a
    base commit that is not present here (a fork, a shallow clone) falls back rather than failing the
    whole review on a `git diff` against a missing object.

    There is deliberately no last-resort fallback to HEAD. That used to be the final branch, and
    it is the one answer that is never right: `git diff HEAD..HEAD` is empty, so every reviewer
    would be handed a change of nothing and would report, honestly and uselessly, that they found
    nothing wrong with it. A base that cannot be resolved is a review that cannot be taken.
    """
    if commit_exists(repo, base or ""):
        return base or ""
    if plan is not None and commit_exists(repo, plan.base_commit):
        return plan.base_commit
    for candidate in ("main", "master"):
        if commit_exists(repo, candidate):
            return repo._git_rc("rev-parse", candidate)[1].strip()
    raise ReviewError(
        "cannot resolve a base commit to review against: the plan's `cycle.base_commit` is not in "
        "this repository and neither `main` nor `master` exists here. Set `cycle.base_commit` to a "
        "commit this checkout has — reviewing HEAD against itself would report an empty change."
    )


def diff_of(
    repo: repo_mod.Repo,
    base: str,
    head: str,
    exclude: Sequence[str],
    *,
    context: int | None = None,
    include: Sequence[str] = (),
) -> str:
    """The change under review, which is the *product* — `not_the_product` is not part of it.

    `include` narrows the diff to one reading's own paths (:class:`Reading`); empty is the whole
    change. It narrows *within* the exclusion and never past it: a reading cannot be pointed at
    something `not_the_product` has already removed.

    The same exclusion `change_digest` above takes, through the same argument, because the two
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
    rc, out = repo._git_rc("diff", *width, f"{base}..{head}", "--", *repo_mod.pathspec_for(include, exclude))
    if rc != 0:
        raise ReviewError(f"cannot diff {base}..{head}: {out.strip()}")
    return out


#: What a withheld body is replaced by, per reason. The wording is the whole content of the
#: replacement, so it says which fact the reader is being handed and nothing beyond it.
_WITHHELD: Mapping[str, str] = {
    "mechanical": "@@ {n} line(s) of mechanical change, body withheld @@\n",
    "deleted": "@@ {n} line(s) removed with the file, body withheld @@\n",
}


def fold_bodies(
    diff_text: str, files: Sequence[diff_facts.DiffFile], *, signalled: Collection[str] = ()
) -> tuple[str, list[str]]:
    """The diff with lockfile, generated-file and deleted-file *bodies* replaced by one line each.

    A lockfile's eight hundred changed lines say one thing — the dependencies moved — and they say
    it by burying the twelve lines of hand-written code in the same diff. Every reviewer here was
    handed the raw whole, twice over (the extractor and the security reviewer each get their own
    copy), and the meaningful change was somewhere in the middle of it.

    A whole-file deletion is the same shape with a different reason, and one measured cycle spent
    294 KB on it — a predecessor tool's deleted scaffolding, 26.6% of what two opus stages read.
    What makes withholding it *allowed* is not that a deletion feels uninteresting: it is that the
    pipeline already refuses to act on it. `_file_facts` omits a path with no blob at head ("there
    is nothing to anchor in"), `review_policy.validate_anchor` rejects any anchor to one as
    fabricated or stale, and the extractor's contract refuses a statement with no anchor and fails
    the whole extraction if any part of it fails. So the blind extractor could never have said one
    word about a deleted body, however many bytes of it were sent.

    **The security reviewer is the exception, and it is why `signalled` exists.** Its findings may
    stand without an anchor (`security_review._validate_finding`), so it *can* report "the deleted
    module held the only permission check and nothing replaces it" — and folding the body blind
    would have taken exactly the quietly-removed-safety case the `deleted_guard` signal was added
    to catch. `signalled` is the set of paths the deterministic detector matched a signal inside,
    and a deletion in it is sent whole. That is the rule `review_policy.coverage_gap_risk` already
    applies to unread content — the gap is worth what the line-by-line scan found in it — used
    here to decide what may go unread in the first place. A lockfile is folded either way: it is
    mechanical by classification, and `dependency` fires on every one of them.

    This is redaction, not summarisation and not priming: nothing is described, interpreted, or
    added. What replaces the hunks is the fact that they were there and how many lines they were,
    which is exactly what `diff_facts` already tells the Coverage Manifest. The honesty property
    holds because the manifest and this function agree about both kinds — a mechanical file is
    reported *not semantically analysed*, and a deleted one is not a file the manifest speaks
    about at all (`diff_facts.build_coverage`). A body the reviewers are not sent is never one the
    manifest calls analyzed; only the token cost of printing the bytes is gone.

    Returns `(text, folded_paths)`.
    """
    withheld: dict[str, tuple[str, int | None]] = {}
    for f in files:
        if diff_facts.classify_path(f.path) in diff_facts.MECHANICAL_KINDS:
            withheld[f.path] = ("mechanical", None)
        # `f.removed_lines` and not `f.deleted` alone: a deleted *binary* has no body to withhold,
        # and folding it would trade nothing for the one line that says it was binary.
        elif f.deleted and f.removed_lines and f.path not in signalled:
            # The exact removal count, not the lines between two headers: a deletion's block also
            # carries `deleted file mode`, `index`, `---` and `+++`, and a replacement that says
            # "N line(s) removed" while counting four lines of git metadata is a stated number
            # that is wrong. The mechanical wording claims no such count.
            withheld[f.path] = ("deleted", len(f.removed_lines))
    if not withheld:
        return diff_text, []
    kept: list[str] = []
    folded: list[str] = []
    current: str | None = None
    hunk_lines = 0

    def replacement(path: str, counted: int) -> str:
        reason, exact = withheld[path]
        return _WITHHELD[reason].format(n=counted if exact is None else exact)

    for line in diff_text.splitlines(keepends=True):
        path = diff_facts.header_path(line.rstrip("\n"))
        if path is not None:
            if current is not None:
                kept.append(replacement(current, hunk_lines))
            current = path if path in withheld else None
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
        kept.append(replacement(current, hunk_lines))
    return "".join(kept), folded


def split_tests(diff_text: str, files: Sequence[diff_facts.DiffFile]) -> tuple[str, str]:
    """`(the source half, the test half)` of one diff, split by file.

    The two reading stages want different things and were forced to take the same bytes.

    **The blind extractor should not see the tests.** Its job is what the code *does*, and a test
    does not ship — it asserts intent. A name like `test_render_grid_matches_expected_bins` is a
    paraphrase of the requirement, handed to the one stage whose entire value is that it has never
    read the requirements. That is the same contamination `not_the_product` removes, weaker in
    degree and identical in kind, and whether tests were written at all is answerable from the
    Coverage Manifest's file list without reading a single assertion. Measured on one cycle:
    `backend/tests/` was 544,421 bytes, 25% of the payload.

    **The security reviewer should.** Tests are code an agent wrote and they run with the
    operator's credentials, which this repository's own config comments say in as many words.

    So the split is not a quality/cost trade in either direction: the extraction gets *more* blind
    and 25% cheaper at once, and the security review loses nothing. `SharedReading` primes on the
    source half — the part both stages read — and the test half rides inline on the security
    reviewer's own branch, which is what keeps one reading serving two stages.
    """
    tests = {f.path for f in files if diff_facts.classify_path(f.path) == "test"}
    if not tests:
        return diff_text, ""
    halves: dict[bool, list[str]] = {True: [], False: []}
    for path, section in _sections(diff_text):
        halves[path in tests].append(section)
    return "".join(halves[False]), "".join(halves[True])


def _sections(diff_text: str) -> list[tuple[str, str]]:
    """`(path, that file's slice of the diff)` for each `diff --git` section, in order.

    One walk, because two answers are taken from it — which half of the reading a file belongs to
    (`split_tests`) and how many bytes of the payload it is (`bytes_by_kind`) — and a second copy
    of "find the header, attribute the following lines to it" is how the two would come to disagree
    about what a file's bytes are. Anything before the first header (there is normally nothing) is
    attributed to `""`, so no byte is silently dropped from a total that claims to be the payload.
    """
    sections: list[tuple[str, list[str]]] = [("", [])]
    for line in diff_text.splitlines(keepends=True):
        named = diff_facts.header_path(line.rstrip("\n"))
        if named is not None:
            sections.append((named, []))
        sections[-1][1].append(line)
    return [(path, "".join(lines)) for path, lines in sections if lines]


def bytes_by_kind(diff_text: str) -> dict[str, int]:
    """How many bytes of the payload each `diff_facts` kind is, largest first.

    The question a reader has once they know the payload is too big is *what it is made of*, and
    answering it took a hand-run script both times it mattered in the field. The diagnosis that
    `docs/` was 31% of a 2.14 MB change, and the one that tests were another 25%, are the two
    changes above this line — and neither was visible from anything the tool printed.

    Deliberately `classify_path`'s vocabulary rather than a new one: those five kinds are what the
    levers are denominated in. `test` is the half `split_tests` sends only to the security
    reviewer; `dependency` and `generated` are `MECHANICAL_KINDS`, whose content nobody needs to
    read; `source` is the thing actually under review. A breakdown in categories the tool cannot
    act on would be trivia. The plan's own prose does not appear at all because
    `not_the_product` has already removed it — which is the point.
    """
    totals: dict[str, int] = {}
    for path, section in _sections(diff_text):
        kind = diff_facts.classify_path(path) if path else "source"
        totals[kind] = totals.get(kind, 0) + len(section.encode("utf-8", errors="replace"))
    return dict(sorted(totals.items(), key=lambda item: -item[1]))


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
    #: The two halves of `text`, split by `split_tests`: what every reading stage gets, and what
    #: only the security reviewer does. `text` stays whole because it is what the ceiling and the
    #: coverage context are measured over — the split decides who is sent which part, not what the
    #: change is.
    source: str = ""
    tests: str = ""

    def as_facts(self) -> dict[str, Any]:
        facts: dict[str, Any] = {"unit": "diff", "context_lines": self.context_lines}
        if self.context_lines < CONTEXT_LADDER[0]:
            facts["narrowed_from"] = CONTEXT_LADDER[0]
        if self.folded:
            facts["bodies_withheld"] = list(self.folded)
        return facts


def file_facts(repo: repo_mod.Repo, head: str, files: Sequence[diff_facts.DiffFile]) -> list[dict[str, Any]]:
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


def reviewable_of(
    repo: repo_mod.Repo,
    base: str,
    head: str,
    files: Sequence[diff_facts.DiffFile],
    exclude: Sequence[str],
    *,
    plain: str,
    ceiling: int,
    signalled: Collection[str],
    include: Sequence[str] = (),
) -> Reviewable:
    """The widest context that fits `ceiling`, falling back to the plain diff already in hand.

    The ceiling is `max_diff_bytes` — not a second limit invented here, but the one byte budget a
    human already approves the review against (`_refuse_over_budget`). That is the
    property worth having: **what a reviewer is sent cannot exceed what was approved**, where
    before it was the approved diff *plus* an unbounded-by-anyone 240 KB of file bodies.

    Ordered widest-first so the common case costs one `git diff`; a change large enough to need the
    ladder pays a few more, which is cheap next to the model launch it is sizing.
    """

    def made(text: str, folded: Sequence[str], lines: int) -> Reviewable:
        source, tests = split_tests(text, files)
        return Reviewable(text=text, context_lines=lines, folded=tuple(folded), source=source, tests=tests)

    for lines in CONTEXT_LADDER:
        widened = diff_of(repo, base, head, exclude, context=lines, include=include)
        text, folded = fold_bodies(widened, files, signalled=signalled)
        if len(text.encode("utf-8")) <= ceiling:
            return made(text, folded, lines)
    # Over the ceiling even at git's default width. Refusing here would be a second budget nobody
    # approved: `_refuse_over_budget` has already passed on this diff, and the answer to a change
    # too big to review is `/revise`, not a narrower window onto it.
    text, folded = fold_bodies(plain, files, signalled=signalled)
    return made(text, folded, PLAIN_CONTEXT)


def refuse_over_budget(diff_bytes: int, limits: Mapping[str, int], *, unit: str = "") -> None:
    """Refuse a reading whose diff is already past the one byte-denominated budget.

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

    `unit` names the reading when a review is composed out of several, so a refusal says which one
    is too big rather than only that something was. A whole-change reading names nothing, which is
    the sentence this has always printed.
    """
    ceiling = limits["max_diff_bytes"]
    if diff_bytes <= ceiling:
        return
    subject = f"the reading of {unit}" if unit and unit != WHOLE else "this change"
    raise ReviewError(
        "review budget exceeded before the pipeline ran — split the scope, do not grow the "
        f"screen: max_diff_bytes is {ceiling} and {subject}'s diff is "
        f"{diff_bytes} bytes. Reduce what this cycle claims through `/revise` and review the "
        "remainder in its own gate ④ round, or raise the limit in `review_policy.budgets` as a "
        "deliberate, recorded decision about how much one person can hold at once."
    )


# -- one unit of reading ------------------------------------------------------


@dataclass(frozen=True)
class Reading:
    """One unit of the change a reviewer is asked to read.

    `unit` is what the record calls it — :data:`WHOLE`, a task id, or the seam a composition
    leaves. `include` is the slice of the product it covers, as git pathspecs; empty means the
    whole change, and that is the reading gate ④ has always taken.

    The unit is not decoration. When a review is composed out of several readings, every Actual
    Statement carries the unit it was read out of, so "extra behaviours: 0" can never be read as a
    statement about a tree nobody read whole.
    """

    unit: str = WHOLE
    include: tuple[str, ...] = ()

    @property
    def whole(self) -> bool:
        return not self.include


#: The reading that covers everything, which is what a review with no composition takes.
WHOLE_READING = Reading()


@dataclass(frozen=True)
class ReadingFacts:
    """One reading, measured and widened, before any model has seen it.

    Everything here is deterministic and cheap: the diff, the detector's analysis of it, the
    widened/folded bytes a reviewer is actually sent, and the blobs it may anchor into. Separated
    from the launches so the expensive half can be skipped, cached, or run somewhere else entirely.
    """

    reading: Reading
    diff_text: str
    facts: diff_facts.DiffFacts
    reviewable: Reviewable
    anchorable: tuple[dict[str, Any], ...]
    coverage: dict[str, Any]
    #: The committed content this reading covers, narrowed to its own paths. What its cache key is
    #: a function of, so a reading survives every commit that cannot have changed it.
    content_digest: str

    # There is deliberately no `risk_floor` here. A reading has one — `facts.risk_floor` is right
    # there — and it is never the number to use: the floor is a property of the whole change, so a
    # slice holding no signal must not be where it drops (`whole_change_risk_floor`). Offering it
    # on this class is how the only caller came to pass it.

    @property
    def analyzed_bytes(self) -> int:
        return self.facts.coverage.analyzed_bytes


def read_facts(
    repo: repo_mod.Repo,
    *,
    reading: Reading = WHOLE_READING,
    base: str,
    head: str,
    exclude: Sequence[str],
    limits: Mapping[str, int],
) -> ReadingFacts:
    """Measure one reading, refuse it if it is over budget, then widen what is left.

    The order is the point. The manifest reads the *whole* diff of this reading — folding a file
    before counting it would be measuring the fold — the budget is checked against that measure,
    and only then is anything widened. A reading nobody can read is refused before it costs a
    `git diff` per rung of the ladder, let alone a launch.
    """
    diff_text = diff_of(repo, base, head, exclude, include=reading.include)
    facts = diff_facts.analyze(diff_text)
    refuse_over_budget(facts.coverage.analyzed_bytes, limits, unit=reading.unit)
    reviewable = reviewable_of(
        repo,
        base,
        head,
        facts.files,
        exclude,
        plain=diff_text,
        ceiling=limits["max_diff_bytes"],
        # A deletion the detector matched a signal inside is sent whole (`fold_bodies`).
        signalled=frozenset(hit.path for hit in facts.signals),
        include=reading.include,
    )
    return ReadingFacts(
        reading=reading,
        diff_text=diff_text,
        facts=facts,
        reviewable=reviewable,
        anchorable=tuple(file_facts(repo, head, facts.files)),
        coverage=facts.coverage.to_manifest(),
        content_digest=change_digest(repo, head, exclude, include=reading.include),
    )


def extraction_request(
    measured: ReadingFacts,
    *,
    trusted_base: str,
    head: str,
    risk_floor: str,
) -> dict[str, Any]:
    """The blind extractor's input for one reading — the plan is deliberately absent (§12.2).

    `risk_floor` is passed rather than taken off `measured` because it is a property of the
    *review*, not of one slice of it: a statement may not claim a risk below what the detector
    found anywhere in the change, and a reading that happens to contain no signal must not be the
    place that lowers it.
    """
    return actual_extraction.build_request(
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
        diff_text=measured.reviewable.source,
        deterministic_facts={
            "coverage": measured.coverage,
            "risk_floor": risk_floor,
            "context": measured.reviewable.as_facts(),
            "files": [dict(entry) for entry in measured.anchorable],
        },
    )


def security_discipline(config: models.Config | None) -> str:
    """The host's own security review, when the configured reviewer has one (`adapters`).

    Read from the role's adapter rather than passed down from a caller, so the offer and the launch
    cannot disagree. It is not in the stage key: `reviewer_identity` already covers the adapter this
    is derived from, so pointing the role at a different CLI re-reads, and pointing it at the same
    one cannot change the answer to this.
    """
    try:
        return adapters.adapter_for_role(config, "security_reviewer").disciplines.get(adapters.SECURITY, "")
    except adapters.LaunchRefused:
        # A role this release cannot launch is refused where it is launched, with a message that
        # says what to repair. Refusing here as well would report it from the wrong place.
        return ""


def security_request(
    measured: ReadingFacts,
    *,
    trusted_base: str,
    head: str,
    prior_blocking: Sequence[Mapping[str, Any]] = (),
    discipline: str = "",
) -> dict[str, Any]:
    """The security reviewer's input for one reading — the only stage sent the test half."""
    return security_review.build_request(
        discipline=discipline,
        diff_text=measured.reviewable.source,
        tests_diff=measured.reviewable.tests,
        deterministic_facts={
            "signals": [h.signal for h in measured.facts.signals],
            "context": measured.reviewable.as_facts(),
            "files": [dict(entry) for entry in measured.anchorable],
        },
        trusted_base_sha=trusted_base,
        subject_head_sha=head,
        prior_blocking=prior_blocking,
    )


def cached_stage(
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


def reviewer_identity(config: models.Config | None, role: str) -> dict[str, str]:
    """Which model answers for `role`. Part of a stage key: a different model is a different answer."""
    if config is None:
        return {"adapter": "", "independence_group": ""}
    return {"adapter": config.adapter(role), "independence_group": config.independence_group(role)}


def reading_keys(
    *,
    config: models.Config | None,
    change: str,
    coverage_digest: str,
    trusted_base: str,
    ceiling: int,
    risk_floor: str,
    prior_blocking: Sequence[str],
    unit: str = WHOLE,
) -> dict[str, str]:
    """What each reviewer stage is a function of, for one reading, one key per stage (`review_cache`).

    Written out rather than folded into one `subject` digest, because that is the whole repair:
    the extractor and the security reviewer are not functions of `plan.yaml`, and none of the three
    is a function of `state.yaml`'s task statuses — the orientation brief is, and no model produces
    it. `comparison` is missing from here because it takes the Actual as an input and the Actual
    does not exist yet; `_comparison_key` mints it once the extractor has answered.

    Three digests that *are* in the binding are deliberately not in any key. `config_digest` covers
    the whole frozen config — the quality gate, the guard paths, limits no reviewer reads — so
    keying on it would re-run three models over a changed test command; the parts a stage really
    depends on (its adapter, its group, the byte ceiling that decides how wide a diff it is sent)
    are named instead. `environment_digest` describes the OCI sandbox, and a reviewer stage does
    not run in one: `review_transport` launches the CLI on the host, in an empty directory.

    And `subject_head_sha`, by the same reasoning one step further: a stage is not a function of
    HEAD's name. `change_digest` is taken over the committed tree with `not_the_product` already
    applied, so it identifies the reviewed content exactly, and the diff, the file facts and the
    anchorable blobs all derive from it and `trusted_base_sha`. Keying on the sha meant a commit
    that provably could not change the payload — anything under `.rein/`, `docs/tasks/`,
    `docs/decisions/`, a `.gitignore` edit — threw away a half-megabyte extraction anyway. Not
    "one changed line invalidates the lot" but zero changed lines invalidating it, which is what
    a `--supervise` retry after a session limit walks into. The sha stays in `subject`, where the
    review binds itself to the commit it was made about.

    **`unit` and `change` are what make a composed review affordable.** Every argument here is
    about *this reading* — `change` is the tree digest narrowed to the reading's own paths and
    `risk_floor` and `coverage_digest` are measured over its own diff — so a reading taken when one
    task landed is still the answer to the same question after ten more have. Keying a per-task
    reading on anything measured over the whole cycle would invalidate every earlier reading each
    time a later task moved, which is the whole-cycle re-read this composition exists to avoid.
    The unit is in the key as well as in its inputs because two readings can cover the same paths
    — a task and the seam over it — and they are not interchangeable answers.
    """
    diff_inputs = {
        "unit": unit,
        "trusted_base_sha": trusted_base,
        "change_digest": change,
        # The ceiling picks the rung of the context ladder, so it decides the exact bytes sent.
        "max_diff_bytes": ceiling,
    }
    return {
        "actual_extraction": review_cache.stage_key(
            "actual_extraction",
            {
                **diff_inputs,
                **reviewer_identity(config, "actual_extractor"),
                "coverage_digest": coverage_digest,
                "risk_floor": risk_floor,
            },
        ),
        "security_review": review_cache.stage_key(
            "security_review",
            {
                **diff_inputs,
                **reviewer_identity(config, "security_reviewer"),
                # A finding the previous review left blocking is in the request, and the validator
                # refuses an answer that drops one — so it changes what a valid answer is.
                "prior_blocking_ids": sorted(prior_blocking),
            },
        ),
    }


# -- composition: how the coverage was actually taken --------------------------

#: A review assembled out of more than one reading. The other value is :data:`WHOLE`.
COMPOSED = models.COMPOSITION_COMPOSED


def compose_coverage(
    whole: Mapping[str, Any],
    readings: Sequence[ReadingFacts],
    *,
    changed_paths: Sequence[str],
) -> dict[str, Any]:
    """The whole change's manifest, plus a record of how it was actually read.

    The manifest stays a statement about the **whole change** — that is the subject a human
    approves the coverage of, and measuring it over the readings instead would make the measure a
    function of how the reading happened to be split. `composition` says the rest: which readings
    it was assembled from, and which changed paths no reading covered.

    **A path in the change that no reading read is unread**, so it makes the manifest
    `insufficient` exactly as an unparseable file does. That is the whole answer to the objection
    composition has to answer: splitting a diff hides the seam, and hiding the seam is what lets
    "extra behaviours: 0" be said about bytes nobody looked at (§13.4). Here the seam is measured,
    named by path, and priced by `review_policy.coverage_blocks` like any other gap.

    A single whole-change reading records `mode: whole` and covers everything, which is the shape
    every review had before composition existed. It is still written down rather than left implicit:
    a reader must never have to infer whether the review in front of them was composed.
    """
    covered = {f.path for measured in readings for f in measured.facts.files}
    unread = sorted(p for p in dict.fromkeys(changed_paths) if p and p not in covered)
    units = [measured.reading.unit for measured in readings]
    composition: dict[str, Any] = {
        "mode": WHOLE if units == [WHOLE] else COMPOSED,
        "readings": [
            {
                "unit": measured.reading.unit,
                "diff_digest": str(measured.coverage.get("diff_digest", "")),
                "analyzed_bytes": measured.analyzed_bytes,
            }
            for measured in readings
        ],
    }
    if unread:
        composition["unread_paths"] = unread
    manifest = dict(whole)
    manifest["composition"] = composition
    if unread:
        manifest["coverage_status"] = "insufficient"
    return manifest


# -- which readings a review is taken in --------------------------------------

#: What the seam reading is called: the part of the change composition cannot attribute to one
#: task — a path two task scopes both cover, and a path no scope covers at all (an integration
#: fix, a conflict resolution, a review fix landed on the work branch).
SEAM = "seam"


def plan_readings(
    plan: models.Plan | None,
    changed_paths: Sequence[str],
    *,
    mode: str = "auto",
) -> list[Reading]:
    """The readings gate ④ takes, derived from the frozen plan's task scopes.

    `[WHOLE_READING]` — one reading of everything — whenever composition has nothing to compose
    along: the operator asked for `whole`, there is no plan, or no task declares a scope. An
    undeclared scope means *unbounded* (`models.PlanTask.scope_include`), and a reading that covers
    everything is not a slice of anything.

    Otherwise one reading per scoped task, in plan order, plus :data:`SEAM`.

    **A task's reading is its declared scope, never the paths that happened to change.** The scope
    was frozen at gate ③ and does not move, so the reading taken while that task landed is still
    the answer to the same question after every later task has landed — which is the whole reason a
    composed review costs less to regenerate than a whole one.

    **The seam is not an afterthought, it is what makes composing honest.** A changed path two
    scopes both cover was read twice in isolation and never as one file; a changed path no scope
    covers was never anybody's to read. Both are the seam, listed by path because that is the only
    way to say "these files and no others" as a pathspec. Whatever the seam still does not reach is
    named in `coverage.composition.unread_paths` and prices the manifest `insufficient`.

    A task's `scope.exclude` is deliberately not subtracted. It bounds what the *implementer* was
    allowed to change; a reading is about what is in the tree, and a path excluded from one task's
    scope that changed anyway is exactly what the seam exists to catch.
    """
    if mode == WHOLE or plan is None:
        return [WHOLE_READING]
    scoped = [task for task in plan.tasks if task.scope_include]
    if not scoped:
        return [WHOLE_READING]
    seam = [
        path
        for path in dict.fromkeys(p for p in changed_paths if p)
        if len([task for task in scoped if common.longest_cover(path, task.scope_include)]) != 1
    ]
    readings = [Reading(unit=task.id, include=tuple(task.scope_include)) for task in scoped]
    if not seam:
        # No path is shared and none is unowned, so there is no seam to read. Appending an empty
        # one would be worse than useless: an empty `include` is the pathspec for *everything*, so
        # the reading meant to cover the gap between the slices would silently re-read the whole
        # change — the one launch this composition exists to avoid.
        return readings
    return [*readings, Reading(unit=SEAM, include=tuple(sorted(seam)))]


def prior_for(reading: Reading, prior_blocking: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The carried-forward blocking findings this reading is answerable for.

    A reading is shown — and held to — only the findings anchored inside the paths it covers. Hand
    a reading a finding about code it was never sent and the refusal for dropping one becomes
    unpassable: the reviewer cannot re-state a finding it cannot see, and cannot resolve it either.
    A finding with no anchor at all belongs to no slice, so it travels with the whole-change
    reading and, in a composed review, with the seam — the reading whose job is what no slice owns.
    """
    if reading.whole:
        return [dict(f) for f in prior_blocking]
    covers = reading.include
    mine: list[dict[str, Any]] = []
    for finding in prior_blocking:
        anchors = finding.get("code_anchors")
        paths = [str(a.get("path", "")) for a in anchors if isinstance(a, Mapping)] if isinstance(anchors, list) else []
        if not paths:
            if reading.unit == SEAM:
                mine.append(dict(finding))
            continue
        if any(common.longest_cover(path, covers) for path in paths if path):
            mine.append(dict(finding))
    return mine


# -- merging what several readings said ---------------------------------------

#: Statement and finding ids are minted per reading, so two readings both start at 001. The
#: patterns the schema enforces are `AST-[0-9]{3,}` and `SEC-[0-9]{3,}`.
_AST = "AST-{:03d}"
_SEC = "SEC-{:03d}"

#: What one review may carry, read from the schema that enforces it. A composition can reach these
#: where a single reading never did, and the answer is to refuse rather than to truncate: a list
#: silently cut is a review that says less than it read, which is the failure "extra behaviours: 0"
#: exists to prevent. 0.3.7 deleted a `truncated` field for exactly this reason.
MAX_STATEMENTS = review_policy.review_schema_max_items("actual_extraction")
MAX_FINDINGS = review_policy.review_schema_max_items("security", "properties", "findings")


@dataclass(frozen=True)
class ReadOut:
    """What one reading's two stages said about it."""

    reading: Reading
    extraction: actual_extraction.ExtractionResult
    security: security_review.SecurityResult
    #: The ids of the blocking findings this reading was handed (`prior_for`). A finding answering
    #: to one of them is the carried finding itself and keeps its number; anything else that comes
    #: back under the same number is a different reading's new finding and is renumbered.
    carried_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Composition:
    """Several readings, merged into the one set of statements and findings a review carries."""

    statements: tuple[dict[str, Any], ...]
    actual_digest: str
    findings: tuple[dict[str, Any], ...]
    resolved: tuple[dict[str, Any], ...]


def merge(readouts: Sequence[ReadOut], *, coverage: Mapping[str, Any]) -> Composition:
    """One set of Actual Statements and one set of security findings, out of several readings.

    Two jobs, and both are about identity.

    **Statements are renumbered into one sequence** and stamped with the reading they came out of.
    Each extraction mints `AST-001` upward on its own, so a composed set would otherwise hold
    several statements with the same id — and the comparator resolves every citation by id. The
    renumbering happens before the comparator sees anything, so nothing downstream ever knows two
    numberings existed. The stamp goes on the statement rather than only on the manifest because a
    statement is what a Decision Card, a claim verdict and an extra behaviour all cite: without it
    a reading of one task's diff and a reading of the seam sit in one list, in one shape, with
    nothing to tell them apart — which is exactly what a reader needs when they ask whether "extra
    behaviours: 0" was said about everything.

    **Findings keep the id they were carried forward under, and only new ones are renumbered.** A
    blocking finding's id is what the next generation hands back to the reviewer that must re-state
    it (`prior_for`, `security_review.run_security_review`), so renumbering a carried finding would
    break the continuity the carry-over exists to provide. A finding this generation minted is new
    to the document either way, so moving its number costs nothing.

    **A whole-change reading is left exactly as it was read** — no renumbering, no stamp, and the
    extractor's own `actual_digest`. Not for continuity's sake but because the digest has to keep
    meaning what it says it means: it binds *the statements the comparator is handed*, and the
    extractor minted it over the statements it produced. Adding a `reading` field to them would
    leave the two describing different bytes. Nothing is lost by leaving it off: `whole` is what
    `coverage.composition.mode` already says, so the field would tell a reader what the manifest
    told them, at the cost of the one digest that ties the Actual to the model that read it.

    Every other shape goes through the merge below, one reading or several: there the stamp is
    load-bearing (a statement about one slice must never read as a statement about the tree) and
    the digest is re-minted over exactly what the comparator receives.
    """
    if len(readouts) == 1 and readouts[0].reading.whole:
        only = readouts[0]
        return Composition(
            statements=tuple(dict(s) for s in only.extraction.actual_statements),
            actual_digest=only.extraction.actual_digest,
            findings=tuple(dict(f) for f in only.security.findings),
            resolved=tuple(dict(f) for f in only.security.resolved),
        )

    statements: list[dict[str, Any]] = []
    for readout in readouts:
        for statement in readout.extraction.actual_statements:
            statements.append(
                {**dict(statement), "id": _AST.format(len(statements) + 1), "reading": readout.reading.unit}
            )

    # Every id a finding was carried forward under is reserved before anything is minted, so a new
    # finding in one reading can never take the number a carried one in another reading answers to.
    taken = {fid for readout in readouts for fid in readout.carried_ids}
    findings: list[dict[str, Any]] = []
    for readout in readouts:
        for finding in readout.security.findings:
            fid = str(finding.get("id", ""))
            if fid not in readout.carried_ids and fid in taken:
                fid = _next_free(_SEC, taken)
            taken.add(fid)
            findings.append({**dict(finding), "id": fid, "reading": readout.reading.unit})
    if len(statements) > MAX_STATEMENTS or len(findings) > MAX_FINDINGS:
        raise ReviewError(
            f"this cycle's readings produced {len(statements)} actual statement(s) and "
            f"{len(findings)} security finding(s), past what one review may carry "
            f"({MAX_STATEMENTS} and {MAX_FINDINGS}). Reduce what this cycle claims through "
            "`/revise` and review the remainder in its own gate ④ round — a list cut to fit is a "
            "review that says less than it read."
        )
    return Composition(
        statements=tuple(statements),
        # The same shape `actual_extraction.run_extractor` mints, over the merged set: the digest
        # binds what the comparator is handed, and what it is handed is this.
        actual_digest=digests.of({"actual_statements": statements, "coverage": dict(coverage)}),
        findings=tuple(findings),
        resolved=tuple(dict(f) for r in readouts for f in r.security.resolved),
    )


def keys_for(
    measured: ReadingFacts,
    *,
    config: models.Config | None,
    trusted_base: str,
    ceiling: int,
    risk_floor: str,
    prior_blocking: Sequence[Mapping[str, Any]] = (),
) -> dict[str, str]:
    """This reading's stage keys, from what the reading measured about itself.

    The one place the mapping from a reading to its keys lives, because two callers make it and
    they have to agree exactly: `review.generate` at the gate, and `build_loop` when a task lands.
    A build that warms a key the gate then misses would pay for both readings and save nothing.

    `risk_floor` is the one input here that is *not* a fact about this reading, and it is passed in
    rather than read off `measured` for exactly that reason (`extraction_request`): the floor is a
    property of the whole change, so a slice holding no signal must not be where it drops. It is in
    the key because it is in the request — a key that did not cover it would serve an answer taken
    under a different instruction — and the coupling it buys is bounded: the floor is a four-value
    ladder that only ever rises as a cycle lands, so at worst the readings taken before a rise are
    re-read once, and in the common case of a floor that never moves nothing is invalidated at all.
    """
    return reading_keys(
        config=config,
        change=measured.content_digest,
        coverage_digest=digests.of(measured.coverage),
        trusted_base=trusted_base,
        ceiling=ceiling,
        risk_floor=risk_floor,
        prior_blocking=[str(f.get("id", "")) for f in prior_blocking],
        unit=measured.reading.unit,
    )


def warm(
    repo: repo_mod.Repo,
    reviewers: review_policy.Reviewers,
    *,
    reading: Reading,
    base: str,
    head: str,
    exclude: Sequence[str],
    limits: Mapping[str, int],
    config: models.Config | None,
    cache: review_cache.StageCache,
) -> ReadOut:
    """Take one reading now, so gate ④ finds it already answered.

    Called when a task lands, where the reading is one task wide and the tree that produced it is
    the one in front of the caller. It answers the *same question* the gate will ask — same
    measure, same keys (`keys_for`), same validation — so the gate reuses it rather than reading
    the change again from a session that has a whole cycle to hold.

    Nothing is carried forward here: `prior_blocking` belongs to a generated review, and during a
    build there is not one yet about this head. A blocking finding the last review recorded is
    handled where it is read, at the gate, which is also the only place that can resolve it.

    The risk floor is measured over the **whole** change and not over this reading, because that is
    what the gate will send and a warm-up that asks a different question is a warm-up nobody
    reuses. It costs one more `git diff` and one regex pass per task landing, both deterministic
    and both cheap next to the launch they are protecting.
    """
    measured = read_facts(repo, reading=reading, base=base, head=head, exclude=exclude, limits=limits)
    risk_floor = whole_change_risk_floor(repo, base=base, head=head, exclude=exclude)
    keys = keys_for(
        measured,
        config=config,
        trusted_base=base,
        ceiling=limits["max_diff_bytes"],
        risk_floor=risk_floor,
    )
    return read_one(
        repo,
        reviewers,
        measured=measured,
        trusted_base=base,
        head=head,
        risk_floor=risk_floor,
        prior_blocking=(),
        discipline=security_discipline(config),
        cache=cache,
        keys=keys,
        ran=set(),
        reused=usage_mod.Ledger(),
        cancel=common.Cancellation(),
    )


def whole_change_risk_floor(repo: repo_mod.Repo, *, base: str, head: str, exclude: Sequence[str]) -> str:
    """The detector's risk floor over the whole change, whatever readings it is then taken in.

    A property of the change and never of a slice of it (`extraction_request`). `review.generate`
    already has this from the whole diff it measures the manifest on; `build_loop` does not, and
    has to take it, because the key the gate will look under is a function of it.
    """
    return diff_facts.analyze(diff_of(repo, base, head, exclude)).risk_floor


def _next_free(pattern: str, taken: Collection[str]) -> str:
    number = 1
    while pattern.format(number) in taken:
        number += 1
    return pattern.format(number)


def read_one(
    repo: repo_mod.Repo,
    reviewers: review_policy.Reviewers,
    *,
    measured: ReadingFacts,
    trusted_base: str,
    head: str,
    risk_floor: str,
    prior_blocking: Sequence[Mapping[str, Any]],
    discipline: str = "",
    on_stage: Callable[[str], None] = lambda _name: None,
    cache: review_cache.StageCache,
    keys: Mapping[str, str],
    ran: set[str],
    reused: usage_mod.Ledger,
    cancel: common.Cancellation,
) -> ReadOut:
    """The two stages that read code, run over one reading of the change.

    The security review reads the same bytes the extractor does and consumes nothing the extractor
    produces, so it does not wait behind it — and when both are configured on one adapter they
    branch a single priming turn of those bytes (`review_transport.SharedReading`), which is why
    they belong to the same reading rather than to the pipeline at large.

    `cancel` is what makes the failure path honest. `Future.cancel()` cannot stop a task that has
    started — with one worker and nothing competing for it, this one always has — and
    `ThreadPoolExecutor.__exit__` then blocks in `shutdown(wait=True)` until the adapter call it
    failed to cancel finishes. So a failure would be reported when the *discarded* call ends rather
    than when it happens: measured across two runs of one cycle, the extraction failure surfaced
    1m36s and 3m54s late, each run having paid in full for a security review nobody would read.
    `shutdown(wait=False, cancel_futures=True)` does not fix it either —
    `concurrent.futures.thread` registers an atexit hook that joins every worker, so the wait moves
    to interpreter exit and the process returns no sooner. The only thing that ends a launch early
    is killing the process it started.

    The extractor's failure is the one reported when both fail: it comes first in the pipeline's
    order, and reporting whichever thread lost a race would make the error a reader sees depend on
    timing.
    """
    security_req = security_request(
        measured, trusted_base=trusted_base, head=head, prior_blocking=prior_blocking, discipline=discipline
    )

    def run_security() -> security_review.SecurityResult:
        # Bound on this thread — the worker's — because the transport is reached through the
        # injected reviewers, whose signature is not ours to change.
        with common.cancelling(cancel):
            return cached_stage(
                cache,
                "security_review",
                keys["security_review"],
                ran,
                lambda ask: security_review.run_security_review(security_req, ask, repo=repo, commit=head),
                reviewers,
                reused=reused,
            )

    with ThreadPoolExecutor(max_workers=1) as pool:
        security_future = pool.submit(run_security)
        try:
            # Blind actual extraction — the plan is deliberately absent from this request (§12.2).
            on_stage("actual_extraction")
            extraction = cached_stage(
                cache,
                "actual_extraction",
                keys["actual_extraction"],
                ran,
                lambda ask: actual_extraction.run_extractor(
                    extraction_request(measured, trusted_base=trusted_base, head=head, risk_floor=risk_floor),
                    ask,
                    repo=repo,
                    commit=head,
                    risk_floor=risk_floor,
                ),
                reviewers,
                reused=reused,
            )
        except BaseException:
            cancel.cancel()
            raise
        on_stage("security_review")
        security = security_future.result()
    return ReadOut(
        reading=measured.reading,
        extraction=extraction,
        security=security,
        carried_ids=tuple(str(f.get("id", "")) for f in prior_blocking),
    )
