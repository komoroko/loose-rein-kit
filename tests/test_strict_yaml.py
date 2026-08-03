"""Tests for strict_yaml.py — the parser that refuses ambiguous input (plan §30.2).

Every rejection here is load-bearing: each corresponds to a way one document could mean two
things to two readers, which is what an evidence-bound harness cannot tolerate.
"""

from __future__ import annotations

import pytest

from rein import strict_yaml


def _err(text: str, **kwargs: object) -> str:
    with pytest.raises(strict_yaml.StrictParseError) as excinfo:
        strict_yaml.load(text, **kwargs)  # type: ignore[arg-type]
    return str(excinfo.value)


# --- what must parse ----------------------------------------------------------


def test_plain_mapping_round_trips() -> None:
    value = strict_yaml.load_mapping("a: 1\nb:\n  - x\n  - y\nc: true\nd: null\n")
    assert value == {"a": 1, "b": ["x", "y"], "c": True, "d": None}


def test_timestamps_stay_strings() -> None:
    # The whole point of dropping the implicit timestamp resolver: a datetime's repr is
    # platform-dependent, so a canonical digest over it would not be reproducible.
    value = strict_yaml.load_mapping("issued_at: 2026-07-23T18:10:00+09:00\nday: 2026-07-23\n")
    assert value == {"issued_at": "2026-07-23T18:10:00+09:00", "day": "2026-07-23"}
    assert all(isinstance(v, str) for v in value.values())


def test_safeloader_implicit_resolvers_are_not_mutated_globally() -> None:
    import yaml

    # The strict loader copies the resolver table; mutating the inherited dict in place would
    # silently reconfigure yaml.SafeLoader for the whole process.
    tags = {tag for resolvers in yaml.SafeLoader.yaml_implicit_resolvers.values() for tag, _ in resolvers}
    assert "tag:yaml.org,2002:timestamp" in tags


# --- ambiguity: the rejections ------------------------------------------------


def test_duplicate_key_rejected() -> None:
    assert "duplicate mapping key 'a'" in _err("a: 1\na: 2\n")


def test_duplicate_key_rejected_when_nested() -> None:
    assert "duplicate mapping key 'risk'" in _err("claim:\n  risk: high\n  risk: low\n")


def test_merge_key_rejected() -> None:
    # No anchor here, so the anchor rule cannot mask the merge rule: `<<` itself is refused.
    assert "merge key" in _err("child:\n  <<: {a: 1}\n  b: 2\n")


def test_quoted_merge_key_rejected_too() -> None:
    # Quoting makes `<<` an ordinary string key that the tag check cannot see, so the
    # construct_mapping rule catches it by name. A human reading the file cannot tell the two
    # spellings apart, so neither may parse.
    assert "merge key" in _err('child:\n  "<<": {a: 1}\n')


def test_merge_via_alias_rejected_at_the_anchor() -> None:
    # The usual spelling carries an anchor too; whichever rule fires first, the document is
    # refused before any part of it can be rewritten by another part.
    assert "not allowed" in _err("base: &b {a: 1}\nchild:\n  <<: *b\n")


def test_anchor_rejected() -> None:
    assert "anchor" in _err("a: &anchor 1\n")


def test_alias_rejected() -> None:
    assert "anchor '&x'" in _err("a: &x 1\nb: *x\n")


def test_alias_bomb_rejected_at_the_first_anchor() -> None:
    # Classic billion-laughs shape: refused before any expansion happens.
    bomb = "a: &a [x,x,x,x,x,x,x,x,x]\nb: &b [*a,*a,*a,*a,*a,*a,*a,*a,*a]\nc: [*b,*b,*b,*b,*b,*b,*b,*b,*b]\n"
    assert "anchor" in _err(bomb)


@pytest.mark.parametrize(
    "text",
    [
        "when: !!timestamp 2026-07-23\n",
        "blob: !!binary aGk=\n",
        "s: !!set {a: null}\n",
        "o: !!omap [{a: 1}]\n",
        "x: !custom value\n",
    ],
)
def test_non_core_tags_rejected(text: str) -> None:
    assert "is not allowed" in _err(text)


def test_python_object_tag_rejected() -> None:
    assert "not allowed" in _err("x: !!python/object/apply:os.system ['echo hi']\n")


def test_non_string_key_rejected() -> None:
    assert "mapping keys must be strings" in _err("1: a\n")


def test_malformed_yaml_is_an_error_not_an_empty_read() -> None:
    assert "plan.yaml" in _err("a: [1, 2\n", what="plan.yaml")


def test_empty_document_rejected() -> None:
    assert "empty document" in _err("")


def test_error_message_names_the_artifact() -> None:
    assert _err("a: 1\na: 2\n", what="plan.yaml").startswith("plan.yaml:")


# --- limits -------------------------------------------------------------------


def test_byte_limit() -> None:
    limits = strict_yaml.Limits(max_bytes=32)
    assert "exceeds the 32-byte limit" in _err("k: " + "x" * 100, limits=limits)


def test_depth_limit() -> None:
    limits = strict_yaml.Limits(max_depth=4)
    assert "nesting deeper than 4" in _err("a:\n b:\n  c:\n   d:\n    e: 1\n", limits=limits)


def test_scalar_length_limit() -> None:
    limits = strict_yaml.Limits(max_scalar_len=8)
    assert "scalar longer than 8" in _err("a: " + "x" * 20, limits=limits)


def test_collection_width_limit() -> None:
    limits = strict_yaml.Limits(max_collection=3)
    assert "collection larger than 3" in _err("a: [1, 2, 3, 4]", limits=limits)


def test_node_count_limit() -> None:
    limits = strict_yaml.Limits(max_nodes=5)
    assert "node limit" in _err("a: [1, 2, 3, 4, 5, 6, 7, 8]", limits=limits)


def test_load_mapping_requires_a_mapping() -> None:
    with pytest.raises(strict_yaml.StrictParseError, match="top level must be a mapping"):
        strict_yaml.load_mapping("- a\n- b\n")


def test_event_limits_are_tighter_than_the_default() -> None:
    assert strict_yaml.EVENT_LIMITS.max_bytes < strict_yaml.DEFAULT_LIMITS.max_bytes
    assert strict_yaml.UNTRUSTED_LIMITS.max_bytes < strict_yaml.DEFAULT_LIMITS.max_bytes


# --- JSON (untrusted reviewer / provider output) -------------------------------


def test_json_mapping_parses() -> None:
    assert strict_yaml.load_json_mapping('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_json_duplicate_key_rejected() -> None:
    with pytest.raises(strict_yaml.StrictParseError, match="duplicate object key 'a'"):
        strict_yaml.load_json('{"a": 1, "a": 2}')


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
def test_json_special_floats_rejected(literal: str) -> None:
    with pytest.raises(strict_yaml.StrictParseError, match="not allowed"):
        strict_yaml.load_json(f'{{"x": {literal}}}')


def test_json_depth_limit() -> None:
    limits = strict_yaml.Limits(max_depth=3)
    with pytest.raises(strict_yaml.StrictParseError, match="nesting deeper than 3"):
        strict_yaml.load_json('{"a":{"b":{"c":{"d":1}}}}', limits=limits)


def test_json_width_limit() -> None:
    limits = strict_yaml.Limits(max_collection=2)
    with pytest.raises(strict_yaml.StrictParseError, match="array larger than 2"):
        strict_yaml.load_json("[1,2,3]", limits=limits)


def test_json_byte_limit() -> None:
    limits = strict_yaml.Limits(max_bytes=8)
    with pytest.raises(strict_yaml.StrictParseError, match="exceeds the 8-byte limit"):
        strict_yaml.load_json('{"key": "a long value"}', limits=limits)


def test_json_malformed_is_an_error() -> None:
    with pytest.raises(strict_yaml.StrictParseError, match="reviewer output"):
        strict_yaml.load_json("{not json", what="reviewer output")


def test_json_mapping_requires_an_object() -> None:
    with pytest.raises(strict_yaml.StrictParseError, match="top level must be an object"):
        strict_yaml.load_json_mapping("[1, 2]")
