# Design

> `/design` generates this, starting from `docs/10-requirements.md` (approved).
> Important technical choices are **decided by the human via a `structured-question`** and recorded in `docs/decisions/ADR-*.md`.
> Finalized once a human approves at **gate ②**.

## Architecture overview
<!-- Overall structure. Component diagram or data flow in prose / a simple diagram -->


## Tech stack (finalized)
| Area | Choice | Rationale (ADR) |
|------|------|-----------|
| Language/runtime | | ADR- |
| Key libraries | | ADR- |
| Data persistence | | ADR- |
| Testing | | |

## Implementation approach per requirement

### R-1 → design
- **Required features/modules**:
- **Implementation method**:
- **Existing assets to reuse**:
- **Corresponding technical choice**: ADR-

### R-2 → design
- **Required features/modules**:
- **Implementation method**:

<!-- Continue per requirement -->

## AI agent application design (only if the product is an AI agent app)

> Fill this **only when the product itself is an AI agent** (an LLM that plans, calls tools, and loops). Delete it otherwise.
> These are decided by the human like any other technical choice (options + trade-offs → ADR).

- **Architecture (chosen pattern + why it is the simplest sufficient one)**:
  <!-- Single Agent / Graph / Agents as Tools / Swarm. Note the autonomy↔control trade-off and the deciding ADR. -->
  - Pattern: | Rationale (simplest that meets the requirements): | ADR-
- **Context strategy (keep it minimal — Context Rot / Lost in the Middle)**:
  <!-- compression (sliding-window / summarization) + external persistence + retrieve-only-what's-relevant on demand -->
- **Tool design (reliable tool use)**:
  <!-- specific/unambiguous definitions; retry-friendly structured errors {status,error,received,expected,example}; per-session invocation cap -->

## How non-functional requirements are met
- **Performance**:
- **Security**:
- **Other**:

## Risks / trade-offs
<!-- Leave the aspects the human judged: cost, effort, operational load, etc. -->
-

## Open questions
-

## Adversarial review
> Findings from the independent `adversarial-reviewer` round before gate ② (procedure: design.md step 6),
> with the lead's disposition per finding. Blockers must be `fixed` or `disputed` (with the reason)
> before the gate; the human sees this table — and settles any unresolved dispute — at gate ②.

| ID | Severity (blocker/major/minor) | Finding (with counterexample) | Disposition (revise_* / request_expert / run_experiment / reduce_scope / dispute_finding: why) |
|----|--------------------------------|-------------------------------|-----------------------------------------------------|
| AR-1 | | | |


## Self-assessment (assumptions, confidence)
> Communicated to the human at gate ② as `.rein/prompts/rules/gate-workflow.md` "Gate self-assessment". Do not hide low-confidence design spots.
- **Assumptions made**: <assumptions taken as given about requirements, existing assets, technical choices>
- **Confidence**: per area high / medium / low (e.g. architecture=high / technical choices=medium / non-functional=low). **Attach a reason for low spots.**
- **Open questions / points for the human to decide**:
- **Anticipated risks / trade-offs**:
- **Context-bloat signal** (when relevant): <if the design has grown, link detail out to an ADR instead of inlining; propose compressing resolved log rows>
