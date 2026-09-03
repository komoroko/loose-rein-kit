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
| `background-wait` | **none** — nothing re-enters the session when a detached command exits, so use the degradation: detach with the output in a file, end the turn, read the log when a human brings you back |

Notes:
- **The implementation phase is `rein build`, and needs a headless agent CLI** (installed and authenticated) — the orchestrator launches the CLI named by `agents.implementer.adapter` (`claude` by default; `codex`, `gemini` and `copilot` also work, so Gemini may be both the host you type in and the CLI the build drives). It is one command whose completion is the signal, never polled; without a CLI it refuses with exit `2` naming what is missing. **The agent host caps how long one command may run and a real build outlasts that cap**, and there is no `background-wait` here, so take its degradation: start the run detached with its output in a file (`nohup rein build --supervise > .rein/build.log 2>&1 &`), end the turn, and read `rein start` / the log when you come back — never re-run `rein build` to check on it (build.md, "When the run outlasts your host's command timeout").
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
