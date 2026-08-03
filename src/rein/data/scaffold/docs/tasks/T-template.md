# T-NNN: <task title>

- **Covers requirement/design**: R-x / the relevant section of 20-design.md   <!-- the claim_ids of this task in .rein/plan.yaml. e.g. C-001 -->
- **Kind**: parallel          <!-- foundation | parallel | integration. foundation=a base many depend on / parallel=independent leaves that run concurrently / integration=a join of several -->
- **Phase**: build   <!-- requirements | design | build | verify. Default build. A bug fix originating from /verify is verify. Context for the human; the plan carries the DAG. -->
- **status**: todo            <!-- todo | in_progress | blocked | needs-revision | done. The truth is in .rein/state.yaml -->
- **blockedBy**: none   <!-- tasks that must be done first. e.g. T-001, T-002 -->
- **Dependents (what waits on this task)**: none  <!-- e.g. T-005, T-006. The more there are, the more parallelism is freed by finishing it early -->
- **Owner**: implementer

## To do
<!-- The concrete content to implement. One task = a small, reviewable unit -->


## Acceptance criteria (Definition of Done)
- [ ]
- [ ]

## Automated-test approach (the basis for the green decision)
> A task with this unfilled is not started in `/build`. The loop advances only once this test goes green.
- **Test kind**: unit / integration
- **Test target / cases**:
  -
- **Test run command**: <e.g. `npm test -- <path>`>

## Notes / design decisions
-

## Self-assessment (assumptions, confidence)
> Material for making low-confidence tasks explicit to the human at gate ③.
- **Confidence**: high / medium / low
- **Assumptions made / risks**: <uncertain points, external dependencies, the risk of misreading due to coarse granularity, etc.>
- **Open questions** (decisions to surface at gate ③):
