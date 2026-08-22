# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`rein upgrade`
shows the sections between the installed version, recorded in `.rein/rein.lock`, and the
new one). `pyproject.toml [project] version` is the single version source.

## [0.3.4] - 2026-08-22

Four defects from a field run. Two are the same sentence twice — the machine asked a question it had
already answered, and somebody paid for the asking. The other two are the mirror image: a question
that needed a human never got asked, and nothing noticed. Then a pass over the always-loaded prose
itself, which had been paying for sentences that told an agent nothing to do.

`state.yaml`'s `handoff` gains an optional `escalation` — no migration, but a state.yaml written by
this release does not validate against an older `rein`. **`rein approve requirements` and
`rein approve design` now refuse while an unresolved `[NEEDS CLARIFICATION]` marker stands in the
document they approve**, which is a gate that used to open and no longer does.

### Gate ④ read the orchestrator's own bookkeeping as if it were code

`rein review generate` failed three times over with `the actual_extractor adapter exited 1`. The
adapter had said why — *"Prompt is too long · the request is ~1,061,094 tokens (limit 1,000,000)"* —
and the pipeline threw that sentence away and reported the exit code, so the reason was reachable
only by wrapping the CLI in a logging shim. Behind it: 27% of that cycle's diff was `.rein/` —
schema payloads, the frozen plan, task state, the hash-chained log — handed to the blind extractor
as reviewable source, and enough of it to push a legitimate change past the model's hard ceiling.

`.rein/` was already outside everything else that answers "what is the change under review": the
digest the review binds itself to, the tree fingerprint, every task commit. Only the diff put it
back in, and it is now excluded through that same one constant. This is deliberately not the fold a
lockfile gets — a folded file is still *in* the change, and the Coverage Manifest goes on reporting
its body unread. `.rein/` is not in the change, so counting it would invent a coverage gap out of
something no reviewer was ever meant to open, and at high risk that gap blocks the gate with an
instruction — "split the unreadable part out of this scope" — nobody can carry out on the SSOT.

Two things follow it. An adapter that fails now reports **what it said**, not merely that it
stopped. And `review_policy.budgets.max_diff_bytes_per_partition` — the one byte-denominated
budget, measurable until now only at the freeze, off the very manifest a change too big to review
prevents — is checked **before a model is launched**. It is the same wall either way: a diff over
that limit cannot be frozen once generated. What changes is that the refusal costs nothing and
arrives carrying the budget's own name and its own instruction.

### A verdict reached over a tree nobody moved, bought once per invocation

0.3.2 stopped a DoD step being retried when it failed identically over a tree the implementer did
not move. The other way an attempt ends — no change at all, a report naming paths the diff does not
contain, an implementer that said it was blocked — had no such protection. A task whose work had
already landed through a salvage merge was handed a fresh implementer once per `rein build`, three
times, each one correctly reporting there was nothing left to do.

That verdict is now recorded with the fingerprint of the tree it was reached over, and a later run
re-raises it marked `futile:` instead of paying for a launch that reaches it again. What is read is
the observation, never the report's text — the same tool-agnostic reading the step-level check
makes — and an unknown fingerprint never matches, so the failure is towards spending the launch.
`rein task reset <T-NNN> --fresh` discards the record, which is how somebody who repaired something
*outside* the tree says so; `rein task reset` now says as much on the way past, because a reset that
produces no launch otherwise looks like a defect.

### The check three documents promised, and nobody had written

The rules module told every reader that `rein approve` machine-checks unresolved
`[NEEDS CLARIFICATION]` markers. It never did — the word appears nowhere in the source — so a
marker left standing opened the gate anyway, and the question it named was answered by whatever
default the draft had already been written against. The document asserting the check was the reason
nobody looked for it.

It exists now, on the deliverable each gate already digests, with the lines named. HTML comments are
dropped first: the scaffold explains the convention *using* the marker, and a check that cannot tell
guidance from an open question is one nobody can leave switched on.

The asking around it changed with it. `/req` used to rank the open points and batch **the top ones**
into a single call — a cap on how much gets confirmed, dressed as a cap on how much one round can
carry. **There is no cap now**: the ordering decides what is asked first, never what goes unasked,
and the rounds continue while anything remains that the agent would otherwise close with its own
default. A marker ends answered and recorded under `## Clarifications`, or demoted to
`## Open questions` with its assumption spelled out — and demoting is the human's call, not the
agent's. `/design` gains the same vocabulary, which it never had: the architect now marks what it
would have settled silently, and gate ② checks for it the same way.

### A ticket that promises a test, and a scope that forbids writing it

A task ticket must state an automated-test approach; the task's `scope` says where its work may
land. Nothing compared them, so a scope could freeze at gate ③ covering only the implementation
file — and the mismatch surfaced at `/build`, as a `scope_violation`, after an implementer had
already written the test (#17).

The ticket now names the file its test goes in, and that path — with any ADR the design says the
task records its decision in — has to be inside the frozen scope. The gate ③ adversarial round gets
it as a lens, which is where a comparison no validator can make belongs: the reported case never
named the test file at all, so a checker reading the ticket would have had nothing to compare. And
the implementer, which is the first party to *know*, is now told to stop **before** doing the work
and report `needs-revision` naming the path, rather than doing it and tripping the merge check.

### Waiting for a build is not polling it

`build.md` had always allowed waiting on the run "if your host can wait at all", but every host
mapping said the same thing regardless: detach it, end the turn, let a human bring you back. Claude
Code can be re-entered when a background command exits — waiting, with no timer and no check — and
the mapping never used it. `background-wait` is now a capability in the vocabulary, mapped where it
exists and degrading to the detach recipe where it does not. Detaching stays the right answer for
one case, now stated: a run that has to outlive the session.

### The always-loaded files pay rent

Every rule an agent reads costs the same context the deliverables need, so the prose that teaches no
action was cut: design rationale addressed to a reader rather than an actor, archaeology of
mechanisms that are already gone, and the paragraphs each phase procedure repeated from `AGENTS.md`,
which is loaded anyway. No rule was dropped — where a sentence was the only home of one, it stayed.
`AGENTS.md` −12%, `gate-workflow.md` (read by every phase) −19%, the phase procedures −4 to −12%.

## [0.3.3] - 2026-08-20

Docs only: `README.md`/`README.ja.md` cut to overview / setup / usage / caveats, dropping
narrative justification that duplicated `.rein/config.yaml`'s own comments and
`.rein/prompts/rules/gate-workflow.md`, and fixing the install command's stale pinned tag
(`@v0.1.0`). The "How it works" diagram is now left-to-right with the `/revise` rollback edges
routed through a single hub node so they no longer cross the main flow.

## [0.3.2] - 2026-08-19

Two rounds of reported defects, and one thing the lifecycle never had a word for. The first round:
gate ④ asked a human to pass a quiz and then to decide, and gave them nothing to decide *with*. The
second, from running the loop on real work: the machine kept charging tasks for its own failures — a
freeze that a rebuilt image broke, retries against a break no task caused, a review pipeline re-read
for nothing, a queue that only grew. The shape recurs: something the code could distinguish and did
not, so a cost landed on whoever was nearest. The third strand is the same shape one step earlier —
no task could state what its change would require of a *person*, so gate ④ had nothing to compare
the operator-facing readings against, and finding out cost whoever ended up operating the result.

**Breaking**, all of it requiring a re-approval or a regeneration rather than a migration:

- `review.yaml` from earlier releases does not validate — `human` no longer accepts
  `challenge_answers` or `counterfactual_answers`, `binding.toolchain_digest` is now
  `environment_digest`, and `machine` carries three new sections. Regenerate with `rein review
  generate`.
- `state.yaml` carrying a `review:` block does not validate — delete it. `state.plan.toolchain_digest`
  is now `environment_digest`; re-approve gate ③.
- The `challenge_answered` and `counterfactual_answered` event types are gone from the vocabulary
  and `environment_repinned` is new; a log containing the first two will not validate.

### Gate 4 says what the change now requires of a person

The orientation stage had a table headed "what the blind extractor read", and it was a list of
statement ids. The text of a reading appeared on screen only where a claim happened to cite it, so
a sentence about a schema no claim covered — exactly the sentence somebody about to operate the
thing needs — was reachable nowhere. Meanwhile nothing in the template had a word for a setting
somebody has to supply, a migration somebody has to run, or a signal somebody has to watch: not the
requirements, not the design, not the plan, not the test plan.

That is a missing **Expected** side, not a missing screen. A task in `plan.yaml` now declares its
`operator_surface` — `{kind, name, paths, adr}` — frozen at gate ③ with everything else, and gate ④
sorts the extractor's operator-facing readings by whether one of those declarations foresaw them:
**what nobody declared first**, then what was declared and never read out, then a **count** of the
ones that went as foreseen. Expected rows are a number, because a table of them is where the first
two go to hide. The `kind` vocabulary is a subset of the extractor's own statement categories rather
than a new one, so the two sides compare without a translation table that could be wrong; matching
is category equality plus path coverage, and stops there, because declaring a human's prose and a
model's prose "the same surface" because they read alike is the overclaim the brief exists to avoid.

### The file as it ends up, not the diff that changed it

A diff shows what moved; what somebody operates is the result. Each declared surface now offers an
**as-built** view — the file at the commit the review is bound to, fetched on demand from
`/api/review/as-built/<path>` rather than copied into `review.yaml`, which would make the document a
second copy of the repository. The route serves only paths the stored brief published; an ordinary
committed source file is refused, because what this may read has to come from the review rather than
from the request.

### The implementer's own account of a task that did not land

`rein report --summary`'s help says "a human reads this". None ever did: `handoff.report` was read
by the loop, to phrase a failure, and by the next attempt's dossier. It now reaches gate ④ for the
tasks that did **not** land — the ones the approver is being asked to sign around, which is when the
reason matters — labelled as the claim it is. For work that landed it stays where it was: an
implementer's explanation of its own code is the one input the blind extractor may not see, and
handing it to the approver instead would move that priming onto the person the whole arrangement
exists to protect.

### Fewer things to read at every gate

- The **self-assessment is three items**, not five. *Anticipated risks and trade-offs* is gone: gates
  ①–③ already put the deliverable through an independent adversarial round, and the same question
  answered a second time by its author is the self-consistent explanation this system declines to
  treat as evidence. The *context-bloat signal* is gone too — hygiene addressed to the agent, and
  the pre-compact check already carries it.
- **Gate ③ presents two graph artefacts, not three.** `rein dag --mermaid` drew the same graph
  `--render` already states; a second rendering of one fact is a second thing to read rather than a
  second thing to know. The flag stays, and `/status` is where the whole picture is drawn.
- **The quality-gate summary names only the exceptions.** It listed every DoD step and how many
  tasks established it; its own docstring said the row worth reading is the step established for
  nothing. Now that is what it prints, beside a count of the rest.

### Where the trade-off stays

An ADR that changes something somebody has to operate must now record **what it requires of a
person** and **whether it can be undone**. Reversibility is a fact about the decision, and it is what
the gate ④ approver is actually handed; the pros and cons stay with the options, at gate ②, where
the choice is. Nothing restates them after the fact — a decision already made does not get
re-litigated in front of the person approving the evidence for it.

### Challenge-first is removed

A high/critical Decision Card withheld its evidence — the Expected the plan states, the Actual a
reviewer that never saw the plan read — until the reviewer had recorded an unprimed guess, and a
guess that missed opened a counterfactual to close before the review could freeze. The intent was
cognitive forcing. The effect was a comprehension quiz standing between a human and the decision
they were there to make, on the one screen that asks for anything.

It also defended against nothing. The priming that matters at gate ④ is the *extractor's* — a
model shown the Expected reports the Expected — and that is enforced separately, in
`actual_extraction.FORBIDDEN_KEYS`, and still is. Withholding evidence from the *human* protects a
different party from a different thing.

What remains is the forcing function nobody can clear by rote: an unanswered high/critical card
blocks the freeze, and every answer carries a confidence the tool will not invent. Gone with it:
`human_review.challenges` and the eight functions around it, `/api/review/challenge` and
`/api/review/counterfactual`, the reveal endpoint, and the pane's `challengeStage` — which was
already unreachable, having no `case` in the stage switch.

One defect fell out of the removal. `_critical_claim_ids` walked the *challenge* set — the three
hardest cards, capped — so a review with five critical cards measured its own
`max_critical_decisions` budget against three of them.

### The orient stage: what was built, and under what conditions

`rein ui`'s gate ④ rail is now **scope → orient → decision → diff → freeze**. The new stage asks
for nothing and exists so the decision stage can ask for less: the delivered tasks and the claims
they answer, which dependency manifests and migrations moved, **which sandbox, image and network
posture each quality-gate step ran under**, what the blind extractor read out about interfaces,
persistence, security boundaries and dependencies, what the gate established and for how many
tasks, whether anything ever *launched* the deliverable, the Expected/Actual comparison on its
three axes, and what is still open.

Every line is derived from the SSOT by the new `brief.py` — ids, paths, commands, image
references, and reviewer prose reached by statement id so its epistemic status stays attached. It
authors no sentence, which is the schema's rule and not a preference. It is built inside
`review.generate` and stored in the machine half, so it describes the same commit range as the
claims beside it; recomputing it on read would put a brief about the working tree next to a review
about `subject_head_sha`. A section with nothing to report is absent rather than empty — "no
migrations changed" must not read the same as "migrations were not looked at".

The `network` line is worth naming separately. It is not a config value echoed back: `OciExecutor`
refuses any profile whose `network_profile` is not `none`, so what the brief prints is what the
runtime enforced — and a `host` profile is printed as `unconfined`, because "none" about it would
be a claim nothing ever made.

### `consider` findings reach gate ④, which they were documented to do

The per-task reviewer's `must_fix` findings are resolved inside the build loop or the task blocks.
Its `consider` findings stop nothing by design and were written to `state.yaml`'s task handoff,
where nothing read them. `build.md`, the reviewer's own prompt, and the state schema all said they
would reach a human at gate ④; no code did it. They now arrive as `machine.residual_findings`,
shown on the orient stage.

Each carries the commit and tree fingerprint it was **observed against**, which is not
`subject_head_sha`: a reviewer looked at one task's worktree at one moment, and presenting that as
an observation about the merged tree would be the overclaim the rest of the document is built to
prevent. A `must_fix` finding on a task that blocked is carried at its own severity — downgrading
it would hide why the task never landed.

### The decision stage renders its whole payload

`stage_data("decision")` had served the gaps, the ungrounded extra behaviours and the security
findings since the stage existed, and the pane rendered the cards alone. The disposition form —
the only way to record what happens to a gap, which `completion_blockers` and the review budget
both expect — lived in a function nothing called, while the POST route accepting it stayed open.

Deleted outright, being neither reachable nor wanted: `overviewStage` (its counts and its budget
table are both on the scope stage) and `genericStage` (a JSON dump of an arbitrary payload key).

### The run's input budget is measured, and survives the run

"Every launch's input is measured and reported at the end of the run — a budget nobody counts is a
statement of intent." The counter saw only the argv this process composes, which is the small half.
What a launch is *told to read* — the dossier plus the ticket, the design slice and the baseline it
names — is where a build's input goes, and it was not measured at all.

Both numbers are now counted, alongside how many launches started **cold** rather than resuming the
agent's own session. That last one makes an existing claim falsifiable: the `Adapter` docstring has
called a non-resumable CLI "the single largest avoidable cost in a long build" since the capability
record was written, and nothing in a run could confirm or refute it.

The totals are appended to the audit chain as `run_measured` at the end of each run. Not
`state.yaml`: a long build's likely ending is `EXIT_RETRY_LATER`, which took the in-process counter
with it, and a state field would hold only the last run's figure. The chain never rotates, so
summing over a cycle is the cycle's total while each run stays separately readable. A run that
launched nothing records nothing — it did not measure zero.

No caching or prompt-trimming follows from this yet, deliberately. Optimising an input budget
before it could be measured is how a symptom gets treated.

### Waiting for a build without polling it

"`rein build` is one command, not an iteration — never schedule wake-ups to poll a run in progress"
has been in `build.md` since 0.2.2, and nothing anywhere said how to *wait*. Every agent host caps
how long one tool call may run and a real build outlasts that cap, so an agent whose command was
cut off starts checking on the run — each check a launch, a context, and a share of the session
limit spent learning that the build is still building.

`build.md` now carries the recipe (detached with its output in a file, end the turn, read `rein
resume` and the log when you come back), and all three capability mappings carry the host-specific
note. Including the one that matters most: **do not re-run `rein build` to check on it** — the
build lock makes the second run exit `3`, which is indistinguishable from a capacity stop, so a
supervisor reading that will sleep on a build that is running perfectly well.

### The environment pin left the plan freeze

Gate ③ froze `config.yaml` whole. A task that legitimately adds a dependency makes the pinned
sandbox image wrong — the closure it needs is not baked in, and a `network: none` sandbox fails
identically on every retry — so rebuilding it mid-cycle cost a `rein revise --to tasks`: the plan
un-froze, every gate below reset in a chain, and a human re-approved a plan nothing had changed.

The freeze is now `Config.frozen_digest()`, which is `config.yaml` with each
`executor_profiles.<name>.image` removed. Everything that is a *decision* stays inside: `kind`,
`network_profile`, `mount_repo`, the limits, the quality gate, the budgets, the guard. Only the
pin — the one thing `rein oci build --write-config` rewrites — is allowed to move underneath it, so
a sandbox that *opens* still breaks the freeze and still needs the human who approved the narrower
one.

Making that trade honest meant fixing the digest that was supposed to carry the other half.
`toolchain_digest` hashed `{"executors": …}` — the role→profile *name* map — while its own
docstring claimed to move when the sandbox a step ran in changed. `executor_profiles` is where
`kind`, `image` and `network_profile` live, so repointing a profile at a different image left it
identical, and nothing anywhere compared it, so the claim was never contradicted. It is now
`Config.environment_digest()`, it covers the profile bodies, and three things read it: gate ③
records it in the freeze and in every receipt, `rein doctor` reports when it has moved (INFO, not
FAIL — this movement is allowed), and gate ④'s orientation shows the approver that the evidence
they are signing over was produced in an environment gate ③ never saw. `rein oci build
--write-config` appends an `environment_repinned` event rather than rewriting `config.yaml` outside
the audit chain.

**Breaking**: `state.plan.toolchain_digest` and `review.yaml`'s `binding.toolchain_digest` are both
renamed `environment_digest`. Re-approve gate ③ and regenerate the review.

### Refusing before the first launch, and not paying for retries that cannot help

Three failure shapes, one root: the loop discovered after spending models what it could have known
before spending any.

**Preflight** (`rein.preflight`). No container runtime while a step needs an OCI sandbox, a pinned
image nobody built here, an agent CLI not on PATH, a `quality_gate` step marked `required:` with no
`command:` — all now refuse the run with exit `2` and the command that repairs each, before the
lock, the worktree, the dossier and the launch. `required` in particular has been in the schema and
in `GateStep`'s docstring — "makes the loop refuse to start, before any implementer has been paid
for" — read by nothing at all.

**Baseline.** The DoD's command steps run once against the work branch before any task touches it.
A step already red there is not a fact about any task, so a task failing it stops after one round
instead of spending three implementer launches on a break outside its own scope. It does not
refuse: a cycle whose first task is "fix the failing tests" runs its implementer *before* the gate.

**Futile retries.** A step that fails identically over a tree the implementer did not move gets no
second round. This is read from the observation — same step, same failure digest, same tree
fingerprint — never from the failure's text: `faults` refuses to interpret build-tool output on
principle, and adding a pattern for lockfile mismatches would be the first of an endless list. It
catches the reported cases (a stale lockfile, a missing browser binary, an absent CDK context)
without knowing anything about any of them.

Both stops record `futile:` on the task's handoff and its `task_failed` event, because "the budget
ran out" and "the budget was abandoned as pointless" mean different things to whoever reads it —
only the second names something to go and repair.

### One review status, and no re-reading a subject that has not moved

`state.yaml` carried a `review` block with six status values. Nothing read it: not the property on
`State`, not the enum in `models`, not one line anywhere. It was written in exactly two places — a
new cycle and a roll back — and could only ever disagree with `review.yaml`, which holds the real
machine and human statuses, digested separately. It is deleted, schema included.

The roll back's half of it is now real. "The review goes stale" is what `revise`'s docstring has
always said, and what it did was set that unread field; it now returns the **human** half of
`review.yaml` to `not_started` in the same transaction. The machine half is left alone: it is a
reading of the code rather than of the plan, and clearing it would destroy the thing those answers
were answers *to*.

`rein review generate` no longer re-runs the pipeline when nothing it reads has moved. Everything
the review is a function of is a digest computed before a model is called — the committed tree, the
frozen plan, the config the approval covers, the sandbox, the coverage manifest — and when all of
them match the review on disk, three reviewer stages would be paid for to read the same bytes *and*
the human half would be reset, discarding answers about a change nothing had touched. A field run
recorded `review_generated` fifteen times in one cycle. `--force` says "read it again anyway".

**Breaking**: a `state.yaml` carrying a `review:` block no longer validates. Delete the block.

### One unreadable file no longer shuts gate ④

`human_review.completion_blockers` blocked the freeze on any `insufficient` coverage manifest,
while `review_policy.coverage_blocks` — which `rein approve build` reads — blocks only at
high/critical. Two rules over one manifest, and the stricter one reinstated exactly the dead end
`coverage_gap_risk`'s docstring records having broken: a single binary asset makes the manifest
insufficient regardless of what was in it, so a low-risk cycle containing one had no way through
gate ④ at all, scope split included, since splitting never removes the file. The freeze now reads
the same risk-priced rule as the gate. Nothing about the honesty property changes — the manifest
still says `insufficient`, and extra-behaviour counts are still withheld rather than rendered zero.

### A directory in `scope` no longer had to be spelled with a slash

`scope: {include: [src/rein]}` matched an exact *file* named `src/rein`, which no changed-path list
contains, so every file under the directory came back as a scope violation and the task blocked for
reaching into territory that was its own. `src/rein/` worked. The same rule ran in `guard.paths`.

The trailing slash is now punctuation: a pattern covers the path it names and everything beneath
it, anchored at a separator, so `src/app` covers `src/app/main.py` and never `src/app.py` — which
is the entire reason the exact case existed. One helper (`common.path_covered`), three call sites,
and the rule written down in both schemas, where it had never appeared.

### Resolved events leave the queue

`plan_invalidated`, `review_failed` and `actual_extraction_failed` had no retirement condition at
all, so `rein status` carried "waiting for you" rows for a rollback re-approved weeks earlier and
for a generation that failed once and succeeded on the retry. A queue that only grows is one people
stop reading, and the rows it buries are the ones that mattered.

Each is now closed by the event that *undoes what it reported* — `plan_frozen` for the first,
`review_generated` for the other two — ordered by the chain's own `seq`, never by a clock.
`events.ndjson` is untouched: this narrows what a view calls pending, exactly as every other row of
the queue is derived rather than stored, and there is still no way to close a record by hand. The
whole policy moved into `events.py` beside `ATTENTION_EVENTS`, so the dashboard feed, the release
gate's counter and `rein status` stop each having their own idea of what is open.

### `task_failed` says what failed

The terminal `task_failed` — the only one a task that blocks on its first round produces — carried
a status and a prose note. Which step went red, and how much budget was left, lived on the
per-attempt records only. Both now travel on the event, lifted from the handoff written in the same
transaction, so nothing new is discovered and the chain becomes something a reader can sort by.

### The rein.lock version canary runs on every pull request

`.rein/rein.lock` records the release that wrote this repository's materialized artifacts, and
`rein sync` stamps it only as a side effect of writing something — so a release changing no payload
byte left it at the previous version while `sync --check`, which compares content, passed. That is
what happened at 0.3.1: the shipped template claimed 0.3.0. The only thing that noticed was a shell
snippet in the release workflow, on the day of the release. It is now `template_lint`'s
`check_rein_lock_version`, beside the CHANGELOG and `uv.lock` canaries, and the workflow step is
gone.

## [0.3.1] - 2026-08-17

Three reported defects, and what looking for more of the same kind turned up. The kind is one
this changelog keeps describing: something written down and answered to by nobody — a caller left
behind by a rename, a field the schema declares and no code writes, a constant placed to prevent
a confusion nothing consults it about. Ten more of them were found by asking the question
mechanically instead of by hand, and the canaries at the end are there so the next one is found
by a test rather than by a person reading.

**Breaking**: `review.yaml` documents from earlier releases no longer validate. Regenerate with
`rein review generate`; there is no migration, deliberately. A `state.yaml` whose `cycle_id` does
not match `^[a-z0-9][a-z0-9-]*$` is refused rather than carried — `cycle-close` could write one.

### A step something outside killed was recorded as the code failing

`_killed_externally` was written for a reported rc=143 in the field and consulted by nobody.
`classify_step` treated everything except an unlaunchable command and a `network: none` resolver
failure as `CONTENT`, so the OOM killer, a supervisor's SIGTERM and a closing terminal all charged
the step's retry budget and went into an append-only chain as facts about the code. This is the
same defect 0.2.3 fixed for DNS failures, left standing on the path that a session limit at 3am
actually takes.

Both spellings count, because both occur: `subprocess` reports a signal death as a negative rc, a
shell and `docker run` report 128+N. Only SIGHUP/SIGKILL/SIGTERM — SIGINT stays out for the reason
it always did, and a segfault's 139 is still a verdict. Our own timeout is untouched: `common.run`
kills the process group with SIGKILL and returns `RC_TIMEOUT`, so "we stopped it for hanging" never
reads as "something else stopped it". A container the kernel killed for exceeding `memory_mb`
arrives as 137 too, and that is partly about the code — the direction that costs a re-run rather
than a wrong verdict wins, and the console line names the limit instead of spending retries.

### A gate ④ that could not be produced now records that it could not

`events.ATTENTION_EVENTS` has always counted `actual_extraction_failed` and `review_failed` among
the things needing a human decision. Nothing emitted either. `generate` appended its five events
on the success path only, while a dozen `raise` sites — an unresolvable HEAD, a reviewer answering
with prose, a blocking security finding, a review.yaml that moved — left the log with nothing at
all, and `rein events` reported "needing a human decision: 0" for a review that had just failed.
The log exists so that no state change goes unexplained; the review pipeline's own failure was the
state change it could not explain.

`actual_extraction_failed` fires only when the extractor is the stage that failed, because that is
a different fact from a comparison failure: the extractor is the stage that reads the code *without*
the plan, so its failure means there is no Actual at all. `Exception`, not `BaseException` — a
Ctrl-C is a human deciding to stop, and filing that as a defect would put a decision in the log as
a failure. `actual_extraction_started` is gone from the vocabulary: a closed vocabulary refuses
unknown names so the log stays aggregatable, which makes a name no code can produce a claim about
the log that is not true.

### The dashboard said a gate it had just opened was still shut

`/api/gate/approve` began as a readiness check and later started recording the approval. `post()`
in api.js kept the message that belonged to the first contract: with no `blockers` in a POST body
it read `blocked === 0` and toasted "gate X is ready — run the command shown to approve". So a
human pressed Approve, the gate opened, the receipt recorded `ui-session` — and the page told them
it had not happened and that they should go to a terminal, while the board refreshed to a ✓ in the
same breath. Nobody was at risk of a double approval (`readiness` reports an approved gate as its
own blocker); what was lost is the human's ability to believe the screen, about the one judgement
here that widens what happens next.

The approval path itself was correct throughout — the launch-link handover, the digest re-binding,
the channel in the receipt. Only the sentence was wrong. It survived because the asset tests here
were about payload field *names*, so two canaries now cover the other thing: api.js must not carry
the retired phrasing, and every `/api/` route ui.py dispatches must be reached from the page (and
the reverse), read out of both sources rather than a list someone keeps.

### A NotebookEdit reached the guard with no path it could read

The Claude matcher read `Write|Edit|MultiEdit`: `MultiEdit` retired upstream, `NotebookEdit` never
added. And even matched it would have changed nothing, because `hook_paths` knew `file_path`,
`filePath` and Codex's patch text but never `notebook_path`. A `.ipynb` under a guarded prefix
passed the edit-stage check untouched, leaving the extension-blind commit-stage check as the only
layer that ever looked at it — layered enforcement degraded to one layer, silently, on the host
this template ships for. A notebook is source: it is code an agent wrote.

`doctor` was asking one question where there are two. "Is the guard registered?" it answered;
"does the registration cover the tools that write?" it never asked, so the hole was invisible to the
command whose job is to find exactly this. `MultiEdit` stays in the matcher — a foreign host's tool
namespace is not a format of ours to keep tidy, and a dead alternative in a regex costs nothing.

### The guard's own registration was protected by nothing

`.claude/settings.json` is where this guard is registered, and it was in neither rule 1
(machine-written), nor rule 2 (frozen at gate ③), nor `guard.paths`, which covers deliverable
directories. The one file an agent could edit to switch off edit-stage enforcement — including the
check that would have stopped it — was the one file no rule mentioned. Denied outright rather than
gated behind an approval: there is no phase at which rewriting it is the expected next step, and a
human who wants it changed does so at their editor, where no PreToolUse hook applies. Not relaxed
by `template_mode`, because a template whose hook can be switched off is a template that ships with
the switch.

### A file a release dropped no longer haunts the repository unseen

`_plan` iterated the payload alone, so a file an earlier release shipped stayed on disk forever —
and the lock rewrite that followed kept only keys still in the payload, so after one `sync` nothing
knew the file had ever been installed and `uninstall`, which works from that record, could not
retract it either. Removal is planned before the rewrite now. An unshipped file somebody edited is
kept *and keeps its lock entry*, because that entry is the only thing left tying it to the tool that
put it there.

Two more around the same lock. `stale_integrations` says in its own docstring that sync *and*
doctor surface the skew, and only sync ever called it — so a repository reading new prompts through
wrappers an older rein wrote came back all PASS. And a lock entry naming a hash the payload no
longer has went unnoticed; three were already stale on main, which is how this was found. That is
not cosmetic: `_plan` reads the record to tell a pristine file from a locally modified one, so the
next release to change one of those files would report a local modification that does not exist and
decline to update it.

### An unmeasured partition stopped passing a budget it was never held to

`analyzed_bytes` was optional, read with a default of 0, and its own description said an absent
value reads as "within budget". It is required now. Every other unknown here falls the other way —
an unavailable fingerprint reads as *changed*, because the honest direction costs a re-run rather
than a verdict — and a byte budget with no actual is the one place that inversion could hide.

### What the schema declared and nobody wrote

Four fields, removed rather than wired, because an affordance no requirement names is scope creep:
`rename_semantics_analyzed`, sitting beside the two flags the coverage status really reads, so
`_default_status` appeared to take a third measure it never took; `human.session`
(`started_at`/`stage`/`completed_stages`), a stored-progress design replaced by deriving it and
never removed; and `dispositions[].owner`/`due`, affordances `record_disposition` cannot produce.

The code equivalents went with them: `HOME_MODE_VALUES` (no `home` property exists in any schema),
`SCENARIO_KIND_ORDER`/`_VALUES`, `Orchestrator._task_status` — whose docstring claimed to be how an
implementer's "blocked" reaches the loop, while the loop reads `_read_report` — `_branch_for`,
`dag.KIND_ORDER`, `Repo.schema_dir` (it pointed at `.rein/schema/` while validation reads the
packaged copy), `Config.project_name`, `Task.needs_human`, and `PERMANENT_DOCS`/`_permanent_docs`,
a cold-maintainer documentation input with no consumer and no slot in any reviewer request.

Two were better wired than deleted. `event_chain.verify_root` now backs the line in `approve` that
spelled the same check out by hand, and `CONFIRMATION_CHANNEL_VALUES` now actually validates
`confirmed_via`, which accepted any string and failed several layers out as a schema violation.

### `.rein/` was spelled six times

Four answers have to agree about what "the tree" excludes — the fingerprint, the paths a task is
credited with changing, the commit it produces, and the change a review is bound to — and they were
two module constants plus four inline `:(exclude).rein` pathspecs, plus a seventh copy inside the
instruction the implementer types. All of them derive from `repo.SSOT_DIR` now. Nothing behaves
differently; the point is that one of them drifting stops being possible, and the way that failure
would have surfaced is a fact invalidated by having been recorded.

### `cycle-close` reset the SSOT's own state.yaml outside the store

Rule 1 of the gate guard denies an agent hand-editing `state.yaml`, `review.yaml`, or
`events.ndjson`, on the grounds that those are written only inside a Central Store transaction. The
reset that opens the next cycle wrote `state.yaml` with a bare `atomic_write` — so the claim the
guard enforces against agents was false about rein's own code, in the one place it mattered most.
Two things followed. The schema never saw the document: `--name ①` was accepted by a `--name` check
that approximated the schema's pattern with `slug.replace("-", "").isalnum()`, and a cycle id
`state.yaml` rejects reached disk and was reported back as an open cycle. And the reset landed
separately from the `cycle_initialized` event, so a crash between them left a fresh cycle with
nothing in the log to say one had been opened. Both go through one transaction now.

The `--name` rule is the schema's, not an approximation of it — the old predicate accepted a leading
dash and every Unicode letter `str.isalnum` counts. `models.CYCLE_ID_RE` is the one spelling, and a
test holds it against the pattern in both `event.schema.json` and `state.schema.json`.

### An event that could not name its cycle went into the chain anyway

`event_chain.make` refuses an unknown event *name* — a typo would create a kind no query knows to
look for — and said nothing about `cycle_id`, which is the field every per-cycle query reads. Three
callers wrote `state.cycle_id if state else ""`. The log is validated against
`event.schema.json`, where `cycle_id` is non-empty and patterned, so appending one of those turned
the failure it was recording into a **defect in the chain a gate receipt pins**: `rein doctor` would
report the audit log as damaged, and `cycle-close` would refuse to archive it. Refused at
construction now, for the reason the event name already was, and each of the three callers says
which document is missing instead of substituting a value the log cannot carry.

`rein review generate`'s own four SSOT reads moved inside the block that records a failure — with
`state.yaml` read first, so a `plan.yaml` that does not parse is recorded under the right cycle
rather than under none. That was the last failure of gate ④'s machinery that left the log empty.

### `rein --version` reported a broken install

It answered `unknown verb '--version' — run 'rein --help' for the verb list` and exited 2. `help`
has had three spellings from the start; `version` had one, and the invocation everyone reaches for
first was the one that failed. `--version` and `-V` now answer as `version` does.

`rein guard` parsed no arguments beyond `--check-diff`, so `rein guard --help` fell through to the
hook's stdin read and answered a human's question with "unparseable hook payload — allowing without
a gate check", exit 0 — the guard's *allow* code. It has exactly two invocations; `--help` prints
them, and an argument it cannot read denies rather than passing, because a guard given arguments it
does not understand does not know what it is being asked. No host registration passes any
(`pass_filenames: false` in the pre-commit hook, none in the three PreToolUse registrations), so the
fail-open on a malformed payload is now reachable only by a host that actually sends one.

### Two canaries for the directions nothing was checking

models.py's vocabulary header says every one of its constants appears as an `enum` in a schema, and
the existing test checks only the converse — a schema enum drifting from the code. Nothing caught a
constant describing a document field that does not exist, which is how two of them survived. And
`template_lint` now requires every declared schema property to be named by some Python or listed
with a reason, since that class has now bitten four times.

A third holds the guard's rule 1 against rein's own code: no module but `store.py` may
`atomic_write` one of the three machine-written documents. It is the invariant `cycle-close` broke,
and the only form of the check that catches the shape rather than one instance of it — a reset with a
*valid* cycle id produced a valid document either way, so the behavioural test did not discriminate.

Every canary here was verified by reintroducing the defect it describes and watching it fail.

### Not a defect: `_tree_state`

Reported as calling a retired `GitWorkspace.tree_state`. Both were renamed together in 0.3.0
(`fingerprint` / `_fingerprint`) and neither name survives anywhere in the tree. The report came
from a 0.2.3 install standing in a 0.3.0 repository, which is the skew `lock.startup_warning`
prints on every invocation.

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
