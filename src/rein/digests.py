"""Canonical serialization and the `sha256:` digests every binding is built from.

Almost every guarantee reduces to "these two things are the same bytes":
a Gate receipt binds a plan digest, a review binds the change it reviewed, a coverage manifest
is reusable only when seven digests match, a Machine review goes stale when the change
digest moves. That only holds if *one* serialization exists — so this module is the single
place a Python object becomes bytes for hashing, and nothing else in the tree may call
``json.dumps`` for that purpose.

Canonical form (plan §17.4): UTF-8 · object keys sorted · **array order preserved** (order is
data, not formatting) · compact separators · no NaN/Infinity · no non-string keys. The input
graph is already restricted to dict/list/str/int/float/bool/None by
:mod:`rein.strict_yaml`, which is what makes the form total rather than best-effort.

Two things are deliberately *not* here. There is no "digest this file's YAML text" helper:
a digest is taken over the parsed value or over raw bytes, never over one artifact's
formatting, so a reflow cannot invalidate a binding. And volatile timestamps are dropped
only when the caller names them (:data:`VOLATILE_TIMESTAMP_KEYS`) — a receipt's
``issued_at`` is part of what it records, a review's ``generated_at`` is not, and no global
rule can tell those apart.

Git tree digests (plan §17.4) hash ``path``/``mode``/``blob ID`` in POSIX path order, so the
subject of a review is the *committed* tree — not the working tree, whose untracked and
unstaged content no review ever saw.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PREFIX = "sha256:"
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BLOB_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")  # git SHA-1 or SHA-256 object id

# Timestamps that record *when a machine wrote a file*, not *what a human decided*. Callers
# hashing a stored artifact pass these to `digest(..., drop=...)` so re-serializing an
# unchanged document cannot move its digest. Never applied automatically — see the module
# docstring for why a receipt's issued_at must stay in what it covers.
VOLATILE_TIMESTAMP_KEYS = frozenset({"updated_at", "generated_at", "frozen_at", "installed_at", "completed_at"})

_CHUNK = 1024 * 1024


class DigestError(ValueError):
    """A value cannot be canonicalized, or a digest string is malformed."""


# --- canonical form -----------------------------------------------------------


def _check(value: Any, path: str) -> None:
    """Reject anything with no canonical form, naming where it sits (a bare TypeError is unactionable)."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / ±Inf
            raise DigestError(f"{path or '<root>'}: NaN/Infinity has no canonical form")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DigestError(f"{path or '<root>'}: mapping key {key!r} is not a string")
            _check(item, f"{path}.{key}" if path else key)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _check(item, f"{path}[{index}]")
        return
    raise DigestError(f"{path or '<root>'}: {type(value).__name__} has no canonical form")


def prune(value: Any, drop: Iterable[str]) -> Any:
    """`value` with every mapping key named in `drop` removed, at any depth (a copy; input untouched)."""
    dropped = frozenset(drop)
    if not dropped:
        return value
    if isinstance(value, dict):
        return {k: prune(v, dropped) for k, v in value.items() if k not in dropped}
    if isinstance(value, list):
        return [prune(item, dropped) for item in value]
    return value


def canonical(value: Any, *, drop: Iterable[str] = ()) -> bytes:
    """The canonical UTF-8 bytes of `value` (module docstring). Raises :class:`DigestError`."""
    pruned = prune(value, drop)
    _check(pruned, "")
    return json.dumps(pruned, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode(
        "utf-8"
    )


# --- digests ------------------------------------------------------------------


def of_bytes(data: bytes) -> str:
    """The `sha256:<hex>` digest of raw bytes."""
    return PREFIX + hashlib.sha256(data).hexdigest()


def of(value: Any, *, drop: Iterable[str] = ()) -> str:
    """The `sha256:<hex>` digest of `value`'s canonical form."""
    return of_bytes(canonical(value, drop=drop))


def of_file(path: str | Path) -> str:
    """The `sha256:<hex>` digest of a file's raw bytes, read in chunks (images/bundles may be large)."""
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            hasher.update(chunk)
    return PREFIX + hasher.hexdigest()


def of_texts(parts: Iterable[str]) -> str:
    """Digest of an ordered sequence of strings, length-prefixed so no concatenation collides.

    Used wherever a composite binding is built from several independently-digested pieces
    (the toolchain digest, for one): without the length prefix, ("ab","c") and ("a","bc")
    would hash identically and two different toolchains would look like one.
    """
    hasher = hashlib.sha256()
    for part in parts:
        blob = part.encode("utf-8")
        hasher.update(str(len(blob)).encode("ascii") + b"\0" + blob)
    return PREFIX + hasher.hexdigest()


def is_digest(value: object) -> bool:
    """True when `value` is a well-formed `sha256:<64 lowercase hex>` string."""
    return isinstance(value, str) and _DIGEST_RE.match(value) is not None


def require(value: object, what: str) -> str:
    """`value` as a validated digest string, or :class:`DigestError` naming `what`."""
    if not is_digest(value):
        raise DigestError(f"{what}: expected a 'sha256:<64 hex>' digest, got {value!r}")
    assert isinstance(value, str)
    return value


def matches(left: object, right: object) -> bool:
    """True when both are well-formed digests and equal — a mismatch and a malformed value
    are both False, so a caller can never read "unparseable" as "same"."""
    return is_digest(left) and is_digest(right) and left == right


# --- git tree digest ----------------------------------------------------------


@dataclass(frozen=True, order=True)
class TreeEntry:
    """One committed path: its POSIX path, git file mode, and object id.

    Ordering is by `path` first (the dataclass field order), which is exactly the POSIX path
    order plan §17.4 requires — so `sorted(entries)` is the canonical order.
    """

    path: str
    mode: str
    blob: str


def parse_ls_tree(output: str) -> list[TreeEntry]:
    """Parse ``git ls-tree -r -z <commit>`` output into entries.

    NUL-delimited (``-z``) on purpose: a path containing a newline or a quote would be
    C-escaped in the default format, and an escaped path is a different string from the one
    on disk — the digest must cover the real path.
    """
    entries: list[TreeEntry] = []
    for record in output.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        fields = meta.split()
        if not path or len(fields) < 3:
            raise DigestError(f"unparseable ls-tree record: {record!r}")
        mode, kind, blob = fields[0], fields[1], fields[2]
        if kind != "blob":  # submodule (commit) / nested tree: not a hashable file content
            raise DigestError(f"{path}: unsupported tree entry kind {kind!r} — pin or remove it before review")
        if not _BLOB_RE.match(blob):
            raise DigestError(f"{path}: unparseable object id {blob!r}")
        entries.append(TreeEntry(path=path, mode=mode, blob=blob))
    return entries


def tree_digest(entries: Sequence[TreeEntry]) -> str:
    """The digest of a committed tree: path/mode/blob in POSIX path order (plan §17.4).

    Duplicate paths are an error rather than a last-one-wins merge — two entries for one path
    means the caller built the list wrong, and silently picking one would make the digest
    depend on iteration order.
    """
    seen: set[str] = set()
    for entry in entries:
        if entry.path in seen:
            raise DigestError(f"duplicate tree entry for {entry.path!r}")
        seen.add(entry.path)
    return of_texts(f"{e.path}\0{e.mode}\0{e.blob}" for e in sorted(entries))


def filter_tree(entries: Iterable[TreeEntry], *, exclude_prefixes: Sequence[str]) -> list[TreeEntry]:
    """Entries whose path is not under any of `exclude_prefixes` (each a POSIX dir prefix).

    `change_digest` excludes the review/state/event artifacts (plan §17.3) — they
    are bound by their own digests, and including them would make every review that writes a
    file invalidate itself.
    """
    prefixes = tuple(p if p.endswith("/") else f"{p}/" for p in exclude_prefixes)
    exact = frozenset(p for p in exclude_prefixes if not p.endswith("/"))
    return [e for e in entries if e.path not in exact and not e.path.startswith(prefixes)]
