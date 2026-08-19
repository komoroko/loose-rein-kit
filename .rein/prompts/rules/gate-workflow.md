# Phase-scoped operating rules (gate workflow)

Read by every phase command (`/req` `/design` `/tasks` `/build` `/verify` `/onboard`) on top
of the always-loaded core rules (AGENTS.md). Everything here applies while a phase procedure
is running; the core's gate rules apply at all times regardless.

## Gate self-assessment (required at every gate)

At every gate (①–⑤), present a **self-assessment block** alongside the deliverable — surfacing
the system's own uncertainty is what lightens the human's review. **Three items, and no more**:
**assumptions made**; **confidence** (high / medium / low by area, always with a reason for low
spots); **open questions / points for the human to decide** (most important). Do not pretend to
high confidence to let the human skip verification. For requirements/design/task tickets, put it in
the deliverable itself (each scaffold's "Self-assessment" section), not just spoken.

**What this block deliberately does not carry.** A list of *anticipated risks and trade-offs* — the
independent `adversarial-reviewer` round below asks that question of somebody who did not write the
deliverable, and the same question answered a second time by its author is the self-consistent
explanation this whole arrangement declines to treat as evidence. And a *context-bloat signal* —
that is hygiene addressed to the agent, not a decision for the human, and the pre-compact check
under "Context budget" is where it already lives. Every item a gate presents costs the reader
attention that the items carrying a decision then have to compete with.

Self-assessment alone is not independent verification: gates ①–③ additionally require one
**adversarial-review round** by the `adversarial-reviewer` role — procedure and recording:
the req.md, design.md, and tasks.md procedure files. There is no waiver: a hotfix reduces
its *scope* through `/revise`, it does not skip the round that would have read it.

## While a gate is pending

Do not sit idle — but **never compromise the gate**. Notify the human immediately
(`notify-and-wait`); batch questions into a single `structured-question`. Pull forward **only
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

The first two stages ask for nothing. That is the design: **a reviewer who has to reconstruct the
change before every card spends their attention on reconstruction.**

- **The scope is stated before anything else.** Which commits this review is bound to, how much of
  the change could be read, what could **not** be (the coverage gap, by path and reason), and
  whether it fits one session's budget. An approval covers a boundary; a reviewer who does not know
  the boundary does not know what they approved.
- **The orient stage says what was built, and under what conditions.** The delivered tasks and the
  claims they answer; which dependency manifests, generated files and migrations moved; **which
  sandbox, image and network posture each quality-gate step ran under**; how many quality-gate steps
  ran and which ran for nothing; whether anything ever *launched* the deliverable; the
  Expected/Actual comparison on its three axes; and what is still open — tasks awaiting evidence,
  open change requests, the per-task review findings marked `consider` that stopped nothing, and
  what the implementer said about each task that did **not** land. Every line is **derived from the
  SSOT**, not written by anything, and where it carries a sentence somebody else wrote, the
  confidence and the code anchor travel with it.
- **Orient's one substantive section is what the change now requires of a person.** A setting
  somebody has to supply, a schema somebody has to migrate, a dependency somebody has to provide, a
  signal somebody has to watch. Gate ③ freezes each task's `operator_surface`, and this section
  sorts the blind extractor's operator-facing readings by whether one of those declarations foresaw
  them: **what nobody declared comes first** — nobody decided that would be somebody's job — then
  what was declared and never read out, then a **count** of the ones that went as foreseen. Expected
  rows are a number, because a table of them is where the first two go to hide. A declared surface
  also offers its **as-built** view: the file as it *ends up* at the reviewed commit, which a diff
  never shows, fetched on demand rather than copied into the review.
- **What the gate does not do here is re-open the choice.** The trade-off was decided at gate ②,
  by a human, and it is recorded in the ADR the declaration names. Restating it after the fact would
  put a sentence nobody observed in front of the person deciding, and re-litigate a decision that is
  already made.
- **If the sandbox moved since gate ③, orient says so.** Gate ③ freezes `config.yaml` without its
  image pins, so a task that legitimately adds a dependency can have its sandbox rebuilt without
  re-approving a plan nothing changed. That permission is paid for here: the approver is told,
  rather than left to find out, that the evidence they are signing over was produced in an
  environment the gate ③ approval never saw. It blocks nothing — `kind`, `network_profile` and
  `mount_repo` are still frozen, so a sandbox that *opened* never reaches this gate at all.
- **Decision Cards are the one screen that asks for anything.** Every finding the review could
  not settle — an unaligned claim, a gap, ungrounded extra behaviour, a security finding —
  becomes a card, derived from the findings rather than authored by a reviewer, and **each card
  carries its own evidence**. **Unanswered high/critical cards block the freeze.** Every answer
  carries the human's own **confidence**; nothing defaults it.
- **No card offers accepting the risk.** The dispositions are revise / experiment / expert /
  reduce scope / dispute-with-a-reason. That absence is the policy.
- **A tick means a recorded judgement**, never that a screen was visited. The reading stages
  record nothing and say so, rather than claiming either that they were done or skipped.
- **Coverage is priced by risk, not by presence.** An `insufficient` manifest is a statement about
  reading, not about danger. At high/critical it blocks — "extra behaviour: undeterminable" cannot
  be waved through as zero — and below that it is recorded, shown, and does not shut the gate. Both
  the freeze and `rein approve build` read the same rule, because two rules over one manifest is
  how a low-risk cycle carrying a single unreadable font file ends up with no way through gate ④,
  scope split included: splitting never removes the file.
- **A blown review budget splits the scope**; it never lengthens the screen. The budgets are
  measured, not declarative — including the diff size — so a change too large for one sitting
  blocks the freeze until the scope is reduced or the limit is raised as a recorded decision.

**What is deliberately gone.** A high/critical card used to withhold its evidence until the
reviewer had recorded an unprimed guess, and a guess that missed opened a counterfactual to close.
The intent was cognitive forcing; the effect was a comprehension quiz standing between a human and
the decision they were there to make — and it defended against nothing, since the priming that
matters is the *extractor's*, and that is enforced separately and still is. The forcing function
that remains is the one nobody can clear by rote: a high/critical card cannot lapse into silence,
and the answer carries a confidence the tool will not invent.

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
**Prefer fetch-on-demand over holding everything** — read the slice you need. **A `docs/notes/` memo is a record, not a permanent tier: once its lesson is
promoted (into `AGENTS.md`, an `ADR-*.md`, or the code) the note has served its purpose and is
deleted** — a note that never promotes-then-exits is how records accumulate (a copy that lands
in a product is deletable there; it is outside `upgrade`/`uninstall`).

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
A `state.yaml` gate flip to `approved` written by hand is denied. The only write path is
`approve.record_approval`, reached by a human confirming in one of **two** places: `rein approve
<gate>` at a terminal (`[y/N]`, default no), or the dashboard's approval footer, whose write
session exists only because someone opened the single-use launch link `rein ui` printed to its own
terminal. Both check readiness first, print the digests the approval would cover, and write the
receipt — which records *which channel* confirmed, never who. **Never run the approve command on
the human's behalf, and never treat `rein next`'s `rein approve <gate>` recommendation as
something to execute.** `rein approve` machine-checks readiness (unresolved `[NEEDS
CLARIFICATION]` markers, the review's `subject_head_sha` freshness, coverage sufficiency, blocking
findings, a frozen human review, open change requests, open escalations) and refuses when anything
is missing — there is **no `--force`**. The only standing exception is `guard.template_mode: true`
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
- `.claude/`, `.github/`, `.agents/` + `.codex/` — per-agent entry points and role-agent
  wrappers, thin over `.rein/prompts/`, present only where `rein install <agent>` ran
