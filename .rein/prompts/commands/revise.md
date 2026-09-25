# /revise — Roll back upstream (the going-back loop)

The **first-class operation** for when "go back to the design, even back to the requirements, and reconsider" becomes necessary during implementation/verification.
Symmetric with the human opening a gate, **rewinding approval is also the human's privilege**. This is that procedure.
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## When to use it
**Only when what was *authorized* turns out to be wrong** — a claim that does not mean what it said,
an acceptance criterion that cannot be met, a `scope` that never covered the thing that broke, a
frozen environment that has to change. That is `--to mandate`, and it re-opens `plan.yaml`.
`--to acceptance` withdraws a change that was already taken.

**Also for re-cutting the task DAG** — splitting, merging or re-scoping tasks. It reads like a
decomposition detail and is not one: `tasks[].acceptance` is in `plan.yaml` with the claims, so a
new breakdown can carry a softened criterion, and the freeze covers the whole document. What needs
no roll back is the *order*: a missing dependency edge — one task has to wait for another the plan
did not say it waits for — is `rein task order <T-NNN> --after <T-MMM> --reason "…"`. It is written
beside the frozen plan, not into it, so the plan's digest and every approval bound to it stand; the
chain records it and acceptance lists it. A re-approval after a real roll back shows the approver
what changed since their last yes, not the plan again.

**Not for any of these**, which need no approval and no roll back:
- A code defect the grounded review found. `rein build` repairs every blocking finding a task's
  declared scope owns and reads the change again, moving no gate (`repair.route`).
- A defect `/verify` finds in the code. Add the task; the mandate already authorizes fixing it.

## Steps
1. **Confirm the defect and the human's decision**: present the escalation log / needs-revision points, say plainly *what about the authorization* is wrong, and have the human decide. Do not roll back on your own — and do not propose one for work that is merely harder than expected.
2. Finalize the target gate (`mandate` | `acceptance`) and the reason **in a single `structured-question`**.
3. **Reset gates in a chain** (deterministic process):
   ```
   rein revise --to <gate> --reason '<reason>'
   ```
   `rein revise` resets that gate and every one after it to `pending` **in a chain**, and records it in the roll-back log. This prevents the stale-approval inconsistency of "upstream pending while downstream approved". `--to mandate` also un-freezes `plan.yaml` and `config.yaml`; from then on `gate_guard` denies writes to the guarded paths again, because nothing authorizes them. Use `--dry-run` to check just the plan.
4. **Task impact analysis (deterministic mark, then reconcile — do not discard)**: before fixing upstream, mark the ripple to existing tasks in code.
   - Identify the tasks **directly affected** by the upstream change, then mark them **and their transitive dependents (downstream)** as `needs-revision` deterministically:
     ```
     rein revise --impacted T-00x,T-00y
     ```
     (combinable with `--to` in one invocation; `--dry-run` previews; `rein dag --impacted` enumerates the same set read-only). Missing an impacted task is the dangerous direction, so the **whole closure is marked mechanically** — nothing in it runs until reconciled.
   - **A code defect the acceptance gate found is not an upstream change, and does not come here.** `rein build` repairs every blocking finding a task's declared scope owns and reads the change again, without moving a gate (`repair.route`). There used to be a `--from-review` that derived the seed ids from those findings, and it marked the task *and its whole dependent closure* `needs-revision` — a status about the plan — so a repair that changed no requirement, no claim and no plan demanded a `/tasks` reconcile and a re-approval of the mandate gate. What reaches `/revise` from the acceptance gate is what a human decided *is* an upstream defect: a Decision Card answered `revise_design` or `revise_requirement`, which is a different sentence from "the code is wrong".
   - Marking is all this step does. The marked closure is then reclassified inside the re-run of `/tasks` ("Re-run after a roll back", which owns the keep / modify / obsolete / new taxonomy and what becomes of a task that was `done`) — nothing in the closure runs until that reconcile has happened.
5. **Guide to rebuilding**: say which document has to change and point at the command that writes it (`/req`, `/design`, `/tasks` — in whatever order the defect calls for). Reflect the reconcile inside the re-run of `/tasks`, and present the **impact (the impacted list and classification)** to the human at the mandate gate for re-approval.

## Principles
- **Rewinding approval is the human's privilege.** `/revise` is run only under the human's explicit judgment.
- **Do not discard and rebuild tasks.** Reconcile existing tasks against the revised upstream, and pick up the impact exhaustively with deterministic computation (`--impacted`).
- The truth is `.rein/state.yaml` (gates, task status), `.rein/plan.yaml` (the tasks), and `.rein/events.ndjson` (the roll-back log). `/revise` also clears the receipts of the reset gates and returns the **human** half of
  `review.yaml` to `not_started` — its answers, and the freeze `rein approve acceptance` re-checks, were
  recorded about an implementation of a mandate that no longer stands. The machine half is left as it
  is: it is a reading of the code rather than of the plan, regenerating it costs three reviewer
  launches, and clearing it here would destroy the thing those answers were answers *to*.
