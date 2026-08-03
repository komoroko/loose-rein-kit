# /onboard — Map an existing codebase into Loose Rein (brownfield only)

Run once right after `rein init` seeded Loose Rein into an ongoing repository (harmless to
re-run — it refreshes the baseline). This is what makes the existing implementation visible to
the template's machinery: `/req` and `/design` start from the baseline this command produces.
(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Scope guard (read first)

- **Read-only toward the product**: this phase changes no product code and opens no gate.
  The only deliverables are `docs/05-current-state.md` and a summary block in
  `docs/00-product-brief.md`.
- **Do not reverse-generate requirements (R-N) from existing behavior.** Exhaustive
  reverse-specification costs far more than it returns, and it floods `rein dag --trace` with
  "uncovered requirement" noise forever. Requirement IDs and traceability apply to **each
  cycle's delta only**. The existing system is captured as a *baseline*, not as requirements.
- Gates are untouched: onboarding adds no new gate, and none is set to approved by it.
  How to enter the lifecycle from each starting state (existing mid-phase documents, in-flight
  code, no documents at all) is the "Entry points by starting state" table below.

## Steps

1. Delegate to the `architect` role (`role-delegation`; read-only survey) to survey:
   - architecture, entry points, module/directory roles;
   - **reusable assets** (shared utilities, patterns, schemas, fixtures);
   - conventions (naming, test style/placement, commit format) and test/CI commands;
   - **existing documents** (requirements/design/ADR/README wherever they live) — link them,
     never convert or move them;
   - **implementation status**: implemented capabilities, in-flight/half-done work, TODO
     comments, and (reference only) open Issues/PRs.
2. Fill `docs/05-current-state.md` (the scaffold's sections map 1:1 to the survey above).
   Keep it lean — link out instead of inlining (`.rein/prompts/rules/gate-workflow.md` "Context budget").
3. Write a short summary of the current product into `docs/00-product-brief.md` (what it is
   today), keeping the brief's brownfield note about delta-scoped cycles.
   - **Finding no documents at all is a supported, normal state** — the baseline's "Existing
     documents" section just says "none". What the code survey cannot recover is *intent*
     (who this is for, the qualities that matter, non-goals): gather those in a **single
     `structured-question`** and write the answers into the brief's own sections (What / For whom /
     Non-goals / Constraints). Recover only those few lines of intent — do not slide into
     reverse-writing a specification (the scope guard above). From then on this brief plus
     the baseline stand in for the missing documents, and `/req` runs as usual.
4. Check `.rein/config.yaml` against the survey: propose `quality_gate` commands if
   still the defaults, and propose `guard.paths` entries for the repo's real code layout
   (e.g. `src/: tasks`) — **propose only**; the human decides when to enable code-path guarding.
5. Present to the human (no gate — conversational confirmation):
   - the baseline summary and anything you were unsure about (per AGENTS.md
     "Gate self-assessment" spirit: assumptions, low-confidence spots);
   - the **candidate list of in-flight/unfinished work** — the human picks what becomes the
     first delta cycle's scope;
   - next step: write the chosen change into `docs/00-product-brief.md` and run `/req`. Since the
     baseline now holds everything the survey learned, this is a clean checkpoint — suggest
     `session-compaction` (or starting `/req` in a fresh session); the survey conversation is no
     longer needed (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget").

Write the deliverables in the user's language.

## Entry points by starting state

Adopted repos arrive with any mix of documentation and implementation progress. The lifecycle
is always the same; only the intake differs:

| Starting state | Entry |
|---|---|
| No documents at all (implementation only) | `/onboard` alone — the survey is code-driven, so it succeeds; recover intent into the brief (step 3 note), then the next change starts a normal `/req` cycle. |
| No docs beyond a README, implementation stable | Same as above (link the README etc. from the baseline). |
| Requirements/design documents exist for not-yet-built work | Run `/req` → `/design` as a **fast intake**: shape each existing document into the deliverable and have the human open gates ①② normally — that approval *is* the adoption of the old document into this system. |
| Implementation in flight (half-done) | Fast intake as above, then `/tasks`' brownfield note: plan only the **remaining delta**, anchored by an **absorb task** that pins the existing partial implementation green. |
| Docs and implementation both complete (adopting for future work) | `/onboard` alone; the first delta cycle starts when the next change arrives. |

## At each cycle close

`/verify` (done handling) updates the "Implementation status" and "Last updated" sections of
`05-current-state.md` with what the cycle changed, so the baseline stays current without ever
being archived.
