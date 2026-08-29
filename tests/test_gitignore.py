"""Tests for rein/gitignore.py — the runtime-artifact block `rein init` / `rein sync` maintain.

The block is *derived* from the code that writes each path, so the assertions here pin the
derivation (worktree dir from config, the three constants) and the merge's idempotence: a file
already carrying the current block must come back unchanged.
"""

from __future__ import annotations

from rein import dossier, gitignore, pr_draft, pr_stack, store
from tests._support import make_config

_CONFIG = store.dump_yaml(make_config()).decode()


def _config(worktree_dir: str) -> str:
    body = make_config()
    body["execution"]["worktree_dir"] = worktree_dir
    return store.dump_yaml(body).decode()


def test_runtime_artifacts_are_derived_from_the_writing_code() -> None:
    assert gitignore.runtime_artifacts(_CONFIG) == {
        ".worktrees/",
        pr_draft.OUT_PATH,
        pr_stack.OUT_DIR.rstrip("/") + "/",
        dossier.RELATIVE_PATH.rstrip("/") + "/",
    }


def test_a_renamed_worktree_dir_moves_the_pattern() -> None:
    artifacts = gitignore.runtime_artifacts(_config(".wt"))
    assert ".wt/" in artifacts
    assert ".worktrees/" not in artifacts


def test_block_lists_the_patterns_sorted_between_its_markers() -> None:
    text = gitignore.block(_CONFIG)
    lines = text.splitlines()
    assert lines[0] == gitignore.SECTION_HEADER
    assert lines[-1] == gitignore.SECTION_END
    patterns = [ln for ln in lines if not ln.startswith("#")]
    assert patterns == sorted(patterns)
    assert set(patterns) == gitignore.runtime_artifacts(_CONFIG)


def test_merge_into_an_empty_file_writes_just_the_block() -> None:
    text, changed = gitignore.merge("", _CONFIG)
    assert changed
    assert text == gitignore.block(_CONFIG) + "\n"


def test_merge_is_idempotent() -> None:
    once, _ = gitignore.merge("", _CONFIG)
    twice, changed = gitignore.merge(once, _CONFIG)
    assert not changed
    assert twice == once


def test_merge_appends_after_existing_content_with_one_blank_line() -> None:
    text, changed = gitignore.merge(".venv/\n__pycache__/\n", _CONFIG)
    assert changed
    assert text == ".venv/\n__pycache__/\n\n" + gitignore.block(_CONFIG) + "\n"


def test_merge_replaces_a_stale_block_in_place() -> None:
    fresh, _ = gitignore.merge(".venv/\n", _CONFIG)
    stale = fresh.replace(".worktrees/", ".wt/")
    healed, changed = gitignore.merge(stale, _CONFIG)
    assert changed
    assert ".worktrees/" in healed
    assert ".wt/" not in healed
    assert healed == fresh


def test_merge_replaces_a_block_that_has_no_terminator() -> None:
    # The template repo's own section ends at the next `# ---- ` header, not an explicit marker.
    legacy = (
        f"{gitignore.SECTION_HEADER}\n.worktrees/\n.rein/work/\n"
        "# ---- environment variables ----\n.env\n"
    )
    healed, changed = gitignore.merge(legacy, _CONFIG)
    assert changed
    assert healed.count(gitignore.SECTION_HEADER) == 1
    assert "# ---- environment variables ----\n.env\n" in healed
    assert gitignore.SECTION_END in healed


def test_remove_removes_the_block_and_the_blank_line_it_added() -> None:
    text, _ = gitignore.merge(".venv/\n__pycache__/\n", _CONFIG)
    assert gitignore.remove(text) == ".venv/\n__pycache__/\n"


def test_remove_of_a_file_that_held_only_the_block_yields_empty() -> None:
    text, _ = gitignore.merge("", _CONFIG)
    assert gitignore.remove(text) == ""


def test_remove_is_a_noop_when_the_block_is_absent() -> None:
    assert gitignore.remove(".venv/\n") == ".venv/\n"
