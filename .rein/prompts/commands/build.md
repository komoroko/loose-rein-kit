# /build — Implementation phase (autonomous loop consumption)

(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `approval-presentation` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite gate check (always first)
Read `.rein/state.yaml` and confirm `gates.tasks == approved`.
If unapproved, do not work; say "please approve `/tasks` first" and stop.

## The consumption algorithm

`rein build` runs all of this **in code** — none of it is yours to re-derive.

1. **Frontier** — todo tasks whose `blockedBy` are all done, ordered foundation/high fan-out first,
   then the critical path. Foundation tasks run serially on the work branch; independent leaves run
   in `git worktree` isolation (never subtree) up to `max_parallel` (default 3).
2. **Dossier** — each launch is handed `.rein/work/T-NNN.json`, assembled fresh: the claims the task
   answers and what each asserts, its declared `scope`, the changed paths already split into
   source / tests / mechanical churn, and what earlier attempts tried.
3. **Read what the attempt produced before asking the gate anything** — an empty diff is
   `no_implementation`, a change outside the plan's declared `scope` is `scope_violation`, and an
   implementer that ended `rein report --outcome blocked` / `needs-revision` parks the task; in all
   three cases without spending a reviewer or a test suite on it, and `--touched` naming paths the
   diff does not contain is itself a finding. Each verdict is recorded with the **fingerprint of the
   tree it was reached over**, so a later `rein build` re-raises it with `futile:` instead of paying
   for a launch that reaches it again (`rein task reset <T-NNN> --fresh` is how a human says they
   repaired something outside the tree).
4. **DoD** — the `quality_gate` pipeline in `.rein/config.yaml` (default `test` → `check` →
   `review` → `smoke`), the single definition for every task; a task has no `test` command of its
   own. A step already established green against **this exact tree, in this exact image** is reused
   rather than re-run (the evidence ledger). Then the **negative control**: the same command steps
   are re-established over the base this change is a change to, with **only the task's test half
   applied**. If every step is still green, no test in the change exercises it and the green that
   would have closed the task is a fact about code that was already there — so it goes back to the
   implementer like a red step. Read the outcomes for what each is worth: the **green** control is
   the strong one, a fact about every test in the change at once; a **red** one says the test half
   is not inert against the old code and no more, since it cannot separate a failed assertion from
   an import the base never had. A task that changed no test file has no control to take, which is
   **recorded, not passed**, and the record is read: the gate ④ orientation counts the tasks whose
   control answered and **names the ones where it could not be taken**, so "this task's green rests
   on tests nobody wrote for it" reaches the approver instead of sitting in a file. Then the task's own **`acceptance`** criteria are established the same way — a
   failing one returns through the same channel a red gate step does, inheriting the send-back
   budget rather than growing a second one beside it.
5. **Budgets** — a failed `cmd` step goes back to the implementer up to that step's own `retries`
   (over the budget → `blocked`). The two verdicts that are **not** configured steps — the negative
   control, and each acceptance criterion — carry one send-back each: the failure names exactly
   what is missing, and an implementer that cannot answer that in one more launch is saying the
   ticket needs a human (`rein report --outcome needs-revision`). **Only a failure the code earned
   counts**: an agent that never
   launched (capacity exhausted, the CLI not on PATH, a supervisor's signal) or a step that could
   not be run at all (no container runtime, no pinned image) produced no verdict, so it spends no
   budget, marks no task, and stops the run instead. A send-back that *cannot help* is not spent
   either — a step already red at the baseline, or one that failed **identically over a tree the
   implementer did not move**, stops the task at once with `futile:` on its record, read from the
   observation, never from the failure's text (the loop parses no build-tool output).
6. **Land** — gate-check every path the task changed (merge-stage gate guard: a pending-gate path
   escalates as `gate_violation` and blocks the task instead of landing) → merge into work
   sequentially in **ascending-id order** → **when a batch merged 2+ leaves, re-run the cmd steps
   once on the merged work branch**, since each leaf was green only in isolation. A red goes to a
   fixer within the step's `retries` budget, else the batch's tasks block; a single-leaf join skips
   it. The `agent` steps run over the join too, and there the reviewer is asked about **what only
   the join can show — cross-task correctness as much as shape**: the suite that just passed here
   is the union of the leaves' suites, and no test in it was written with this merge in view, so a
   green over the merged tree says nothing about the interaction.
7. **Close** — mark the merged tasks `done`, **each carrying the tree its DoD was established on**
   (`evidence` in `state.yaml`) — or **`awaiting-evidence`** when a criterion nobody here can
   establish is still open, which merges the work and parks only the task, and gate ④ cannot open
   while one stands. Then recompute. An empty frontier with unfinished tasks = all
   blocked/needs-revision → escalate and stop; all done → gate ④ below. **Only the human opens
   `gates.build`.**

## Running it — `rein build`
The installed orchestrator reads `.rein/config.yaml`, `plan.yaml` and `state.yaml`, and launches
its implementer/reviewer agents headless in the **OCI sandbox** via the adapters set by `rein agent <role> <cli>` (default
`claude`), so the requirement is **that CLI installed and authenticated** — any agent (or the
human in a terminal) may invoke `rein build`. At the
start it code-checks `gates.tasks == approved` and stops doing nothing if unapproved.

**This is the only way the implementation phase runs.** There is no hand-driven equivalent: the
loop's guarantees are the code's, `state.yaml` is machine-written (`rein guard` denies edits to
it), and a leaf's decisions reach the audit chain only through the control plane the orchestrator
serves. With no headless CLI on the machine, install one and point the roles at it with
`rein agent <cli>` (`claude` / `codex` / `gemini`, or any command); until then `rein build`
refuses with exit `2` naming what is missing.

```
rein build              # run
rein build --dry-run   # check just the control flow without calling the agent CLI/git
```

It refuses to start (exit `2`) when a document it would send an agent to read has moved since
gate ③ froze it, or has uncommitted changes. The second has no other symptom: a parallel leaf is cut
from the work branch's tip and reads only what is committed there, so an uncommitted ticket edit
reaches no task at all. Commit it, or roll back with `rein revise --to tasks` if the approval no
longer covers it.

It also refuses **before the first agent launch** on anything about the machine that this run
cannot finish without: no container runtime while a step needs an OCI sandbox, a pinned image
nobody built here, an agent CLI that is not on PATH, a `quality_gate` step marked `required:` with
no `command:` to run. Each is reported with the command that repairs it, and all of them at once.

Then it establishes a **baseline**: the DoD's command steps, run once against the work branch as
it stands, before any task has touched it. A step already red there is not a fact about any task,
so a task that fails it is not sent back to an implementer — it stops after one round with
`futile:` on its `task_failed` record, naming what the baseline said. It costs one gate run, cached
by content in the evidence ledger, and it does not refuse: a cycle whose first task is "fix the
failing tests" runs its implementer *before* the gate, so the step goes green and none of it
applies.

**`rein build` is one command, not an iteration** — it runs the whole algorithm to completion
and its exit is the signal. Never schedule wake-ups to poll a run in progress; wait for the
command. The exit code says what to do next, and is meant for an unattended supervisor as much
as a human:

| code | meaning | what to do |
|---|---|---|
| `0` | every task is done | go to gate ④ |
| `1` | a task could not pass the gate, or the frontier is empty with work left | a human reads the escalation |
| `2` | it refused to start, or the machine failed in a way waiting cannot fix (gate ③ unapproved, plan not frozen, the agent CLI not on PATH, an unpinned sandbox image) | repair what it names |
| `3` | the machine failed in a way time fixes — agent capacity exhausted, a signal, another run holding the lock. **No task was marked and no retry budget was spent** | re-run later; it continues from the preserved work |

A **session/usage limit is a normal event**, not an incident. The loop exits `3` immediately
rather than sleeping on it — a limit that lifts in hours has no business holding the build lock and
a set of worktrees — so the waiting belongs to whatever re-runs the command. `rein build
--supervise` carries that recipe in-process (only `3` is retried, each attempt a fresh run against
the current `state.yaml`), so unattended progress survives a session limit with nothing outside the
process watching:

```sh
rein build --supervise   # [--supervise-interval-sec N], default 900

# equivalent, if something outside `rein` should own the interval/backoff instead:
while :; do
  rein build && break
  rc=$?; [ "$rc" -eq 3 ] || exit "$rc"   # anything else needs a human
  sleep 900
done
```

A run stopped that way leaves each unfinished task `todo` with its worktree in place; the next
run finalizes and salvages that work onto the leaf's branch and the implementer **continues**
rather than restarting. `rein start` and `rein doctor` both say so when you come back.

### When the run outlasts your host's command timeout

Every agent host caps how long one tool call may run, and a real build outlasts that cap.
**"Never poll" is not the same as "never wait"**: an agent whose command was cut off starts checking
on the run, and each check is a launch, a context, and a share of the session limit spent on
learning that the build is still building.

**Wait for it. Your capability mapping says how** — every host has one of these, and they are in
this order:

1. **The host re-enters you when it exits.** Start `rein build --supervise` through that mechanism
   and get on with something else; its exit brings you back on its own. No timer, no check, no
   launch spent.
2. **The tool call itself waits.** Run it in the **foreground** with the longest wait your shell
   tool allows — a timeout parameter you can raise, or a cap that counts *silence* rather than
   runtime. The build prints a `[waiting]` line every minute while a launch or a gate step is in
   flight, exactly so that a wait like that can hold: the line costs nothing, and it is what tells
   the host the command is alive.

**Detach only when neither can hold it** — when the run has to outlive this session, or your host
kills a foreground command by the clock no matter what it prints. A host-managed background task
belongs to the host; an orphaned process does not:

```sh
nohup rein build --supervise > .rein/build.log 2>&1 &   # returns immediately; the run owns the lock
```

Then **end your turn** and let the human bring you back. This is the worst of the three: a build
nobody is waiting on is a build that finished hours before anyone read it.

**Never turn a detach into a poll.** Re-entering to ask "is it done yet" spends a launch, a context
and a share of the session limit on learning that the build is still building, and it does that
every time. If you cannot wait, stop — do not check. Either way, when you come back read the
run's own record — never re-invoke the build to find out how it is going:

- `rein start` — what changed since you last looked, and what is waiting on a human
  (`--full` adds the whole board; `rein ui` serves the same thing in a browser)
- `tail -n 40 .rein/build.log` — the run's console output

**Do not re-run `rein build` to check on it.** A second run cannot start while the first holds the
build lock: it exits `3`, which is indistinguishable from a capacity stop, and a supervisor reading
that will sleep on a build that is running perfectly well. Re-run it only after the first has
exited, and only for the exit code it actually returned.

**Set no wake-up timer at all.** A build's unit of progress is a task, which is minutes at the
fastest; a check measures nothing that has changed and costs a context each time.

The non-deterministic parts are each task's implementation code content and the `review` agent
step's fixes. Both are absorbed deterministically: after an agent step changes code, the
already-passed cmd steps are re-run; a red cmd step retries until green, else blocked. With
the claude preset the implementer resumes its own session across its retries (a step's final
retry is forced fresh); the `review` step, the integration fixer, and the security reviewer
always run in **fresh contexts, independent of the implementer** — independent verification
is the point; never fold them into the implementer's session.

## Quality-gate step notes

- **`check`** = the project's lint / format / type-check command (lint / format / type-check,
  all of it). Auto-fixable hooks (ruff/format) resolve on the re-run; manual fixes (mypy, tsc)
  are part of the step. In a project without `make`, substitute that project's commands in the
  config steps.
- **`review`** asks two disciplines — **correctness** (bugs) and **simplification** (reuse,
  needless complexity, and what the ticket's acceptance criteria do not require: speculative
  generality, unused knobs/hooks; YAGNI) — and then reads the **tests as evidence**: for each acceptance criterion, which test in this change would go red if
  the behaviour were wrong, and which assertions would hold for any output at all. The negative
  control below can show that the test half is not *inert*; whether the tests are any *good* is
  asked here and nowhere else. **It reports; it does
  not repair, and it is launched without write access.** Findings go to
  `.rein/work/T-NNN.findings.json`; the implementer resolves the `must_fix` ones within the
  step's own `retries` budget and the reviewer looks again. A review whose findings cannot be read
  stops the step: an unreadable answer is not an answer that found nothing.
  Both disciplines are **named to the host that has them**: under Claude Code the reviewer is
  pointed at `/code-review` and `/simplify`, which read the branch it is on — with the two rules
  those commands do not carry themselves, that `/simplify`'s fix-applying phase must not run here
  (whoever judges does not repair) and that findings come back through the findings file and never
  through a printed report. The questions are written out in the prompt regardless, so a host
  without them asks exactly the same thing (`adapters.Adapter.disciplines`).
  **Declared `stage: integration`, it reads the tree the merge produced instead** — the thing no
  per-task reviewer can see, because each was right to stay inside its own task's scope:
  duplication between what two tasks added, one responsibility now in two places, an abstraction
  one task introduced that the next worked around. Its `must_fix` findings go to the integration
  fixer within the step's own budget; its `consider` findings are filed against the merged task
  whose scope owns the anchor and reach the human at gate ④.
- **`stage:`** on any step says where it runs — `task`, `integration`, or `both` (the default).
  It moves *when* a step runs, never whether: a fast focused suite can guard each task while the
  whole one runs once over the join.
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

0. **If this cycle ships as a stack, publish it now — as drafts.** `rein pr-stack` cuts the work
   branch into one pull request per task and prints the `gh pr create --draft` lines; `--push`
   does it, after a confirmation typed at a terminal. **You never run `--push` for the human** (the
   same rule as `rein approve` — `rein doctor` refuses to let it be pre-authorized). They open as
   drafts because the grounded review below has not run yet, and that is the point: a reviewer can
   start reading one slice at a time while nothing looks approved. Skip this step for a cycle that
   ships as a single pull request.

   From here, **a fix for a review finding is committed onto the slice that introduced the code, not
   onto the work branch.** `rein revise --to build --from-review` derives which task answers each
   blocking finding, `rein build` re-runs those tasks and lands each fix on the pull request that
   owns it, and `rein pr-stack --restack` carries the fixes up the stack by merging. **Never rebase
   a stack**: it strands every `completed_commit` and gate receipt on commits that no longer exist.

1. **Answer any open change requests first.** Run `rein changes list --gate build --json`. Each anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong: **read and edit only the slice it names** — do not re-run the phase over the whole deliverable. Then `rein changes address <id> --note <what you changed>`; the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds gate ④ shut, and approving is what closes the addressed ones.
2. **Generate the grounded review — the artefact gate ④ approves.** Run `rein review
   generate` (bound to the current HEAD). It runs a deterministic Coverage Manifest, a **blind**
   actual-behaviour extraction (never given the plan), the structured security review, and the
   Expected/Actual comparison — writing `.rein/review.yaml` and recording the pipeline events.
   **The change is read in *readings*, not in one sitting**: one per task the plan scopes, plus the
   seam over what two scopes share and what none covers, each launched on its own so one launch
   holds one task's slice. Most of them are already answered — `rein build` takes each task's
   reading as it lands — so a regeneration after a review fix re-reads only the task whose code
   moved. `coverage.composition` records every reading by name and `unread_paths` names any changed
   path none of them covered, which makes the manifest `insufficient`; a composed reading is
   refused outright at critical risk. Set `review_policy.composition: whole` to pay for one reading
   of everything instead. What it reads is the **product**: not `.rein/`, not the plan's own prose
   (the documents gate ③ froze, `docs/tasks/`, the ADRs), not the surfaces `rein install` wrote,
   and — for the blind extractor alone — not the tests. The diff is measured
   against `review_policy.budgets.max_diff_bytes` *before* a model is launched — over
   it the answer is to split the scope (`/revise`), never to grow the request. **Do not wait for
   gate ④ to find that out**: `rein start --full` carries the outlook, `rein doctor` names it, and
   `rein build` says so as each task lands, which is while splitting is still possible.
   Findings sit on three separate axes (integrity / semantic support / conformance); there is no
   single `verified`, and "extra behaviours: 0" appears only with the Coverage Manifest that
   earned it. **Triage it**: a blocking security finding, a diverged high/critical claim, an ungrounded
   high/critical extra behaviour, or an insufficient Coverage Manifest blocks the gate — return
   those to the implementer to fix (a fix moves HEAD, so re-generate; a later commit leaves the
   review stale) and record judgment calls as escalation events for the human. Do not present
   gate ④ while a blocker stands. **A security finding closes itself**: the next generation
   re-checks the code each blocking finding anchored to, and records the finding `resolved` when
   that code is gone — in that generation's findings and in the audit chain, which is where it
   outlives a document the next generation rewrites. Fixing it is the way through, and re-stating
   it is refused only while the code is still there. One that named no anchor is closed by a
   human's `dispute_finding` in the review, never by the reviewer omitting it.
3. `notify-and-wait`: tell the human the gate-④ approval is pending.
   - **(Only with GitHub integration)** Run `rein issue-sync` to reflect each task's
     latest status (done → close, etc.) to Issues. Best-effort; do not stop the gate if it
     fails (auto-skips if `github.enabled: false` / gh/remote absent). It stays outside the
     deterministic loop — networking does not belong there.
4. Present the implementation summary as an **`approval-presentation`** and confirm "may we approve
   this as implementation-complete?". **Open with the review's scope, before any result**: the
   commit range it is bound to, how large the change is, what the review could **not** read, and
   whether it fits the review budget. Then the completed tasks, key additions/changes, test results, **the grounded-review
   results — the three axes and coverage** — and unresolved items. **Do not re-narrate the orient
   stage in chat**: what was delivered, what moved underneath it, which sandbox and network posture
   each gate step ran under, what the gate established, and what is still open are all derived into
   the review itself and read there. Say where it is, not what it says. The human review is
   completed in `rein ui` — the stage rail walks the reviewer from the scope through the orient
   brief and the Decision Cards to the freeze button
   (`.rein/prompts/rules/gate-workflow.md` "The gate ④ human review") — and frozen
   there or with `rein review complete` before the gate can be requested. Unanswered
   high/critical Decision Cards block the freeze; say so rather than presenting the gate.
   - **Smoke-step check**: if the deliverable is runnable (CLI, server, …) and
     `quality_gate`'s `smoke.run` is still empty, say so explicitly at the gate — the DoD ran
     without a launch check — and propose the command to fill in plus `required: true` (the loop
     prints this nudge mechanically at gate ④; with `required: true` set, an empty run refuses
     to build at all — an unnoticed empty smoke silently defeats its purpose).
   - **Always present a self-assessment as well** (`.rein/prompts/rules/gate-workflow.md` "Gate self-assessment"),
     including the outcomes of spots that produced blocked/needs-revision.
5. **While waiting for approval**, only outcome-independent speculative work
   (`.rein/prompts/rules/gate-workflow.md` "While a gate is pending"; record it as
   speculative-work events):
   concretizing functional test cases in `docs/test/test-plan.md`, a trial run of
   `make audit`, and other `/verify` prep pulled forward. Do not make changes that could
   require redoing the implementation.
6. Once a human approves (acknowledging the `approval-presentation`, or an explicit "approve")
   — **running the next command (`/verify`) is not itself approval** — ask the human to run
   `rein approve build` **themselves**, never for them (mechanics: AGENTS.md "Gate rules" 2).
   Declining is recorded as a change request, not lost — see step 0. Point to "next is `/verify`",
   and after committing the gate's deliverables, suggest `session-compaction` (pre-compact check:
   `.rein/prompts/rules/gate-workflow.md` "Context budget").
