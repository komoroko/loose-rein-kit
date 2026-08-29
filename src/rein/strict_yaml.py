"""The strict input boundary — every YAML/JSON document Loose Rein reads passes through here.

``yaml.safe_load`` and ``json.loads`` are *tolerant* parsers, and tolerance is exactly what an
evidence-bound harness cannot afford: a duplicate key silently keeps the last value (so two
readers of the same bytes can disagree about what the document says), a merge key or alias lets
one part of a document rewrite another after review, an alias bomb turns a 200-byte file into
gigabytes of nodes, and an unquoted ``2026-07-23T10:00:00+09:00`` becomes a ``datetime`` whose
repr differs across platforms — which would make a canonical digest non-reproducible.

So the rule is: **a document that is not unambiguous is not a document.** Anything listed below
is a parse error, never a best-effort read:

  duplicate mapping key · merge key (``<<``) · anchor · alias · non-core tag
  (``!!timestamp`` / ``!!binary`` / ``!!set`` / ``!!omap`` / ``!!python/*`` / any ``!custom``)
  · nesting past `Limits.max_depth` · a scalar, collection, node count, or total byte
  count past its limit · NaN / Infinity (JSON)

Dropping the implicit timestamp resolver is deliberate and load-bearing, not incidental
hardening: timestamps stay `str`, so :mod:`rein.digests` sees the same bytes the file
holds and a digest computed on one machine matches one computed on another.

Failure posture: **one exception type, always fail closed.** :class:`StrictParseError`
carries a human-readable reason with the source mark; no caller is offered a "tolerant"
variant, because a second, laxer entry point is how tolerance creeps back in.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any

import yaml

from rein import common

# --- limits -------------------------------------------------------------------
#
# Sized against the artifacts actually read, with roughly an order of magnitude of
# headroom, so a limit fires on an attack or a runaway generator and never on honest input.
# A caller that legitimately needs more passes its own Limits rather than editing these.


@dataclass(frozen=True)
class Limits:
    """Resource ceilings for one parse. Exceeding any of them is a :class:`StrictParseError`."""

    max_bytes: int = 8 * 1024 * 1024
    max_depth: int = 32
    max_scalar_len: int = 256 * 1024
    max_collection: int = 8192  # entries in one mapping or sequence
    max_nodes: int = 200_000  # total nodes in the document (the alias-bomb ceiling)


DEFAULT_LIMITS = Limits()

# One event line must stay small enough that the whole chain can be re-verified cheaply
# (plan §18.7 "1 event の size に上限を設ける").
EVENT_LIMITS = Limits(max_bytes=64 * 1024, max_depth=16, max_scalar_len=8192, max_collection=512, max_nodes=2000)

# Reviewer/provider output is untrusted input from a process we do not control (plan §12.7,
# §8.3): tighter than our own SSOT, looser than one event.
UNTRUSTED_LIMITS = Limits(max_bytes=1024 * 1024, max_depth=24, max_scalar_len=64 * 1024, max_collection=4096)


class StrictParseError(common.ReinError, ValueError):
    """The document is not unambiguous (or not within limits) — never a partial read."""


# --- YAML ---------------------------------------------------------------------

# The core tags a document may use. Everything else — including the YAML 1.1 types SafeLoader
# would happily build (`!!timestamp`, `!!binary`, `!!set`, `!!omap`, `!!pairs`) — is refused,
# so the object graph only ever holds dict / list / str / int / float / bool / None.
_MERGE_TAG = "tag:yaml.org,2002:merge"
_ALLOWED_TAGS = frozenset(
    {
        "tag:yaml.org,2002:map",
        "tag:yaml.org,2002:seq",
        "tag:yaml.org,2002:str",
        "tag:yaml.org,2002:int",
        "tag:yaml.org,2002:float",
        "tag:yaml.org,2002:bool",
        "tag:yaml.org,2002:null",
    }
)


def _mark(node_or_event: Any) -> str:
    mark = getattr(node_or_event, "start_mark", None)
    return f" at line {mark.line + 1}, column {mark.column + 1}" if mark is not None else ""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader with every ambiguity removed. Instantiated per parse (it carries counters)."""

    def __init__(self, stream: str, limits: Limits = DEFAULT_LIMITS) -> None:
        super().__init__(stream)
        self._limits = limits
        self._depth = 0
        self._nodes = 0

    def compose_node(self, parent: Any, index: Any) -> Any:
        """Reject aliases and anchors up front; enforce depth and total node count."""
        if self.check_event(yaml.events.AliasEvent):  # type: ignore[no-untyped-call]
            event = self.peek_event()  # type: ignore[no-untyped-call]
            raise StrictParseError(f"YAML alias '*{event.anchor}' is not allowed{_mark(event)}")
        event = self.peek_event()  # type: ignore[no-untyped-call]
        if getattr(event, "anchor", None) is not None:
            raise StrictParseError(f"YAML anchor '&{event.anchor}' is not allowed{_mark(event)}")

        self._nodes += 1
        if self._nodes > self._limits.max_nodes:
            raise StrictParseError(f"document exceeds the {self._limits.max_nodes}-node limit")
        self._depth += 1
        if self._depth > self._limits.max_depth:
            raise StrictParseError(f"nesting deeper than {self._limits.max_depth} levels{_mark(event)}")
        try:
            node = yaml.SafeLoader.compose_node(self, parent, index)
        finally:
            self._depth -= 1
        if node is None:
            return None

        if node.tag == _MERGE_TAG:
            # Caught here rather than in construct_mapping (the tag check below would otherwise
            # report it as a generic bad tag); "merge key" is the actionable diagnostic.
            raise StrictParseError(f"YAML merge key '<<' is not allowed{_mark(node)}")
        if node.tag not in _ALLOWED_TAGS:
            raise StrictParseError(f"tag '{node.tag}' is not allowed{_mark(node)}")
        if isinstance(node, yaml.ScalarNode) and len(node.value) > self._limits.max_scalar_len:
            raise StrictParseError(f"scalar longer than {self._limits.max_scalar_len} characters{_mark(node)}")
        if isinstance(node, yaml.CollectionNode) and len(node.value) > self._limits.max_collection:
            raise StrictParseError(f"collection larger than {self._limits.max_collection} entries{_mark(node)}")
        return node

    def construct_mapping(self, node: Any, deep: bool = False) -> dict[Any, Any]:
        """Build a mapping with duplicate keys and merge keys refused.

        Deliberately does NOT call SafeConstructor.flatten_mapping (which is what implements
        ``<<``), so a merge key reaches us as an ordinary key and is rejected by name. That
        also catches a *quoted* ``"<<"``, which the tag check above cannot see.
        """
        if not isinstance(node, yaml.MappingNode):
            raise StrictParseError(f"expected a mapping{_mark(node)}")
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == _MERGE_TAG or key_node.value == "<<":
                raise StrictParseError(f"YAML merge key '<<' is not allowed{_mark(key_node)}")
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise StrictParseError(f"mapping keys must be strings, got {type(key).__name__}{_mark(key_node)}")
            if key in mapping:
                raise StrictParseError(f"duplicate mapping key {key!r}{_mark(key_node)}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


# The implicit resolvers are class state on the loader, so they must be *copied* before being
# edited — mutating the inherited dict would reconfigure yaml.SafeLoader process-wide and
# change how unrelated code (including PyYAML's own tests) parses. Dropping the timestamp
# resolver is what keeps dates as `str` for reproducible digests (module docstring).
_StrictLoader.yaml_implicit_resolvers = {
    first: [(tag, regexp) for tag, regexp in resolvers if tag != "tag:yaml.org,2002:timestamp"]
    for first, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}


def load(text: str, *, limits: Limits = DEFAULT_LIMITS, what: str = "document") -> Any:
    """Parse one strict YAML document. Raises :class:`StrictParseError` for anything ambiguous.

    `what` names the artifact in error messages (e.g. "plan.yaml"), so a failure points at
    the file the human has to fix rather than at a generic "document".
    """
    encoded = len(text.encode("utf-8"))
    if encoded > limits.max_bytes:
        raise StrictParseError(f"{what}: {encoded} bytes exceeds the {limits.max_bytes}-byte limit")
    loader = _StrictLoader(text, limits)
    try:
        node = loader.get_single_node()  # type: ignore[no-untyped-call]
        if node is None:
            raise StrictParseError(f"{what}: empty document")
        value = loader.construct_document(node)  # type: ignore[no-untyped-call]
    except StrictParseError as exc:
        raise StrictParseError(f"{what}: {exc}") from None
    except yaml.YAMLError as exc:
        raise StrictParseError(f"{what}: {exc}") from exc
    finally:
        loader.dispose()  # type: ignore[no-untyped-call]
    _reject_special_floats(value, what)
    return value


def load_mapping(text: str, *, limits: Limits = DEFAULT_LIMITS, what: str = "document") -> dict[str, Any]:
    """:func:`load`, additionally requiring the top level to be a mapping (every SSOT file is)."""
    value = load(text, limits=limits, what=what)
    if not isinstance(value, dict):
        raise StrictParseError(f"{what}: top level must be a mapping, got {type(value).__name__}")
    return value


# --- JSON ---------------------------------------------------------------------


def _reject_special_floats(value: Any, what: str) -> None:
    """Refuse NaN / ±Infinity anywhere in the graph — they have no canonical serialization."""
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, float):
            if math.isnan(current) or math.isinf(current):
                raise StrictParseError(f"{what}: NaN/Infinity is not allowed")
        elif isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise StrictParseError(f"duplicate object key {key!r}")
        seen[key] = value
    return seen


def _depth_and_size(value: Any, limits: Limits, what: str) -> None:
    """Post-parse structural check for JSON (json.loads has no hooks for depth/width)."""
    stack: list[tuple[Any, int]] = [(value, 1)]
    nodes = 0
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_nodes:
            raise StrictParseError(f"{what}: exceeds the {limits.max_nodes}-node limit")
        if depth > limits.max_depth:
            raise StrictParseError(f"{what}: nesting deeper than {limits.max_depth} levels")
        if isinstance(current, str) and len(current) > limits.max_scalar_len:
            raise StrictParseError(f"{what}: string longer than {limits.max_scalar_len} characters")
        elif isinstance(current, dict):
            if len(current) > limits.max_collection:
                raise StrictParseError(f"{what}: object larger than {limits.max_collection} entries")
            stack.extend((v, depth + 1) for v in current.values())
        elif isinstance(current, list):
            if len(current) > limits.max_collection:
                raise StrictParseError(f"{what}: array larger than {limits.max_collection} entries")
            stack.extend((v, depth + 1) for v in current)


def load_json(text: str, *, limits: Limits = UNTRUSTED_LIMITS, what: str = "document") -> Any:
    """Parse one strict JSON document — the entry point for every untrusted AI/provider output.

    Same posture as :func:`load`: duplicate keys, NaN/Infinity, and oversize/over-deep graphs
    are errors. Defaults to UNTRUSTED_LIMITS because that is what this function is for.
    """
    encoded = len(text.encode("utf-8"))
    if encoded > limits.max_bytes:
        raise StrictParseError(f"{what}: {encoded} bytes exceeds the {limits.max_bytes}-byte limit")
    try:
        value = json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_parse_constant)
    except StrictParseError as exc:
        raise StrictParseError(f"{what}: {exc}") from None
    except (json.JSONDecodeError, RecursionError) as exc:
        raise StrictParseError(f"{what}: {exc}") from exc
    _depth_and_size(value, limits, what)
    return value


def _parse_constant(name: str) -> Any:
    raise StrictParseError(f"{name} is not allowed")


def load_json_mapping(text: str, *, limits: Limits = UNTRUSTED_LIMITS, what: str = "document") -> dict[str, Any]:
    """:func:`load_json`, additionally requiring a top-level object."""
    value = load_json(text, limits=limits, what=what)
    if not isinstance(value, dict):
        raise StrictParseError(f"{what}: top level must be an object, got {type(value).__name__}")
    return value
