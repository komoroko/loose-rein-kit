# =========================================================
# Makefile — development targets for the rein package itself.
#
# Products no longer need make: every operation is an `rein <verb>`
# (install the CLI with `uv tool install git+<this repo>`). This file only
# wraps the package's own dev workflow (macOS / Linux; use WSL on Windows).
# =========================================================

.PHONY: install setup pre-commit pre-push check test template-lint sync-check audit clean

# Install the uv binary (the one bootstrap prerequisite)
install:
	curl -LsSf https://astral.sh/uv/install.sh | sh

# Sync dependencies (including the dev group; generates/updates uv.lock) and install the
# git hooks (idempotent) so the commit-stage layer — gitleaks and the Loose Rein gate guard —
# actually fires on `git commit`, not only inside `make check`.
# `uv sync` here is the one deliberate lock-update entry point: every other target runs
# `--frozen` so it uses the committed uv.lock verbatim (the relative `exclude-newer` cooldown
# otherwise re-resolves and churns the lock on each run). Change a dependency → `make setup`
# (or `uv lock`) → commit the lock.
setup:
	uv sync
	uv run pre-commit install

# Commit-stage hooks (ruff lint / gitleaks / various checks)
pre-commit:
	uv run --frozen pre-commit run --all-files

# Pre-push-stage hooks (ruff-format / mypy)
pre-push:
	uv run --frozen pre-commit run --all-files --hook-stage pre-push

# The full quality gate: both hook stages plus the template drift canaries and the
# materialized-artifact check. CI runs this same target.
check: pre-commit pre-push template-lint sync-check

# The package's test suite (the same suite CI's matrix runs).
test:
	uv run --frozen pytest -vv tests/

# Drift canaries across the hand-maintained template files (wrapper parity, capability-mapping
# set-equality, vocabulary echoes, README EN↔JA structure, pyproject↔CHANGELOG, data parity).
# Lives in scripts/, not the installed package — it is the template repo's own maintenance tool.
template-lint:
	uv run --frozen python scripts/template_lint.py

# The materialized .rein/prompts|schema|rules must match the packaged payload.
sync-check:
	uv run --frozen rein sync --check

# Dependency vulnerability audit (supply-chain check). Mandatory in /verify.
audit:
	@req="$$(mktemp)"; \
	uv export --frozen --format requirements-txt --no-emit-project -o "$$req" && uvx pip-audit -r "$$req"; \
	status=$$?; rm -f "$$req"; exit $$status

# Cleanup
clean:
	uv run pre-commit clean ; \
	uv cache clean ; \
	find . \( -type d -name "__pycache__" -o -type d -name ".pytest_cache" -o -type f -name "*.pyc" \) -exec rm -rf {} +
