"""The `.gitignore` block `rein init` / `rein sync` maintain in a product repository.

Loose Rein writes two kinds of file into a repo. The SSOT — `.rein/*.yaml`,
`.rein/events.ndjson`, `.rein/rein.lock`, the materialized `prompts/`, `schema/`, `oci/`, the
scaffold snapshot — and `docs/**` are the record every gate receipt binds: they are committed
and reviewed at each gate, never ignored. The other kind is scratch the loop regenerates every
run — parallel worktrees, per-task dossiers, generated PR bodies — and a naive `git add -A`
that sweeps those in is the failure this block exists to prevent.

The list is *derived* from the code that writes each path, not hand-kept: a written-out ignore
list outlives what it describes (the pre-history here is a `.gitignore` that named three
`build-loop.*` files for releases after the loop's locks moved to `$XDG_RUNTIME_DIR`). The
canary `scripts/template_lint.py` imports :func:`runtime_artifacts` from here so the template
repository's own section and the block products receive cannot drift apart.

The template repository keeps its own curated section (same header, extra teaching comments);
callers skip the merge when `guard.template_mode` is set, the way the CLAUDE.md append is
skipped.
"""

from __future__ import annotations

from rein import models

#: Opens the section. Kept byte-identical to the header the template repo's own `.gitignore`
#: uses so `scripts/template_lint.py`'s canary reads both the same way.
SECTION_HEADER = "# ---- Loose Rein runtime artifacts ----"
#: Closes the section. Matches the `^# ---- ` shape the canary already treats as a boundary, so
#: an explicit terminator is available without inventing a second marker convention.
SECTION_END = "# ---- end Loose Rein ----"

_COMMENT = (
    "# Managed by `rein init` / `rein sync` — derived from the code that writes these paths.",
    "# The SSOT (.rein/*.yaml, .rein/events.ndjson, .rein/rein.lock) and docs/** are committed,",
    "# reviewed at each gate, and never listed here.",
)


def runtime_artifacts(config_text: str) -> set[str]:
    """The paths the tool writes *into* the repository and regenerates every run.

    Derived, never listed: the worktree directory is configurable (`execution.worktree_dir`);
    the PR draft, the stacked PR bodies and the dossier directory are constants
    (`pr_draft.OUT_PATH`, `pr_stack.OUT_DIR`, `dossier.RELATIVE_PATH`). Directories carry a
    trailing slash so git reads them as directory-only patterns.
    """
    from rein import dossier, pr_draft, pr_stack

    return {
        models.Config.parse(config_text).worktree_dir.rstrip("/") + "/",
        pr_draft.OUT_PATH,
        pr_stack.OUT_DIR.rstrip("/") + "/",
        dossier.RELATIVE_PATH.rstrip("/") + "/",
    }


def block(config_text: str) -> str:
    """The full section text (header, comment, sorted patterns, terminator), no trailing newline."""
    lines = [SECTION_HEADER, *_COMMENT, *sorted(runtime_artifacts(config_text)), SECTION_END]
    return "\n".join(lines)


def _section_bounds(lines: list[str]) -> tuple[int, int] | None:
    """`(start, end)` inclusive indices of an existing section, or None.

    `end` is the terminator line when present, else the last pattern before the next `# ---- `
    header or end of file — the same boundary the canary's parser stops at.
    """
    try:
        start = lines.index(SECTION_HEADER)
    except ValueError:
        return None
    for i in range(start + 1, len(lines)):
        if lines[i].strip() == SECTION_END:
            return start, i
        if lines[i].startswith("# ---- "):
            return start, i - 1
    return start, len(lines) - 1


def merge(existing_text: str, config_text: str) -> tuple[str, bool]:
    """Return `(text, changed)` — the section replaced in place if present, else appended.

    Idempotent: a file already carrying the current block comes back unchanged with
    `changed=False`.
    """
    new_block = block(config_text)
    lines = existing_text.splitlines()
    bounds = _section_bounds(lines)
    if bounds is not None:
        start, end = bounds
        rebuilt = lines[:start] + new_block.split("\n") + lines[end + 1 :]
    else:
        prefix = lines + [""] if lines and lines[-1].strip() else lines
        rebuilt = prefix + new_block.split("\n")
    text = "\n".join(rebuilt).rstrip("\n") + "\n"
    return text, text != existing_text


def remove(text: str) -> str:
    """Remove the section (and the one blank line that preceded an appended block)."""
    lines = text.splitlines()
    bounds = _section_bounds(lines)
    if bounds is None:
        return text
    start, end = bounds
    if start > 0 and lines[start - 1].strip() == "":
        start -= 1
    remaining = lines[:start] + lines[end + 1 :]
    if not remaining:
        return ""
    return "\n".join(remaining).rstrip("\n") + "\n"
