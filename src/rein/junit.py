"""Which tests a JUnit XML report says failed, and which file each one lives in.

The loop reads a red at the granularity of the step that went red — `make test` failed — and so
charged it to whichever task was under test. The test that failed may belong to another task, or
fail one run in three; neither is something the task under test could fix. JUnit XML is the one
report nearly every test runner can write (`pytest --junitxml`, `go test` via gotestsum, jest,
maven), and it is the only thing parsed here: the output of the runner is never read.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path


def failing(report: Path) -> frozenset[str] | None:
    """The failing tests in `report` as `<file or classname>::<name>`. None when there is no
    readable report — which is not the same answer as "nothing failed", and the caller says so."""
    try:
        root = ET.parse(report).getroot()
    except (OSError, ET.ParseError):
        return None
    failed: set[str] = set()
    for case in root.iter("testcase"):
        if case.find("failure") is not None or case.find("error") is not None:
            where = case.get("file") or case.get("classname") or ""
            failed.add(f"{where}::{case.get('name', '')}")
    return frozenset(failed)


def node_path(node: str, root: Path) -> str:
    """The repository file a failing test lives in, or "" when the report does not say one.

    A report that carries a `file` attribute names it outright. One that carries only a dotted
    `classname` (pytest's default xunit2, for one) is resolved against the tree: `a.b.test_c.TestD`
    is `a/b/test_c.py` when that file exists. Nothing is guessed past what exists on disk.
    """
    where = node.split("::", 1)[0]
    if "/" in where or where.endswith(".py"):
        return where
    parts = [part for part in where.split(".") if part]
    while parts:
        candidate = "/".join(parts) + ".py"
        if (root / candidate).is_file():
            return candidate
        parts.pop()
    return ""
