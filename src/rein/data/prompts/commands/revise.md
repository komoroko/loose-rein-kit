# /revise — Roll back upstream (the going-back loop)

The **first-class operation** for when "go back to the design, even back to the requirements, and reconsider" becomes necessary during implementation/verification.
Symmetric with the human opening a gate, **rewinding approval is also the human's privilege**. This is that procedure.
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## When to use it
- When `/build`'s implementer reports `needs-revision` (a requirements/design defect) and the loop has stopped.
- When `/verify` reveals a requirement/design-level problem (a spec error, etc.).
- When `/verify` finds an **implementation-level defect serious enough to reopen the build**: target `build` — `gates.build`/`gates.release` go back to `pending` and gate ④ is re-taken after the fix (see `/verify` step 4).
- Small implementation-convenience rework within a still-open build is out of scope (handle that with a fix within the task). Use this **only when an already-approved gate needs reopening**.

## Steps
1. **Confirm the defect and the human's decision**: present the escalation log / needs-revision points and have the human decide "how far to go back (requirements, design, or build)". Do not roll back on your own.
2. Finalize the target phase (`requirements` | `design` | `tasks` | `build`) and the reason **in a single `structured-question`**.
3. **Reset gates in a chain** (deterministic process):
   ```
   rein revise --to <phase> --reason '<reason>'
   ```
   `rein revise` resets every gate from the target onward to `pending` **in a chain**, moves `current_phase` back, and records it in the roll-back log. This prevents the stale-approval inconsistency of "upstream pending while downstream approved". The editing order from then on is mechanically enforced by `gate_guard` (e.g. while design is pending, edits to `docs/tasks/**` and implementation code are denied). Use `--dry-run` to check just the plan.
4. **Task impact analysis (deterministic mark, then reconcile — do not discard)**: before fixing upstream, mark the ripple to existing tasks in code.
   - Identify the tasks **directly affected** by the upstream change, then mark them **and their transitive dependents (downstream)** as `needs-revision` deterministically:
     ```
     rein revise --impacted T-00x,T-00y
     ```
     (combinable with `--to` in one invocation; `--dry-run` previews; `rein dag --impacted` enumerates the same set read-only). Missing an impacted task is the dangerous direction, so the **whole closure is marked mechanically** — nothing in it runs until reconciled.
   - Marking is all this step does. The marked closure is then reclassified inside the re-run of `/tasks` ("Re-run after a roll back", which owns the keep / modify / obsolete / new taxonomy and what becomes of a task that was `done`) — nothing in the closure runs until that reconcile has happened.
5. **Guide to rebuilding**: "next is `/<phase>`". Reflect the above reconcile inside the re-run of `/design`/`/tasks`, and present the **impact (the impacted list and classification)** to the human at gate ③ for re-approval.

## Principles
- **Rewinding approval is the human's privilege.** `/revise` is run only under the human's explicit judgment.
- **Do not discard and rebuild tasks.** Reconcile existing tasks against the revised upstream, and pick up the impact exhaustively with deterministic computation (`--impacted`).
- The truth is `.rein/state.yaml` (gates, task status), `.rein/plan.yaml` (the tasks), and `.rein/events.ndjson` (the roll-back log). `/revise` also invalidates the receipts and the review built on the reset gates.
