"""Verify data.py (the packaged payload accessor) and the package version fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

import rein
from rein import data


def test_payload_root_holds_the_shipped_surfaces() -> None:
    names = {entry.name for entry in data.root().iterdir()}
    assert {"prompts", "schema", "rules", "scaffold", "integrations", "CHANGELOG.md"} <= names


def test_read_text_and_bytes_agree() -> None:
    rel = "prompts/commands/req.md"
    text = data.read_text(rel)
    assert text and text.encode("utf-8") == data.read_bytes(rel)


def test_read_text_raises_on_an_absent_entry() -> None:
    with pytest.raises(OSError):
        data.read_text("prompts/commands/no-such-phase.md")


def test_iter_files_is_sorted_and_prefixed() -> None:
    files = list(data.iter_files("prompts"))
    rels = [rel for rel, _ in files]
    assert rels == sorted(rels)
    assert all(rel.startswith("prompts/") for rel in rels)
    assert "prompts/commands/req.md" in rels


def test_version_is_resolved_and_matches_the_source_tree() -> None:
    # Editable install or PYTHONPATH=src run alike: never the "unknown" sentinel here,
    # and the source-tree fallback parses the same pyproject the install reads.
    assert rein.__version__ not in ("", "0.0.0+source")
    pyproject = Path(rein.__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.is_file():
        assert rein._source_tree_version() == rein.__version__
