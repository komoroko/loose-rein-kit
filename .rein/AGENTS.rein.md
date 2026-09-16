# Loose Rein — Agent Operating Rules

Loose Rein develops software **Human on the Loop**: a coding agent performs the work and every
phase is judged against **externally-anchored, independent evidence** — not the agent's own
self-consistent explanation; **humans review/decide at the "gate" on each phase boundary, and a
gate opens only by a recorded human approval.** The machinery is an installed CLI (`rein`); the
repository carries only its state — `.rein/` (SSOT, lock, materialized prompts/schema) and
`docs/`.

This file holds the **always-true rules**. Each phase's procedure lives in
`.rein/prompts/commands/*.md`; phase-scoped rules live in
`.rein/prompts/rules/gate-workflow.md` — the phase commands read both. Your capability
mapping (`CLAUDE.md`, or the Copilot / Codex mapping file — present only when installed)
realizes the vocabulary below; with none, use the degradation column.

## Capability vocabulary (portable verbs)

Rules and procedures name the points where they need something of the host — a human interaction,
or a way of running something — with these neutral capabilities, never an agent-specific tool.

| Capability | Meaning | Lacking it |
|---|---|---|
| `phase-invocation` | run a phase procedure (`/req` … `/status`) | read the command body, execute it |
| `structured-question` | batched multiple-choice questions | numbered chat options, then wait |
| `notify-and-wait` | flag a pending decision, then stop | state it, end the turn |

`notify-and-wait` is how *you* hand a decision back. Reaching the person who is not looking is
not yours and does not degrade with the host: `rein ui` watches the SSOT for as long as it runs
and runs the channel configured in `$XDG_CONFIG_HOME/rein/notify.yaml` when the decision waiting
on a human changes. The two gates say how often the work stops; this is what decides how long
each stop lasts, so it is the harness's and not the CLI's. A notification carries what is waited
on and where — never the evidence, and never a way to answer.
| `approval-presentation` | present a deliverable for approval | ask for an explicit "approve" |
| `session-compaction` | human-run session reset at a checkpoint | a fresh session; SSOT rehydrates |
| `role-delegation` | delegate a phase's work to a role agent (analyst/architect/reviewer) | adopt the role inline, then return |
| `command-preauthorization` | pre-authorize known-safe commands | approve each interactively |
| `background-wait` | wait out a command that runs longer than one turn's worth of patience, without asking it anything: either the host re-enters you when it exits, or the tool call itself waits | run it in the foreground with the longest wait the host allows; only when even that cannot hold it, detach with the output in a file, end the turn, and read the log when a human brings you back |

## Language

Conversation and deliverables (`docs/**`) are written in **the user's language**; template
files stay in English. Machine-read vocabulary (`pending`/`approved`, task `status`/`kind`
values, `epistemic_status`) stays as-is in every language.

## Development lifecycle

**A human approves twice, and neither approval is about the order of the work.**

```
                    ┌─ mandate ─────────────────┐   ┌─ acceptance ───────────┐
drafting ───────────┤ what the loop may change, ├───┤ the evidence is there, ├─── done
  /req /design      │ what it must make true,   │   │ take the change        │
  /tasks (any       │ what evidence counts      │   └────────────────────────┘
  order, repeated)  └───────────────────────────┘        ▲gate②
                             ▲gate①                      /verify presents it
```

`/req`→`docs/10-requirements.md`+the claims · `/design`→`docs/20-design.md`+ADRs ·
`/tasks`→`docs/tasks/T-*.md`+`plan.yaml`'s task DAG+a measured **baseline**. The three write **one
mandate** between them and are material for it, not gates of their own: run them in whatever order
the change calls for, repeat them, or skip one whose answer is already obvious. `rein approve
mandate` is the single decision that covers all three.

Inside an approved mandate, `/build` implements and verifies — decomposing, reordering and
re-running as it needs to, because none of that changes what it may touch or what it must prove.
`/verify` adds `docs/test/test-plan.md` and a **dependency audit**, then presents the **grounded
review** for `rein approve acceptance`.

The mandate measures the work branch's quality gate before it authorizes a plan against it
(`rein baseline measure`): a step already red is fixed or frozen as a deliberate decision, never
discovered by the first task to spend its send-back budget on it.

`/status` shows progress; `rein next`/`ui` show the same board (a fixed safe-operations
whitelist, never phase execution). At `done`, `/verify` records `docs/retrospective.md`. An
ongoing repo repeats the lifecycle as **delta cycles**, closed with `rein cycle-close`
(mechanics: the rules module). **Widening what the loop may change — a new scope path, a new claim,
a relaxed acceptance criterion — goes through `/revise` or the next cycle, never silently. So does
re-cutting the tasks, because a task's acceptance criteria are in the plan with them: the freeze is
`plan.yaml` whole.** What needs nobody is the *order* — consuming the DAG, reordering what the
dependencies allow, re-running what went red.

## Single Source of Truth (SSOT)

Four documents, distinct roles — do not conflate them:

- **`.rein/plan.yaml`** — the frozen **Expected Model**: one claim per requirement
  (`R-N`/`NFR-N`), the task DAG, and the `decisions` record. `claim_ids` threads each task back to
  the claim it answers, cross-checked by `rein dag --trace`. Frozen when the mandate is approved.
- **What reaches a human is decided by reach, never by "would I otherwise use a default".** Each
  decision drafting meets carries `reach: mandate | local`. `mandate` means undoing it later moves
  a claim, a scope boundary or what counts as evidence — a human settles it. `local` means undoing
  it costs one task and no claim — **the loop settles it and records the reasoning**, and the
  mandate screen shows the human what they were *not* asked, which is where that reasoning is
  overruled while overruling it is still cheap. The wider criterion asks about nearly every choice
  a design contains and spends the drafting phase before the irreversible ones come up.
- **A review lens is a record with a condition, not a paragraph.** `rein lens --select <stage>`
  gives the reviewer the lenses whose condition holds for *this* change; a `standard` one applies
  without asking, a `conditional` one is proposed at the mandate gate, an `unclassified` one is off
  until somebody writes down when it applies. Sending a reviewer at a failure that cannot occur
  here costs a pass over the deliverable and returns nothing, while the findings that *are*
  possible compete with it for attention. Over-reviewing is not thorough.
- **A lens earns its place the second time, and loses it by never finding.** A one-off finding is
  recorded `unclassified` — once is an incident, twice is what tells you the condition. The reverse
  rule is `rein lens --stats`: applied and found counts per lens, across archived cycles, naming
  the ones that keep applying and never find. Counted, never capped — a ceiling on how many lenses
  may exist gets answered by deleting whichever is cheapest to delete.
- **`status: unknown` is an answer.** Record it rather than filling it in with a default, and never
  write a claim for it: a claim nothing can make true cannot be judged. A `mandate` decision left
  `unknown` is refused by `rein approve mandate` — narrow the mandate so it does not reach it, or
  make answering it this cycle's scope. Relabelling it `local` to clear the check is falsifying the
  record.
- **`.rein/state.yaml`** — phase, gate approvals, task status. `gates.<name>` is
  `pending`|`approved` — **the only write path to `approved` is a human approval `rein`
  recorded**, and the receipt binds the digests that approval covered. The mandate approval also pins the
  **prose the build reads** (`plan.sources`: the task tickets and the design/requirements
  documents); a task's `evidence` records the tree its `done` was decided on. The receipt
  records which channel confirmed, never which human.
- **`.rein/review.yaml`** — the **machine review** and the **human review**, digested
  *separately*. Regenerating the machine review resets the human review; a human answer never
  makes the machine review stale.
- **`.rein/events.ndjson`** — the hash-chained audit log. Every state change records why;
  a deleted, reordered, or re-hashed line breaks the chain a gate receipt pins.

## Gate rules (strict)

1. **Do not change the product without an approved mandate that covers the path.** `/build` checks
   it up front; if the mandate is pending, stop and say what is missing. Writing the mandate's own
   material — requirements, design, task tickets — needs no approval and has no order: what is
   gated is touching the product, not the sequence you think in.
2. **Only a human opens a gate, and never you.** Go only as far as an `approval-presentation` and
   stop. The human confirms in one of two places, and **you use neither**: `rein approve <gate>`
   at their own terminal (readiness checked, the covered digests printed, `[y/N]` with the default
   no), or the dashboard's approval footer. **Never edit a gate line yourself, never run
   `rein approve` for them, and never pre-authorize it.** When `rein next` recommends
   `rein approve <gate>` — it does, once a gate is ready — that is a line to *show*, not to run.
3. **Do not silently fix problems in requirements/design.** Set the task `needs-revision`,
   record a `knowledge-gap`/escalation event, and raise it to the human.
4. **Do not widen the mandate to fit the work.** Reaching outside its `scope`, adding a claim, or
   softening an acceptance criterion is a change to what was authorized, and `/revise` is how it is
   asked for — **and re-cutting the task DAG is one of these, not an exception to it.** A task's
   `acceptance` list lives in `plan.yaml` beside the claims, so "just a different decomposition" is
   a shape a softened criterion travels in; the freeze covers the whole document and `rein guard`
   refuses an edit to it. What the loop decides alone is the order it consumes that DAG in.

Enforcement is layered: `rein guard` denies violations in code at edit/commit/merge
stage; unreadable gates and an unreadable scope **fail closed**. **A guard denial marks a boundary
of what was delegated — never disable, relax, or bypass it** (detail: the rules module).

## Roll back (returning upstream)

On a confirmed defect in what was authorized, roll back at the human's discretion with `/revise
--to mandate` (or `--to acceptance`): **gates reset in a chain** — an upstream `pending` never
leaves a downstream gate `approved`, and it invalidates the receipts and the review built on top of
it. **Rewinding approval is a human privilege**, never automatic. Reclassify each task the impact analysis (`rein dag
--impacted`) flags, never discard (procedure: revise.md, tasks.md).

## Task dependency graph

Tasks form a **DAG**: kind = **foundation** / **parallel** / **integration**; layers and the
critical path derive from `blockedBy`. Consumption order, parallelism, merge, and stopping
run **in code**, not LLM discretion (detail: build.md, tasks.md). A task's `scope` in the plan
says where its work belongs, and the loop checks the diff against it — reaching into another
task's territory blocks the task rather than landing.

**The loop derives; agents do not re-derive.** Each launch is handed a **dossier**
(`.rein/work/T-NNN.json`) with the claims the task answers and what each asserts, its acceptance
criteria, its scope, the changed paths split into source / tests / mechanical churn, and what
earlier attempts tried. The one deliberate exception is the grounded review's blind extractor: never give it
the plan.

**Whoever judges does not repair — and both halves are the loop's.** The reviewer is launched
read-only and writes findings; an implementer resolves them and the reviewer looks again. That
holds over the whole change as well as inside a task: the build reads the change, repairs every blocking
finding a task's declared scope owns, and reads it again from cold, up to
`review_policy.repair_rounds`. No gate moves — a repair inside an approved scope changes no
requirement, no claim and no plan, and `rein guard` makes that mechanical rather than promised.
Whether a finding closed is decided by the next reading, never by the fixer's account of itself.

**A finding is routed by what repairing it would change, not by who found it** (`repair.route`).
Three classes: **code** — one task's scope owns it and no claim, criterion or requirement moves —
the loop repairs it. **plan** — a claim, criterion, requirement, or the frozen environment has to
change — a human, through `/revise`. **judgement** — deciding which of those two it *is*: a
`diverged` claim, an extra behaviour nobody asked for. That is what a Decision Card is for, and
answering one `revise_implementation` hands the subject back to the loop as a code repair. The
human decides *whether*; the loop does the work.

## Principles

- **Reuse first; build only the minimum acceptance criteria require (YAGNI)** — speculative
  generality no requirement names is scope creep.
- **A claim with no evidence is `unknown`, never prose.** In the grounded review, whether the code satisfies
  a claim is judged on three separate axes (integrity / semantic support / conformance) by
  comparing what the plan says (Expected) against what a reviewer that never saw the plan read
  out of the code (Actual) — there is no single `verified`, and "extra behaviours: 0" shows only
  with the Coverage Manifest that earned it.
- **Pass the quality gate before moving on.** DoD = `quality_gate` in
  `.rein/config.yaml` (default `test`→`check`→`review`→`smoke`; runnable deliverables
  set `smoke`'s `required: true`). The lead **re-runs each command step and reads its exit
  status** — a delegated agent's textual "green" is never evidence. A command step **runs repo code
  and tests in the OCI sandbox, never on the host** (`executors.quality_gate_profile`, `kind: oci`,
  no egress). The agent CLI that *wrote* them is the other question and has its own answer:
  `executors.agent_profile` (`kind: oci-agent`) launches every implementer, reviewer and fixer in a
  box with the worktree mounted, the control socket bound in, and **no HOME of yours, no ~/.ssh, no
  ~/.aws, no docker socket**. That kind *is* granted egress — an agent that cannot reach its model
  API does nothing — so it is not a boundary against exfiltration and does not claim to be. The key
  is **optional**: the image has to carry the CLI, so absent it the agent runs on the host with your
  credentials, and `rein doctor`, the dossier and the acceptance brief all say which it was.
- **`done` means the evidence was there, not that the agent stopped.** A task closes only when
  the DoD went green **against the tree the task actually produced** — a content fingerprint
  `state.yaml` records beside the status. An attempt that changed nothing does not reach the gate
  at all: a green over an unchanged tree is a fact about code that was already there. An
  implementer ends with **`rein report --outcome implemented|blocked|needs-revision`**, the only
  channel its account of the work travels on; `blocked` and `needs-revision` park the task
  **before** a reviewer or a test suite is spent on it, and no outcome it can report finishes
  anything. What it says is a claim (`--touched` is checked against the real diff), never a verdict.
- **A green is evidence only if it could have been red.** The tests the DoD runs were written by
  the implementer in the same launch as the code, and re-running them defends against an agent that
  *lies*, never against one that *self-confirms*. So the loop takes a **negative control**: the same
  command steps re-established over the base, with only the task's test half applied. Still green
  means no test in the change exercises it, and the task goes back rather than landing. **The two
  outcomes are not worth the same**: a green control is a fact about every test in the change at
  once, while a red one says only that the test half is not inert against the old code — it cannot
  tell an assertion that failed from an import that was never there, and does not claim to.
  Whether the tests are any *good* is the reviewer's question, and the reviewer reads them. A task
  that changed no test file has no control to take — **recorded, never passed**, so "this green
  rests on tests nobody wrote for it" is on the record instead of being a silence.
- **A task's own bar is `acceptance` in the plan, and the DoD still runs.** The DoD asks whether
  the code is *sound*; a task's acceptance criteria ask whether it did what it was *for* — both,
  and neither chosen by the implementer (a human freezes the list with the mandate). Each criterion says
  how it is judged: `command`, `artifact`, `external`, or nothing at all, which is honest for a
  judgement call and leaves it to the grounded review. **`external` is evidence this loop cannot obtain** — a
  staging check, a device, a person — so the work merges and the task waits at
  **`awaiting-evidence`** until somebody records what they saw with `rein evidence record`. That
  record binds the tree it was made against, so changing the code retires it.
- **Small and sure.** One commit, one concern; approval before destructive/outward-facing ops.
- **Context isolation and hygiene.** Delegate phase work to role agents; keep deliverables and
  logs lean (tiers, GC, compaction: the rules module).
- **Promote durable lessons** from `docs/retrospective.md` into the always-loaded files at
  acceptance, not archived away.
- If anything behaves oddly, run `rein doctor` first.
- **The verb list is in the CLI, not in this file.** `rein help --all` names every verb (the
  default listing carries only the ones a human types) and `rein <verb> --help` gives its
  arguments — read those rather than guessing a spelling out of prose.

## Security gate

**gitleaks** at commit stage; a **structured security review** feeds the grounded review before
acceptance. What "stale" means there is measured on content, over **two subjects**: the *product*, and
the **host surfaces** `rein install` wrote — the settings, hooks, MCP servers and instruction files
a CLI reads before it reads its prompt. The security reviewer is sent a checkout of the head with
those in it and told that a pre-authorized command or a hook added there is a finding, while the
product digest is taken with them excluded so the blind extractor never reads this tool's own
orchestration text. One digest could not carry both questions, and the one it dropped was the
security one: a commit that widened `permissions.allow` and touched nothing else moved no key,
replayed the cached answer, launched no reviewer, and left the review calling itself fresh.
`.rein/` is in neither subject — committing `review.yaml` is itself a later commit and must not
invalidate the review it records. A false positive is contradicted by a human with
`dispute_finding`, and that record lives in `state.yaml` bound to the anchored text — so it
survives the regeneration that discards the human review, and lapses if that code is edited. Acceptance **carries the review rather than re-reading the code** — its
receipt binds the machine digest — and runs `rein audit run`, the one security answer that is not
a function of the tree and therefore the only one that expires without the repository moving. It
runs on the host and nowhere else — an audit reads a published database, and the one sandbox kind
with egress exists to carry an agent's model calls, not to produce a security answer somewhere
nobody can see — and a machine that could not answer records nothing, because "could not ask" is
not "the answer is bad" (detail: build.md, verify.md).

## Branch / commit / permissions

- Implement **on a work branch** (`work_branch` in `config.yaml`), never on main; parallel leaves
  use worktree branches (`<branch>-T-NNN`) and route every decision through the control plane so a
  worktree's record survives its deletion.
- Per-task commits **`T-NNN: <summary>`**; commit each phase's deliverables at its gate approval.
- **Push and PR are outward-facing** — human approval only, same for GitHub Issues.
- **Merging into the base is outside this harness.** It takes a change to acceptance and leaves it
  reviewable; acceptance approved the change, not the push to the base, and asking a second time
  for the same decision is one approval too many. Whoever owns the base lands it.
- A cycle may ship as **one pull request** (`rein pr-draft` assembles the body) or as a **stack of
  them, one per task** (`rein pr-stack`). A stack opens as **drafts** before acceptance and is
  lifted by `rein pr-stack --ready` once a human approves it. Both confirm at a terminal first and
  neither may be pre-authorized. The slices are registered as a **GitHub stack** at push time.
- **A stack is merged whole, never in part.** This is the harness's to *say*, not to do: merging a
  subset makes GitHub rebase the pull requests above the cut onto the new base with new commit ids,
  so every `completed_commit` above it names a commit in no branch's history. Squash and rebase
  merges strand them the same way. `gh stack merge <top> --merge` lands the whole of it atomically,
  and nothing is rebased. The pull-request body carries this warning to whoever presses the button.
- **A stack is never rebased.** A review fix is committed onto the slice that introduced the code
  and carried upward by `rein pr-stack --restack`, which merges. Rewriting history strands every
  `completed_commit` and gate receipt on commits that no longer exist. The grounded review's own repairs follow
  the same rule and the build loop does it for them: the fix is committed in a worktree on the
  owning slice's branch and merged upward, never at the work branch's tip.
- `command-preauthorization` of known-safe commands cuts repeated prompts **without touching
  gates** (generic commands in the installed settings; product-specific ones in the product's
  own) — never pre-authorize push / PR / `cycle-close` / `pr-stack`, nor `rein
  approve` (gate rule 2). A worktree merge into the work branch is not one of those: the build
  loop does it, so it is pre-authorized. `rein doctor` checks the gate-opening verbs in code,
  including in the gitignored local settings file.
