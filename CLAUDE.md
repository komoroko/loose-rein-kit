# Loose Rein — Claude Code capability mapping

The operating rules live in `AGENTS.md` — imported below. This file only maps their
capability vocabulary onto Claude Code's mechanisms.

@AGENTS.md

## Capability mapping (Claude Code)

| Capability | Claude Code mechanism |
|---|---|
| `phase-invocation` | slash commands `/req` `/design` `/tasks` `/build` `/verify` `/status` `/revise` `/onboard` (`.claude/commands/*.md`) |
| `structured-question` | `AskUserQuestion` (batch up to ~4 questions, multiple-choice with a recommended option) |
| `notify-and-wait` | `PushNotification`, then end the turn |
| `approval-presentation` | plan mode + `ExitPlanMode`; outside plan mode, present the summary and ask for an explicit "approve" |
| `session-compaction` | `/compact` (human-run; the agent only suggests it) |
| `role-delegation` | subagents in `.claude/agents/` (`requirements-analyst`, `architect`, `adversarial-reviewer`) |
| `command-preauthorization` | `permissions.allow` in `.claude/settings.json` |

Claude Code also carries the **mechanism layer** of the gates: the PreToolUse hook in
`.claude/settings.json` runs `rein guard` on every Write/Edit (AGENTS.md "Gate rules").

The implementation phase is `rein build` (headless, via the adapters `rein agent <role> <cli>`
sets) — one command whose completion is the signal, so never schedule wake-ups to poll it;
retry-after-capacity is a shell loop on its exit code (build.md).
