# Loose Rein — Codex capability mapping

This repo runs the Loose Rein Human-on-the-Loop lifecycle. The operating rules are in `AGENTS.md`
(Codex loads it automatically); the phase procedures are in `.rein/prompts/commands/*.md`.
This file only maps AGENTS.md's capability vocabulary onto Codex mechanisms.

## Capability mapping (Codex)

| Capability | Codex mechanism |
|---|---|
| `phase-invocation` | skills `$req` `$design` `$tasks` `$build` `$verify` `$status` `$revise` `$onboard` (`.agents/skills/*/SKILL.md`) |
| `structured-question` | ask numbered, multiple-choice options (with a recommended one) in chat, then end the turn and wait |
| `notify-and-wait` | end the turn with an explicit "waiting on gate N approval" summary (there is no push channel) |
| `approval-presentation` | present the summary and ask for an explicit "approve" |
| `session-compaction` | `/compact`, or a fresh session; the next command rehydrates from the SSOT (`.rein/state.yaml`, `plan.yaml`, `docs/**`) |
| `role-delegation` | subagents `.codex/agents/*.toml` — delegate **explicitly**; Codex never auto-spawns a custom agent |
| `command-preauthorization` | `approval_policy` / `sandbox_mode` in `.codex/config.toml` — coarse by nature: Codex has no per-command allowlist |
| `background-wait` | **none** — Codex's `exec` has no way to be re-entered when a detached command exits, so use the degradation: detach with the output in a file, end the turn, read the log when a human brings you back |

Notes:
- The gates' **mechanism layer** runs under Codex: `.codex/hooks.json` registers `rein guard`
  as a PreToolUse hook on `apply_patch`, which denies edits to next-phase deliverables while the
  prerequisite gate is `pending`. Project-scoped Codex config is read **only once you trust the
  project** — until then the guard is not registered and only the commit-stage check runs.
- `command-preauthorization` is **not** what keeps gate rule 2. `rein approve` refuses unless it
  is at an interactive terminal, where it asks for the gate name to be typed out. No Codex
  setting can hand an agent a gate.
- **The implementation phase is `rein build`, and needs a headless agent CLI** (installed and
  authenticated) — the orchestrator launches the CLI named by `agents.implementer.adapter`
  (`claude` by default; `codex` / `gemini` also work). It is one command whose completion is the
  signal, never polled.
- **Codex's `exec` caps how long one command may run**, and a real build outlasts that cap. With no
  `background-wait`, take its degradation: start the run detached with its output in a file
  (`nohup rein build --supervise > .rein/build.log 2>&1 &`), end the turn, and read `rein resume` /
  the log when you come back — **never re-run `rein build` to check on it** (build.md, "When the run
  outlasts your host's command timeout").
- The security review before gate ④ / at `/verify`: perform a security-focused review pass; it is
  recorded in `review.yaml`'s `machine.security` by `rein review generate`, bound to the
  reviewed HEAD, and summarized in the test plan's security column.
