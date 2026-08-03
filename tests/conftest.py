"""Shared fixtures. The pure, importable helpers they build on live in `tests/_support.py`.

`make_repo` seeds a tmp `.rein/` repo and chdirs into it (auto-restored). An autouse
fixture points every XDG directory at the tmp tree: the Central Store keeps its lock,
journal, and control socket under `$XDG_RUNTIME_DIR`, and a test using the developer's real
one would contend with their live session — and could leave a stale lock behind on failure.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from rein import repo as repo_mod
from tests._support import seed_repo


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch the real XDG directories or inherit repo-pointing env vars.

    Autouse because forgetting it is silent: the test still passes, it just quietly wrote to
    the developer's home directory.
    """
    for var, name in (("XDG_RUNTIME_DIR", "run"), ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config")):
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(directory))
    monkeypatch.delenv("REIN_ROOT", raising=False)
    monkeypatch.delenv("REIN_TRUST_MANIFEST", raising=False)


@pytest.fixture
def make_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Callable[..., Path]:
    """Factory: seed a repo under tmp_path and chdir into it (auto-restored). Kwargs → `seed_repo`."""

    def _make(*, chdir: bool = True, **kwargs: object) -> Path:
        seed_repo(tmp_path, **kwargs)  # type: ignore[arg-type]
        if chdir:
            monkeypatch.chdir(tmp_path)
        return tmp_path

    return _make


@pytest.fixture
def make_repo_obj(make_repo: Callable[..., Path]) -> Callable[..., repo_mod.Repo]:
    """Same as `make_repo`, returning the discovered :class:`Repo` instead of the path."""

    def _make(**kwargs: object) -> repo_mod.Repo:
        return repo_mod.Repo(make_repo(**kwargs))

    return _make


@pytest.fixture
def chdir_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """chdir into an empty tmp repo (auto-restored); for tests that seed their own files."""
    monkeypatch.chdir(tmp_path)
    return tmp_path
