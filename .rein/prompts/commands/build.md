# /build — Implementation phase (autonomous loop consumption)

(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `role-delegation` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite gate check (always first)
Read `.rein/state.yaml` and confirm `gates.tasks == approved`.
If unapproved, do not work; say "please approve `/tasks` first" and stop.

## The consumption algorithm (one definition, both modes)

Both modes run the same deterministic loop — only who runs the machinery differs. Compute the
frontier (todo tasks whose `blockedBy` are all done) → sort the consumption order
(foundation/high fan-out first — the sooner done, the more leaves free up; then the critical
path) → foundation tasks run serially on the work branch (isolating them would strand
derivatives on a stale base); independent leaves run in `git worktree` isolation (never
subtree) at up to `max_parallel` (default 3) in parallel → for each task, run the quality-gate
pipeline `quality_gate` in `.rein/config.yaml` — the single definition of the DoD
(default: `test` → `check` → `review` → `smoke`). A task has no `test` command of its own: the
DoD is shared and runs in a sealed sandbox, never a command the implementer chose for itself.
Each `cmd` step is gate-decided by exit code; a fail goes back to the implementer up to **that
step's own `retries` budget** (over the budget → `blocked`) → **gate-check every path the task
changed** (merge-stage gate guard — a pending-gate path escalates as `gate_violation` and
blocks the task instead of landing) → merge into work sequentially in **ascending-id order** →
**when a batch merged 2+ leaves, re-run the cmd steps once on the merged work branch** — the
integration gate, not a knob: each leaf was green only in isolation and the combined file set can
still be red. A red goes to a fixer within the step's `retries` budget, else the batch's tasks
block; a single-leaf join skips it (its tree is identical to the already-verified worktree) →
mark the merged tasks `done` → recompute. Empty frontier
with unfinished tasks = all blocked/needs-revision → escalate and stop; all done → gate ④
below. **Only the human opens `gates.build`.**

### A. Deterministic execution (recommended; requires a headless agent CLI) — `rein build`
The installed orchestrator runs the algorithm **in code** from `.rein/config.yaml`,
`plan.yaml`, and `state.yaml` — not LLM discretion. It launches its implementer/reviewer agents
headless in the **OCI sandbox** via the adapters set by `rein agent <role> <cli>` (default
`claude`), so the requirement is **that CLI installed and authenticated** — any agent (or the
human in a terminal) may invoke `rein build`. At the
start it code-checks `gates.tasks == approved` and stops doing nothing if unapproved.

```
rein build              # run
rein build --dry-run   # check just the control flow without calling the agent CLI/git
```

The non-deterministic parts are each task's implementation code content and the `review` agent
step's fixes. Both are absorbed deterministically: after an agent step changes code, the
already-passed cmd steps are re-run; a red cmd step retries until green, else blocked. With
the claude preset the implementer resumes its own session across its retries (a step's final
retry is forced fresh); the `review` step, the integration fixer, and the security reviewer
always run in **fresh contexts, independent of the implementer** — independent verification
is the point; never fold them into the implementer's session.

### B. Interactive loop — `autonomous-build-iteration` of /build (fallback: the lead runs the algorithm by hand)
For when the orchestrator can't run (no headless CLI on the machine) or the human wants to
drive in conversation. Start it with your environment's `autonomous-build-iteration` mechanism
(see your capability mapping), or simply repeat the algorithm above iteration by iteration
until the frontier is empty. Leaves go by **`role-delegation` to the `implementer` role with
`git worktree` isolation**, each reporting its branch name for the merge; merge conflicts are
resolved by the implementer at the merge point — *if your environment cannot delegate or
isolate, run the leaves serially on the work branch, adopting the implementer role inline per
its role file*. Behavior is identical to A — what changes is who runs the machinery, which
puts five duties on the lead that mode A does in code:

0. **Take the batch from `rein dag --frontier`, never from your own reading of the plan.** It
   returns only tasks whose every `blocked_by` is already `done`, in consumption order, so a
   task cannot be started on top of unfinished upstream work and no two tasks in one batch can
   depend on each other. Reading the DAG yourself and picking what "looks ready" is the one way
   to break an ordering mode A cannot break — cap the batch at `max_parallel` and take the
   prefix.
1. **Run every gate decision yourself and read its exit status.** A delegated implementer's
   textual "green" report is not evidence, even when it pastes output — summarized or elided
   pastes have hidden real failures. Run the pipeline steps in the algorithm's order yourself
   (reds go back to the implementer within each step's `retries` budget), **including the
   integration gate after the ascending-id merges** — by hand, it is the effective backstop
   against green-report inaccuracy.
2. **Check each worktree's branch base.** A delegated worktree may branch from the default
   branch rather than the work branch; the implementer then lacks the foundation tasks'
   deliverables and must first pull the work branch in (`git merge`, `--ff-only` if possible)
   before implementing. (Mode A branches from the work branch, so this never arises there.)
3. **Keep the records by hand.** Statuses (`in-progress` → `done`/`blocked`) in `state.yaml.tasks` as
   you go — never `done` with any DoD step unmet; blocked/needs-revision recorded through the
   control plane (`rein decision add` / `rein knowledge-gap add` — the write paths that
   reach the audit chain; `rein events` is read-only); per-task commits
   **`T-NNN: <summary>`** (one commit = one task — the worktree's commits are exactly that
   task's diff, which is what scopes the review step); merged worktrees cleaned up; `state.yaml`
   and its `updated_at` refreshed each iteration. A newly discovered dependency or task
   split is a plan change — take it through `/revise` (the plan is frozen at gate ③); an upstream (requirements/design) defect is
   `needs-revision` + escalation — never fixed on your own; roll back via `/revise` at the
   human's discretion.
   **Write down what a re-run would need.** A delegated implementer's context dies with the
   session, so before you delegate a task read `state.yaml.tasks.<id>.handoff` and pass what it
   holds into the delegation (which gate step failed last, what it said, how much of the step's
   `retries` budget is actually left, and any salvage branch holding an interrupted attempt's
   commits). When an iteration ends with a task unfinished, record the same there. Without it a
   build picked up in another terminal starts the task cold, with a full budget it has already
   spent. (Mode A writes and reads this record itself.)
4. **Session hygiene.** At a layer boundary, when the conversation is heavy with re-run
   output, you may suggest `session-compaction` — only when no task is `in-progress`, merges
   are committed and marked `done`, and observations are recorded in tickets / `state.yaml`
   (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget"; the SSOT rehydrates the next iteration).
   Never mid-retry or while a worktree awaits its merge. (Mode A runs in separate processes
   and needs none of this.)

## Quality-gate step notes (both modes)

- **`check`** = the project's lint / format / type-check command (lint / format / type-check,
  all of it). Auto-fixable hooks (ruff/format) resolve on the re-run; manual fixes (mypy, tsc)
  are part of the step. In a project without `make`, substitute that project's commands in the
  config steps.
- **`review`** applies the **`/code-review`** (bugs/correctness) and **`/simplify`** (reuse/
  simplification/efficiency — including stripping what the ticket's acceptance criteria do not
  require: speculative generality, unused knobs/hooks; YAGNI) disciplines and fixes findings
  in place; if code changed, the already-passed cmd steps are re-run. (In an environment
  without those commands, perform an equivalent review pass along the same two axes.)
- **`smoke` (runnable deliverables only)** — for CLI, server, etc., minimally confirm it
  actually launches and the main commands/endpoints work. Tests can be green while the launch
  path (packaging, entry point, dependency resolution) is broken; this catches that within
  build. If it cannot launch, set `blocked` or add a task that makes it launchable. **Fill a
  provisional `smoke.run` as soon as any entry point launches — don't wait for the integration
  task** — and note it in the foundation task's Notes. Once the deliverable is runnable, also
  set the step's **`required: true`** (the human decision knob): from then on an empty `run`
  makes `rein build` refuse to start instead of silently skipping the launch check.
  Register that command's execution permission in the product's committed permission settings
  (`command-preauthorization`) so the smoke step doesn't re-prompt every loop.

## When all tasks complete (gate ④)

0. **Answer any open change requests first.** Run `rein changes list --gate build --json`. Each one anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong — that anchor is the point. **Read and edit only the slice it names**; do not re-run the phase over the whole deliverable and regenerate text nobody complained about. Then `rein changes address <id> --note <what you changed>`: the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds gate ④ shut, and approving is what closes the addressed ones.
1. **Generate the grounded review — the artefact gate ④ approves.** Run `rein review
   generate` (bound to the current HEAD). It runs a deterministic Coverage Manifest, a **blind**
   actual-behaviour extraction (never given the plan), the Expected/Actual comparison, and the
   structured security and maintainability review — writing `.rein/review.yaml` and recording
   the pipeline events.
   Findings sit on three separate axes (integrity / semantic support / conformance); there is no
   single `verified`, and "extra behaviours: 0" appears only with the Coverage Manifest that
   earned it. **Triage it**: a blocking security finding, a diverged high/critical claim, an ungrounded
   high/critical extra behaviour, or an insufficient Coverage Manifest blocks the gate — return
   those to the implementer to fix (a fix moves HEAD, so re-generate; a later commit leaves the
   review stale) and record judgment calls as escalation events for the human. Do not present
   gate ④ while a blocker stands.
2. `notify-and-wait`: tell the human the gate-④ approval is pending.
   - **(Only with GitHub integration)** Run `rein issue-sync` to reflect each task's
     latest status (done → close, etc.) to Issues. Best-effort; do not stop the gate if it
     fails (auto-skips if `github.enabled: false` / gh/remote absent). It stays outside the
     deterministic loop — networking does not belong there.
3. Present the implementation summary as an **`approval-presentation`** and confirm "may we approve
   this as implementation-complete?". **Open with the review's scope, before any result**: the
   commit range it is bound to, how large the change is, what the review could **not** read, and
   whether it fits the review budget. A reader who does not know the boundary cannot weigh what
   follows. Then the completed tasks, key additions/changes, test results, **the grounded-review
   results — the three axes, coverage, and the Challenge-first human review** — and unresolved
   items. The human review is completed in `rein ui` — the stage
   rail walks the reviewer from the unprimed challenge through the Decision Cards to the freeze
   button (`.rein/prompts/rules/gate-workflow.md` "The gate ④ human review") — and frozen
   there or with `rein review complete` before the gate can be requested. Unanswered
   high/critical Decision Cards block the freeze; say so rather than presenting the gate.
   - **Smoke-step check**: if the deliverable is runnable (CLI, server, …) and
     `quality_gate`'s `smoke.run` is still empty, say so explicitly at the gate — the DoD ran
     without a launch check — and propose the command to fill in plus `required: true` (mode A
     prints this nudge mechanically at gate ④; with `required: true` set, an empty run refuses
     to build at all — an unnoticed empty smoke silently defeats its purpose).
   - **Always present a self-assessment as well** (`.rein/prompts/rules/gate-workflow.md` "Gate self-assessment"),
     including the outcomes of spots that produced blocked/needs-revision.
4. **While waiting for approval**, only outcome-independent speculative work
   (`.rein/prompts/rules/gate-workflow.md` "While a gate is pending"; record it as
   speculative-work events):
   concretizing functional test cases in `docs/test/test-plan.md`, a trial run of
   `make audit`, and other `/verify` prep pulled forward. Do not make changes that could
   require redoing the implementation.
5. Once a human approves (acknowledging the `approval-presentation`, or an explicit "approve")
   — **running the next command (`/verify`) is not itself approval** — ask the human to run
   `rein approve build` **themselves**. It checks readiness, prints the digests the approval
   would cover plus any change requests it would close, and asks `[y/N]` at their terminal
   (default no). They may instead approve in `rein ui`, whose write session comes from the launch
   link printed to the terminal it runs in; the receipt records which channel confirmed.
   Declining is recorded as a change request, never lost. Never edit a gate line yourself and
   never run the approve command for them (mechanics: AGENTS.md "Gate rules" 2). Point to
   "next is `/verify`", and after committing the gate's deliverables, suggest
   `session-compaction` (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget").

## Monitoring long-running loops (optional)
When running long in the background, you may periodically notify the human of progress
(equivalent to /status) if your environment has a scheduling mechanism (see your capability
mapping).
