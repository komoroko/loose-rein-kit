# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`rein upgrade`
shows the sections between the installed version, recorded in `.rein/rein.lock`, and the
new one). `pyproject.toml [project] version` is the single version source.

## [0.3.0] - 2026-08-17

`rein build` decided a task by asking one question — did the quality gate pass? — of a process it
had launched and then stopped listening to. This release replaces that with evidence: what the
attempt produced, what the attempt said, and what the verdict was reached on.

### A task that changed nothing is no longer `done`

The loop discarded a launch's output, never looked at the diff, and treated a clean tree as a
successful no-op commit. So an implementer whose sandbox refused to let it write produced an exit
code of zero and a green gate over the code that was already there, and the run recorded `done`
with the *pre-existing* HEAD as the task's completing commit. Nothing in the whole path was a
statement about the task.

An attempt is now read before the gate is asked anything. An empty diff escalates as
`no_implementation` and blocks the task; a report naming paths the diff does not contain
escalates as `report_mismatch`. Neither spends a step's retry budget, because no step ran.

### `rein report` — how an implementer ends its attempt

The `task.status` capability had been in `LEAF_CAPABILITIES` since the control plane landed, with
no verb that reached it: an agent's only channel was its exit code. `rein report --outcome
implemented|blocked|needs-revision --summary …` is that verb. It goes over the same control plane
and the same capability token as every other leaf record, so a report survives the worktree it was
written in, and it can only ever *narrow* what happens next — `blocked` and `needs-revision` park
the task **before** a reviewer or a full test suite is spent on an attempt that already answered,
and there is no outcome an agent can report that finishes anything. `--touched` is a claim,
checked against the real diff.

The implementer role no longer pastes verbatim `make test` output as evidence. The caller re-runs
the DoD itself and decides by exit status, so a green the agent pastes is a green nobody counts —
it was pure token cost standing in for a channel that did not exist.

### Diagnostics survive the terminal, without becoming verdicts

`handoff.last_agent` keeps what the last launch actually said (role, adapter, rc, session, output
tail); `handoff.last_fault` keeps the environment fault that stopped one. The second matters most:
an environment fault reaches no verdict *by design* — no status moves, no budget is spent — and
that was being enforced by recording nothing at all, so the reason existed only in a terminal that
has since closed. Diagnostics ride along with the next status write rather than getting a
transaction of their own; a store write here must record *why*, and "an agent produced output" is
not a line a hash-chained log should carry.

### The evidence ledger: `done` says what it was established on, and a fact is established once

`state.yaml`'s `tasks.<id>.evidence` records the content fingerprint a task's `done` was reached
on and which gate steps were green against it, written in the same transaction as the status. A
task leaving `done` loses it, for the same reason `completed_commit` is dropped.

Alongside it, a content-addressed cache outside the working tree
(`$XDG_CACHE_HOME/rein/<repo_id>/evidence.jsonl`) skips a step already green on this exact tree in
this exact image. It decides nothing: only greens are recorded, an unknown fingerprint never
matches, and losing the file costs time and nothing else. `REIN_NO_CACHE=1` turns it off.

### The per-task reviewer reports; it no longer repairs

It was launched with write access and told to apply its own fixes. Two things followed. One
participant both judged a change and edited the judgement away — the failure mode this repository
names in every other context and had built into its own quality gate. And the tree moved
underneath the gate, so every already-passed step had to be re-run behind it, on top of the suite
the reviewer had been told to get green itself: the same tests, three times, for one verdict.

Now it is launched read-only, writes findings to `.rein/work/T-NNN.findings.json`, and is told
plainly not to run the gate commands — the caller re-runs them and decides by exit status, so a
green the reviewer reports is a green nobody counts. `must_fix` findings go to the implementer
within the step's own `retries` budget and the reviewer looks again; `consider` stops nothing and
is carried to gate ④. A review whose findings cannot be read stops the step rather than passing
it: an unreadable answer is not an answer that found nothing.

### `stage:` — where a DoD step runs

A step can name `stage: task`, `integration`, or `both` (the default, and what every step has
always done). It moves *when* a step runs, never whether: a fast focused suite guards each task
while the whole one runs once over the join, instead of every attempt of every task
re-establishing the entire thing. Frozen at gate ③ like `paths:`, so it is an operator's decision
about the DoD's shape and never a task's opt-out.

### A lockfile's body no longer reaches the reviewers

`rein review generate` handed the extractor and the security reviewer the raw whole diff — each
its own copy — so eight hundred lines saying "the dependencies moved" arrived twice, with the
twelve lines of hand-written code somewhere in the middle. Mechanical files (lockfiles, generated
files) now reach them as a header and a line count, and their head-side bodies are not sent at
all. This is redaction, not summarisation: nothing is described or interpreted, and **the Coverage
Manifest still reads the whole diff** — what it measures is how much of the change could be
analysed, and folding a file before counting it would be measuring the fold. A dependency change
goes on making the coverage `insufficient` for exactly the reason it always did.

### The run measures what it put in front of a model

The loop composes every prompt itself, so the input is the one thing it can count exactly. It now
does, by role, and prints it at the end of the run and at gate ④ beside the ledger's reuse count.
Gate ④'s review budget has always been measured rather than declared; the build side of the run
had no number at all, which is why "we are re-sending too much" could only ever be an impression.
Bytes, not tokens — a token count belongs to a tokenizer nobody here owns, and reporting an
estimate as a measurement is the habit this codebase is built against.

### A task's acceptance criteria are in the plan, and something checks them

They lived as markdown checkboxes in `docs/tasks/T-NNN.md`, which nothing parsed. So "the
acceptance criteria are met" was an assertion by the agent that had just written the code,
standing beside a quality gate that only ever answered a different question — is this code
*sound*, not did it do what it was *for*.

`plan.yaml` tasks now carry `acceptance: [{id, statement, evidence}]`, and the loop establishes
them after the DoD. This is **not** a task choosing its own gate: the shared DoD runs unchanged
either way, and a human freezes the list at gate ③ exactly like the rest of the plan. Evidence
comes in three kinds — `command` (an argv run in a sandboxed profile, decided by exit status),
`artifact` (the named paths must exist), and `external`. A criterion with no `evidence` at all
is prose, which most criteria honestly are; it establishes nothing and blocks nothing, and gate
④ is where a human reads it. A failing criterion returns through the same channel a red gate step
does, so it inherits the send-back budget rather than growing a second one beside it.

### `awaiting-evidence`: neither failed nor done

`external` says up front that this loop cannot establish something — a staging check, a device,
a person. Both other answers would be a lie: `blocked` says the code failed, and `done` claims an
observation nobody made. So the work **merges** (it passed the entire DoD; nothing about it is in
question) and the task parks at `awaiting-evidence`, off the frontier, with gate ④ held shut by
the unfinished task. Merging first is what makes the observation possible at all — nobody can
check a staging deployment of code that only exists on an unmerged leaf branch.

`rein evidence show` lists what is waiting; `rein evidence record --task --ac --note` records what
somebody saw. It runs from the canonical checkout only and needs a terminal — an implementer that
could record its own acceptance would be signing off on its own work — and the record binds the
content fingerprint it was made against, so changing the code retires it. The next `rein build`
promotes the task on the spot rather than re-running an implementer over merged, verified code
whose one missing piece was a person looking at a screen.

### The tree fingerprint stopped counting `.rein/` — and stopped being silently constant

Two defects in what "the same tree" means, both found by the acceptance work:

`.rein/` was part of the fingerprint, so the act of recording that a step had passed changed the
tree it had passed against. Orchestration state is not the product — the same reason
`finalize_commit` has always excluded it from a task commit.

And the committed half was read with `git ls-tree -r` where `parse_ls_tree` requires `-z`. Fed
the unseparated form it parsed the entire listing as **one** entry whose path began `.rein/`, so
the exclusion dropped everything and every tree in every repository hashed identically. A
fingerprint that is silently constant is worse than none; there is now an explicit check for the
empty parse that produced it. The committed half is also hashed by path/mode/blob id rather than
by commit id, so a salvage merge that changes not one byte no longer invalidates every fact
established about that content.

### The prose an agent reads is pinned, and an uncommitted edit stops the build

`plan.yaml` was frozen by digest at gate ③. The documents an implementer is actually *sent to
read* — its ticket, the design document, the baseline — were bound to nothing, so an edit after
the approval changed what got built and left no trace that it had. The freeze now records
`plan.sources`: every such document, digested. `rein doctor` reports a drifted one, and `rein
build` refuses to start (exit `2`) rather than implementing text nobody approved.

The second half had no symptom at all. A parallel leaf is created with `git worktree add <path>
<branch>` and therefore reads what is *committed* on the work branch — so an uncommitted ticket
edit reached no task, silently, while its author watched the new version on screen. The build now
names that too, and asks for a commit.

### The per-task dossier: the loop derives, agents stop re-deriving

Every agent was handed a pointer and sent to find things out. The implementer got
`docs/tasks/T-004.md` and `docs/20-design.md` as *paths*, and read them cold on every launch —
and on every retry, for any CLI that cannot resume. The reviewer got a path list and re-surveyed
the diff. Each was re-establishing, from the repository, facts the orchestrator had already
computed and dropped: what the claims this task answers actually say, the scope the plan gave it,
which changed paths are source and which are 800 lines of lockfile, what the last four attempts
tried.

`.rein/work/T-NNN.json` is those facts, assembled fresh per launch and handed over. Two
consequences of the same change: fewer tokens, because nothing is read twice, and better answers,
because what the loop knows stopped being something the model has to guess at. The reviewer is
also told plainly not to run the gate commands — the caller re-runs them itself and decides by
exit status, so a suite the reviewer ran was a suite run twice for one verdict.

Gate ④'s blind extractor never receives one. Re-deriving everything without ever seeing the plan
is the point of that stage, not an oversight.

### The scope in the plan is checked, not just requested

`plan.yaml` has carried `task.scope.include` / `exclude` since the schema was written, and
nothing read them: "do not reach into other tasks' territory" was an instruction in a prompt.
A diff outside the declared scope now blocks the task as a `scope_violation` — a scope change to
an approved plan is a human's decision. A task with no declared scope stays unbounded, which is
what an empty `include` has always meant.

### An adapter declares what it can do, and `doctor` can reason about it

Two hard-coded dicts and one `adapter == "claude"` test decided whether an implementer could
write a byte, whether a retry continued its session, and — silently — nothing at all about the
fact that `codex` brings its own process sandbox. `ADAPTER_TABLE` makes each of those a field, so:

- **Nested sandboxes are named.** Inside a container, `codex exec --sandbox workspace-write`
  needs kernel features the outer sandbox has already dropped, and it fails exactly where the
  agent writes — reaching the run as a task that produced no change, a symptom pointing nowhere
  near its cause. `doctor` now WARNs on the combination and says what to do about it.
- **A CLI that cannot resume says what that costs.** Not a defect — it is what the CLI offers —
  but a `codex` implementer re-reads its ticket, its design slice and the code on every retry,
  and nothing anywhere said so.

Roles also reach the agent as `REIN_ROLE` / `REIN_TASK_ID` / `REIN_RUN_ID` / `REIN_SANDBOX`
rather than being inferred from the shape of a prompt.

### A security finding no longer follows the review onto a different base

A blocking finding was carried into the next `rein review generate` by **id alone**. So one taken
against base A kept blocking a regeneration against base B — a different diff, sometimes not even
containing the code the finding named — and the only way past it was for the reviewer to
re-assert something it could no longer see. The carry-over is now conditional on
`binding.trusted_base_sha` matching, each finding records the `first_seen` base and head it is a
statement about, and the check itself was widened: it compared id sets, so re-listing `SEC-001`
with `blocking: false` cleared the block exactly as well as fixing it did.
`review_policy.reject_blocking_removal` — written for this and never called from anywhere — is
wired in.

Relatedly, `_resolve_base` no longer falls back to HEAD. That was the last branch, and it is the
one answer that is never right: `git diff HEAD..HEAD` is empty, so every reviewer would be handed
a change of nothing and would report, honestly and uselessly, that they found nothing wrong.

### "Extra behaviours: 0" is now a reading rather than an empty list's length

`machine.extra_behaviors` — behaviour present in the code that no claim in the plan accounts for,
the section that answers *did it build something nobody asked for* — was defined in the schema,
consumed by the decision cards, the review budget, `pr_draft` and `doctor`, and **written by
nobody**: `assemble` took it as a parameter that the one real call site never passed. So a review
whose Coverage Manifest came back sufficient reported "extra behaviours: 0" every time, from a
list that could not have held anything. That is prose standing in for evidence, in the place this
product least tolerates it.

The Comparator now reports them, because it is the only participant that sees both the Expected
Model and the Actual. Each one must name the Actual Statements it was read from: no claim accounts
for an extra behaviour — that is what makes it extra — so the plan cannot check the citation, and
the Actual is the only thing left that can. An unanchored entry is refused, as is a category
outside the declared list; there is no neutral category that would be honest, so filing one under
an invented name is worse than refusing it. An omitted `grounded` reads as `false`, since
`grounded: true` is what takes an extra behaviour off the human's list and an absent flag must not
be the thing that does it. The change's risk floor is deliberately *not* applied here: a claim's
risk restates the change's, but an extra behaviour's risk is a property of that behaviour.

### `rein doctor` stops implying that two models are mandatory

Independence is required for a **critical** review, and between the actual-extractor and the
comparator only — never between the implementer and the code reviewer, which the shipped config
points at the same CLI. Reporting an undeclared pair as a FAIL regardless of the plan made the
whole thing read as a hard requirement on a template whose plan has no claims in it at all. A
shared group stays a FAIL; an undeclared pair is a WARN until a `critical` claim needs it.

### "Did the tree change?" is now a content question

`tree_state` compared HEAD plus `git status --porcelain` — a list of names and status codes. A
second edit to a file that was already modified left it byte-identical, so the agent step's
"nothing changed" short-circuit skipped re-running the passed cmd steps over a tree that had in
fact moved. It is replaced by `fingerprint()`: HEAD, the full tracked diff including binaries, and
the blob ids of untracked files. Unavailable or truncated comes back as `""`, which reads as
"changed" and as a cache miss — the direction that costs a re-run rather than a verdict.

A leaf's changed-path set now includes its uncommitted work as well as its commits. The
implementer is told to commit and `finalize_commit` exists precisely because it sometimes does
not, so "produced nothing" and "has not committed yet" had been the same answer.

## [0.2.3] - 2026-08-14

Eight pieces of friction from one long `rein build` run against a real product repository, and
what each one turned out to actually be.

### A `network: none` step's own dependency failure no longer burns its retry budget

Every sandboxed step runs with no network (plan §10.2), so a `test`/`check` command that needed
to resolve a hostname mid-run — a dependency the pinned image never baked in — failed the same
way on every retry, and `classify_step` (`faults.py`) had no way to tell that from the code
actually being wrong: it charged the step's retry budget like any other content failure. A
narrow, literal set of OS/resolver strings (glibc's "Temporary failure in resolving", curl's
"Could not resolve host", Node's `ENOTFOUND`, and their kin — not a guess at arbitrary build-tool
output, the thing this module has always refused to do) now reads this as `ENV_PERMANENT`: no
budget spent, and the console message says what actually fixes it — bake the dependency into the
pinned image.

### A gate-guarded edit inside a worktree is caught right after the implementer runs, not only at merge

`config.yaml` (and anything else `guard.paths` protects) was already checked before a leaf's
commits reached the work branch — but only once, at merge time. A task that never got that far —
blocked on a later content failure, or the run stopped by an environment fault first — could
carry an unnoticed violation in an unmerged worktree indefinitely, found only by running
`rein doctor` by hand. The same check now also runs immediately after each attempt's
implementer, for both worktree leaves and serial/foundation tasks, so the gap between "the edit
happened" and "something looked" is one attempt, not "until someone thinks to check."

### A custom OCI profile can build from the repository, not only a packaged Containerfile

Three Containerfiles ship with the package; a repository with a stack none of them cover had no
way to sandbox it. A profile can now set
`dockerfile:` — a repo-relative path, frozen alongside the rest of `config.yaml` at gate ③ like
everything else there — and `rein oci build --profile <name>` builds it exactly as it would a
packaged one. Deliberately not a `build_command:` — a Dockerfile stays declarative; an arbitrary
shell command in a frozen config is a bigger door than this needed to open.

### A quality-gate step can be scoped to the paths it actually applies to

The DoD's "no opt-out knob" was, and stays, about implementers: nothing here lets a task choose
its own gate. But an operator deciding *at gate 3* that one stack's suite has no business running
for a commit that never touched it was never the same thing, and the schema had no way to say it.
A `quality_gate` step can now name `paths:` (fnmatch-style globs);
`_steps_for` skips it for a task whose diff does not intersect them. A step naming no `paths:`
is unchanged — every packaged step still runs for every task — and an unresolved diff (a fresh
worktree, dry-run) is never read as an empty one: it runs the full DoD rather than guess a scope
that was never decided.

### `rein doctor` checks that a pinned image is actually present, not just shaped like one

`check_sandbox` verified a profile named a well-formed digest and that a container runtime
existed on PATH — never whether an image under that digest was actually sitting in the local
store. `executors.verify_pinned` already existed and answered exactly that (`rein oci verify`
already used it); `doctor` now calls it too, WARNing when the image has simply never been built
here yet and FAILing when a local image exists under a *different* digest than the pin — the
sharper, config-actually-drifted case.

### `rein status` stops asking about a `task_failed` the task has since lived down

`task_failed`/`knowledge_gap` are `ATTENTION_EVENTS`, and the chain that records them is
append-only by design — but "waiting on you" read that list straight off the chain with no
regard for what happened after. A task that failed three times and then reached `done` kept
every one of those three events on the board forever. `rein status` (not the log itself, and not
`rein events --summary`, which stays a faithful, unfiltered view of the chain) now drops a
`task_failed`/`knowledge_gap` from the queue once every task it named has reached `done` — a
batch event naming several tasks stays until all of them have. Everything else (a review-pipeline
escalation, `plan_invalidated`) is unaffected: closing those is still a signed disposition in
`review.yaml`, never an inference this command is positioned to make for them.

### Also: `--impacted`'s seed is the scoping decision, not a mechanism to fix

A related report — a config change appeared to invalidate the entire task DAG — turned out not
to be a bug: no automatic, config-driven invalidation exists anywhere in this codebase.
`rein revise --impacted` only ever marks the seeds named on the command line and their
transitive dependents; naming an early foundation task pulls in most of the plan because that is
what the closure is for, not because the tool guessed too broadly. README clarified rather than
code changed.

### `rein build --supervise` carries the documented retry-while-loop in-process

`EXIT_RETRY_LATER` (3) has always meant "nothing was marked, nothing was spent, re-run later" —
and the docs have shown the same few-line shell loop since 0.2.2 for whatever does that
re-running. In practice that loop only works for as long as something keeps it alive: a
terminal, a session, a person who remembers to come back. A run that stops on a capacity or
lock fault with nothing outside it watching just stays stopped — for as long as nobody notices,
not for as long as the fault takes to clear. `--supervise` (with `--supervise-interval-sec`,
default 900) is the same recipe carried inside `rein build` itself: on 3, sleep and call the
loop again against the repository's current state; on anything else, return immediately. One
long-lived process instead of a hand-rolled wrapper that has to be re-created correctly, and
survive intact, every time it is needed.

### `rein doctor` escalates a retryable stop nobody has come back to

`check_last_run` already said when the last run stopped on a machine fault and nothing has
progressed since — but a stop from five minutes ago and one from a day ago read identically. A
retryable stop that has sat unattended well past when its own kind of fault would normally have
cleared now escalates from an informational note to a warning, naming `--supervise` as the fix
that would have kept it from happening at all. The comparison is against event timestamps only —
never the fault's own free-text "resets at…" report, which stays exactly as unparsed as before.

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

### One build mode: the interactive re-enactment is gone

`/build` documented a second mode — the lead running the consumption algorithm by hand in
conversation — and it was not executable. It instructed the lead to keep task statuses in
`state.yaml`, which `rein guard` denies as machine-written; no verb can mark a task `done`
anyway; and the control plane that carries a leaf's decisions into the audit chain is served
only by the orchestrator. It also contradicted the rule the loop exists for — consumption order,
parallelism, merge and stopping decided in code, not LLM discretion — and none of the recovery
above (fault classification, exit codes, salvage, handoff) applied to it.

So `rein build` is the implementation phase, and `autonomous-build-iteration` leaves the
capability vocabulary with it: nothing re-invokes a procedure that is one command. Without a
headless agent CLI, install one and point the roles at it with `rein agent <cli>`; `rein doctor`
now checks the configured adapters resolve on PATH — a warning before the build phase, a failure
once it is open.

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
