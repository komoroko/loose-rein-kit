# /tasks — Task breakdown phase

(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Where this sits
This assembles the **mandate** and presents it: the scope the loop may change, the claims it must
make true, the evidence that counts, and the task DAG that answers them. `/req` and `/design` write
the material, in whatever order the change called for; this is where the three become one thing a
human can approve.

Read `.rein/state.yaml` first. If `gates.mandate == approved`, the mandate is closed — changing it
needs `/revise` and a re-approval. If the claims are missing, run `/req` (or write them here); if
the approach is undecided, run `/design`. Neither is a gate you are waiting on.

## Re-run after a roll back (reconcile, when tasks already exist)
If `plan.yaml` already has tasks (a re-run after rolling back with `/revise`), **do not rebuild from scratch**: read `.rein/prompts/reference/tasks-detail.md` "Reconcile after a roll back" and reconcile the revised design against them.

On the first run (no tasks generated yet), create them with the steps below.

## Steps
0. **Answer any open change requests first.** Run `rein changes list --gate mandate --json`. Each anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong: **read and edit only the slice it names** — do not re-run the phase over the whole deliverable. Then `rein changes address <id> --note <what you changed>`; the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds the mandate gate shut, and approving is what closes the addressed ones.
1. Read `docs/20-design.md`.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), read it first, then `.rein/prompts/reference/tasks-detail.md` "Brownfield" before cutting any task.
2. Split the design into **review-sized task tickets** and create `docs/tasks/T-NNN.md` (following the `T-template.md` scaffold). Each ticket must have:
   - the requirement/design it covers
   - acceptance criteria
   - an **automated-test approach** (kind, **the path of the file the test goes in**, target cases, run command) ← do not create a task without this. `/build` uses it for the green decision.
   - **the paths that answer must be inside this task's `scope`** (step 6): the test file named above, and any `docs/decisions/ADR-*.md` the design section for this requirement says this task records its decision in. A ticket that promises a test and a `scope` that forbids writing it surfaces at `/build` as a `scope_violation`, after an implementer has already done the work. Size the scope here, where it costs a sentence.
3. **Group the tasks into issues, and classify each issue by kind.** Each task becomes a **sub-issue** of exactly one **issue** (`I-NNN`). An issue is the most one launch may carry: the loop may implement several of its sub-issues in one launch (a *bundle*), never work from two issues, and how it groups them is its business, not the mandate's — regrouping withdraws no approval. So cut sub-issues **small** (each with its own scope and criteria, the unit a finding is charged to and a human approves) and issues to **the size one launch can carry and one pull request can show**: a bundle lands as one commit and one slice of the stack. An issue's sub-issues are one line of work on one area; a sub-issue that crosses an irreversible point, has `operate` steps or `requires`, or is produced by a person always runs alone, so it costs nothing to keep it in the issue it belongs to. `kind` is the issue's:
   - **foundation**: a shared base many tasks depend on (shared models/schema, shared utilities, auth, config, types/interfaces, etc.).
   - **parallel**: feature tasks (leaves) that can run independently and concurrently once the foundation is in place.
   - **integration**: a task that joins several parallel tasks once they complete (integration, E2E, wrap-up).
   - **Cutover decomposition (replacing shared infrastructure)**: when a cycle *replaces* a shared asset that many tasks touch (a colour LUT, an auth layer, a serialization format, a config field), **do not split the removal of the old asset across the foundation/leaf tasks that build the new one**. Deleting the shared old path early makes those intermediate tasks fail their own per-task DoD (`test`+`smoke` can't stay green while producers and consumers straddle two worlds). Instead: keep every intermediate task **additive** (add the new API/field alongside the old, old path still runs at its defaults), and **concentrate the removal of the old infrastructure in the single integration/cutover task** that wires the new path end to end and can delete the old one in the same commit while keeping the suite green.
4. **Assemble the dependency graph (DAG)** at both levels. An issue's `blocked_by` names the issues whose **every** sub-issue comes first; a sub-issue's `blocked_by` names only sub-issues **of its own issue** (an edge across issues is the issues' edge — the plan refuses the other spelling, and says which issue edge to write). Within an issue, list sub-issues in the order they are built: a bundle is a contiguous run of that order. Confirm there are no cycles (it is a DAG).
5. **Derive the execution plan** from the DAG:
   - **Execution layers**: assign to L0, L1, … in topological order (within a layer, parallel is possible). Foundation should cluster up front.
   - **Critical path**: identify the longest chain (the path that determines the overall duration).
   - **Executable frontier**: todo tasks with no dependencies, ready to start now.
6. **Write the mandate's `scope` and the task DAG into `.rein/plan.yaml` (the frozen Expected Model) as the machine-readable SSOT.**

   **Before writing any field, read `.rein/prompts/reference/tasks-detail.md` "Plan fields"**: what each field claims (`scope`, the sub-issues and their `acceptance`, `requires`/`produced_by`, `environment`, `operate`, `rehearsal`/`attempts`, `premises`, `operator_surface`), and what checks it.
   - After writing, run `rein dag --validate` to confirm no cycles, unknown dependencies, or duplicate IDs (fix if non-zero).
   - **Consistency trace** `rein dag --trace` mechanically checks whether the **requirements → design → tasks thread is unbroken**. The requirement checklist is the `R-N` / `NFR-N` **headings of `docs/10-requirements.md`**, so a requirement nobody wrote a claim for is a break rather than an id the trace never hears about. Exit code **0=whole / 1=broken (fix) / 2=nothing to check (no requirement id on either side — the report says `unknown`, which is not a pass)**.
     - Breaks detected: ① a declared requirement no claim states (ERROR) ② a declared requirement no section of `docs/20-design.md` mentions (ERROR) ③ a claim citing an `R-N`/`NFR-N` the requirements document does not declare — a dangling reference (ERROR) ④ a task with no `claim_ids` (ERROR: it answers for nothing) ⑤ a claim no task is answerable for (WARN). **Non-functional requirements (`NFR-N`) trace with softer rules**: no claim / no design section is a WARN, not an ERROR (cross-cutting NFRs are verified at `/verify`, where `rein dag --trace --test-plan docs/test/test-plan.md` enforces their presence in the test plan) — confirm each NFR WARN is deliberate at the gate.
   - The human-facing task table & execution plan is `rein dag --render` output; present it at the gate (the truth lives in `plan.yaml` + `state.yaml`).
7. **Reviews before the mandate.** Run **`rein reviews show --stage tasks`**, then follow `.rein/prompts/reference/tasks-detail.md` "Reviews before the mandate": the adversarial review in a fresh context, its lenses recorded, every blocker resolved before the gate.
7a. **Run the dependency audit once** (`rein audit run`, on the host) before presenting the gate — the mandate refuses to open without a current record of one (over today's lockfiles, within `max_age_days`). It does not have to pass: a finding is a change to the environment this approval freezes, so it goes on the mandate screen as a decision (a task that bumps the lock, or an explicit deferral), where it costs a line in the plan rather than a roll back at acceptance. `rein doctor` says first whether the audit command can start at all.
8. **The mandate gate**: present **what is being delegated** as an `approval-presentation` — the `scope`, the claims and their acceptance criteria, the frozen environment, and the measured baseline — and confirm "may the loop change this, to make these true, judged this way?". Then, as supporting material, the issues and their sub-issues, the **dependency chain (layer diagram, critical path, foundation tasks)**, the **consistency trace**, and the **adversarial-review summary** (finding counts by severity, dispositions, unresolved disputes — the human's to settle). Be explicit about what the approval freezes: the whole of `plan.yaml`, the sub-issues and each one's acceptance criteria included. The loop chooses the *order* it works through them and how it bundles an issue's sub-issues into launches, and nothing more, so a sub-issue that turns out wrong comes back through `/revise --to mandate` — which is the reason to get the criteria right here rather than to treat the list as provisional. Generate and present `rein dag --render` (layers/critical-path text, **and how the acceptance gate will read this plan** — one reading per scoped task, or one reading of the whole cycle when no task declares a `scope`; say which, because a plan read whole is the shape that runs into a session limit and by the acceptance gate the only remedy left is to split the scope and re-approve) and **`rein dag --trace` (the requirement-coverage table)** — two artefacts, not three: `rein dag --mermaid` draws the same graph `--render` already states (it stays available, and `/status` is where the whole picture is drawn). Make it so the human can see at a glance that **every requirement is linked to a task and there are no dangling references**.
   - **Always present a self-assessment as well** (contents: `.rein/prompts/rules/gate-workflow.md` "Gate self-assessment"), highlighting high-risk tasks (uncertain/external-dependency/coarse-grained); also note low-confidence tasks in the ticket's "Notes / design decisions".

Write the deliverables (`docs/tasks/T-NNN.md`) and the plan's prose — issue and sub-issue titles, criterion statements — in the language `project.language` in `.rein/config.yaml` names (`en` when it is absent).

## While waiting for approval
`notify-and-wait` first; then only **outcome-independent, throwaway-by-default** work (rules: `.rein/prompts/rules/gate-workflow.md` "While a gate is pending"), recorded in `docs/speculative-work.md`:
- Preparing test fixtures/harness/scaffolding that are clearly needed from the agreed design
  (`tests/` is deliberately unguarded for exactly this; the paths in `guard.paths` wait for the mandate).
- **Forbidden**: real implementation of each feature.

## Once approved
- Only after an explicit human "approve": ask the human to run `rein approve mandate` **themselves** — never for them, and **running the next command is not itself approval** (mechanics: AGENTS.md "Gate rules" 2). That approval is what freezes `plan.yaml` and `config.yaml`. Declining is recorded as a change request, not lost — see step 0.
- **(Only with GitHub integration)** Run `rein issue-sync` to one-way-mirror the approved issues to GitHub Issues, each sub-issue as a GitHub sub-issue of its issue (needs gh 2.103 or later). It only acts when `github.enabled: true` in `.rein/config.yaml`, and auto-skips if gh/remote is absent (does not fail). **Do not run it before approval** (avoid making issues for unapproved tasks). `plan.yaml` + `state.yaml` are always the SSOT; Issues are not read back.
- After committing the gate's deliverables, suggest `session-compaction` (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget") and point to "next is `/build`".
