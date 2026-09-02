"""The prompt texts build_loop hands to its headless agents — pure builders, no orchestration state.

One function per headless launch (implementer, review step, integration fixer, security
reviewer). Kept apart from the Orchestrator so the wording can be read, diffed, and tested
without threading through its git/worktree machinery; the Orchestrator's `_*_prompt` methods
are thin delegates that pass in the few facts a prompt actually needs.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from rein import adapters, dag
from rein import repo as repo_mod


def _gate_list(gate_cmds: Sequence[str]) -> str:
    return " and ".join(f"`{c}`" for c in gate_cmds) or "the quality-gate commands"


def _pathspec() -> str:
    """The commit pathspec, shell-quoted, from the one constant that defines it.

    The implementer is told to run the same exclusion `finalize_commit` applies when the
    implementer does not. Spelled out twice by hand, those two could drift — and the instruction
    is the copy an agent actually types.
    """
    return " ".join(part if part == "." else f"'{part}'" for part in repo_mod.SSOT_PATHSPEC)


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
    handoff: Mapping[str, object] | None = None,
    dossier_path: str = "",
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
    # measured against, and the gate-③ frozen quality_gate is the judgement boundary — not a
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
    prompt = (
        f'You are the implementer subagent. Your only task is {task.id} "{task.title}".\n'
        f"{dossier_ref}"
        f"Then read docs/tasks/{task.id}.md, {design_ref}, and the existing code, and implement "
        f"following the protocol in .rein/prompts/agents/implementer.md.{baseline_ref}\n"
        f"{task_test_ref}"
        f"Write automated tests and get {_gate_list(gate_cmds)} green.\n"
        "When done, commit your changes to this branch (excluding the orchestration state .rein/):\n"
        f'  git add -A -- {_pathspec()} && git commit -m "{task.id}: <summary>"\n'
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
    return prompt


def _disciplines_note(disciplines: Mapping[str, str] | None, *, at_the_join: bool = False) -> str:
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
    note = (
        f"\n**Your host carries {named} as disciplines of its own — use them for the reading above.** "
        "They read the branch you are on, and they were written for this by people who do nothing "
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
            "only the join shows; a finding about one task alone was already reviewed on its own "
            "branch, and reporting it again spends an implementer round on a settled question.\n"
        )
    note += (
        "- Report through neither of them. Whatever they produce, the answer this step reads is the "
        "findings file below — do not print a report and do not use `ReportFindings`.\n"
        "- A discipline that is missing, disabled or renamed on this host is not a reason to stop: "
        "ask the questions above yourself.\n"
    )
    return note


def review_prompt(
    task: dag.Task,
    *,
    gate_cmds: Sequence[str],
    changed_paths: Sequence[str] = (),
    diff_cmd: str = "",
    dossier_path: str = "",
    findings_path: str = "",
    disciplines: Mapping[str, str] | None = None,
) -> str:
    # Scope the reviewer's read to the task's actual diff: it runs in a fresh context (independent
    # verification — deliberately not the implementer's session), and without this hint it must
    # re-survey the tree cold to even find the changes it is reviewing. The dossier goes further:
    # it already separates the source from the tests from the 800 lines of lockfile, and says what
    # the task was supposed to establish, so the reviewer stops re-inferring both from a raw diff.
    cmds = ", ".join(f"`{c}`" for c in gate_cmds)
    if dossier_path:
        scope = (
            f"**Read {dossier_path} first.** It carries the claims this task answers, its acceptance "
            "criteria and how each one is judged, its declared scope, and its changed paths already "
            "split into source, tests and mechanical churn — review the source and tests, not the "
            "churn. Judge the change against the acceptance criteria, starting with the ones whose "
            "`evidence.kind` is `prose`: a criterion carrying a `command` or an `artifact` was "
            "already established by the caller, and a prose one is judged by nobody between you and "
            f"gate ④. The full diff is `{diff_cmd}`.\n"
            if diff_cmd
            else f"**Read {dossier_path} first.** It carries the claims this task answers, its acceptance "
            "criteria, its declared scope, and its changed paths split by kind.\n"
        )
    elif changed_paths:
        listing = "\n".join(f"  {p}" for p in changed_paths)
        scope = (
            f"The task's changes are exactly these paths (diff: `{diff_cmd}`):\n{listing}\n"
            "Review that diff plus the code it interacts with — do not re-survey the whole tree.\n"
        )
    elif diff_cmd:
        scope = f"The task's diff is `{diff_cmd}` — review it plus the code it interacts with.\n"
    else:
        scope = ""
    return (
        f'You are the reviewer for task {task.id} "{task.title}" (the quality gate\'s agent step).\n'
        f"{scope}"
        "Review this branch's changes for this task for correctness bugs, then for simplification: "
        "reuse existing code, needless complexity, and anything the ticket's acceptance criteria do "
        "not require — speculative generality, unused knobs/hooks (YAGNI). Stay within this task's "
        "scope; a requirements/design defect is a finding like any other, not something to work "
        "around.\n"
        f"{_disciplines_note(disciplines)}"
        "\n"
        "**Then read the tests as evidence, not as code that passes.** The caller re-establishes the "
        "gate's command steps over the base with only this change's test half applied, which can show "
        "that the test half is not inert against the old code — never that the tests are any good, and "
        "this is the only place that judgement is made. For each acceptance criterion, name the test in "
        "this change that would go red if the behaviour the criterion describes were wrong; a criterion "
        "with no such test is a finding. So is an assertion that would hold for any output (a bare "
        "not-null or truthiness check where the criterion names a value, a mock asserted against "
        "itself), a test that pins the implementation's internals rather than its behaviour, and an "
        "expected-exception check that never looks at what was raised.\n"
        "\n"
        "**You do not change the code, and you do not run anything.** You have no write access to it, "
        f"and running {cmds} would only repeat what the caller runs itself and decides by. Judging a "
        "change and then editing it away is one participant doing both halves of a review; the "
        "implementer fixes what you find, and you get to look again.\n"
        "\n"
        f"Write your findings to `{findings_path}` and nothing else:\n"
        '  {"findings": [{"severity": "must_fix", "statement": "…", "anchor": "src/x.py:42"}]}\n'
        "`must_fix` is a defect the change cannot land with — a bug, a broken contract, a security "
        "problem. `consider` is everything else worth saying; it stops nothing and is carried to the "
        "human at gate ④. An empty list is a real answer, and the right one when the change is sound: "
        "inventing a finding to look thorough costs an implementer round for nothing."
    )


def review_fix_prompt(task: dag.Task, findings: str, *, gate_cmds: Sequence[str], dossier_path: str = "") -> str:
    """Hand a reviewer's must-fix findings back to the implementer.

    The reviewer used to apply its own fixes, which made the tree move underneath the gate and
    forced every already-passed step to be re-run — and put judging and repairing in one pair of
    hands. Separating them costs this one extra launch and buys a review whose findings somebody
    else had to act on.
    """
    reference = f"Your dossier is {dossier_path}.\n" if dossier_path else ""
    return (
        f'You are the implementer for task {task.id} "{task.title}". An independent reviewer read your '
        "change and found the following, and each one has to be resolved before the task can land:\n"
        f"{findings}\n"
        f"{reference}"
        "Fix them with the minimal change — do not widen scope, and do not redo the task. If a finding "
        "is wrong, say so in your `rein report --summary` rather than silently ignoring it: the reviewer "
        f"looks again afterwards. Keep {_gate_list(gate_cmds)} green, and commit with the "
        f'"{task.id}: " prefix.'
    )


def integration_fix_prompt(ids: str, failure_log: str, *, gate_cmds: Sequence[str]) -> str:
    return (
        f"You are the integration fixer. The independent leaf tasks {ids} each passed the quality gate "
        "in their own isolated worktrees, but after merging them into this work branch the combined "
        "state fails the deterministic gate. Fix the integration failure below (typically a cross-file "
        "lint/format/type error, or the tasks' changes interfering) with the minimal change — do not "
        "widen scope or redo the tasks themselves.\n"
        "Commit your fix to this branch (excluding the orchestration state .rein/):\n"
        f'  git add -A -- {_pathspec()} && git commit -m "{ids}: integration fix"\n'
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
) -> str:
    """Review the tree the merge produced, which no per-task reviewer ever saw.

    Each leaf was reviewed in its own worktree, against its own ticket, and the reviewers were
    right to stay there — a reviewer that wanders outside its task's scope reviews somebody else's
    work. What none of them could see is the thing the merge makes: two tasks that each added a
    helper, a responsibility that ended up in two places, an abstraction one task introduced and
    the next one worked around, a contract two tasks now read differently.

    **This asks about correctness as well as shape, and the argument that it should not was
    wrong.** It used to be the `/simplify` discipline alone, on the reasoning that the command
    steps had just run over this exact tree so the bugs were already answered. But the suite over
    the merged tree is the *union of the leaves' suites*, and not one test in it was written with
    the merge in view: each was written in an isolated worktree against one ticket, by an
    implementer that could not see the other tasks. The interaction defect a merge creates is by
    construction the one no leaf's tests exercise, so "already settled" named the half that is
    least settled here. Nothing else covers it either — gate ④'s seam reading takes the paths two
    scopes share or none covers, and two tasks whose files are disjoint produce no seam at all.
    Cross-task correctness had no owner; it has one now.
    """
    cmds = ", ".join(f"`{c}`" for c in gate_cmds)
    return (
        f"You are the reviewer for the merged state of {ids} (the quality gate's integration step).\n"
        f"Each of these tasks was reviewed on its own branch; this is the first time anyone has read "
        f"them as one tree. The combined change is `{diff_cmd}`.\n"
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
        "\n"
        "Do not re-review either task against its own ticket — that already happened, on its own "
        f"branch. Do not run {cmds}: the caller has just run them over this exact tree and decides by "
        "their exit status, and re-running them tells you only what it already knows.\n"
        "\n"
        "**You do not change the code.** You have no write access to it; an implementer resolves what "
        "you find and you look again.\n"
        "\n"
        f"Write your findings to `{findings_path}` and nothing else:\n"
        '  {"findings": [{"severity": "must_fix", "statement": "…", "anchor": "src/x.py:42"}]}\n'
        "`must_fix` is a defect the merged tree cannot land with. `consider` is everything else worth "
        "saying; it stops nothing and is carried to the human at gate ④. An empty list is a real "
        "answer, and the right one when the join is sound."
    )
