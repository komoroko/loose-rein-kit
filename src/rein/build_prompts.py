"""The prompt texts build_loop hands to its headless agents — pure builders, no orchestration state.

One function per headless launch (implementer, review step, integration fixer, security
reviewer). Kept apart from the Orchestrator so the wording can be read, diffed, and tested
without threading through its git/worktree machinery; the Orchestrator's `_*_prompt` methods
are thin delegates that pass in the few facts a prompt actually needs.
"""

from __future__ import annotations

from collections.abc import Sequence

from rein import dag


def _gate_list(gate_cmds: Sequence[str]) -> str:
    return " and ".join(f"`{c}`" for c in gate_cmds) or "the quality-gate commands"


def implementer_prompt(task: dag.Task, failure_log: str, *, gate_cmds: Sequence[str], has_baseline: bool) -> str:
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
    prompt = (
        f'You are the implementer subagent. Your only task is {task.id} "{task.title}".\n'
        f"Read docs/tasks/{task.id}.md, {design_ref}, and the existing code, and implement "
        f"following the protocol in .rein/prompts/agents/implementer.md.{baseline_ref}\n"
        f"{task_test_ref}"
        f"Write automated tests and get {_gate_list(gate_cmds)} green.\n"
        "When done, commit your changes to this branch (excluding the orchestration state .rein/):\n"
        f"  git add -A -- . ':(exclude).rein' && git commit -m \"{task.id}: <summary>\"\n"
        "Do not reach outside scope (other tasks' territory). If you find a requirements/design defect, "
        "do not fix it on your own — report it."
    )
    if failure_log:
        # failure_log is already a compact summarize_failure() output (salient lines, budget-capped),
        # so it is passed through as-is — no crude tail-slicing that could cut the actionable lines.
        prompt += f"\n\nResolve the previous quality-gate failure:\n{failure_log}"
    return prompt


def review_prompt(
    task: dag.Task, *, gate_cmds: Sequence[str], changed_paths: Sequence[str] = (), diff_cmd: str = ""
) -> str:
    # Scope the reviewer's read to the task's actual diff: it runs in a fresh context (independent
    # verification — deliberately not the implementer's session), and without this hint it must
    # re-survey the tree cold to even find the changes it is reviewing.
    cmds = ", ".join(f"`{c}`" for c in gate_cmds)
    if changed_paths:
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
        "Review this branch's changes for this task for correctness bugs (the /code-review discipline), "
        "then simplify: reuse existing code, remove needless complexity, and strip what the ticket's "
        "acceptance criteria do not require — speculative generality, unused knobs/hooks (YAGNI; the "
        "/simplify discipline). Apply the fixes directly.\n"
        "Stay within this task's scope; if you find a requirements/design defect, report it instead of fixing it.\n"
        f'If you change anything, commit with the "{task.id}: " prefix and keep {cmds} green.'
    )


def integration_fix_prompt(ids: str, failure_log: str, *, gate_cmds: Sequence[str]) -> str:
    return (
        f"You are the integration fixer. The independent leaf tasks {ids} each passed the quality gate "
        "in their own isolated worktrees, but after merging them into this work branch the combined "
        "state fails the deterministic gate. Fix the integration failure below (typically a cross-file "
        "lint/format/type error, or the tasks' changes interfering) with the minimal change — do not "
        "widen scope or redo the tasks themselves.\n"
        "Commit your fix to this branch (excluding the orchestration state .rein/):\n"
        f"  git add -A -- . ':(exclude).rein' && git commit -m \"{ids}: integration fix\"\n"
        f"Keep {_gate_list(gate_cmds)} green.\n\n"
        f"Resolve this integration failure:\n{failure_log}"
    )
