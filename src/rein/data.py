"""Access to the package-data payload under ``src/rein/data/``.

The harness ships its non-code artifacts inside the wheel: the shared prompt bodies
(``data/prompts/``), the JSON schemas (``data/schema/``), the rules body (``data/rules/``),
the repo scaffolds `init` seeds (``data/scaffold/``), the per-agent integration surfaces
`install` writes (``data/integrations/``), and the CHANGELOG `upgrade` quotes. Everything
is read through :func:`importlib.resources.files`, so the same code works from a source
checkout, an editable install, and an installed wheel.

This module is the only place that knows the payload lives at ``rein/data`` — every
consumer names payload files by their data-relative posix path (e.g.
``prompts/commands/req.md``).
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from importlib.resources import files

if sys.version_info >= (3, 11):
    # importlib.abc.Traversable is deprecated in 3.12 and removed in 3.14; the resources.abc
    # spelling is the supported home on 3.11+ (our floor is 3.10, hence the guard).
    from importlib.resources.abc import Traversable
else:
    from importlib.abc import Traversable


def root() -> Traversable:
    """The payload root (the ``data/`` directory inside the installed package)."""
    return files("rein").joinpath("data")


def path(rel: str) -> Traversable:
    """The payload entry at data-relative posix path `rel` (existence not checked)."""
    entry = root()
    for part in rel.split("/"):
        entry = entry.joinpath(part)
    return entry


def read_text(rel: str) -> str:
    """The payload file `rel` as UTF-8 text (KeyError-like FileNotFoundError when absent)."""
    return path(rel).read_text(encoding="utf-8")


def read_bytes(rel: str) -> bytes:
    """The payload file `rel` as bytes."""
    return path(rel).read_bytes()


def iter_names(prefix: str = "") -> Iterator[str]:
    """Every payload file under `prefix`, as data-relative posix paths, sorted.

    The deterministic ordering is what makes the consumers' plans (init's seed list,
    sync's refresh report, the lock's file map) reproducible run over run.

    Names without bytes, because some callers only need to know *where* the payload lands:
    `install.present_surfaces` asks which of an integration's destinations exist on disk, and
    reading the whole tree to answer it cost the dashboard 300+ms on every status read.
    """
    base = path(prefix) if prefix else root()

    def _walk(entry: Traversable, rel: str) -> Iterator[str]:
        for child in sorted(entry.iterdir(), key=lambda e: e.name):
            child_rel = f"{rel}/{child.name}" if rel else child.name
            if child.is_dir():
                yield from _walk(child, child_rel)
            else:
                yield child_rel

    yield from _walk(base, prefix)


def iter_files(prefix: str = "") -> Iterator[tuple[str, bytes]]:
    """Every payload file under `prefix`, as (data-relative posix path, bytes), sorted.

    Built on :func:`iter_names` so that "which files the payload ships" has one answer.
    """
    for rel in iter_names(prefix):
        yield rel, read_bytes(rel)
