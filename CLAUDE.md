# Loose Rein — Claude Code capability mapping

The operating rules live in `AGENTS.md` — imported below. This file only maps their
capability vocabulary onto Claude Code's mechanisms.

@AGENTS.md

## Capability mapping (Claude Code)

| Capability | Claude Code mechanism |
|---|---|
| `phase-invocation` | slash commands `/req` `/design` `/tasks` `/build` `/verify` `/status` `/revise` `/onboard` (`.claude/commands/*.md`) |
| `structured-question` | `AskUserQuestion` (up to 4 questions per call, multiple-choice with a recommended option). Four is what one round carries, **not** a cap on how many things get confirmed — keep asking in further rounds |
| `notify-and-wait` | `PushNotification`, then end the turn |
| `approval-presentation` | plan mode + `ExitPlanMode`; outside plan mode, present the summary and ask for an explicit "approve" |
| `session-compaction` | `/compact` (human-run; the agent only suggests it) |
| `role-delegation` | subagents in `.claude/agents/` (`requirements-analyst`, `architect`, `adversarial-reviewer`) |
| `command-preauthorization` | `permissions.allow` in `.claude/settings.json` |
| `background-wait` | `Bash` with `run_in_background: true` — the command keeps running across turns and re-invokes you when it exits |

Claude Code also carries the **mechanism layer** of the gates: the PreToolUse hook in
`.claude/settings.json` runs `rein guard` on every Write/Edit (AGENTS.md "Gate rules").

The implementation phase is `rein build` (headless, via the adapters `rein agent <role> <cli>`
sets) — one command whose completion is the signal, so never schedule wake-ups to poll it;
retry-after-capacity is a shell loop on its exit code (build.md).

**Bash caps how long one foreground command may run and a real build outlasts that cap**, so use
`background-wait`: `rein build --supervise` with `run_in_background: true`, then get on with
something else. The run's exit re-invokes you — that is waiting, not polling, and it costs no
launch. **Detach instead (`nohup rein build --supervise > .rein/build.log 2>&1 &`, then end the
turn) when the run has to outlive this session**: a host-managed background task belongs to the
host, an orphaned process does not. Either way, when you come back read `rein resume` / the log,
and **never re-run `rein build` to check on it** — the build lock makes the second run exit `3`,
which is indistinguishable from a capacity stop (build.md, "When the run outlasts your host's
command timeout").
