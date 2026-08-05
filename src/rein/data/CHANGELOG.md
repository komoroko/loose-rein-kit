# Changelog

Releases, newest first — one `## [x.y.z] - YYYY-MM-DD` heading per release (`rein upgrade`
shows the sections between the installed version, recorded in `.rein/rein.lock`, and the
new one). `pyproject.toml [project] version` is the single version source.

## [0.2.0] - 2026-08-06

A correctness release, and one doctrine correction. Three reported defects and five rough edges,
with the theme that the lifecycle could record **yes** but not much else: it could not tell you
*when* to say it, could not let you say it where you had just read the thing, and had nowhere at
all to put **"not yet, change this"**.

### Gate ③ actually freezes the plan (#1)

Three documents said gate ③'s approval freezes the plan, `rein guard` rule 2 protected
`plan.yaml`/`config.yaml` only while the plan was frozen, and `rein build` refused to start
against a draft — but **no code anywhere ever wrote `frozen`**. A repository that had properly
approved gate ③ therefore could not build, and rule 2 never once engaged. The freeze now happens
in the same Central Store transaction that writes the receipt, with a `plan_frozen` event beside
it, and refuses if `plan.yaml` or `config.yaml` moved since the digests the human was shown.

### Drift in what the freeze covers is detected (#2)

`rein doctor` only checked that a receipt's digests were *present*, never that they still
described anything — so pinning a sandbox image and adding a `guard.paths` entry after gate ③
kept printing `0 FAIL` against a `config.yaml` nobody had approved. New `check_freeze_drift`
compares the freeze record against the documents on disk, then each post-freeze receipt against
that record. It is scoped to the freeze rather than to every receipt: gates ① and ② were approved
while the plan was a draft, and `/design` and `/tasks` then moved it legitimately. `rein guard`'s
commit-stage check now covers `config.yaml` alongside `plan.yaml`.

### `rein next` tells you when a gate is waiting on you

The `approve_gate` recommendation kind existed and nothing ever returned it, so a finished phase
with a clear gate still said "run the phase again" — and the dashboard's waiting-state signals
(tab title, favicon, notification) stayed silent for the entire wait.

### Approve where you read it, and where write authority comes from

`rein ui` can now record a gate approval, from the pane that just showed you the deliverable and
the digests. This replaces a doctrine that said "a localhost click is not authentication" while
embedding the page's write token *in the page* — so anything able to `curl` it could write,
including the gate-④ human-review answers the gate itself requires.

The line is not authentication, because nothing in a repository can prove a human. It is the
channel the capability travels over: `rein ui` prints a **single-use launch link** to its own
terminal, redeeming it mints the write session, and a page fetched any other way is read-only.
What is guaranteed, and now said plainly everywhere it is claimed, is that **an approval cannot
happen by accident, by default, or by a configuration someone pre-authorized**. Receipts record
`confirmed_via`; the terminal prompt is `[y/N]` with the default **no**, since retyping a gate
name already on the command line established nothing while a stray Enter must never approve.

### "Not yet, change this" is recorded — `rein changes`

The answer between *yes* and *roll back a yes* had no home: it lived in a chat message, so the
gate stayed ready, the board kept recommending the approval, and a new session never knew. A
change request is **anchored** to a place (`docs/10-requirements.md#R-3`, `T-004`) so answering it
means reading that slice rather than re-running the phase; `open` holds the gate shut, the agent
moves it to `addressed` with a note saying what changed, and the approval closes what it covered.

### Sandbox setup is part of initialization, and uv is current (#3)

`rein init` never mentioned the one precondition everything else needs, and every surface
recommended `rein oci build --profile <first of three>` without `--write-config` — a command that
could not clear the failure it was answering. The wizard now offers to build and pin; `rein next`,
`rein doctor`, the dashboard and `rein init` all name the same complete command; `rein oci build`
checks for a container runtime before starting, shows per-image progress, and verifies the pins it
writes. The packaged `uv` moves 0.9.7 → 0.11.28, pinned by digest: 0.9.7 could not parse a
relative `exclude-newer` and silently dropped the entire `[tool.uv]` table, re-resolving without
the lockfile's cutoff — a sandbox-only divergence in the one place whose purpose is that the
pinned environment is what the evidence is about.

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
