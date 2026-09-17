# Speculative work log

Work pulled forward while a gate was pending. One table, for every phase, because the question
asked of it later is one question: **did waiting cost anything?**

The rule this records against is `.rein/prompts/rules/gate-workflow.md` "While a gate is pending".
Only **outcome-independent** work belongs here — scaffolding, dev-env/CI setup, read-only
investigation, fixtures — never a deliverable premised on the decision being waited on. It stays
outside `guard.paths` (`tests/` is deliberately unguarded for exactly this), and a `rein guard`
denial is where the boundary is, not an obstacle to route around.

Everything here is **throwaway-by-default**. The gate's answer decides whether it survives, and
that decision is a human's: `rein next` / `/status` names the rows still blank, and
`docs/retrospective.md` §3 is where they are finalized at the end of the cycle.

| Phase | What was done | Premised on | Adopt? (human) |
|---|---|---|---|
| | | | |

- **Phase** — the gate that was pending: `mandate` or `acceptance`.
- **Premised on** — what the work would be wasted by. If nothing, say "nothing"; that is what
  makes it outcome-independent, and a row that cannot fill this in was not speculative work, it
  was the deliverable.
- **Adopt? (human)** — blank until a human decides. `adopt` / `discard`, with where it landed.
