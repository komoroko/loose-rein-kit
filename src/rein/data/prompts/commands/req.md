# /req — Requirements phase

You drive this project's requirements definition. **Human on the Loop**: you do the work, the human decides.
(Phase-scoped rules — gate self-assessment, approval-wait, context budget: read `.rein/prompts/rules/gate-workflow.md` before starting.)
(Capability terms like `structured-question` resolve per AGENTS.md "Capability vocabulary" and your agent's capability mapping.)

## Steps
0. **Answer any open change requests first.** Run `rein changes list --gate requirements --json`. Each anchors a place (`docs/...#R-3`, `T-004`, `C-001`) and says what is wrong: **read and edit only the slice it names** — do not re-run the phase over the whole deliverable. Then `rein changes address <id> --note <what you changed>`; the note is what the human reads beside the digests before deciding, so "done" is not an answer. An open request holds gate ① shut, and approving is what closes the addressed ones.
1. Read `.rein/state.yaml`. Check `current_phase`.
   - If `gates.requirements == approved` already, say "Requirements are approved; changing them needs re-approval" and wait for the human's instruction.
2. Read `docs/00-product-brief.md`. If empty, first prompt the human to fill it in and stop.
   - **Brownfield**: if `docs/05-current-state.md` exists (an adopted repo), **read it first**. Requirements are scoped to **this cycle's delta** (the change), not the whole product. If it links existing requirement documents, take them as the starting point rather than re-deriving from scratch — the human approving gate ① is what adopts them as this cycle's truth (the **fast intake** entry described in `/onboard`). In-flight/unfinished work listed there is candidate delta scope.
3. Delegate to the `requirements-analyst` role (`role-delegation`) to produce a requirements draft, gaps, and open points.
4. **Ask the human about every open point — there is no cap on how many.** Order the analyst's `[NEEDS CLARIFICATION]` markers and open points by **impact × uncertainty**, then work down the list in batched `structured-question` rounds until nothing is left that you would otherwise close with your own default. The ordering decides *what is asked first*, never *what goes unasked*: a low-impact ambiguity is asked late, not dropped. One `structured-question` carries about four questions — that is what the capability can hold in one round, not a budget for the phase, so keep asking in further rounds (prefer multiple-choice with a recommended option). Do not drip-feed one question at a time either; batching is what makes a round cheap to answer.

   A marker ends in exactly one of two places, and both are visible at the gate: **answered** — propagate the answer into the requirement text (resolving the marker) and record the Q&A in the `## Clarifications` section of `10-requirements.md`, the audit trail of how ambiguities were closed — or **demoted** to `## Open questions` with the assumption you wrote the text under spelled out. **Demoting is the human's call, not yours**: do it when they say to stop asking and leave the rest to you, never to keep a round short. An ambiguity you close silently is a decision taken on your default, recorded nowhere.
5. Write the agreed content into `docs/10-requirements.md` (in the scaffold structure). Give every functional requirement an `R-N` heading and **every non-functional requirement an `NFR-N` heading with a measurable criterion and how it will be verified** — both ID families are the thread `rein dag --trace` follows through design, tasks, and the test plan (NFR rules are softer: no dedicated design section/task is only a WARN, but presence in the test plan is enforced at `/verify`).
6. **Write the claims into `.rein/plan.yaml`** — one per `R-N` / `NFR-N` heading, and the step without which gate ① cannot open. The requirements document is the *checklist*; a claim is what the requirement **means**, stated so that it could be false:

   ```yaml
   claims:
     - id: C-001
       requirement_ids: [R-1]
       statement: <the behaviour this requirement demands, as a checkable sentence>
       risk: medium          # low | medium | high | critical
   ```

   Then run **`rein dag --trace`** and confirm it exits 0. It blocks on a heading no claim covers, on a claim citing an `R-N` the document does not declare, and on a design section missing for a functional requirement (NFRs warn instead). With neither side filled it reports `unknown` and exits **2** — that is not a pass, and `rein approve requirements` refuses on the same ground. `/tasks` later points each task's `claim_ids` at these ids, which is what closes the thread.

7. **Adversarial review** (required before gate ①): delegate to the `adversarial-reviewer` role (`role-delegation`) in a **fresh context — never the analyst that drafted** — with `docs/00-product-brief.md` and the written `docs/10-requirements.md` as its inputs. Record every finding in the `## Adversarial review` section of `10-requirements.md`, each with one of (`revise_requirement`, `request_expert`, `run_experiment`, `reduce_scope`, `dispute_finding: <why>`, …) — and accepting the risk is not among them. **Every blocker must be resolved** (fixed or disputed-with-reason) before presenting the gate; re-invoke the reviewer once, on the blocker fixes only — no further rounds. Findings that need the human's judgment fold into the gate's single `structured-question` below. A hotfix reduces its *scope* through `/revise`; it does not skip this round (there is no waiver, and no event kind that could record one).
8. **Gate ①**: present the requirements summary as an **`approval-presentation`** and ask "may we freeze the requirements with this content?". Ask any accompanying confirmations **in a single `structured-question`**.
   - Present only when **no unresolved `[NEEDS CLARIFICATION]` marker remains** in `10-requirements.md`: anything deliberately left open is demoted to an explicit "Open questions" entry the human sees at the gate — never left as a stray marker or a silent assumption. `rein approve requirements` checks this in code (HTML comments excluded, so the scaffold's own guidance does not count) and refuses while a marker stands, naming the lines.
   - **Always present a self-assessment as well** (contents: `.rein/prompts/rules/gate-workflow.md` "Gate self-assessment"); also leave it in the "Self-assessment" section of `10-requirements.md`, flagging low-confidence requirements for the human's attention.
   - **Also present the adversarial-review summary**: finding counts by severity, the dispositions, and any unresolved dispute (an unresolved dispute is the human's to settle — never loop further with the reviewer).

Write the deliverable (`docs/10-requirements.md`) in the user's language.

## While waiting for approval
`notify-and-wait` first; then only **outcome-independent, throwaway-by-default** work (rules: `.rein/prompts/rules/gate-workflow.md` "While a gate is pending"), recorded as speculative-work events:
- Repo scaffolding / directory layout / skeleton of the dev environment and CI.
- **Read-only investigation** of candidate technologies surfaced in the brief (do not finalize the design).
- **Forbidden**: writing the design body that pre-empts the requirements.

## Once approved
Only after an explicit human "approve": ask the human to run `rein approve requirements` **themselves** — never for them, and **running the next command is not itself approval** (mechanics: AGENTS.md "Gate rules" 2). Declining is recorded as a change request, not lost — see step 0. After committing the gate's deliverables, suggest `session-compaction` (pre-compact check: `.rein/prompts/rules/gate-workflow.md` "Context budget") and point to "next is `/design`".
