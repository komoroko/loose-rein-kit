"""rein — the Human-on-the-Loop development harness as an installable package.

Installed as a CLI (`rein <verb>`, see cli.py); product repositories keep only their
state (.rein/ and docs/) — the machinery lives here.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def _source_tree_version() -> str:
    """The [project] version of the adjacent pyproject.toml — for PYTHONPATH=src runs."""
    import re
    from pathlib import Path

    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return "0.0.0+source"
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else "0.0.0+source"


try:
    __version__ = version("loose-rein-kit")
except PackageNotFoundError:  # running from a source tree without installation
    __version__ = _source_tree_version()
