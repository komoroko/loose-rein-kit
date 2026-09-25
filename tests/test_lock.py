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

#: The documents a repository owns, and whose shape `lock.FORMAT` is the name of. `event` is not
#: among them: events are appended by this tool and never read out of an older repository's file
#: into a newer release's model, so their shape is not what a lock has to agree about.
_PINNED_DOCUMENTS: tuple[str, ...] = ("plan", "state", "review", "config")

#: Keys that explain a schema rather than constrain a document. Stripped before the pin is taken.
_PROSE_KEYS = frozenset({"description", "title", "$comment", "examples"})

#: `(lock.FORMAT, :func:`_schema_shapes`)`. Updated by hand, both halves together — see
#: :func:`test_the_format_string_moves_when_a_document_shape_moves`.
#:
#: 0.8.1 moved the digest alone, for the one reason that permits it: `state.schema.json` **dropped**
#: the `change_requests[].id` pattern. The failure this pin guards against needs the new schema to
#: refuse a document the old one accepted, and a widening cannot do that — while a bump would stop
#: every verb in every 0.8.0 repository over a compatibility that was never at risk. A shape change
#: that removes nothing, or that removes and adds, is not this case and takes the bump.
#:
#: `v6` is the bump: `review_policy` gained `lens_judgement`, and `review_policy` is closed
#: (`additionalProperties: false`). A repository that configures a decider therefore holds a
#: `config.yaml` that 0.8.1 refuses — opt-in, but a real refusal, and the direction this string
#: exists to announce. A repository that configures nothing writes exactly what it wrote before;
#: the bump costs it a `rein sync --force`, and the alternative costs the ones that did opt in a
#: schema error with no version anywhere to explain it.
#:
#: The digest then moved again inside the same unreleased release, for `execution.max_cost_usd`,
#: and `execution` is closed too — so it is the same refusal in the same direction, and `v6` is
#: already the announcement of it. **This is the one shape in which a digest may move without the
#: string:** the string has not been released yet, so no repository anywhere reads `v6` as meaning
#: the earlier shape. A digest moving under a version somebody has installed is the failure this
#: pin exists to catch, and it is not this.
_FORMAT_PIN: tuple[str, str] = (
    "rein-grounded-v8",
    "sha256:29503ea2ba5aeaf530f589e6b2b1148467135e7e9a44d398f26b2deb2404f69a",
)


def _schema_shapes() -> str:
    """One digest over the *shape* of the four schemas — prose stripped.

    `description` is where these files do most of their explaining, and an explanation is not a
    shape: hashing the raw bytes made every reworded sentence demand a `lock.FORMAT` bump, which is
    the fastest way to teach everyone to update the pin without reading why it moved. What is left
    is what a document is refused for — the keys, the types, the enums, `required`,
    `additionalProperties`.
    """
    import json

    from rein import data as data_mod
    from rein import digests

    def shape(node: object) -> object:
        if isinstance(node, dict):
            return {k: shape(v) for k, v in node.items() if k not in _PROSE_KEYS}
        if isinstance(node, list):
            return [shape(v) for v in node]
        return node

    return digests.of(
        {name: shape(json.loads(data_mod.read_text(f"schema/{name}.schema.json"))) for name in _PINNED_DOCUMENTS}
    )


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
    assert "`rein init` a fresh one" in str(excinfo.value)


def test_a_foreign_format_is_refused(tmp_path: Path) -> None:
    path = write(tmp_path / "rein.lock", "format: rein-grounded-v0\ntool_version: 1.0.0\n")
    with pytest.raises(lock.LockError, match=f"reads '{lock.FORMAT}' only"):
        lock.read(path)


def test_the_refusal_names_both_versions_and_both_ways_out(tmp_path: Path) -> None:
    """The message every verb stops on (`cli._lock_check`), so it has to be the whole answer.

    It used to say "upgrade the tool", which is the wrong advice for the commoner direction: a
    current tool standing in a repository written by an older one. Naming the version that wrote
    the lock is what makes "install that one again" a thing an operator can actually do.
    """
    path = write(tmp_path / "rein.lock", "format: rein-grounded-v0\ntool_version: 0.4.7\n")
    with pytest.raises(lock.LockError) as caught:
        lock.read(path)
    message = str(caught.value)
    assert "written by rein 0.4.7" in message
    assert "there is no\nmigration" in message or "there is no migration" in message
    assert "rein init" in message and "CHANGELOG.md" in message


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


def _repo_with(tmp_path: Path, version: str, source: str = "") -> repo_mod.Repo:
    (tmp_path / ".rein").mkdir(parents=True, exist_ok=True)
    lock.write(tmp_path / ".rein" / "rein.lock", lock.new(version, source))
    return repo_mod.Repo(tmp_path)


def test_no_warning_when_the_versions_match(tmp_path: Path) -> None:
    assert lock.startup_warning(_repo_with(tmp_path, "0.1.0"), "0.1.0") is None


def test_no_warning_for_a_missing_lock(tmp_path: Path) -> None:
    (tmp_path / ".rein").mkdir()
    assert lock.startup_warning(repo_mod.Repo(tmp_path), "0.1.0") is None


def test_an_older_tool_is_told_to_upgrade(tmp_path: Path) -> None:
    """The command is derived from how this install was made, not quoted.

    `uv tool upgrade` is a no-op for the tag-pinned install the README prescribes, so the warning
    used to point at something that would not move the reader at all.
    """
    repo = _repo_with(tmp_path, "0.1.5", source="git+https://github.com/o/r@v0.1.5")
    warning = lock.startup_warning(repo, "0.1.0")
    assert warning is not None and "uv tool install --force 'git+https://github.com/o/r@vX.Y.Z'" in warning


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


def test_write_drops_keys_the_format_has_no_place_for(tmp_path: Path) -> None:
    """A retired key must not be carried forever.

    This repository's own lock held `rein: {version: 0.1.0}` from a layout that no longer exists,
    beside a `tool_version` of 0.3.12 — a machine-written file disagreeing with itself. The lock is
    derived from the installed package, so dropping the key is the whole migration.
    """
    path = tmp_path / "rein.lock"
    data = lock.new("0.4.0", "git+https://github.com/o/r@v0.4.0")
    data["rein"] = {"version": "0.1.0"}  # the retired spelling
    lock.write(path, data)

    written = lock.read(path)
    assert written is not None
    assert "rein" not in written
    assert set(written) <= set(lock.KEYS)
    assert lock.tool_version_of(written) == "0.4.0"
    assert lock.source_of(written) == "git+https://github.com/o/r@v0.4.0"


def test_source_of_reads_the_top_level_field(tmp_path: Path) -> None:
    assert lock.source_of({"source": "git+https://x/y"}) == "git+https://x/y"
    assert lock.source_of({}) == ""
    assert lock.source_of({"source": 3}) == ""


def test_written_by_newer_names_the_writer_and_stays_quiet_otherwise(tmp_path: Path) -> None:
    """What `doctor` needs to tell "this document is damaged" from "this tool is behind"."""
    behind = lock.written_by_newer(_repo_with(tmp_path, "0.5.0"), "0.4.0")
    assert behind is not None
    recorded, hint = behind
    assert recorded == "0.5.0"
    assert hint.startswith("`") and "install" in hint
    assert lock.written_by_newer(_repo_with(tmp_path, "0.4.0"), "0.4.0") is None
    assert lock.written_by_newer(_repo_with(tmp_path, "0.3.0"), "0.4.0") is None, "older is not this check's business"
    assert lock.written_by_newer(repo_mod.Repo(tmp_path / "nowhere"), "0.4.0") is None


def test_behind_summary_states_the_fact_and_leaves_the_consequence_to_the_caller(tmp_path: Path) -> None:
    """One sentence, written once. What being behind *costs* differs by where it is noticed —
    a document that is not damaged, a receipt that must not be written — and that half is the
    caller's; the fact and its repair are not."""
    summary = lock.behind_summary(_repo_with(tmp_path, "0.5.0", source="git+https://github.com/o/r@v0.5.0"), "0.4.0")
    assert summary is not None
    assert "written by rein 0.5.0" in summary
    assert "running 0.4.0" in summary
    assert "uv tool install --force" in summary
    assert lock.behind_summary(_repo_with(tmp_path, "0.4.0"), "0.4.0") is None
    assert lock.behind_summary(_repo_with(tmp_path, "0.3.0"), "0.4.0") is None


def test_the_format_string_moves_when_a_document_shape_moves() -> None:
    """The mechanism behind :data:`lock.FORMAT`'s docstring, because the instruction alone failed.

    `FORMAT` says which shape of the four SSOT documents this release reads, and a release that
    changes one of them and leaves this string alone ships a repository a schema will refuse while
    the lock reports it fine. That is not hypothetical: it went unchanged across 0.3.6–0.3.8 while
    two keys were renamed, and again through the redesign that collapsed five gates into two.

    So the schemas are pinned here. **When this fails, that is the test working**: read what moved,
    change `lock.FORMAT`, and put the new digest below in the same commit — never the digest alone,
    which is the failure this exists to catch wearing a green tick.
    """
    assert (lock.FORMAT, _schema_shapes()) == _FORMAT_PIN
