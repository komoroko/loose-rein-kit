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

Rules and procedures name human-interaction points with these neutral capabilities, never an
agent-specific tool.

| Capability | Meaning | Lacking it |
|---|---|---|
| `phase-invocation` | run a phase procedure (`/req` … `/status`) | read the command body, execute it |
| `structured-question` | batched multiple-choice questions | numbered chat options, then wait |
| `notify-and-wait` | flag a pending decision, then stop | state it, end the turn |
| `approval-presentation` | present a deliverable for approval | ask for an explicit "approve" |
| `session-compaction` | human-run session reset at a checkpoint | a fresh session; SSOT rehydrates |
| `role-delegation` | delegate to a role agent (analyst/architect/implementer/reviewer) | adopt the role inline, then return; parallel leaves go serial |
| `autonomous-build-iteration` | drive `/build` without per-iteration prompts | re-invoke the procedure each iteration |
| `command-preauthorization` | pre-authorize known-safe commands | approve each interactively |

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
  recorded**, and the receipt binds the digests that approval covered.
- **`.rein/review.yaml`** — the **machine review** and the **human review**, digested
  *separately*. Regenerating the machine review resets the human review; a human answer never
  makes the machine review stale.
- **`.rein/events.ndjson`** — the hash-chained audit log. Every state change records why;
  a deleted, reordered, or re-hashed line breaks the chain a gate receipt pins.

An approval records that a human confirmed at a terminal, never *which* human — there is no
identity-bound mode, so authority never depends on anything outside the repository.

## Gate rules (strict)

1. **Do not work on the next phase while its prerequisite gate is unapproved.** Each command
   checks its prerequisite up front; if unapproved, stop and say what is needed.
2. **Only a human opens a gate, and never you.** Go only as far as an `approval-presentation` and
   stop. The human confirms in one of two places, and **you use neither**: `rein approve <gate>`
   at their own terminal (readiness checked, the covered digests printed, `[y/N]` with the default
   no), or the dashboard's approval footer. **Never edit a gate line yourself, never run
   `rein approve` for them, and never pre-authorize it.** When `rein next` recommends
   `rein approve <gate>` — it does, once a gate is ready — that is a line to *show*, not to run.

   **What this establishes.** Not that a human approved — nothing in the repository can show that,
   and the receipt records only that *a* confirmation happened and over which channel. What holds
   is narrower: **an approval cannot happen by accident, by default, or by a configuration someone
   pre-authorized.** Three mechanisms carry it — the TTY requirement (a piped stdin, a CI job, an
   agent's captured subprocess all fail it), the dashboard's single-use launch link, printed to the
   terminal `rein ui` runs in and readable by nothing else, and `rein doctor`'s check that no
   settings file pre-authorizes a gate-opening verb. A local process with a real pty defeats all
   three, as it always could.

   What is left is **direction**: a surface may record judgements that only ever *narrow* what
   happens next — a change request, a review answer, a disposition — while the one judgement that
   *widens* it needs the capability handover above.
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
run **in code**, not LLM discretion (detail: build.md, tasks.md).

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
- **Small and sure.** One commit, one concern; approval before destructive/outward-facing ops.
- **Context isolation and hygiene.** Delegate phase work to role agents; keep deliverables and
  logs lean (tiers, GC, compaction: the rules module).
- **Promote durable lessons** from `docs/retrospective.md` into the always-loaded files at
  gate ⑤, not archived away.
- If anything behaves oddly, run `rein doctor` first.

## Security gate

**gitleaks** at commit stage; a **structured security review** feeds the grounded review before
gate ④ (bound to the reviewed HEAD; a later commit leaves it stale), repeated with a **dependency
audit** at `/verify` (detail: build.md, verify.md).

## Branch / commit / permissions

- Implement **on a work branch** (`work_branch` in `config.yaml`), never on main; parallel leaves
  use worktree branches (`<branch>-T-NNN`) and route every decision through the control plane so a
  worktree's record survives its deletion.
- Per-task commits **`T-NNN: <summary>`**; commit each phase's deliverables at its gate approval.
- **Push / PR / merge to main are outward-facing** — human approval only, same for GitHub Issues.
- `command-preauthorization` of known-safe commands cuts repeated prompts **without touching
  gates** (generic commands in the installed settings; product-specific ones in the product's
  own) — never pre-authorize push / PR / **merge to main** / `cycle-close`, nor `rein
  approve` (gate rule 2). A worktree merge into the work branch is not one of those: the build
  loop does it, so it is pre-authorized. `rein doctor` checks the gate-opening verbs in code,
  including in the gitignored local settings file.
