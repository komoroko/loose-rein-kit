# scripts/

**Product-specific** scripts only (data prep, operational helpers, etc.). Add freely per product.

The Loose Rein harness no longer lives here: it is an **installed CLI** (`uv tool install
git+<this repo>@vX.Y.Z`, then `rein <verb>`) whose code ships in the `rein` package,
not as copied repo source. There is therefore no `scripts/rein/` foundational-tools
directory anymore — the orchestrator, DAG derivation, gate hook, Issues mirror, and lifecycle
verbs are all reached through `rein …`.

**One exception**: `template_lint.py` is this template repository's own drift canary (bilingual
README parity, wrapper parity, capability-mapping set-equality). It is not a product script and
not a `rein` verb — it stays here, outside the installed package, specifically so a product
built from this template never carries a tool that checks *this* repository's own hand-maintained
files. `rein init` does not copy `scripts/`, so it never reaches a product either way.

## Relation to the gate (`rein guard`)

- A Write/Edit under `scripts/` is treated as **implementation code** and is **denied** by the
  mechanism hook unless `gates.tasks` is approved (same as `backend/**` / `frontend/**`; see
  `guard_paths` in `.rein/config.yaml`).
- The old always-allowed carve-out for `scripts/rein/` is gone with the directory — the
  installed package is not repo source, so it needs no self-protection path.
