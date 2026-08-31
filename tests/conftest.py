"""Shared fixtures. The pure, importable helpers they build on live in `tests/_support.py`.

`make_repo` seeds a tmp `.rein/` repo and chdirs into it (auto-restored). An autouse
fixture points every XDG directory at the tmp tree: the Central Store keeps its lock,
journal, and control socket under `$XDG_RUNTIME_DIR`, and a test using the developer's real
one would contend with their live session — and could leave a stale lock behind on failure.
The same fixture pins PATH, for the same reason: see `_agent_cli_stub`.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable
from pathlib import Path

import pytest

from rein import repo as repo_mod
from tests._support import seed_repo

#: Agent CLIs the seeded config names. `preflight` asks PATH whether they exist, so on a machine
#: without them every full `rein build` in the suite refuses to start — a result about the host,
#: not about the code. The tests replace the launcher (`build_loop._run`), never the lookup.
STUBBED_AGENT_CLIS = ("claude",)


@pytest.fixture(scope="session")
def _agent_cli_stub(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory of agent-CLI stand-ins, prepended to PATH for every test.

    Each one refuses loudly if it is ever really executed: nothing in the suite launches an agent,
    so a stub that ran and said nothing would hide a test that had started launching one for real.
    """
    directory = tmp_path_factory.mktemp("agent-cli")
    for name in STUBBED_AGENT_CLIS:
        stub = directory / name
        stub.write_text(
            f'#!/bin/sh\necho "{name}: the test suite\'s stand-in was executed for real" >&2\nexit 99\n',
            encoding="utf-8",
        )
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return directory


@pytest.fixture(autouse=True)
def _isolated_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _agent_cli_stub: Path) -> None:
    """Never touch the real XDG directories, inherit repo-pointing env vars, or read the host's
    agent CLIs.

    Autouse because forgetting it is silent: the test still passes, it just quietly wrote to
    the developer's home directory — or quietly passed only on a machine with `claude` installed.
    """
    for var, name in (("XDG_RUNTIME_DIR", "run"), ("XDG_CACHE_HOME", "cache"), ("XDG_CONFIG_HOME", "config")):
        directory = tmp_path / name
        directory.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(var, str(directory))
    monkeypatch.setenv("PATH", f"{_agent_cli_stub}{os.pathsep}{os.environ.get('PATH', '')}")
    # `doctor` asks GitHub whether a newer rein exists, and `run_checks` is called all over this
    # suite. Today it happens to short-circuit — the editable install these tests run under has no
    # VCS origin — but that is a fact about this checkout, not a guarantee: run the same suite
    # against a `uv tool install git+…` and the tests start making network calls. Off by
    # construction, and the checks that exercise the fetch path delete it.
    monkeypatch.setenv("REIN_NO_UPDATE_CHECK", "1")
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
