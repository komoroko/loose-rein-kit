# Retrospective

> `/verify` generates/updates this at acceptance approval / reaching `done`. It recovers the process metacognition and
> leaves learning for the next cycle (next product / next iteration).
> It **closes the open items** of each log in the phase deliverables (escalation, speculative work).

## 1. Where rework originated
Classify needs-revision / blocked by origin. Upstream defects are material for improving the next requirements/design.

| Task/gate | Event | Class (upstream defect / implementation convenience / external factor) | Root cause | Countermeasure for next time |
|---------------|------|------------------------------------------|----------|------------|
| | | | | |

## 2. Lenses this cycle should have had
The other direction of §1. `rein lens --stats` only ever argues for *removing* a lens — it counts the
ones you have and can never see the one nobody wrote — so a library read through it alone only shrinks.
This section is where a lens gets in, and there is no verb for it: **a human writes it, by hand.**

Put the two sides next to each other. `rein lens` prints what was being watched for this cycle; §1 above
holds what actually went wrong. **For each root cause in §1, ask whether any applied lens was watching for
it.** A cause nothing was watching for is a lens to write; a cause a lens *was* watching for and missed is
a lens to narrow, not a new one. Neither is automatic: no identifier ties a cause in this cycle to a cause
in the last one, so recurrence is your judgement, made here, with both lists in front of you.

The library is `lenses.yaml` under your user config (`rein lens --stats` prints the path). It is shared by
every repository you use, so a lens written here applies to all of them — that is the point of a library,
and the reason to write the `when:` condition rather than leaving it unconditional.

| Root cause (from §1) | Was a lens watching for it? | Lens to add or narrow (id / when / what to look for) | Written to library? |
|---|---|---|---|
| | | | |

## 3. Recovering the escalation log
Conclude every open escalation in the event log: `rein events --render` lists them. The log
is append-only and has **no `resolve` verb** — a record an operator can close by hand is not
evidence — so the conclusion lives here, one line each.

-

## 4. Adoption of speculative work
For each row in `docs/speculative-work.md`, finalize adopt / discard (fill the "Adopt? (human)" column).

-

## 5. Lessons for upstream
When building something similar next time, what should have been firmed up first at the requirements/design stage.
Durable ones are promoted the same way as §6 (`upstream` included) — record where each landed.

| Lesson | Promote? | Promoted to (file) |
|--------|----------|--------------------|
| | | |

## 6. Process / template improvement proposals
Improvement ideas for how this loop is run, the gates, self-assessment, and deterministic orchestration
(feedback to the template itself is welcome too). **Before `cycle-close` archives this file, promote any keeper
into the always-loaded template (`AGENTS.md` / `.rein/prompts/**` / the per-agent wrappers / the lens library)
and record where** — a durable lesson must not stay only here. A proposal for the upstream template itself gets
`Promote? = upstream`; the human files it on the template repository by hand (issue/PR) and records the URL in
"Promoted to (file)".

| Proposal | Promote? | Promoted to (file) |
|----------|----------|--------------------|
| | | |

## 7. What went well (ways worth keeping)
-
