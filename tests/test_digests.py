"""Tests for digests.py — the canonical form every binding is built from.

The properties that matter are invariances and *non*-invariances: re-serializing must not
move a digest, and any change to data must. A test here failing means a binding could be
valid for the wrong bytes (or invalid for the right ones).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import digests

# --- canonical form -----------------------------------------------------------


def test_key_order_does_not_change_the_digest() -> None:
    assert digests.of({"b": 1, "a": 2}) == digests.of({"a": 2, "b": 1})


def test_array_order_does_change_the_digest() -> None:
    # Array order is data (a quality-gate step's argv, a claim's requirement_ids), never formatting.
    assert digests.of({"x": [1, 2]}) != digests.of({"x": [2, 1]})


def test_nested_key_order_does_not_change_the_digest() -> None:
    left = {"outer": {"z": 1, "a": {"n": 2, "m": 3}}}
    right = {"outer": {"a": {"m": 3, "n": 2}, "z": 1}}
    assert digests.of(left) == digests.of(right)


def test_canonical_is_compact_utf8_with_sorted_keys() -> None:
    assert digests.canonical({"b": 1, "a": "é"}) == b'{"a":"\xc3\xa9","b":1}'


def test_digest_has_the_sha256_prefix() -> None:
    value = digests.of({"a": 1})
    assert value.startswith("sha256:")
    assert digests.is_digest(value)


def test_types_without_a_canonical_form_are_refused() -> None:
    with pytest.raises(digests.DigestError, match=r"when: datetime .*no canonical form"):
        import datetime

        digests.of({"when": datetime.datetime(2026, 7, 23)})


def test_error_names_the_path_of_the_offending_value() -> None:
    with pytest.raises(digests.DigestError, match=r"a\.b\[1\]: set has no canonical form"):
        digests.of({"a": {"b": [1, {"x"}]}})


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_special_floats_refused(bad: float) -> None:
    with pytest.raises(digests.DigestError, match="NaN/Infinity"):
        digests.of({"x": bad})


def test_non_string_keys_refused() -> None:
    with pytest.raises(digests.DigestError, match="is not a string"):
        digests.of({1: "a"})


# --- prune / volatile timestamps ----------------------------------------------


def test_prune_removes_named_keys_at_any_depth() -> None:
    value = {"a": {"updated_at": "x", "b": [{"updated_at": "y", "c": 1}]}, "updated_at": "z"}
    assert digests.prune(value, {"updated_at"}) == {"a": {"b": [{"c": 1}]}}


def test_prune_does_not_mutate_its_input() -> None:
    value = {"updated_at": "x", "a": 1}
    digests.prune(value, {"updated_at"})
    assert value == {"updated_at": "x", "a": 1}


def test_dropping_a_volatile_timestamp_keeps_the_digest_stable() -> None:
    before = {"a": 1, "updated_at": "2026-07-23"}
    after = {"a": 1, "updated_at": "2026-07-24"}
    drop = digests.VOLATILE_TIMESTAMP_KEYS
    assert digests.of(before, drop=drop) == digests.of(after, drop=drop)
    assert digests.of(before) != digests.of(after)  # …but only because the caller asked


def test_issued_at_is_not_volatile() -> None:
    # A receipt records when it was confirmed; dropping it would let an old receipt be replayed
    # onto a later decision (plan §7.3).
    assert "issued_at" not in digests.VOLATILE_TIMESTAMP_KEYS


# --- of_texts: length-prefixed composition ------------------------------------


def test_of_texts_is_not_plain_concatenation() -> None:
    assert digests.of_texts(["ab", "c"]) != digests.of_texts(["a", "bc"])


def test_of_texts_is_order_sensitive() -> None:
    assert digests.of_texts(["a", "b"]) != digests.of_texts(["b", "a"])


# --- of_file ------------------------------------------------------------------


def test_of_file_matches_of_bytes(tmp_path: Path) -> None:
    path = tmp_path / "blob.bin"
    payload = b"x" * (1024 * 1024 + 7)  # spans the read chunk boundary
    path.write_bytes(payload)
    assert digests.of_file(path) == digests.of_bytes(payload)


# --- digest string handling ---------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["sha256:" + "a" * 63, "sha256:" + "A" * 64, "sha1:" + "a" * 64, "a" * 64, "", None, 5],
)
def test_is_digest_rejects_malformed(value: object) -> None:
    assert not digests.is_digest(value)


def test_require_names_the_subject() -> None:
    with pytest.raises(digests.DigestError, match="plan_digest: expected"):
        digests.require("nope", "plan_digest")


def test_matches_treats_malformed_as_not_equal() -> None:
    good = digests.of({"a": 1})
    assert digests.matches(good, good)
    assert not digests.matches(good, "nope")
    assert not digests.matches("nope", "nope")  # equal strings, but neither is a digest


# --- git tree digest ----------------------------------------------------------

_LS_TREE = "100644 blob " + "a" * 40 + "\tsrc/app.py\0" + "100755 blob " + "b" * 40 + "\tbin/run.sh\0"


def test_parse_ls_tree() -> None:
    entries = digests.parse_ls_tree(_LS_TREE)
    assert entries == [
        digests.TreeEntry("src/app.py", "100644", "a" * 40),
        digests.TreeEntry("bin/run.sh", "100755", "b" * 40),
    ]


def test_parse_ls_tree_keeps_paths_with_odd_characters() -> None:
    # NUL-delimited output is why: the default format would C-escape this path, and the
    # escaped string is not the path on disk.
    record = "100644 blob " + "c" * 40 + '\tdocs/a "b"\n c.md\0'
    assert digests.parse_ls_tree(record)[0].path == 'docs/a "b"\n c.md'


def test_parse_ls_tree_refuses_a_submodule() -> None:
    record = "160000 commit " + "d" * 40 + "\tvendor/lib\0"
    with pytest.raises(digests.DigestError, match="unsupported tree entry kind"):
        digests.parse_ls_tree(record)


def test_tree_digest_is_path_order_independent() -> None:
    entries = digests.parse_ls_tree(_LS_TREE)
    assert digests.tree_digest(entries) == digests.tree_digest(list(reversed(entries)))


def test_tree_digest_changes_when_a_blob_changes() -> None:
    entries = digests.parse_ls_tree(_LS_TREE)
    moved = [digests.TreeEntry(entries[0].path, entries[0].mode, "f" * 40), entries[1]]
    assert digests.tree_digest(entries) != digests.tree_digest(moved)


def test_tree_digest_changes_when_a_mode_changes() -> None:
    # A file becoming executable is a real behaviour change the digest must not hide.
    entries = digests.parse_ls_tree(_LS_TREE)
    chmoded = [digests.TreeEntry(entries[0].path, "100755", entries[0].blob), entries[1]]
    assert digests.tree_digest(entries) != digests.tree_digest(chmoded)


def test_tree_digest_refuses_duplicate_paths() -> None:
    entry = digests.TreeEntry("a.py", "100644", "a" * 40)
    with pytest.raises(digests.DigestError, match="duplicate tree entry"):
        digests.tree_digest([entry, entry])


def test_filter_tree_excludes_prefixes_and_exact_paths() -> None:
    entries = [
        digests.TreeEntry("src/app.py", "100644", "a" * 40),
        digests.TreeEntry(".rein/state.yaml", "100644", "b" * 40),
        digests.TreeEntry(".rein/prompts/commands/req.md", "100644", "c" * 40),
        digests.TreeEntry(".rein/plan.yaml", "100644", "d" * 40),
    ]
    kept = digests.filter_tree(entries, exclude_prefixes=[".rein/prompts/", ".rein/state.yaml"])
    assert [e.path for e in kept] == ["src/app.py", ".rein/plan.yaml"]
