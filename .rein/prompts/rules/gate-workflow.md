# Phase-scoped operating rules (gate workflow)

Read by every phase command (`/req` `/design` `/tasks` `/build` `/verify` `/onboard`) on top
of the always-loaded core rules (AGENTS.md). Everything here applies while a phase procedure
is running; the core's gate rules apply at all times regardless.

## Gate self-assessment (required at every gate)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — surfacing
the system's own uncertainty is what lightens the human's review. **Three kinds of item, and no more**:
**assumptions made**; **confidence** (high / medium / low by area, always with a reason for low
spots); **open questions / points for the human to decide** (most important). Three *kinds* is a
limit on what the block is about, never on how many open questions it lists — every point the
human has to decide belongs there, however many there are. Do not pretend to
high confidence to let the human skip verification. For requirements/design/task tickets, put it in
the deliverable itself (each scaffold's "Self-assessment" section), not just spoken.

**What this block deliberately does not carry.** *Anticipated risks and trade-offs* — the
`adversarial-reviewer` round below asks that of somebody who did not write the deliverable, and the
author's second answer to it is the self-consistent explanation this arrangement declines to treat
as evidence. And a *context-bloat signal* — hygiene addressed to the agent, not a decision for the
human; it lives in the pre-compact check under "Context budget".

Self-assessment alone is not independent verification: gates ①–③ additionally require one
**adversarial-review round** by the `adversarial-reviewer` role — procedure and recording:
the req.md, design.md, and tasks.md procedure files. There is no waiver: a hotfix reduces
its *scope* through `/revise`, it does not skip the round that would have read it.

## While a gate is pending

Do not sit idle — but **never compromise the gate**. Notify the human immediately
(`notify-and-wait`); batch questions into a `structured-question` and keep asking in further rounds
until nothing is left that you would otherwise close with your own default — **there is no cap on
how many things get confirmed**, only on how many one round can carry. Batching is against
drip-feeding one question at a time, never a budget for the phase. Pull forward **only
outcome-independent work** (scaffolding, dev-env/CI setup, read-only investigation, fixtures)
— never deliverables premised on the pending decision. Speculative work stays **outside
`guard.paths`** (`tests/` is deliberately unguarded for this); a gate_guard denial marks
the boundary. It is throwaway-by-default, recorded in the phase deliverable's "speculative work
log" (per-phase specifics: each procedure file's "While waiting for approval" section).

## The gate ④ human review

Gate ④ does not review a document, it reviews a **generated grounded review**, and what it asks the
human for is a **judgement**, not a reading. `rein review generate` writes the machine half;
the human half is worked through in `rein ui` — a rail of
**scope → orient → decision → diff → freeze** — and frozen with `rein review complete`. Freezing is
a precondition of `rein approve build` — it is **not** the approval.

The first two stages ask for nothing: a reviewer who has to reconstruct the change before every
card spends their attention on reconstruction.

- **The scope is stated before anything else** — which commits the review is bound to, how much of
  the change could be read, what could **not** be (the coverage gap, by path and reason), and
  whether it fits one session's budget. An approval covers a boundary.
- **The orient stage says what was built, and under what conditions** — the delivered tasks and the
  claims they answer; which dependency manifests, generated files and migrations moved; **which
  sandbox, image and network posture each quality-gate step ran under**; whether anything ever
  *launched* the deliverable; **which tasks' greens were controlled and which could not be** (a task
  that changed no test file is named, not counted); the Expected/Actual comparison on its three axes; and what is still
  open, including what the implementer said about each task that did **not** land. Every line is
  **derived from the SSOT**, and where it carries a sentence somebody else wrote, the confidence and
  the code anchor travel with it. If the sandbox moved since gate ③ it says so: that blocks nothing
  (`kind`, `network_profile` and `mount_repo` are frozen), but the approver is told the evidence was
  produced in an environment the gate ③ approval never saw.
- **Orient's one substantive section is what the change now requires of a person** — a setting to
  supply, a schema to migrate, a dependency to provide, a signal to watch. It sorts the blind
  extractor's operator-facing readings against each task's frozen `operator_surface`: **what nobody
  declared comes first**, then what was declared and never read out, then a **count** of the ones
  that went as foreseen. It does not re-open the choice — that was decided at gate ② and is recorded
  in the ADR the declaration names.
- **Decision Cards are the one screen that asks for anything.** Every finding the review could
  not settle — an unaligned claim, a gap, ungrounded extra behaviour, a security finding —
  becomes a card, derived from the findings rather than authored by a reviewer, and **each card
  carries its own evidence**. **Unanswered high/critical cards block the freeze.** Every answer
  carries the human's own **confidence**; nothing defaults it. The dispositions are revise /
  experiment / expert / reduce scope / dispute-with-a-reason — **no card offers accepting the
  risk**, and that absence is the policy.
- **A tick means a recorded judgement**, never that a screen was visited.
- **Coverage is priced by risk, not by presence.** An `insufficient` manifest blocks at
  high/critical — "extra behaviour: undeterminable" cannot be waved through as zero — and below
  that is recorded, shown, and does not shut the gate. The freeze and `rein approve build` read the
  same rule.
- **A composed review says so, and what it could not read across.** `coverage.composition` names
  every reading the change was read in — one per scoped task, plus the seam over what two scopes
  share and what none covers — and every statement and finding carries the reading it came out of.
  A changed path no reading covered is listed in `unread_paths` and makes the manifest
  `insufficient`. What composition cannot rule out is behaviour that exists only once two readings
  are in one tree; at `critical` it is therefore refused outright and the change is read whole or
  reduced until reading it whole fits.
- **A blown review budget splits the scope**; it never lengthens the screen. The budgets are
  measured, not declarative — including the diff size — so a change too large for one sitting
  blocks the freeze until the scope is reduced or the limit is raised as a recorded decision.

## Context budget (context hygiene)

More context is not better (*Context Rot*, *Lost in the Middle*); every session re-reads the
SSOT and deliverables, so keeping them lean is a first-class quality lever. **Memory lives in
three tiers, each with its own refresh cycle and exit** — no tier grows without bound:

| Tier | Lives in | Refresh cycle | Exit (folds into the next tier) |
|------|----------|---------------|--------------------------------|
| **Short** — session | conversation, open log rows in the phase deliverable, `in_progress` state | each checkpoint (gate approval / build-layer boundary): flush → compress resolved rows → suggest `session-compaction` | only decisions/outcomes survive, into deliverables and resolved log rows |
| **Mid** — cycle | phase deliverables (`docs/**`), `.rein/state.yaml`, retrospective | written per phase, committed at each gate; logs closed at `/verify` | archived by `rein cycle-close`; durable lessons promoted to the long tier |
| **Long** — permanent | `AGENTS.md`, the capability mappings, `.rein/prompts/**`, `docs/00-product-brief.md`, `docs/05-current-state.md`, `docs/archive/` | promotions at gate ⑤; `05-current-state.md` updated at `/verify`; archive appended at `cycle-close` | none — always loaded, keep it leanest |

Rules: **keep deliverables lean; push detail out to linked files** (e.g. an `ADR-*.md`).
**Compress the append-only deliverable logs** at each checkpoint — summarize resolved
deliverable log rows, keep the decision, drop the transcript. **`events.ndjson` is the exception:
it never rotates.** A record that can disappear is not evidence, so the audit chain only grows —
budget for that when deciding what deserves an event. **Failures are summarized, not dumped.**
**Prefer fetch-on-demand over holding everything** — read the slice you need. **A `docs/notes/`
memo is a record, not a permanent tier: once its lesson is promoted (into `AGENTS.md`, an
`ADR-*.md`, or the code) the note has served its purpose and is deleted.**

**Compact the session at clean checkpoints, not mid-flight.** `session-compaction` is
human-run; the agent suggests it — only at a phase or build-layer boundary, and only when the
**pre-compact check** passes in full: (1) the gate decision is recorded and the deliverables
committed; (2) every instruction the human gave this phase is reflected in a deliverable or
the SSOT; (3) no unanswered question or gate presentation is in flight; (4) no task is
`in_progress`, completed tasks merged and `done`; (5) checkpoint GC applied to the resolved
log rows. If any item fails, do not suggest it. Compacting never touches gate truth; `/status`
rehydrates afterwards.

## Cycles, scope changes, hotfixes

An ongoing repo repeats the lifecycle as **delta cycles** — each cycle's docs describe one
change. After `done`, the human runs `rein cycle-close --name <slug>`: deliverables
archive to `docs/archive/`, gates/phase reset; `docs/00-product-brief.md` and the baseline
`docs/05-current-state.md` persist (in a brownfield repo the latter is the existing codebase's
baseline — `/req`/`/design` read it first; traceability R-N / NFR-N covers the delta only).

**Mid-cycle scope change / hotfix / abandonment** (each a human decision): a non-defect scope
addition defers to the next cycle or reopens gate ① via `/revise`. An emergency hotfix is a
*minimal* delta cycle (gates in order, one-paragraph deliverables); if even that is too slow
the human fixes outside the loop — log the escalation, fold it into `docs/05-current-state.md`
at the next `/verify`. Abandonment is `rein cycle-close --name abandoned-<slug>`
(archives partials, resets gates/phase).

## Enforcement detail (the gate rules' mechanism layer)

The installed `rein guard` denies in code at three checkpoints — **edit-time** (editor
hook on deliverable writes), **commit-stage** (`rein guard --check-diff` in pre-commit /
the quality gate), and **merge-stage** (`rein build` re-checks every path a task changed
before it lands; violations escalate as `gate_violation`). Guarded paths: `guard.paths`.
A `state.yaml` gate flip to `approved` written by hand is denied: the only write path is
`approve.record_approval`, reached by a human confirming at their own terminal or in the dashboard
(AGENTS.md "Gate rules" 2). Both check readiness first, print the digests the approval would cover,
and write the receipt. `rein approve` machine-checks readiness (unresolved `[NEEDS
CLARIFICATION]` markers in the document the gate approves — HTML comments excluded, so the
scaffold's own guidance does not count — the review's `subject_head_sha` freshness, coverage
sufficiency, blocking findings, a frozen human review, open change requests, and unfinished tasks,
which is where a task parked by an escalation shows up) and refuses when anything is missing — there is **no `--force`**. The only standing exception is `guard.template_mode: true`
while the repo IS the template. Detail: the `guard` block's comments in `config.yaml`.

Declining is a first-class answer, not a dead end: answering `n`, or using the dashboard's
"request changes", records a change request against the gate (`rein changes`) that **holds the
gate shut until it is answered** and survives the session.

## Repo map

- `.rein/` — SSOT (`state.yaml`, `plan.yaml`, `review.yaml`, `config.yaml`), the event log
  (`events.ndjson`, created on first event), `rein.lock` (tool version + a hash per
  installed file), and the materialized artifacts `rein sync` refreshes: `prompts/`
  (phase procedures, role definitions, these rules modules), `schema/`, and
  `AGENTS.rein.md` (the core rules body)
- `docs/` — phase deliverables; `docs/retrospective.md` holds the retrospective at `done`
- `.gitignore` — `rein init` / `rein sync` keep a marker-guarded block here ignoring the
  scratch the loop regenerates (`.worktrees/`, `.rein/work/`, the generated PR bodies); the
  SSOT and `docs/**` are committed and reviewed at each gate, never ignored
- `.claude/`, `.github/`, `.agents/` + `.codex/` — per-agent entry points and role-agent
  wrappers, thin over `.rein/prompts/`, present only where `rein install <agent>` ran
