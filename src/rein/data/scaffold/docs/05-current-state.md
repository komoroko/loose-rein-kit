# Current state (baseline of the existing codebase)

> `/onboard` generates this in an adopted (brownfield) repo by surveying the existing code
> read-only. It is the **persistent baseline**: `/req` and `/design` read it first, and it
> survives `rein cycle-close` (updated — not archived — at the end of each cycle).
> Existing documents are **linked from here, never converted or moved**; they stay the truth
> where they already live. In a greenfield repo this file may simply stay unused.

## Architecture overview
<!-- The existing structure in prose / a simple diagram: entry points, main components, data flow -->


## Modules / directories
<!-- What lives where, one line each. e.g. `src/api/` — HTTP handlers (FastAPI) -->

| Path | Role |
|------|------|
| | |

## Reusable assets
<!-- Shared utilities, patterns, schemas, fixtures the next cycles should reuse instead of rewriting -->
-

## Conventions
<!-- Naming, test placement/style, commit format, error handling — what new code must match -->
-

## Test / CI commands
<!-- How this repo tests and checks itself (these usually also belong in config.yaml's quality_gate) -->
-

## Existing documents
<!-- Links to the requirements/design/ADR/README documents that already exist, wherever they live.
     Finding none is a normal, supported state — write "none"; the baseline itself is then the
     only map (see /onboard's no-docs note). -->
-

## Implementation status
<!-- The mapping of "how far things are": done capabilities, in-flight work, known TODOs.
     In-flight/unfinished items are candidates for the next cycle's delta requirements (R-N). -->

### Implemented capabilities
-

### In-flight / unfinished work
-

### Known TODOs / open issues (reference only — Issues are never read back as truth)
-

## Last updated
<!-- Update this section at each cycle close: what the cycle added/changed -->
- <YYYY-MM-DD>: initial onboarding survey.
