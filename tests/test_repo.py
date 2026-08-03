"""Verify repo.py's root discovery (flag > env > walk-up) and the Repo path bundle."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rein import gate_guard
from rein import repo as repo_mod
from tests._support import GATE_ORDER, make_state, seed_repo


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    seed_repo(tmp_path, state=None)
    (tmp_path / "docs" / "tasks").mkdir(parents=True)
    return tmp_path


_ALL_PENDING = {name: "pending" for name in GATE_ORDER}


def test_find_root_walks_up_from_a_subdirectory(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REIN_ROOT", raising=False)
    assert repo_mod.find_root(start=repo_root / "docs" / "tasks") == repo_root
    assert repo_mod.find_root(start=repo_root) == repo_root


def test_find_root_without_marker_raises_with_guidance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REIN_ROOT", raising=False)
    (tmp_path / "plain").mkdir()
    with pytest.raises(repo_mod.RepoNotFoundError) as exc:
        repo_mod.find_root(start=tmp_path / "plain")
    assert "rein init" in str(exc.value) and "--repo" in str(exc.value)


def test_find_root_env_var_wins_over_walk_up(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    other = tmp_path / "other"
    (other / ".rein").mkdir(parents=True)
    monkeypatch.setenv("REIN_ROOT", str(other))
    assert repo_mod.find_root(start=repo_root) == other.resolve()


def test_find_root_override_beats_env_and_must_hold_marker(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REIN_ROOT", str(tmp_path / "nowhere"))
    assert repo_mod.find_root(override=str(repo_root)) == repo_root
    # An explicit choice without .rein/ is an error, never silently walked past.
    (tmp_path / "empty").mkdir()
    with pytest.raises(repo_mod.RepoNotFoundError):
        repo_mod.find_root(override=str(tmp_path / "empty"))


def test_bad_env_var_is_an_error_not_a_fallback(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REIN_ROOT", str(repo_root / "missing"))
    with pytest.raises(repo_mod.RepoNotFoundError):
        repo_mod.find_root(start=repo_root)


def test_repo_paths_are_absolute_and_root_anchored(repo_root: Path) -> None:
    repo = repo_mod.Repo(repo_root)
    assert repo.state == repo_root / ".rein/state.yaml"
    assert repo.config.is_absolute() and repo.plan.is_absolute() and repo.lock.is_absolute()
    assert repo.path("docs/20-design.md") == repo_root / "docs/20-design.md"


def test_repo_rel_normalizes_and_rejects_outside_paths(repo_root: Path) -> None:
    repo = repo_mod.Repo(repo_root)
    assert repo.rel(repo_root / "docs" / "20-design.md") == "docs/20-design.md"
    assert repo.rel("docs/20-design.md") == "docs/20-design.md"  # relative = repo-relative
    assert repo.rel(repo_root.parent / "elsewhere.md") is None


@pytest.mark.integration
def test_gate_guard_resolves_repo_from_the_payload_cwd(repo_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hook fired with cwd anywhere inside the repo judges paths against the discovered root."""
    monkeypatch.delenv("REIN_ROOT", raising=False)
    seed_repo(
        repo_root,
        state=make_state(gates=dict.fromkeys(GATE_ORDER, "pending")),
    )
    payload = json.dumps(
        {
            "cwd": str(repo_root / "docs"),
            "tool_name": "Write",
            "tool_input": {"file_path": str(repo_root / "docs" / "20-design.md")},
        }
    )
    env = {**os.environ, "PYTHONPATH": "src"}
    proc = subprocess.run(
        [sys.executable, "-m", "rein.gate_guard"],
        input=payload,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        env=env,
    )
    assert proc.returncode == 0
    assert '"deny"' in proc.stdout  # requirements gate pending → the design doc write is denied


def test_evaluate_accepts_an_explicit_repo_without_chdir(repo_root: Path) -> None:
    seed_repo(
        repo_root,
        state=make_state(
            gates={"requirements": "approved", "design": "pending", "tasks": "pending"}, plan_status="draft"
        ),
    )
    repo = repo_mod.Repo(repo_root)
    ok, _ = gate_guard.evaluate(str(repo_root / "docs" / "20-design.md"), repo)
    assert ok is True  # requirements approved → design doc editable
    ok, reason = gate_guard.evaluate(str(repo_root / "docs" / "tasks" / "T-001.md"), repo)
    assert ok is False and "design" in reason
