# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`rein upgrade`
shows the sections between the installed version, recorded in `.rein/rein.lock`, and the
new one). `pyproject.toml [project] version` is the single version source.

## [0.1.0] - 2026-08-02

The first release of **Loose Rein** — a coding-agent harness for developing software
*Human on the Loop*: the agent does the work and self-tests from requirements through testing,
and the human approves or decides only at the **gate** on each phase boundary. The name is the
posture: the horse runs on its own, the rider keeps the reins.

The harness is an installed CLI (`rein`). A product repository carries only its *state* —
`.rein/` (the SSOT, the lock, the materialized prompts/schema) and `docs/` (the deliverables).

### The lifecycle

`brief → requirements → design → tasks → build → verify → done`, driven by `/req` `/design`
`/tasks` `/build` `/verify`, with gates ①–⑤ on the boundaries. A phase cannot start while its
prerequisite gate is `pending`, and **only a human opens a gate** — never the agent, and never a
localhost click. `/status` shows the board and names the next command; `/revise` rolls back
upstream, resetting gates in a chain. An ongoing repository repeats the lifecycle as delta
cycles, closed with `rein cycle-close`.

### Single Source of Truth

Four documents with distinct roles: `.rein/plan.yaml` (the frozen Expected Model — claims and the
task DAG), `.rein/state.yaml` (phase, gate approvals, task status), `.rein/review.yaml` (the
machine review and the human review, digested separately), and `.rein/events.ndjson` (the
hash-chained audit log — every state change records why, and a broken chain is visible).

### Evidence, not self-consistency

A claim with no evidence is `unknown`. At gate ④, whether the code satisfies a claim is judged on
three separate axes — integrity, semantic support, conformance — by comparing what the plan says
(Expected) against what a reviewer that never saw the plan reads out of the code (Actual). There is
no single "verified". `rein review generate` produces the grounded review gate ④ approves; a
structured security review feeds it, and a dependency audit joins at `/verify`.

### Gate enforcement in code

`rein guard` runs as a PreToolUse/pre-commit hook and denies edits that cross a phase boundary;
unreadable gates fail closed. The only write path to `approved` is `rein approve <gate>`: it
checks readiness, prints the digests the approval would cover, and records a confirmation typed at
an interactive terminal, binding those digests in one receipt. An approval records that a human
confirmed, never *which* human — there is no identity-bound mode, so authority never depends on
anything outside the repository.

### The build loop

`rein build` is a deterministic DAG scheduler, not LLM discretion: tasks are foundation /
parallel / integration, layers and the critical path derive from `blockedBy`, and parallel leaves
run in git worktrees against a control plane so a worktree's record survives its deletion.
Repository code and tests run in a sealed OCI sandbox (`rein oci build`), never on the host. The
quality gate (`test` → `check` → `review` → `smoke`) is re-run and its exit status read by the
lead — a delegated agent's textual "green" is not evidence.

### Agent support

Claude Code and VS Code GitHub Copilot are fully supported, hook-enforced gates included
(Copilot's hook mechanism is a VS Code preview feature). Codex and any other agent that reads
`AGENTS.md` work at the rules-and-procedures level, with gates by convention. `rein install
claude|copilot|codex` writes each host's surfaces on demand.

### Human surface

`rein start` (setup wizard, then where-you-are), `rein next` (the next recommended command),
`rein ui` (a local dashboard that reads a gate's deliverables and shows its readiness, handing back
the `rein approve` command for the human's own terminal — a page click is never authentication,
with a project switcher across repositories registered by `rein project add`), `rein agent <cli>` (swap
the headless agent CLI), `rein doctor` (diagnose the environment and the SSOT).

### Adopting an existing repository

`rein init` auto-detects a brownfield repository and hints `/onboard`, which surveys the codebase
read-only into `docs/05-current-state.md`. `rein init` writes only state — no build files, no
makefile, and no agent surfaces unless you install them.
