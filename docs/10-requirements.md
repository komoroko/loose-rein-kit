# Requirements

> `/req` generates/updates this, sounding out from `docs/00-product-brief.md`.
> Finalized once a human approves at **gate ①**. Changes after approval need re-approval.

## Summary
<!-- The value this product provides, in 3 lines or fewer -->


## List of things to achieve

Each item expresses "what the user can do". Priority: Must / Should / Could.
Write acceptance criteria as **measurable, technology-agnostic** checks (a number, a state, an observable behaviour — not "fast" or "easy").
<!-- While drafting, mark anything undecided inline as a `[NEEDS CLARIFICATION: <what>]` marker at the
     exact spot it affects, instead of picking a plausible default. There is no cap on how many:
     every marker is asked of the human or demoted to Open questions with its assumption written
     out, and `rein approve requirements` refuses while any is left standing in the prose. -->

### R-1: <title>
- **Overview**:
- **Background / why needed**:
- **Priority**: Must
- **Acceptance criteria (conditions under which it can be called satisfied)**:
  - [ ]
  - [ ]

### R-2: <title>
- **Overview**:
- **Background / why needed**:
- **Priority**: Should
- **Acceptance criteria (conditions under which it can be called satisfied)**:
  - [ ]

<!-- Add R-3, R-4... as many as needed -->

## Non-functional requirements (criteria)

Non-functional requirements get IDs too (`NFR-1`, `NFR-2`, … in the headings), so `rein dag --trace` can
follow them into the design, tasks, and the test plan the same way as R-N. A cross-cutting NFR with no
dedicated design section or task is fine (the trace only WARNs) — but every NFR **must** appear in
`docs/test/test-plan.md`, which is checked mechanically at `/verify` (`--trace --test-plan`).

### NFR-1: <title, e.g. Performance>
- **Criterion (measurable)**: <e.g. main operations complete within 1 second at the expected data volume>
- **How verified**: <e.g. timed run in the test plan §2 / a task's automated test (set the task's `req: NFR-1`)>

### NFR-2: <title, e.g. Security>
- **Criterion (measurable)**: <e.g. no secrets stored or logged in plaintext>
- **How verified**: <e.g. /security-review + make audit results recorded in the test plan §2>

<!-- Add NFR-3, NFR-4... as many as needed (availability/reliability, operations/maintainability, ...) -->

## Out of scope (non-goals)
-

## Clarifications
<!-- Audit trail of how ambiguities were closed: one bullet per resolved [NEEDS CLARIFICATION] marker / question. -->
- Q: <question> → A: <the human's answer> (YYYY-MM-DD)

## Open questions
<!-- Points to confirm with the human. Resolve before gate ①. -->
-

## Adversarial review
> Findings from the independent `adversarial-reviewer` round before gate ① (procedure: req.md step 6),
> with the lead's disposition per finding. Blockers must be `fixed` or `disputed` (with the reason)
> before the gate; the human sees this table — and settles any unresolved dispute — at gate ①.

| ID | Severity (blocker/major/minor) | Finding (with counterexample) | Disposition (revise_* / request_expert / run_experiment / reduce_scope / dispute_finding: why) |
|----|--------------------------------|-------------------------------|-----------------------------------------------------|
| AR-1 | | | |


## Self-assessment (assumptions, confidence)
> Communicated to the human at gate ① as `.rein/prompts/rules/gate-workflow.md` "Gate self-assessment". Leave it here, not just spoken.
- **Assumptions made**: <assumptions taken as given without confirming; points where, if wrong, the requirements break>
- **Confidence**: high / medium / low (may be split per requirement/area; **attach a reason for low spots**)
- **Open questions / points for the human to decide**:
- **Anticipated risks / trade-offs**:
- **Context-bloat signal** (when relevant): <if this document or its logs have grown enough to risk Context Rot, propose trimming — push detail out to a linked file, compress resolved log rows>
