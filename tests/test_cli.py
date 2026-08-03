"""Verify cli.py: the `rein` dispatcher stays a thin, predictable verb surface."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from rein import cli, init_cmd, registry, store, ui
from tests._support import SANDBOXED_PROFILES, make_config


@pytest.fixture
def repo(make_repo: Callable[..., Path]) -> Path:
    return make_repo(config=make_config(profiles=SANDBOXED_PROFILES))


def test_help_lists_the_verbs_and_the_operations(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    out = capsys.readouterr().out
    for verb in ("start", "next", "ui", "agent", "project", "init", "install", "sync", "upgrade", "approve", "guard"):
        assert verb in out
    assert "the human's confirmation" in out  # gate rule 2's single guarded spelling stays discoverable
    assert cli.main(["--help"]) == 0


def test_unknown_verb_points_at_help(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["frobnicate"]) == 2
    assert "--help" in capsys.readouterr().err


def test_next_passes_through_to_status_api(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["next"]) == 0
    assert capsys.readouterr().out.startswith("next: /build")
    assert cli.main(["next", "--json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["command"] == "/build" and parsed["kind"] == "run_phase"


def test_agent_passes_through_to_agent_cli(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["agent", "codex"]) == 0
    assert "adapter: codex" in (repo / ".rein" / "config.yaml").read_text(encoding="utf-8")


def test_ui_passes_its_args_through(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_ui_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(ui, "main", fake_ui_main)
    assert cli.main(["ui", "--read-only", "--port", "0"]) == 0
    assert seen == [["--read-only", "--port", "0"]]


def test_project_passes_through_to_registry(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake_registry_main(argv: list[str]) -> int:
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(registry, "main", fake_registry_main)
    assert cli.main(["project", "add", "web", "/tmp/x"]) == 0
    assert seen == [["add", "web", "/tmp/x"]]


def test_verb_table_resolves_and_is_documented() -> None:
    for verb, spec in cli.VERBS.items():
        assert callable(cli._resolve(spec)), spec
        assert verb in cli.HELP, verb


def test_repo_flag_may_precede_the_verb(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(repo.parent)  # cwd is not a repo; only the global --repo points at one
    assert cli.main(["--repo", str(repo), "next"]) == 0
    assert capsys.readouterr().out.startswith("next: /build")


def test_version_short_circuits_the_lock_check(chdir_tmp: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (chdir_tmp / ".rein").mkdir()
    (chdir_tmp / ".rein" / "rein.lock").write_text("version: 99\n", encoding="utf-8")
    assert cli.main(["version"]) == 0  # identity must stay answerable under any lock
    capsys.readouterr()
    assert cli.main(["status"]) == 1  # every other verb hard-stops on a newer lock format
    assert "is in format" in capsys.readouterr().err


# --- start: wizard on a fresh copy, orientation afterwards -------------------------


def test_start_initialized_prints_where_you_are_and_next(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "project: demo" in out and "gates: 3/5 approved" in out
    assert "next: /build" in out


def test_start_uninitialized_non_tty_refuses_with_the_make_alternative(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo / ".rein" / "config.yaml").write_bytes(store.dump_yaml(make_config(template_mode=True)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["start"]) == 2
    assert "rein init --name" in capsys.readouterr().err


def test_start_uninitialized_tty_runs_the_wizard(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (repo / ".rein" / "config.yaml").write_bytes(store.dump_yaml(make_config(template_mode=True)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    called: list[bool] = []

    def fake_wizard(root: Path | None = None) -> int:
        called.append(True)
        return 0

    monkeypatch.setattr(init_cmd, "wizard", fake_wizard)
    assert cli.main(["start"]) == 0
    assert called == [True]


def test_start_rejects_extra_arguments(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start", "--force"]) == 2
    assert "no arguments" in capsys.readouterr().err
