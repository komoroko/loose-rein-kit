"""The prompt texts build_loop hands to its headless agents — pure builders, no orchestration state.

One function per headless launch (implementer, review step, integration fixer, security
reviewer). Kept apart from the Orchestrator so the wording can be read, diffed, and tested
without threading through its git/worktree machinery; the Orchestrator's `_*_prompt` methods
are thin delegates that pass in the few facts a prompt actually needs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rein import adapters, dag


def _gate_list(gate_cmds: Sequence[str]) -> str:
    return " and ".join(f"`{c}`" for c in gate_cmds) or "the quality-gate commands"


def _pathspec(pathspec: Sequence[str]) -> str:
    """The commit pathspec, shell-quoted, as the run itself computed it.

    The implementer is told to run the same exclusion `finalize_commit` applies when the
    implementer does not. Spelled out twice by hand, those two could drift — and the instruction
    is the copy an agent actually types. Taken as an argument rather than read from
    `repo.SSOT_PATHSPEC` because half of it is configurable: the leaf worktree root
    (`execution.worktree_dir`) is excluded alongside `.rein/`, and a repair committing from the
    repository root would otherwise embed every leaf's worktree in its own commit.
    """
    return " ".join(part if part == "." else f"'{part}'" for part in pathspec)


def handoff_note(handoff: Mapping[str, object]) -> str:
    """What the previous, interrupted attempt at this task left behind — or "" if nothing did.

    The implementer's own agent session does not survive the terminal that ran it, so this is
    what a build restarted elsewhere can actually tell the next attempt. Only the salvage state
    is spelled out; the failure itself already reaches the prompt through `failure_log`.
    """
    branch, state = str(handoff.get("salvage_branch", "")), str(handoff.get("salvage_state", ""))
    if not branch:
        return ""
    if state == "restored":
        return (
            f"A previous attempt at this task was interrupted. Its committed work has already been "
            f"merged into this branch from {branch} — continue from it rather than starting over."
        )
    if state == "conflict":
        return (
            f"A previous attempt at this task was interrupted. Its committed work is on {branch}, but "
            f"merging it here conflicted. Inspect it (`git diff {branch}`), then take what is still "
            "correct — do not assume it is all wrong, and do not merge it blind."
        )
    return f"A previous attempt at this task was interrupted; its committed work is on {branch}."


def implementer_prompt(
    task: dag.Task,
    failure_log: str,
    *,
    gate_cmds: Sequence[str],
    has_baseline: bool,
    pathspec: Sequence[str],
    handoff: Mapping[str, object] | None = None,
    dossier_path: str = "",
    continued_from: str = "",
    continued_worktree: str = "",
    review_findings: str = "",
) -> str:
    # Point the implementer at the design section for this task's requirement rather than the whole
    # design doc: reading only the relevant slice keeps the subagent context lean and avoids
    # "Lost in the Middle" on a long design (see AGENTS.md "Context budget"). Fall back to the whole
    # doc when the task has no req linkage.
    design_ref = (
        f"the design section(s) covering {', '.join(task.claim_ids)} in docs/20-design.md"
        if task.claim_ids
        else "docs/20-design.md"
    )
    # In an adopted (brownfield) repo the baseline doc carries the conventions and the
    # reusable-asset inventory the implementer must match — point at it when present.
    baseline_ref = (
        " Consult docs/05-current-state.md for the existing architecture, conventions, and reusable assets."
        if has_baseline
        else ""
    )
    # Name the claims this task is answerable for. The implementer must know what it is being
    # measured against, and the mandate's frozen quality_gate is the judgement boundary — not a
    # command the implementer chose.
    task_test_ref = (
        f"This task is answerable for: {', '.join(task.claim_ids)} (see .rein/plan.yaml).\n" if task.claim_ids else ""
    )
    # The dossier is everything the loop already worked out — the claims and what each one says,
    # the declared scope, the classified diff, what earlier attempts tried. Reading it first is
    # what stops the agent re-deriving all of that from the repository on every single launch.
    dossier_ref = (
        f"**Read {dossier_path} first.** It carries this task's claims and what each one asserts, its "
        "acceptance criteria and how each one will be judged, the paths you are scoped to, what has "
        "already changed (with lockfiles and generated files summarized rather than spelled out), and "
        "what earlier attempts tried. It is assembled fresh for this launch — trust it over anything "
        "you would go and re-derive.\n"
        if dossier_path
        else ""
    )
    # A launch forked from the session that finished the upstream task remembers that task's
    # worktree: its paths, and the files as they were before the merge. Both are wrong here, and an
    # edit made through a remembered absolute path lands outside this task's tree, where no gate and
    # no merge will ever see it.
    continuation = (
        f"**This session continues from the one that implemented {continued_from}.** That task is "
        f"finished: its change is merged into the branch this one forked from, and its worktree "
        f"(`{continued_worktree}`) no longer exists. Your working directory is this one. A path you "
        "remember under that worktree is at the same relative path here — edit it here, never "
        "there — and re-read a file before you change it, because what you remember predates the "
        "merge. What you learned about the codebase still holds; the task has changed.\n"
        if continued_from
        else ""
    )
    prompt = (
        f'You are the implementer subagent. Your only task is {task.id} "{task.title}".\n'
        f"{continuation}"
        f"{dossier_ref}"
        f"Then read docs/tasks/{task.id}.md, {design_ref}, and the existing code, and implement "
        f"following the protocol in .rein/prompts/agents/implementer.md.{baseline_ref}\n"
        f"{task_test_ref}"
        f"Write automated tests and get {_gate_list(gate_cmds)} green.\n"
        "When done, commit your changes to this branch (excluding the orchestration state .rein/):\n"
        f'  git add -A -- {_pathspec(pathspec)} && git commit -m "{task.id}: <summary>"\n'
        "Do not reach outside scope (other tasks' territory). If you find a requirements/design defect, "
        "do not fix it on your own — report it.\n"
        "End with one `rein report --outcome implemented|blocked|needs-revision --summary … --touched …` "
        "call: it is the only channel by which anything you say reaches the caller."
    )
    note = handoff_note(handoff or {})
    if note:
        prompt += f"\n\n{note}"
    if failure_log:
        # failure_log is already a compact summarize_failure() output (salient lines, budget-capped),
        # so it is passed through as-is — no crude tail-slicing that could cut the actionable lines.
        prompt += f"\n\nResolve the previous quality-gate failure:\n{failure_log}"
    if review_findings:
        # The change passed the deterministic gate; what comes back is a reader's judgement, which
        # can be wrong in a way an exit status cannot. Disputing one is a real option for that
        # reason, and it is what the reviewer reads when it looks again.
        prompt += (
            "\n\nYour change passed the gate. An independent reviewer then read it and found the "
            "following, and each one has to be resolved before the task can land:\n"
            f"{review_findings}\n"
            "Fix them with the minimal change — do not widen scope, and do not redo the task. If a "
            "finding is wrong, say so in your `rein report --summary` rather than silently ignoring it: "
            "the reviewer looks again afterwards."
        )
    return prompt


def _disciplines_note(disciplines: Mapping[str, str] | None, *, at_the_join: bool = False, batch: bool = False) -> str:
    """Offer the host's own review disciplines — with what each one must not do here.

    The prompts state every question in full whether or not this returns anything, so a host
    without these asks exactly the same thing. What this adds is the host's own reading of it,
    and the two sentences that keep rein's contract on top of it: `/simplify` ends by applying its
    fixes, and a reviewer that edits is the arrangement this loop was changed to remove; both
    report where they choose, and the only answer this step reads is the findings file.
    """
    offered = dict(disciplines or {})
    correctness = offered.get(adapters.CORRECTNESS, "")
    simplification = offered.get(adapters.SIMPLIFICATION, "")
    if not correctness and not simplification:
        return ""
    named = " and ".join(f"`{c}`" for c in (correctness, simplification) if c)
    where = (
        "They read the branch you name, and yours is the work branch, which holds none of these "
        "changes yet: point them at each task's branch in turn"
        if batch
        else "They read the branch you are on"
    )
    note = (
        f"\n**Your host carries {named} as disciplines of its own — use them for the reading above.** "
        f"{where}, and they were written for this by people who do nothing "
        "else; re-deriving the same questions from scratch is the worse of the two readings.\n"
    )
    if simplification:
        note += (
            f"- **{simplification} ends by applying its fixes. Run its review phase only.** Leave every "
            "file exactly as you found it — whoever judges does not repair here, and a tree that moves "
            "under the gate sends every already-passed step back through it.\n"
        )
    if correctness:
        note += (
            f"- **Never `{correctness} --fix`, and never `{correctness} ultra`.** `--fix` applies the "
            "findings to the working tree, which is the rule above reached by another route: the fix "
            "would be the reviewer's own and nobody would read it. `ultra` is billed and "
            "user-triggered, and an agent cannot launch it.\n"
        )
    if at_the_join:
        note += (
            "- They read the whole branch, which here is the join plus every task inside it. Keep what "
            "only the join shows; a finding about one task alone was already reviewed before the merge, "
            "and reporting it again spends an implementer round on a settled question.\n"
        )
    note += (
        "- Report through neither of them. Whatever they produce, the answer this step reads is the "
        "findings file below — do not print a report and do not use `ReportFindings`.\n"
        "- A discipline that is missing, disabled or renamed on this host is not a reason to stop: "
        "ask the questions above yourself.\n"
    )
    return note


def lens_note(applied: Sequence[str], proposed: Sequence[str]) -> str:
    """The lenses this cycle's mandate froze for the code stage, handed to the reviewer.

    Handed rather than listed in the prompt for the same reason the drafting reviewers are handed
    theirs: a lens whose condition does not hold here attacks a failure this change cannot have, and
    it costs a pass over the diff and comes back "attacked, nothing" while the findings that *are*
    possible compete with it for the reader's attention. The set is the one `plan.lenses` holds, so
    a reviewer launched today reads for what the gate screen said it would.
    """
    if not applied and not proposed:
        return ""
    note = "\n**Lenses this cycle froze for the code stage — work through exactly these:**\n"
    for line in applied:
        note += f"- {line}\n"
    for line in proposed:
        note += f"- (proposed at the gate, apply if it holds here) {line}\n"
    note += (
        "A lens you were not handed is not an oversight: its condition does not hold for this "
        "change. Report anything you find that no lens covers, and say so — it is recorded as an "
        "unclassified lens and earns a condition the second time the same cause comes back.\n"
    )
    return note


@dataclass(frozen=True)
class ReviewSubject:
    """One task of a batch as its reviewer is shown it: where the change is and what it was for."""

    task: dag.Task
    #: The branch the task's change is on, which the host's disciplines can be pointed at.
    branch: str
    #: The whole change, as a command run from the repository root.
    diff_cmd: str
    #: The task's dossier, relative to the repository root.
    dossier_path: str


def batch_review_prompt(
    subjects: Sequence[ReviewSubject],
    *,
    gate_cmds: Sequence[str],
    findings_path: str,
    disciplines: Mapping[str, str] | None = None,
    lenses_applied: Sequence[str] = (),
    lenses_proposed: Sequence[str] = (),
) -> str:
    """One reviewer launch for every task of a batch that passed its deterministic gate.

    Each task used to get a reviewer of its own, and a batch of two or more then got one more over
    the join, reading the union of what the others had read. Independence needs a launch that is
    not the implementer's; it does not need one per task. So one reader takes the batch before
    anything merges, answers each task separately — the answer per task is what goes back to that
    task's implementer — and, where there are several, reads them as the one change they are about
    to become.
    """
    cmds = ", ".join(f"`{c}`" for c in gate_cmds)
    ids = [subject.task.id for subject in subjects]
    listing = "".join(
        f'- **{s.task.id}** "{s.task.title}": dossier `{s.dossier_path}`, branch `{s.branch}`, change `{s.diff_cmd}`.\n'
        for s in subjects
    )
    joint = (
        "\n**Then read them as one change**, because they are about to be merged into one tree and no "
        "test in any of them was written with the others in view:\n"
        "- **Correctness across tasks**: a contract two tasks now read differently, an invariant one "
        "relies on and another removes, shared state two of them write.\n"
        "- **Shape**: duplication between what two tasks added, one responsibility now in two places.\n"
        "A finding about how two tasks meet belongs to the task that has to change for it to hold.\n"
        if len(subjects) > 1
        else ""
    )
    return (
        f"You are the reviewer for {', '.join(ids)} (the quality gate's agent step). Each was implemented "
        "on its own branch, in its own worktree, and has already passed the deterministic gate; none "
        "is merged yet.\n"
        f"{listing}"
        "**Read each task's dossier first.** It carries the claims the task answers, its acceptance "
        "criteria and how each one is judged, its declared scope, and its changed paths split into "
        "source, tests and mechanical churn — review the source and tests, not the churn. Judge each "
        "change against its own acceptance criteria, starting with the ones whose `evidence.kind` is "
        "`prose`: a criterion carrying a `command` or an `artifact` was already established by the "
        "caller, and a prose one is judged by nobody between you and acceptance.\n"
        "\n"
        "For each task, review its change for correctness bugs, then for simplification: reuse existing "
        "code, needless complexity, and anything its acceptance criteria do not require — speculative "
        "generality, unused knobs/hooks (YAGNI). A requirements/design defect is a finding like any "
        "other, not something to work around.\n"
        f"{_disciplines_note(disciplines, batch=True)}"
        f"{lens_note(lenses_applied, lenses_proposed)}"
        "\n"
        "**Then read the tests as evidence, not as code that passes.** The caller re-establishes the "
        "gate's command steps over the base with only a change's test half applied, which can show "
        "that the test half is not inert against the old code — never that the tests are any good, and "
        "this is the only place that judgement is made. For each acceptance criterion, name the test in "
        "that task's change that would go red if the behaviour the criterion describes were wrong; a "
        "criterion with no such test is a finding. So is an assertion that would hold for any output (a "
        "bare not-null or truthiness check where the criterion names a value, a mock asserted against "
        "itself), a test that pins the implementation's internals rather than its behaviour, and an "
        "expected-exception check that never looks at what was raised.\n"
        f"{joint}"
        "\n"
        "**You do not change the code, and you do not run anything.** You have no write access to it, "
        f"and running {cmds} would only repeat what the caller runs itself and decides by. Judging a "
        "change and then editing it away is one participant doing both halves of a review; each "
        "task's implementer fixes what you find in it, and you get to look again.\n"
        "\n"
        f"Write your findings to `{findings_path}` and nothing else, one entry per task:\n"
        '  {"tasks": {"' + ids[0] + '": {"findings": [{"severity": "must_fix", "statement": "…", '
        '"anchor": "src/x.py:42"}]}}}\n'
        "**Every task above gets an entry**, an empty `findings` list included: a task with no entry "
        "has not been reviewed, and it does not land. `must_fix` is a defect the change cannot land "
        "with — a bug, a broken contract, a security problem — and it goes back to that task's "
        "implementer. `consider` is everything else worth saying; it stops nothing and is carried to "
        "the human at acceptance. An empty list is a real answer, and the right one when the change "
        "is sound: inventing a finding to look thorough costs an implementer round for nothing."
    )


def gate_four_fix_prompt(task: dag.Task, findings: str, *, gate_cmds: Sequence[str]) -> str:
    """Hand acceptance's blocking findings about one task back to an implementer.

    Not the batch review's send-back (`implementer_prompt`'s `review_findings`): that one is about a
    change that has not landed, inside its own worktree, with the task's own send-back budget. This is the *grounded*
    review — a blind reading of the merged tree compared against the frozen plan — and its
    findings arrive after everything is `done` and merged. What differs is not the tone: it is
    what a finished fix looks like. The plan is frozen, the task's acceptance criteria are already
    established, and this is a repair inside a scope somebody already approved. Widening it is not
    an option the implementer has, and saying so is what keeps a security finding from turning
    into a redesign.

    The findings name their own code anchors, so the change is pointed at lines rather than at a
    subject. And the reviewer that raised them has no memory of having done so: the next round
    reads the code again from cold, which is what decides whether the finding closed — never this
    launch's account of it.
    """
    return (
        f'You are the implementer for task {task.id} "{task.title}". The grounded review at acceptance read '
        "the merged tree against the frozen plan and found the following in code your task's declared "
        "scope owns:\n"
        f"{findings}\n"
        "Repair them, and nothing else. The plan is frozen and this task is already done and merged: "
        "this is a fix inside a scope that was approved, not a second attempt at the task. Stay inside "
        "the scope — a change outside it blocks rather than lands. If a finding is *wrong*, say so in "
        "`rein report --summary` and change nothing for it; the review is taken again from cold "
        "afterwards, by a reader with no memory of having raised it, and that is what decides whether "
        "it closed.\n"
        "Write or amend a test that would have caught it wherever the finding admits one — a repair no "
        "test exercises is a claim about code nobody re-reads. Keep "
        f'{_gate_list(gate_cmds)} green, and commit with the "{task.id}: " prefix.'
    )


def integration_review_fix_prompt(ids: str, findings: str, *, gate_cmds: Sequence[str], pathspec: Sequence[str]) -> str:
    """Hand the integration reviewer's must-fix findings back to an implementer.

    The join's analogue of a batch review's send-back, and it did not exist: both of the join's
    send-backs went through `integration_fix_prompt`, whose first sentence says the combined state
    "fails the deterministic gate" and whose subject is "typically a cross-file lint/format/type
    error". A reviewer's findings are neither. An implementer told it is looking at a lint failure,
    and handed a paragraph about a contract two tasks now read differently, has to work out for
    itself that the framing is wrong before it can start — and the framing is what says how much of
    the tree is in question and what a finished fix looks like.

    What the two send-backs actually have in common is only the commit prefix and the budget. The
    rest differs: this one names an independent reader who will look again, which is what makes
    disputing a finding a real option rather than a silence.
    """
    return (
        f"You are the implementer for the merged state of {ids}. An independent reviewer read these "
        "tasks as one merged tree — the first time anyone did; they were reviewed before the merge — and "
        "found the following, and each one has to be resolved before this batch can land:\n"
        f"{findings}\n"
        "Fix them with the minimal change. These are findings about the **join**: a contract two "
        "tasks now read differently, a responsibility that ended up in two places, an invariant one "
        "of them removed. So the fix usually belongs between the tasks rather than inside one of "
        "them — do not redo either task, and do not widen scope to tidy what the finding did not "
        "name. If a finding is wrong, say so in your `rein report --summary` rather than silently "
        "ignoring it: the reviewer looks again afterwards.\n"
        "Commit your fix to this branch (excluding the orchestration state .rein/):\n"
        f'  git add -A -- {_pathspec(pathspec)} && git commit -m "{ids}: review fix"\n'
        f"Keep {_gate_list(gate_cmds)} green."
    )


def integration_fix_prompt(ids: str, failure_log: str, *, gate_cmds: Sequence[str], pathspec: Sequence[str]) -> str:
    """Hand the *deterministic* post-merge failure to an implementer.

    Its subject is a red command step over the merged tree, never a reviewer's findings — those go
    to :func:`integration_review_fix_prompt`.
    """
    return (
        f"You are the integration fixer. The independent leaf tasks {ids} each passed the quality gate "
        "in their own isolated worktrees, but after merging them into this work branch the combined "
        "state fails the deterministic gate. Fix the integration failure below (typically a cross-file "
        "lint/format/type error, or the tasks' changes interfering) with the minimal change — do not "
        "widen scope or redo the tasks themselves.\n"
        "Commit your fix to this branch (excluding the orchestration state .rein/):\n"
        f'  git add -A -- {_pathspec(pathspec)} && git commit -m "{ids}: integration fix"\n'
        f"Keep {_gate_list(gate_cmds)} green.\n\n"
        f"Resolve this integration failure:\n{failure_log}"
    )


def conflict_prompt(
    ours: dag.Task | None,
    theirs: dag.Task | None,
    paths: Sequence[str],
    *,
    gate_cmds: Sequence[str],
) -> str:
    """Hand a merge conflict to the implementer **with both sides' purpose**, never just the hunks.

    Showing the hunks alone is how a stopgap gets written: whoever resolves has to pick, and with
    nothing to pick on they pick whatever compiles. What each side was *for* — its claims, its
    acceptance criteria, its declared scope — is the only thing that makes one resolution right and
    another a paper-over. The instruction to report `needs-revision` rather than invent a merge is
    the other half: two frozen intentions that genuinely disagree are a defect in the plan, and the
    implementer is the first to be in a position to see it.
    """
    listed = "\n".join(f"  - {path}" for path in paths)
    return (
        "You are resolving a merge conflict between two tasks of this cycle. Both sides are already\n"
        "committed work that passed the quality gate on its own; what is in front of you is where they\n"
        f"met.\n\nConflicted paths:\n{listed}\n\n"
        f"{_side('The branch you are merging INTO (ours)', ours)}"
        f"{_side('The branch being merged IN (theirs)', theirs)}"
        "Resolve so that **both sides still do what they were for**. Keep each side's change inside its\n"
        "own declared scope, stage the resolved files, and do not commit — the loop commits, and it "
        "records both task ids and every path you touched.\n"
        f"Keep {_gate_list(gate_cmds)} green.\n\n"
        "If the two sides genuinely contradict each other — they cannot both hold, so any resolution\n"
        "would have to drop or reinterpret one of them — **do not invent a merge**. Run\n"
        "`rein report --outcome needs-revision --summary <which two intentions collide and why>`. "
        "That is a defect in the plan, and papering over it here is exactly what must not happen. "
        "Otherwise finish with `rein report --outcome implemented`."
    )


def _side(label: str, task: dag.Task | None) -> str:
    if task is None:
        return f"{label}: (no task — these commits belong to none)\n\n"
    claims = ", ".join(task.claim_ids) or "(none)"
    scope = ", ".join(task.scope_include) or "(undeclared — unbounded)"
    criteria = "".join(f"    - {a.get('id', '?')}: {a.get('statement', '')}\n" for a in task.acceptance)
    criteria = criteria or "    (none declared)\n"
    return (
        f"{label}: {task.id} \u2014 {task.title}\n"
        f"  claims it answers: {claims}\n"
        f"  declared scope:    {scope}\n"
        f"  acceptance:\n{criteria}\n"
    )


def integration_review_prompt(
    ids: str,
    *,
    gate_cmds: Sequence[str],
    diff_cmd: str,
    findings_path: str,
    disciplines: Mapping[str, str] | None = None,
    lenses_applied: Sequence[str] = (),
    lenses_proposed: Sequence[str] = (),
) -> str:
    """Review the tree the merge produced, which the batch's reviewer never saw.

    A `stage: both` agent step reads a batch before it merges, and the join is only read again when
    a merge had to resolve a conflict — then the merged tree holds code no reviewer was shown. A
    `stage: integration` step reads every join. What either is here for is the thing the merge
    makes: two tasks that each added a helper, a responsibility that ended up in two places, an
    abstraction one task introduced and the next one worked around, a contract two tasks now read
    differently.

    **This asks about correctness as well as shape, and the argument that it should not was
    wrong.** It used to be the `/simplify` discipline alone, on the reasoning that the command
    steps had just run over this exact tree so the bugs were already answered. But the suite over
    the merged tree is the *union of the leaves' suites*, and not one test in it was written with
    the merge in view: each was written in an isolated worktree against one ticket, by an
    implementer that could not see the other tasks. The interaction defect a merge creates is by
    construction the one no leaf's tests exercise, so "already settled" named the half that is
    least settled here. Nothing else covers it either — acceptance's seam reading takes the paths two
    scopes share or none covers, and two tasks whose files are disjoint produce no seam at all.
    Cross-task correctness had no owner; it has one now.
    """
    cmds = ", ".join(f"`{c}`" for c in gate_cmds)
    return (
        f"You are the reviewer for the merged state of {ids} (the quality gate's integration step).\n"
        f"These tasks were reviewed before the merge; this is the first time anyone has read the tree "
        f"the merge produced. The combined change is `{diff_cmd}`.\n"
        "\n"
        "Review it for what only the join can show, and for both halves of that:\n"
        "- **Correctness across tasks**: a contract two tasks now read differently, an invariant one "
        "task relies on and another removed, an order or lifetime that only holds when one of them is "
        "absent, shared state two tasks both write. The suite that just passed here is the union of "
        "the leaves' suites and no test in it was written with this merge in view, so a green says "
        "nothing about the interaction — that is the gap you are here for.\n"
        "- **Shape**: duplication between what two tasks added, one responsibility now living in two "
        "places, an abstraction one task introduced that the next worked around, anything no ticket's "
        "acceptance criteria require.\n"
        f"{_disciplines_note(disciplines, at_the_join=True)}"
        f"{lens_note(lenses_applied, lenses_proposed)}"
        "\n"
        "Do not re-review either task against its own ticket — that already happened, before the "
        f"merge. Do not run {cmds}: the caller has just run them over this exact tree and decides by "
        "their exit status, and re-running them tells you only what it already knows.\n"
        "\n"
        "**You do not change the code.** You have no write access to it; an implementer resolves what "
        "you find and you look again.\n"
        "\n"
        f"Write your findings to `{findings_path}` and nothing else:\n"
        '  {"findings": [{"severity": "must_fix", "statement": "…", "anchor": "src/x.py:42"}]}\n'
        "`must_fix` is a defect the merged tree cannot land with. `consider` is everything else worth "
        "saying; it stops nothing and is carried to the human at acceptance. An empty list is a real "
        "answer, and the right one when the join is sound."
    )
