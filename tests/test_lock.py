"""Tests for lock.py — the read/write round-trip and the fail-closed format check.

The lock carries an opaque `format:` string rather than a numeric version, and the reason is
the assertion at the bottom of this file: a numeric version invites "newer than I know, but
probably close enough", and every compatibility shim starts life as that sentence. An opaque
string has no ordering, so there is nothing to be lenient about.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import lock
from rein import repo as repo_mod


def write(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def test_new_write_read_round_trip(tmp_path: Path) -> None:
    path = tmp_path / ".rein" / "rein.lock"
    lock.write(path, lock.new("0.1.0", "git+https://example/repo"))
    loaded = lock.read(path)

    assert loaded is not None
    assert loaded["format"] == lock.FORMAT
    assert lock.tool_version_of(loaded) == "0.1.0"
    assert loaded["source"] == "git+https://example/repo"
    assert loaded["created_at"] and loaded["updated_at"]


def test_write_stamps_the_format_whatever_the_caller_passed(tmp_path: Path) -> None:
    """Never take the caller's word for the format it just wrote."""
    path = tmp_path / "rein.lock"
    lock.write(path, {"format": "something-else", "tool_version": "0.1.0"})
    loaded = lock.read(path)
    assert loaded is not None and loaded["format"] == lock.FORMAT


def test_an_absent_lock_reads_as_none(tmp_path: Path) -> None:
    assert lock.read(tmp_path / "nope.lock") is None


def test_a_lock_without_a_format_key_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "rein.lock", "version: 1\nrein:\n  version: 0.0.4\n")
    with pytest.raises(lock.LockError) as excinfo:
        lock.read(path)
    assert "is in format None" in str(excinfo.value)
    assert "re-initialize the repository" in str(excinfo.value)


def test_a_foreign_format_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "rein.lock", "format: rein-grounded-v2\ntool_version: 1.0.0\n")
    with pytest.raises(lock.LockError, match="reads 'rein-grounded-v1' only"):
        lock.read(path)


def test_there_is_no_ordering_to_be_lenient_about() -> None:
    # An opaque string, deliberately. A numeric version is what makes "close enough" thinkable.
    assert isinstance(lock.FORMAT, str)
    assert not hasattr(lock, "FORMAT_VERSION")
    assert not hasattr(lock, "SCHEMA_VERSIONS")


def test_a_malformed_lock_is_refused_not_read_partially(tmp_path: Path) -> None:
    path = write(tmp_path / "rein.lock", "format: [unclosed\n")
    with pytest.raises(lock.LockError, match="restore it from git"):
        lock.read(path)


def test_a_duplicate_key_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "rein.lock", f"format: {lock.FORMAT}\ntool_version: 1\ntool_version: 2\n")
    with pytest.raises(lock.LockError, match="duplicate mapping key"):
        lock.read(path)


def test_norm_hash_ignores_line_endings() -> None:
    """A checkout's CRLF conversion is not an edit."""
    assert lock.norm_hash(b"a\r\nb\r\n") == lock.norm_hash(b"a\nb\n")


# --- the startup version-skew check -------------------------------------------


def _repo_with(tmp_path: Path, version: str) -> repo_mod.Repo:
    (tmp_path / ".rein").mkdir(parents=True, exist_ok=True)
    lock.write(tmp_path / ".rein" / "rein.lock", lock.new(version, ""))
    return repo_mod.Repo(tmp_path)


def test_no_warning_when_the_versions_match(tmp_path: Path) -> None:
    assert lock.startup_warning(_repo_with(tmp_path, "0.1.0"), "0.1.0") is None


def test_no_warning_for_a_missing_lock(tmp_path: Path) -> None:
    (tmp_path / ".rein").mkdir()
    assert lock.startup_warning(repo_mod.Repo(tmp_path), "0.1.0") is None


def test_an_older_tool_is_told_to_upgrade(tmp_path: Path) -> None:
    warning = lock.startup_warning(_repo_with(tmp_path, "0.1.5"), "0.1.0")
    assert warning is not None and "uv tool upgrade loose-rein-kit" in warning


def test_a_newer_tool_is_told_to_sync(tmp_path: Path) -> None:
    warning = lock.startup_warning(_repo_with(tmp_path, "0.1.0"), "0.1.5")
    assert warning is not None and "rein sync" in warning


def test_canonically_equal_versions_are_silent(tmp_path: Path) -> None:
    assert lock.startup_warning(_repo_with(tmp_path, "0.1.01"), "0.1.1") is None


def test_an_unparseable_version_is_reported_not_swallowed(tmp_path: Path) -> None:
    """The check that runs on every invocation does not go quiet about the file it just read."""
    (tmp_path / ".rein").mkdir()
    write(
        tmp_path / ".rein" / "rein.lock",
        f"format: {lock.FORMAT}\ntool_version: not-a-version\n",
    )
    warning = lock.startup_warning(repo_mod.Repo(tmp_path), "0.1.0")
    assert warning is not None and "damaged" in warning
