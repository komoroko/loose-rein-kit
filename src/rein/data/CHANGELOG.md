# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`rein upgrade`
shows the sections between the installed version, recorded in `.rein/rein.lock`, and the
new one). `pyproject.toml [project] version` is the single version source.

## [0.2.2] - 2026-08-12

One reported defect, and the two things it turned out to be sitting on: a category error in what
the loop is allowed to record, and a recovery path that was complete but unreachable.

### `rein build` no longer records a machine's failure as a task's verdict (#8)

Every agent-launch site raised `StopLoop` on any nonzero rc — an exhausted session limit, a
`claude` that is not on PATH, a supervisor's SIGTERM — which unwound past the step retry budgets
and marked the task `blocked` on its **first** invocation, with none of the budget the pipeline
already has machinery for. `_run_cmd_step` made the same mistake from the other side, summarizing
an `ExecutorError` (no container runtime, an unpinned image) as if the code had failed the gate.

Two consequences, and the second is the worse one. `task_failed` and `knowledge_gap` are both
`ATTENTION_EVENTS`, so a rate limit left a permanent unresolved escalation on gate ⑤'s screen, in
a log that is append-only by design. And `blocked` takes a task off the frontier — which is the
one place 0.2.1's salvage/restore machinery can ever run from. **A build stopped by a session
limit therefore parked the task somewhere no re-run would collect it**: the recovery was finished
and could not be reached.

The line is now drawn once, in `faults.py`, as a pure function of `(rc, output)`. An agent launch
can never be classified as the code's fault — launching produces no quality-gate verdict, so a
nonzero rc is by construction about the machine — and a cmd step is the code's fault unless it
could not be run at all. An environment fault leaves the task exactly as it was found (status,
attempts, retry budget, handoff), keeps its worktree standing for the next run to salvage, and
stops the run rather than feeding the next task to the same broken machine. Leaves that did pass
their gate still finalize, gate-check and merge: their evidence is real.

### An unattended re-run can tell "wait" from "give up"

A session limit on a build of any length is close to certain, and people meet it unattended —
something re-runs `rein build` from another terminal afterwards. The exit code is all that
decision has to go on, and it conflated "the gate is unapproved" with "another run holds the
lock". Now: `0` done, `1` a real verdict needs a human, `2` repair something first, `3` nothing is
broken, re-run later. `3` covers capacity exhaustion, an external signal, and a held build lock.

The loop does **not** sleep on a capacity limit: one that lifts in hours has no business holding
the build lock and a set of worktrees, so it exits at once and the waiting belongs to whatever
re-runs it (both READMEs and `build.md` carry the supervisor loop). `rein resume` and `rein
doctor` report the stop when you come back, because a run that marks nothing correctly leaves a
repository that looks exactly as it did before.

### `rein task reset` — the write path `state.yaml`'s own rule presumes

`state.yaml` is written only inside a Central Store transaction and `rein guard` denies a hand
edit, but a human deciding a blocked task should be tried again had nowhere to record that. The
troubleshooting section told them to edit `state.yaml`, which the guard refuses; what was left
was calling an internal function from a Python shell. The status change and the typed `--reason`
now land in one transaction. It keeps the handoff by default — a task that cannot pass must not
earn an unlimited allowance by being reset in a loop — and `--fresh` discards it and says so. It
does not close the escalation, and it cannot declare a task `done`.

### `autonomous-build-iteration` is mode B's capability, not mode A's

It means "re-invoke the procedure each iteration", and the mapping files assigned it to both
modes. Mode A is one command whose completion is the signal; naming a polling mechanism there
invites waiting on a run by waking up to look at it. All four mapping surfaces now scope it to
mode B, and `build.md` says scheduling is for telling a human how it is going.

### The security review stops waiting behind the extraction

`rein review generate` runs three LLM stages at up to fifteen minutes each. The comparator
genuinely reads what the blind extractor produced; the security review reads only the diff and
the relevant code and ran last for no reason but the order the calls were written in. It now runs
alongside the chain. An optimization, not a correctness fix — the review's independence
properties are unchanged, and the results merge and the events append in a fixed order.

## [0.2.1] - 2026-08-07

Two reported defects that made `rein build` unusable past its first stumble, one thing the board
could not say, and one thing no run could tell the next one.

### `rein build` no longer dies at the moment it has something to tell you (#5)

`_escalate()` passed the escalation's *kind* (`blocked`, `no_runnable`, `gate_violation`,
`integration_red`) straight through as the audit chain's *event type*. None are members of
`EVENT_ORDER`, so **every escalation path raised out of `event_chain.make`** — a blocked leaf, a
deadlocked frontier, a gate-guard violation, a red integration gate all killed the orchestrator
with a traceback instead of recording the escalation and stopping. Escalations are now recorded
as `knowledge_gap` (what `rein events --summary` lists as still open) with the kind in the
detail, the same shape `set_task_status` already used for statuses. A batch escalation records
one subject per task rather than one comma-joined string, which overran the schema's 64-character
subject at eleven leaves.

### A sandboxed gate step can find its git again (#6)

A leaf runs in a `git worktree`, whose `.git` is a *file* naming the main repository's
`.git/worktrees/<id>` by absolute host path. The OCI mount bound only the checkout, so that
redirect pointed at nothing inside the container and **every gate step that shells out to git
failed identically on every retry, for every leaf** — `pre-commit`, and so `gitleaks`, on a
typical DoD. Never for a foundation task, which runs on the main checkout. The shared `.git` is
now bound at its own host path so the existing redirect resolves, and the sandbox passes
`safe.directory` so git does not refuse the tree it was handed as dubiously owned.

### A build picked up in another terminal continues the work

The implementer's agent session is process-local and dies with the terminal that ran it, and so
did the failure log and the per-step retry budgets. An interrupted attempt's commits were
preserved on a salvage branch — and nothing ever read them back. A restarted build therefore
re-implemented the task from zero, on a fresh branch off the work branch, with a full retry
allowance it had already spent. `state.yaml.tasks.<id>.handoff` now records which step failed,
what it said, what budget is actually left, and where the preserved work went; the next attempt
merges that work into its worktree (reporting a conflict rather than forcing it), inherits the
remaining budget, and is told in its prompt that it is continuing rather than starting. Mode B's
lead is asked to keep the same record by hand, since its subagent has no session to resume
either.

### The task board is readable again

- A running task's DAG node rendered **black**: the stylesheet spelled the class `in_progress`
  (Mermaid's spelling, where `-` cannot appear in an identifier) while the DOM carries the status
  verbatim, `in-progress`. It matched no rule and fell through to the SVG default.
- In the dark palette `done` was *darker* than `todo` and barely separable from the panel, so a
  finished task read as an empty slot. Status now drives each node's stroke as well as its fill.
- The layer bar's `done` segment was the only one filled with its border colour, so it never
  matched its own chip. All five now fill the same way.
- The graph's edges had no arrowheads and no key, so nothing said which end had to finish first,
  that a column is an execution layer, or that the teal is the critical path. They do now.
- A task's detail says what it carried over from an interrupted attempt, and which commit landed it.

### Which commit closed T-NNN is recorded where the schema always said it was

`state.yaml.tasks.<id>.completed_commit` has been in the schema, and named in `dag.py` as one of
the fields a build mutates, since 0.1.0 — and no code ever wrote it. The commit lived instead in a
**second** `task_completed` event appended beside the first, which cost twice:

- Everything that counts events counted every finished task twice. `rein events --summary`
  reported `task_completed×6` for three tasks, and the resume packet — read at the start of every
  session — printed `tasks completed: 6 (T-001, T-002, T-003)`, a number contradicting the ids
  next to it.
- The hash was read from the work branch at logging time, which for a parallel batch is *after*
  the whole batch has merged and the integration gate has run. All three leaves of a batch
  recorded the same commit: the last merge, not the one that landed them.

The commit is now written into the task entry and carried in the same event the status writes, one
per completion, read at the moment that task's commit becomes HEAD. A task sent back for revision
loses it, since it names the commit that *completed* the task. The dashboard shows it on the task.

### Also

- `rein dag --frontier` is named in `/build` mode B as *the* source of a batch. Mode A already
  could not start a task with unfinished upstream work at any `max_parallel`; the invariant now
  has a test, and the mode-B lead is told not to hand-pick what looks ready.

## [0.2.0] - 2026-08-06

A correctness release, and one doctrine correction. Three reported defects and five rough edges,
with the theme that the lifecycle could record **yes** but not much else: it could not tell you
*when* to say it, could not let you say it where you had just read the thing, and had nowhere at
all to put **"not yet, change this"**.

### Gate ③ actually freezes the plan (#1)

Three documents said gate ③'s approval freezes the plan, `rein guard` rule 2 protected
`plan.yaml`/`config.yaml` only while the plan was frozen, and `rein build` refused to start
against a draft — but **no code anywhere ever wrote `frozen`**. A repository that had properly
approved gate ③ therefore could not build, and rule 2 never once engaged. The freeze now happens
in the same Central Store transaction that writes the receipt, with a `plan_frozen` event beside
it, and refuses if `plan.yaml` or `config.yaml` moved since the digests the human was shown.

### Drift in what the freeze covers is detected (#2)

`rein doctor` only checked that a receipt's digests were *present*, never that they still
described anything — so pinning a sandbox image and adding a `guard.paths` entry after gate ③
kept printing `0 FAIL` against a `config.yaml` nobody had approved. New `check_freeze_drift`
compares the freeze record against the documents on disk, then each post-freeze receipt against
that record. It is scoped to the freeze rather than to every receipt: gates ① and ② were approved
while the plan was a draft, and `/design` and `/tasks` then moved it legitimately. `rein guard`'s
commit-stage check now covers `config.yaml` alongside `plan.yaml`.

### `rein next` tells you when a gate is waiting on you

The `approve_gate` recommendation kind existed and nothing ever returned it, so a finished phase
with a clear gate still said "run the phase again" — and the dashboard's waiting-state signals
(tab title, favicon, notification) stayed silent for the entire wait.

### Approve where you read it, and where write authority comes from

`rein ui` can now record a gate approval, from the pane that just showed you the deliverable and
the digests. This replaces a doctrine that said "a localhost click is not authentication" while
embedding the page's write token *in the page* — so anything able to `curl` it could write,
including the gate-④ human-review answers the gate itself requires.

The line is not authentication, because nothing in a repository can prove a human. It is the
channel the capability travels over: `rein ui` prints a **single-use launch link** to its own
terminal, redeeming it mints the write session, and a page fetched any other way is read-only.
What is guaranteed, and now said plainly everywhere it is claimed, is that **an approval cannot
happen by accident, by default, or by a configuration someone pre-authorized**. Receipts record
`confirmed_via`; the terminal prompt is `[y/N]` with the default **no**, since retyping a gate
name already on the command line established nothing while a stray Enter must never approve.

### "Not yet, change this" is recorded — `rein changes`

The answer between *yes* and *roll back a yes* had no home: it lived in a chat message, so the
gate stayed ready, the board kept recommending the approval, and a new session never knew. A
change request is **anchored** to a place (`docs/10-requirements.md#R-3`, `T-004`) so answering it
means reading that slice rather than re-running the phase; `open` holds the gate shut, the agent
moves it to `addressed` with a note saying what changed, and the approval closes what it covered.

### Sandbox setup is part of initialization, and uv is current (#3)

`rein init` never mentioned the one precondition everything else needs, and every surface
recommended `rein oci build --profile <first of three>` without `--write-config` — a command that
could not clear the failure it was answering. The wizard now offers to build and pin; `rein next`,
`rein doctor`, the dashboard and `rein init` all name the same complete command; `rein oci build`
checks for a container runtime before starting, shows per-image progress, and verifies the pins it
writes. The packaged `uv` moves 0.9.7 → 0.11.28, pinned by digest: 0.9.7 could not parse a
relative `exclude-newer` and silently dropped the entire `[tool.uv]` table, re-resolving without
the lockfile's cutoff — a sandbox-only divergence in the one place whose purpose is that the
pinned environment is what the evidence is about.

## [0.1.0] - 2026-08-02

The first release of **Loose Rein** — a coding-agent harness for developing software
*Human on the Loop*: the agent does the work and self-tests from requirements through testing,
and the human approves or decides only at the **gate** on each phase boundary. The name is the
posture: the horse runs on its own, the rider keeps the reins.

The harness is an installed CLI (`rein`). A product repository carries only its *state* —
`.rein/` (the SSOT, the lock, the materialized prompts/schema) and `docs/` (the deliverables).

### The lifecycle

`brief → requirements → design → tasks → build → verify → done`, driven by `/req` `/design`
`/tasks` `/build` `/verify`, with gates ①–⑤ on the boundaries. A phase cannot start while its
prerequisite gate is `pending`, and **only a human opens a gate** — never the agent, and never a
localhost click. `/status` shows the board and names the next command; `/revise` rolls back
upstream, resetting gates in a chain. An ongoing repository repeats the lifecycle as delta
cycles, closed with `rein cycle-close`.

### Single Source of Truth

Four documents with distinct roles: `.rein/plan.yaml` (the frozen Expected Model — claims and the
task DAG), `.rein/state.yaml` (phase, gate approvals, task status), `.rein/review.yaml` (the
machine review and the human review, digested separately), and `.rein/events.ndjson` (the
hash-chained audit log — every state change records why, and a broken chain is visible).

### Evidence, not self-consistency

A claim with no evidence is `unknown`. At gate ④, whether the code satisfies a claim is judged on
three separate axes — integrity, semantic support, conformance — by comparing what the plan says
(Expected) against what a reviewer that never saw the plan reads out of the code (Actual). There is
no single "verified". `rein review generate` produces the grounded review gate ④ approves; a
structured security review feeds it, and a dependency audit joins at `/verify`.

### Gate enforcement in code

`rein guard` runs as a PreToolUse/pre-commit hook and denies edits that cross a phase boundary;
unreadable gates fail closed. The only write path to `approved` is `rein approve <gate>`: it
checks readiness, prints the digests the approval would cover, and records a confirmation typed at
an interactive terminal, binding those digests in one receipt. An approval records that a human
confirmed, never *which* human — there is no identity-bound mode, so authority never depends on
anything outside the repository.

### The build loop

`rein build` is a deterministic DAG scheduler, not LLM discretion: tasks are foundation /
parallel / integration, layers and the critical path derive from `blockedBy`, and parallel leaves
run in git worktrees against a control plane so a worktree's record survives its deletion.
Repository code and tests run in a sealed OCI sandbox (`rein oci build`), never on the host. The
quality gate (`test` → `check` → `review` → `smoke`) is re-run and its exit status read by the
lead — a delegated agent's textual "green" is not evidence.

### Agent support

Claude Code and VS Code GitHub Copilot are fully supported, hook-enforced gates included
(Copilot's hook mechanism is a VS Code preview feature). Codex and any other agent that reads
`AGENTS.md` work at the rules-and-procedures level, with gates by convention. `rein install
claude|copilot|codex` writes each host's surfaces on demand.

### Human surface

`rein start` (setup wizard, then where-you-are), `rein next` (the next recommended command),
`rein ui` (a local dashboard that reads a gate's deliverables and shows its readiness, handing back
the `rein approve` command for the human's own terminal — a page click is never authentication,
with a project switcher across repositories registered by `rein project add`), `rein agent <cli>` (swap
the headless agent CLI), `rein doctor` (diagnose the environment and the SSOT).

### Adopting an existing repository

`rein init` auto-detects a brownfield repository and hints `/onboard`, which surveys the codebase
read-only into `docs/05-current-state.md`. `rein init` writes only state — no build files, no
makefile, and no agent surfaces unless you install them.
