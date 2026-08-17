# T-NNN: <task title>

- **Covers requirement/design**: R-x / the relevant section of 20-design.md   <!-- the claim_ids of this task in .rein/plan.yaml. e.g. C-001 -->
- **Kind**: parallel          <!-- foundation | parallel | integration. foundation=a base many depend on / parallel=independent leaves that run concurrently / integration=a join of several -->
- **Phase**: build   <!-- requirements | design | build | verify. Default build. A bug fix originating from /verify is verify. Context for the human; the plan carries the DAG. -->
- **status**: todo            <!-- todo | in-progress | blocked | needs-revision | awaiting-evidence | done. The truth is in .rein/state.yaml -->
- **blockedBy**: none   <!-- tasks that must be done first. e.g. T-001, T-002 -->
- **Dependents (what waits on this task)**: none  <!-- e.g. T-005, T-006. The more there are, the more parallelism is freed by finishing it early -->
- **Owner**: implementer

## To do
<!-- The concrete content to implement. One task = a small, reviewable unit -->


## Acceptance criteria
> **The machine-readable copy lives in `.rein/plan.yaml` under this task's `acceptance:`, and that
> is the one the loop reads.** A checkbox here is prose: nothing parses it, so "the acceptance
> criteria are met" was only ever an assertion by whoever wrote the code. Write each criterion
> here for a human, and give it an `id` matching the plan entry so the two can be read together.
>
> In the plan, each criterion says how it is judged: `command` (an argv the loop runs in a
> sandboxed profile), `artifact` (paths that must exist), `external` (an observation this loop
> cannot make — a staging check, a device, a person; the task waits at `awaiting-evidence` until
> somebody records it with `rein evidence record`), or no `evidence` at all, which is honest for
> a criterion that is genuinely a judgement call and leaves it to the gate ④ review.

- **A-1**: <one thing, stated so that it could be false>
- **A-2**:

## Automated-test approach (the basis for the green decision)
> The shared DoD (`quality_gate` in `.rein/config.yaml`) is what decides green, for every task
> alike — this section says what *these* tests should cover, not which command runs them.
- **Test kind**: unit / integration
- **Test target / cases**:
  -

## Notes / design decisions
-

## Self-assessment (assumptions, confidence)
> Material for making low-confidence tasks explicit to the human at gate ③.
- **Confidence**: high / medium / low
- **Assumptions made / risks**: <uncertain points, external dependencies, the risk of misreading due to coarse granularity, etc.>
- **Open questions** (decisions to surface at gate ③):
