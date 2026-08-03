# /status — Progress dashboard

Show the Human-on-the-Loop monitoring view. **Do not change state (read-only).**

1. Run **`rein status`** and present its board in the user's language. It is the deterministic
   answer — `/status`, `rein next` and the dashboard all read the same object, so do **not**
   re-derive phase, gates or counts from `.rein/state.yaml` / `.rein/plan.yaml` by hand.
2. Lead with **"Waiting on you"**: the count, then each item with its severity
   (`blocking` = a gate cannot open while it stands / `attention` = needs a person / `info`), what
   it is, and the command that addresses it. If the heading says gate readiness was not probed, say
   so — an unprobed queue is not an empty one. Then the gate list and the `next` recommendation.
3. **Task progress**: run `rein dag --render` and show its deterministic output (counts,
   execution layers, critical path, executable frontier). Skip if the plan has no tasks yet
   (before `/tasks`).
   - **Dependency graph**: `rein dag --mermaid` renders the whole picture (`graph TD`, status
     colour-coding, critical path in bold). Include it **when asked, or when the layer structure
     changed** — pasting it every time buries the board it is meant to illustrate.
   - **Consistency trace**: only when the plan has tasks (after `/tasks`) and the requirements
     document (`docs/10-requirements.md`) exists, run `rein dag --trace`. Highlight broken
     requirement → design → task linkage (uncovered requirements, dangling references).
     Distinguish the cause by exit code: **1 = missing (belongs under "Waiting on you") /
     2 = cannot check** (requirements document absent, 0 requirement IDs, or no tasks in the plan
     → guide as a path/notation setup problem).
4. **Speculative work**: from the phase deliverable's speculative-work log, list the items whose
   adoption is still undecided. If `done` is reached but `docs/retrospective.md` is unfilled, or a
   log still has open items, prompt about them.
5. **(Only with GitHub integration)** If `github.enabled: true` in `.rein/config.yaml`, give a
   one-line note that `rein issue-sync` can bring Issues into line with this board (the plan's
   tasks). Issues are a one-way mirror, not the SSOT.

End with 1–2 lines on "what you (the human) should do now".

For a live browser view of the same board, `rein ui` serves a local dashboard (the same queue,
gates, tasks and the deterministically computed next command; safe operations and gate-approval
recording can be run from it — `rein ui --read-only` disables those).
