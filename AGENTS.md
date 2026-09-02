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
| `approval-presentation` | present a deliverable for approval | ask for an explicit "approve" |
| `session-compaction` | human-run session reset at a checkpoint | a fresh session; SSOT rehydrates |
| `role-delegation` | delegate a phase's work to a role agent (analyst/architect/reviewer) | adopt the role inline, then return |
| `command-preauthorization` | pre-authorize known-safe commands | approve each interactively |
| `background-wait` | start a long command detached and be re-entered when it exits — waiting, not polling | detach with the output in a file, end the turn, read the log when a human brings you back |

## Language

Conversation and deliverables (`docs/**`) are written in **the user's language**; template
files stay in English. Machine-read vocabulary (`pending`/`approved`, task `status`/`kind`
values, `epistemic_status`) stays as-is in every language.

## Development lifecycle

```
brief → requirements → design → tasks → build → verify → done
        (/req)        (/design) (/tasks) (/build) (/verify)
          ▲gate①        ▲gate②     ▲gate③   ▲gate④    ▲gate⑤
```

`/req`→`docs/10-requirements.md` (gate① requirements) · `/design`→`docs/20-design.md`+ADRs
(gate② design) · `/tasks`→`docs/tasks/T-*.md`+`plan.yaml` (gate③ tasks) · `/build`→code+tests
then a **grounded review** (gate④ build) · `/verify`→`docs/test/test-plan.md` (gate⑤ release).

`/status` shows progress; `rein next`/`ui` show the same board (a fixed safe-operations
whitelist, never phase execution). At `done`, `/verify` records `docs/retrospective.md`. An
ongoing repo repeats the lifecycle as **delta cycles**, closed with `rein cycle-close`
(mechanics: the rules module). **A scope change to approved requirements goes through
`/revise` or the next cycle — never widened silently.**

## Single Source of Truth (SSOT)

Four documents, distinct roles — do not conflate them:

- **`.rein/plan.yaml`** — the frozen **Expected Model**: one claim per requirement
  (`R-N`/`NFR-N`), and the task DAG. `claim_ids` threads each task back to the claim it
  answers, cross-checked by `rein dag --trace`. Frozen at gate ③.
- **`.rein/state.yaml`** — phase, gate approvals, task status. `gates.<name>` is
  `pending`|`approved` — **the only write path to `approved` is a human approval `rein`
  recorded**, and the receipt binds the digests that approval covered. Gate ③ also pins the
  **prose the build reads** (`plan.sources`: the task tickets and the design/requirements
  documents); a task's `evidence` records the tree its `done` was decided on. The receipt
  records which channel confirmed, never which human.
- **`.rein/review.yaml`** — the **machine review** and the **human review**, digested
  *separately*. Regenerating the machine review resets the human review; a human answer never
  makes the machine review stale.
- **`.rein/events.ndjson`** — the hash-chained audit log. Every state change records why;
  a deleted, reordered, or re-hashed line breaks the chain a gate receipt pins.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command
   checks its prerequisite up front; if unapproved, stop and say what is needed.
2. **Only a human opens a gate, and never you.** Go only as far as an `approval-presentation` and
   stop. The human confirms in one of two places, and **you use neither**: `rein approve <gate>`
   at their own terminal (readiness checked, the covered digests printed, `[y/N]` with the default
   no), or the dashboard's approval footer. **Never edit a gate line yourself, never run
   `rein approve` for them, and never pre-authorize it.** When `rein next` recommends
   `rein approve <gate>` — it does, once a gate is ready — that is a line to *show*, not to run.
3. **Do not silently fix problems in requirements/design.** Set the task `needs-revision`,
   record a `knowledge-gap`/escalation event, and raise it to the human.

Enforcement is layered: `rein guard` denies violations in code at edit/commit/merge
stage; unreadable gates **fail closed**. **A guard denial marks a gate boundary — never
disable, relax, or bypass it** (detail: the rules module).

## Roll back (returning upstream)

On a confirmed upstream defect, roll back at the human's discretion with `/revise`: **gates
reset in a chain** — an upstream `pending` never leaves a downstream gate `approved`, and it
invalidates the receipts and the review built on top of it. **Rewinding approval is a human
privilege**, never automatic. Reclassify each task the impact analysis (`rein dag
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
earlier attempts tried. The one deliberate exception is gate ④'s blind extractor: never give it
the plan.

**Whoever judges does not repair.** The per-task reviewer is launched read-only and writes
findings; the implementer resolves them and the reviewer looks again.

## Principles

- **Reuse first; build only the minimum acceptance criteria require (YAGNI)** — speculative
  generality no requirement names is scope creep.
- **A claim with no evidence is `unknown`, never prose.** At gate ④, whether the code satisfies
  a claim is judged on three separate axes (integrity / semantic support / conformance) by
  comparing what the plan says (Expected) against what a reviewer that never saw the plan read
  out of the code (Actual) — there is no single `verified`, and "extra behaviours: 0" shows only
  with the Coverage Manifest that earned it.
- **Pass the quality gate before moving on.** DoD = `quality_gate` in
  `.rein/config.yaml` (default `test`→`check`→`review`→`smoke`; runnable deliverables
  set `smoke`'s `required: true`). The lead **re-runs each command step and reads its exit
  status** — a delegated agent's textual "green" is never evidence. Repo code and tests run in
  the **OCI sandbox**, never on the host.
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
  and neither chosen by the implementer (a human freezes the list at gate ③). Each criterion says
  how it is judged: `command`, `artifact`, `external`, or nothing at all, which is honest for a
  judgement call and leaves it to gate ④. **`external` is evidence this loop cannot obtain** — a
  staging check, a device, a person — so the work merges and the task waits at
  **`awaiting-evidence`** until somebody records what they saw with `rein evidence record`. That
  record binds the tree it was made against, so changing the code retires it.
- **Small and sure.** One commit, one concern; approval before destructive/outward-facing ops.
- **Context isolation and hygiene.** Delegate phase work to role agents; keep deliverables and
  logs lean (tiers, GC, compaction: the rules module).
- **Promote durable lessons** from `docs/retrospective.md` into the always-loaded files at
  gate ⑤, not archived away.
- If anything behaves oddly, run `rein doctor` first.
- **The verb list is in the CLI, not in this file.** `rein help --all` names every verb (the
  default listing carries only the ones a human types) and `rein <verb> --help` gives its
  arguments — read those rather than guessing a spelling out of prose.

## Security gate

**gitleaks** at commit stage; a **structured security review** feeds the grounded review before
gate ④ (bound to the reviewed HEAD; a later commit leaves it stale). Gate ⑤ **carries that review
rather than re-reading the code** — its readiness refuses a review that is not about this HEAD, and
its receipt binds the machine digest — and runs a **dependency audit**, the one security answer
that is not a function of the tree and therefore the only one that must be taken again (detail:
build.md, verify.md).

## Branch / commit / permissions

- Implement **on a work branch** (`work_branch` in `config.yaml`), never on main; parallel leaves
  use worktree branches (`<branch>-T-NNN`) and route every decision through the control plane so a
  worktree's record survives its deletion.
- Per-task commits **`T-NNN: <summary>`**; commit each phase's deliverables at its gate approval.
- **Push / PR / merge to main are outward-facing** — human approval only, same for GitHub Issues.
- A cycle may ship as **one pull request** (`rein pr-draft` assembles the body) or as a **stack of
  them, one per task** (`rein pr-stack`). A stack opens as **drafts** before gate ④ and is lifted
  by `rein pr-stack --ready` once a human approves it, and landed by `rein pr-stack --merge`. All
  three confirm at a terminal first and none may be pre-authorized. The slices are registered as a
  **GitHub stack**, and `--merge` lands the whole of it in one atomic `gh stack merge`.
- **A stack is merged whole, never in part.** Merging a subset makes GitHub rebase the pull
  requests above the cut onto the new base with new commit ids, so every `completed_commit` above
  it names a commit in no branch's history. Squash and rebase merges strand them the same way.
  Merged atomically, nothing is rebased and the commits the build produced are the ones that land.
- **A stack is never rebased.** A review fix is committed onto the slice that introduced the code
  and carried upward by `rein pr-stack --restack`, which merges. Rewriting history strands every
  `completed_commit` and gate receipt on commits that no longer exist.
- `command-preauthorization` of known-safe commands cuts repeated prompts **without touching
  gates** (generic commands in the installed settings; product-specific ones in the product's
  own) — never pre-authorize push / PR / **merge to main** / `cycle-close` / `pr-stack`, nor `rein
  approve` (gate rule 2). A worktree merge into the work branch is not one of those: the build
  loop does it, so it is pre-authorized. `rein doctor` checks the gate-opening verbs in code,
  including in the gitignored local settings file.
