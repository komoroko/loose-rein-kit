# Loose Rein — Gemini CLI capability mapping

This repo runs the Loose Rein Human-on-the-Loop lifecycle. The operating rules are in
`.rein/AGENTS.rein.md` (read it first — `GEMINI.md` imports it); the phase procedures are in
`.rein/prompts/commands/*.md`. This file only maps the rules' capability vocabulary onto Gemini
CLI mechanisms.

## Capability mapping (Gemini CLI)

| Capability | Gemini CLI mechanism |
|---|---|
| `phase-invocation` | custom commands `/req` `/design` `/tasks` `/build` `/verify` `/status` `/revise` `/onboard` (`.gemini/commands/*.toml`) |
| `structured-question` | ask numbered, multiple-choice options (with a recommended one) in chat, then end the turn and wait |
| `notify-and-wait` | end the turn with an explicit "waiting on gate N approval" summary (there is no push channel) |
| `approval-presentation` | present the summary in chat and ask for an explicit "approve" |
| `session-compaction` | the human starts a new chat; the next command rehydrates from the SSOT (`.rein/state.yaml`, `plan.yaml`, `docs/**`) |
| `role-delegation` | skills `.gemini/skills/*/SKILL.md` — the model activates the one it needs; if that is unavailable, adopt the role inline per its file in `.rein/prompts/agents/` |
| `command-preauthorization` | `--approval-mode` (`default` / `auto_edit` / `yolo`), or the tool-permission settings in `.gemini/settings.json` |
| `background-wait` | `run_shell_command` in the **foreground**: its cap is `tools.shell.inactivityTimeout` — 300s *without output*, not 300s of runtime — and `rein build` prints a `[waiting]` line every minute, so the tool call waits the run out. Raise the setting if your build has longer silences. `is_background: true` returns a PID and never tells you it finished, so it is not a wait; detaching is the last resort |

Notes:
- **The implementation phase is `rein build`, and needs a headless agent CLI** (installed and authenticated) — the orchestrator launches the CLI named by `agents.implementer.adapter` (`claude` by default; `codex`, `gemini`, `copilot`, `cursor`, `amp` and `opencode` also work, so Gemini may be both the host you type in and the CLI the build drives; `rein agent --show` lists them). It is one command whose completion is the signal, never polled; without a CLI it refuses with exit `2` naming what is missing. **A real build takes far longer than a normal tool call, and you wait for it anyway** — in the foreground, per `background-wait` above; the cap there counts silence, and the run prints a `[waiting]` line every minute. Detach (`nohup rein build --supervise > .rein/build.log 2>&1 &`) and end the turn only if the run has to outlive this session, and never turn that into a poll: re-entering to ask whether it is done spends a launch to learn that it is still building (build.md, "When the run outlasts your host's command timeout").
- The gates' **mechanism layer** also runs under Gemini: `.gemini/settings.json` registers `rein guard` as a `BeforeTool` hook, which denies edits to next-phase deliverables while the prerequisite gate is `pending`. The hook answers in both dialects at once — `decision`/`reason`, which Gemini reads, and `hookSpecificOutput`, which the other hosts read — so one guard serves every host rather than one per host drifting apart.
- The security review before gate ④ / at `/verify`: Gemini has no `/security-review` command — perform an equivalent security-focused review pass; it is recorded in `review.yaml`'s `machine.security` by `rein review generate`, bound to the reviewed HEAD, and summarized in the test plan's security column.

## Role-agent tool mapping (single source)

| Claude Code tools | Gemini CLI tools |
|---|---|
| Read, Grep, Glob | `read_file`, `read_many_files`, `glob`, `search_file_content` |
| WebFetch | `web_fetch` |
| WebSearch | `google_web_search` |
| Edit, Write | `replace`, `write_file` |
| Bash | `run_shell_command` |
