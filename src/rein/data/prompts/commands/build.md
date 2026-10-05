# /build — Implementation phase (autonomous loop consumption)

(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `approval-presentation` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Prerequisite check (always first)
Read `.rein/state.yaml` and confirm `gates.mandate == approved`. If it is pending, do not work: say
what the mandate still needs (`rein next` names it) and stop. This is the one gate implementation
waits on — and what it authorizes is the `scope` and the claims, not an order of work. Inside it the
loop decomposes, reorders and re-runs as it needs to.

## The consumption algorithm

`rein build` runs all of this **in code** — none of it is yours to re-derive.

1. **Frontier** — todo tasks whose `blocked_by` are all done, ordered foundation/high fan-out first,
   then the critical path. Every task runs in `git worktree` isolation (never subtree) on its own
   branch and lands by a merge: a foundation task alone, independent leaves up to `max_parallel`
   (default 3) at a time.
2. **Dossier** — each launch is handed `.rein/work/T-NNN.json`, assembled fresh: the claims the task
   answers and what each asserts, its declared `scope`, the changed paths already split into
   source / tests / mechanical churn, and what earlier attempts tried.
3. **Read what the attempt produced before asking the gate anything** — an empty diff is
   `no_implementation`, a change outside the plan's declared `scope` is `scope_violation`, and an
   implementer that ended `rein report --outcome blocked` / `needs-revision` parks the task; in all
   three cases without spending a reviewer or a test suite on it, and `--touched` naming paths the
   diff does not contain is itself a finding. `scope_violation` parks the task **`needs-revision`,
   not `blocked`**: the plan drew the scope, so either the change belongs to another task or the
   scope was too small, and both are answered by a human at `/revise` rather than by an implementer
   trying again. Each verdict is recorded with the **fingerprint of the
   tree it was reached over**, so a later `rein build` re-raises it with `futile:` instead of paying
   for a launch that reaches it again (`rein task reset <T-NNN> --fresh` is how a human says they
   repaired something outside the tree).
4. **DoD** — the `quality_gate` pipeline in `.rein/config.yaml` (default `test` → `check` →
   `review` → `smoke`), the single definition for every task; a task has no `test` command of its
   own. Each leaf runs the **command** steps; the **agent** steps read the batch once, below (5a).
   **The attempt is committed before anything reads it** (`finalize_commit`), so the gate, the
   reviewer and the merge all read one thing — the task's branch — and work an implementer left
   uncommitted is never tested by one reader and missing for the next.
   **A task with `operate` is read before its run**, by the same agent steps, on its own: its run is
   the costly part (hours, real data, or the irreversible act), and a finding answered after it
   would send the task round the run again. Every start of the run after the first in one attempt
   is recorded as an attempt of its own, and one past the task's `attempts.max` is not started. A step already established green against **this exact tree, in this exact image** is reused
   rather than re-run (the evidence ledger). Then the **negative control**: the steps that run the tests
   (`runs_tests: true` — never a linter, whose red is true of any new test file) are re-established
   over the base this change is a change to, with **only the task's test half applied**. If every step is still green, no test in the change exercises it and the green that
   would have closed the task is a fact about code that was already there — so it goes back to the
   implementer like a red step. Read the outcomes for what each is worth: the **green** control is
   the strong one, a fact about every test in the change at once; a **red** one says the test half
   is not inert against the old code and no more, since it cannot separate a failed assertion from
   an import the base never had. A task that changed no test file has no control to take, which is
   **recorded, not passed**, and the record is read: the acceptance orientation counts the tasks whose
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
5a. **Review the batch in one launch** — once every leaf of the batch has finished its
   deterministic gate, one reviewer reads all the leaves that passed, before anything merges, and
   answers **per task**. A leaf's `must_fix` findings go back to that leaf's implementer through
   the send-back above — its own session resumed, the whole deterministic gate established again —
   and only the leaves sent back are read again, from cold, within the step's `retries`. A leaf
   still holding a `must_fix`, or one the reviewer wrote no entry for, does not land; the rest of
   the batch does. Independence needs a launch that is not the implementer's, not a launch per
   task.
6. **Land** — gate-check every path the task changed (merge-stage gate guard: a pending-gate path
   escalates as `gate_violation` and blocks the task instead of landing) → merge into work
   sequentially in **ascending-id order** → **when a batch merged 2+ leaves, re-run the cmd steps
   once on the merged work branch**, since each leaf was green only in isolation. A red goes to a
   fixer within the step's `retries` budget, else the batch's tasks block; a single-leaf join skips
   it. A `stage: integration` agent step reads every join, and a `stage: both` one reads it again
   only when a merge had to resolve a conflict — otherwise the join is exactly the union the
   batch's reviewer already read. There the reviewer is asked about **what only the join can show —
   cross-task correctness as much as shape**: the suite that just passed here is the union of the
   leaves' suites, and no test in it was written with this merge in view.
7. **Close** — mark the merged tasks `done`, **each carrying the tree its DoD was established on**
   (`evidence` in `state.yaml`) — or **`awaiting-evidence`** when a criterion nobody here can
   establish is still open, which merges the work and parks only the task, and the acceptance gate cannot open
   while one stands. Then recompute. An empty frontier with unfinished tasks = all
   blocked/needs-revision → escalate and stop; all done → the grounded review below, then `/verify`.
   **Only the human opens `gates.acceptance`.**

## Running it — `rein build`
The installed orchestrator reads `.rein/config.yaml`, `plan.yaml` and `state.yaml`, and launches
its implementer/reviewer agents headless in the **OCI sandbox** via the adapters set by `rein agent <cli> [--role <role>]` (default
`claude`), so the requirement is **that CLI installed and authenticated** — any agent (or the
human in a terminal) may invoke `rein build`. At the
start it code-checks `gates.mandate == approved` and stops doing nothing if unapproved.

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
the mandate gate froze it — commit it, or roll back with `rein revise --to mandate` if the approval no longer
covers it.

It also refuses on **any** uncommitted change in the working tree. Every task forks from the last
commit on the work branch and is merged back into this checkout, so an uncommitted edit is in no
task's tree, no gate ran over it, and a merge that touches it fails after the task passed. Commit or
stash first.

A task's change is what its own branch holds, never "whatever landed on the work branch since it
started". Committing the orchestration record between two attempts (a rollback, the design and tasks
deltas, an approval) is therefore never charged to a task that is still unlanded, and a blocked task
does not hold the work branch: tasks that do not depend on it keep running.

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

A step that runs the tests and declares `junit:` (the JUnit XML report its command writes) is read
**per failing test** instead of per step. A red is re-run once on the same tree: green the second
time is a flaky test, recorded against the task that owns it, and it stops nothing. A red that
holds is run again on the tree the task forked from: a test red there too was red before this
change, so it goes to the task whose `scope` holds the test — back on the frontier if it is `done`
and nothing stands on it, to a person otherwise — and the task under test is not charged. Only a
test this change turned red, or one in the task's own scope, is its failure. Whether the change
imports the failing test's code is never asked: a behaviour can break a test through no import.

**`rein build` is one command, not an iteration** — it runs the whole algorithm to completion
and its exit is the signal. Never schedule wake-ups to poll a run in progress; wait for the
command. The exit code says what to do next, and is meant for an unattended supervisor as much
as a human:

| code | meaning | what to do |
|---|---|---|
| `0` | every task is done | run `/verify` |
| `1` | a task could not pass the gate, or the frontier is empty with work left | a human reads the escalation |
| `2` | it refused to start, or the machine failed in a way waiting cannot fix (the mandate gate unapproved, plan not frozen, the agent CLI not on PATH, an unpinned sandbox image) | repair what it names |
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
step's findings. Both are absorbed deterministically: a finding goes back through the same
send-back a red step takes, and the whole deterministic gate runs again over the fixed tree; a red
cmd step retries until green, else blocked. With
the claude preset the implementer resumes its own session across its retries (a step's final
retry is forced fresh), and a task with exactly one upstream task starts by **forking** the session
that finished that upstream rather than reading the codebase from cold. The fork leaves the
upstream's session as it was, so two leaves under one foundation never see each other's
conclusions. Which session finished which task is a cache outside the working tree
(`$XDG_CACHE_HOME/rein/<repo>/sessions.json`); a miss is a cold start and nothing else. The
`review` step, the integration fixer, and the reviewer of the whole change
always run in **fresh contexts, independent of the implementer** — independent verification
is the point; never fold them into the implementer's session.

## Quality-gate step notes

- **`check`** = the project's lint / format / type-check command (lint / format / type-check,
  all of it). Auto-fixable hooks (ruff/format) resolve on the re-run; manual fixes (mypy, tsc)
  are part of the step. In a project without `make`, substitute that project's commands in the
  config steps.
- **The `review` step** asks every review `.rein/reviews.yaml` adds at `build` (not
  `config.yaml`; none added, no step) — the packaged **adversarial** (an attempt to refute the
  change: the input that breaks it, the claim with no evidence behind it, the side that did not
  change; the packaged default reads for this alone), **correctness** (bugs), **simplification**
  (reuse, needless complexity, and what the ticket's acceptance criteria do not require:
  speculative generality, unused knobs/hooks; YAGNI) and **security**, and any custom review the
  file names with a question of its own — and then
  reads the **tests as evidence**: for each acceptance criterion, which test in this change would go red if
  the behaviour were wrong, and which assertions would hold for any output at all. The negative
  control below can show that the test half is not *inert*; whether the tests are any *good* is
  asked here and nowhere else. **It reports; it does
  not repair, and it is launched without write access.** One launch reads the whole batch (5a),
  and its findings go to `.rein/work/review.<task ids>.findings.json` with one entry per task; each task's
  implementer resolves its `must_fix` ones within `review_policy.repair_rounds` and the reviewer
  looks again at the tasks sent back. A review whose findings cannot be read holds back every task
  it was reading: an unreadable answer is not an answer that found nothing.
  **The code-stage lenses this cycle's mandate froze reach it too** — `rein lens --select code
  --task <T-NNN>`, read back from `plan.lenses` and narrowed to the task in hand. The selection is
  resolved once against the whole plan, because most conditions count what the plan states rather
  than what one task changes; `--task` then drops the ones whose `paths` fall outside that task's
  own scope — resolved against the files that scope covers, since an entry names a subtree and a
  lens pattern matches a file, and a task that declares no scope is unbounded and keeps all of them. Without it a single task touching a schema file would put the schema lens on every
  other task's review, and a reviewer sent to attack a failure this change cannot carry costs a
  pass over the diff and returns "attacked, nothing" while the findings that *are* possible compete
  with it for attention. The narrowing only ever removes: a path matching one task's scope matches
  the union too, so nothing outside the frozen selection can reach a reviewer through it.
  Each lens the reviewer actually applies is recorded with `rein lens --record <id> --found
  <yes|no> --stage code --task <T-NNN>` — no `--task` for the integration reading, which is about
  the merged tree and not about one task. **`--stage` and `--task` are what say *where* it was
  applied**: without them the count is still right and `rein lens --grid` cannot place the row, so
  a lens applied to one task reads the same as one applied to the whole cycle.
  The disciplines are **named to the host that has them**: under Claude Code the reviewer is
  pointed at `/code-review`, `/simplify` and `/security-review` — only those for the reviews the
  step lists, reading the branch it is on — with the two rules
  those commands do not carry themselves, that `/simplify`'s fix-applying phase must not run here
  (whoever judges does not repair) and that findings come back through the findings file and never
  through a printed report. The questions are written out in the prompt regardless, so a host
  without them asks exactly the same thing (`adapters.Adapter.disciplines`).
  **Declared `stage: integration`, it reads the tree the merge produced instead** — and takes the
  selection unnarrowed (`rein lens --select code`, no `--task`), because the union of every task's
  scope is exactly what it is reading. The thing the batch's reviewer could not see, because the
  merged tree did not exist yet:
  duplication between what two tasks added, one responsibility now in two places, an abstraction
  one task introduced that the next worked around. Its `must_fix` findings go to the integration
  fixer within the step's own budget; its `question` findings are filed against the merged task
  whose scope owns the anchor and reach the human at the acceptance gate.
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

## When all tasks complete

0. **If this cycle ships as a stack, publish it now — as drafts.** `rein pr-stack` cuts the work
   branch into one pull request per task and prints the `gh pr create --draft` lines; `--push`
   does it, after a confirmation typed at a terminal. **You never run `--push` for the human** (the
   same rule as `rein approve` — `rein doctor` refuses to let it be pre-authorized). They open as
   drafts because the grounded review below has not run yet, and that is the point: a reviewer can
   start reading one slice at a time while nothing looks approved. Skip this step for a cycle that
   ships as a single pull request.

   From here, **a fix for a review finding is committed onto the slice that introduced the code, not
   onto the work branch.** `rein build` reads the change, repairs the findings a task's declared
   scope owns and commits each **where a review fix belongs**: on the slice that introduced the
   code when this cycle is a stack — in a worktree on that slice's branch, then carried up into the
   work branch by merging — and on the work branch itself when it is a single pull request. **Never
   rebase a stack**: it strands every `completed_commit` and gate receipt on commits that no longer
   exist, which is why the fix goes down to its slice and merges up rather than the history moving.
   `rein pr-stack --restack` is the same walk, for a fix you commit onto a slice yourself.

1. **Answer any open change requests first.** Run `rein changes list --gate acceptance --json`. Each anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong: **read and edit only the slice it names** — do not re-run the phase over the whole deliverable. Then `rein changes address <id> --note <what you changed>`; the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds the acceptance gate shut, and approving is what closes the addressed ones.
2. **The grounded review is taken by the run itself, and the run repairs what it may.** `rein
   build` ends by reading the change, repairing every open finding a task's declared scope owns
   **whatever its severity** (severity decides whether a finding holds the gate shut, not whether
   it is repaired), and reading it again from cold — up to `review_policy.repair_rounds` (default
   2). No gate moves: a repair changes no requirement, no claim and no plan, and `rein guard`
   denies a write to `plan.yaml` or `config.yaml` while the plan is frozen, so it cannot become
   one. **A repair is made at the cause and proven.** It may write into another task's paths — the
   mandate covers them — and, while it runs, past the mandate's `include` (never into `exclude`);
   each path it writes past `include` comes to you as a card at acceptance, to adopt into the scope
   or refuse, and a refused one is taken back out by the next `rein build`. It lands only if a test
   it adds fails against the code as it was: the run applies only the repair's test changes to the
   commit before it and runs the tests there, and a repair with no test, or whose tests pass there,
   is undone and the reason handed to the next round. A finding no round could prove repaired is
   what reaches you. Whether a finding closed is decided by the **next** reading — a blind one
   with no memory of having raised it — never by the fixer's account of its own work. What reaches
   you is what a machine cannot decide: whether a `diverged` claim means the code is wrong or the
   plan is, and whether an extra behaviour nobody asked for is unwanted. **Answering such a card
   `revise_implementation` hands that subject back to the loop** — it is you saying the code is
   the mistaken half — and the next `rein build` repairs it like any other finding. Run `rein
   review generate` yourself when the reading could not be taken, or when `repair_rounds` is 0.

   The reading runs a deterministic Coverage Manifest, a **blind**
   actual-behaviour extraction (never given the plan), the reviews `reviews.yaml` adds at
   `acceptance` read over the whole change, and the Expected/Actual comparison — each only while
   `reviews.yaml` asks for it — writing `.rein/review.yaml` and recording the pipeline events.
   **The change is read in *readings*, not in one sitting**: one per dependency chain the plan
   scopes — a line of tasks each built on the one before and on nothing else, read as the one
   change it is, and task by task only when the chain's diff will not fit `max_diff_bytes` — plus
   the seam over what two readings share and what none covers, each launched on its own. Most of
   them are already answered — `rein build` takes a reading as the last of its tasks lands — so a
   regeneration after a review fix re-reads only the reading whose code moved. **A blocking
   security finding that reading turns up is repaired at once only when it is about the task whose
   work is the tip of the branch** — once, with whatever still stands left to the repair rounds
   above. A repair is committed on top of the branch, and a stack is cut along the tasks'
   `completed_commit`s, so a fix for any other task (an earlier task of a chain, a leaf another
   leaf merged on top of) would sit in the wrong pull request; those are repaired at acceptance,
   on their own slice. The readings are cut from the task graph with the order and scope added
   after the mandate (`rein task order`, `rein task scope-add`), the same one `rein dag` counts.
   `coverage.composition` records every reading by name and `unread_paths` names any changed
   path none of them covered, which makes the manifest `insufficient`; a composed reading is
   refused outright at critical risk. The readings are taken **highest-risk first**, by the
   deterministic detector's own floor for each slice, so a run cut short leaves the most
   consequential ones answered. Set `review_policy.composition: whole` to pay for one reading
   of everything instead. What it reads is the **product**: not `.rein/`, not the plan's own prose
   (the documents the mandate gate froze, `docs/tasks/`, the ADRs), not the surfaces `rein install` wrote,
   and — for the blind extractor alone — not the tests. **Each reading** is measured
   against `review_policy.budgets.max_diff_bytes` *before* a model is launched — that budget bounds
   one launch, not one cycle, so a reading over it is a task whose scope is too broad to read, and
   the answer is to narrow it at the mandate gate, never to grow the request. **Do not wait for
   the acceptance gate to find that out**: `rein start --full` carries the outlook, `rein doctor` names it, and
   `rein build` says so as each task lands, which is while splitting is still possible.
   A run is dozens of launches over hours: it prints `[review] N/M <stage>[T-NNN]` as each stage
   lands, and `rein ui` shows the same figure live, so a human need not watch the terminal.
   Findings sit on three separate axes (integrity / semantic support / conformance); there is no
   single `verified`, and "extra behaviours: 0" appears only with the Coverage Manifest that
   earned it. **Triage it**: a blocking security finding, a diverged high/critical claim, an ungrounded
   high/critical extra behaviour, or an insufficient Coverage Manifest blocks the gate — return
   those to the implementer to fix (a fix moves HEAD, so re-generate; a later commit leaves the
   review stale) and record judgment calls as escalation events for the human. Do not present
   the acceptance gate while a blocker stands. **A security finding closes itself**: the next generation
   re-checks the code each carried blocking finding anchored to, and records the finding `resolved` when
   that code is gone — in that generation's findings and in the audit chain, which is where it
   outlives a document the next generation rewrites. Fixing it is the way through, and re-stating
   it is refused only while the code is still there. One that named no anchor is closed by a
   human's `dispute_finding` in the review, never by the reviewer omitting it.
3. **(Only with GitHub integration)** Run `rein issue-sync` to reflect each task's latest status
   (done → close, etc.) to Issues. Best-effort; do not stop if it fails (auto-skips if
   `github.enabled: false` / gh/remote absent). It stays outside the deterministic loop —
   networking does not belong there.
4. Point to "next is `/verify`": it adds the test plan and the dependency audit, and it is the one
   procedure that presents the acceptance gate. Commit what this phase wrote, and suggest
   `session-compaction` (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget").
