"""Verify cli.py: the `rein` dispatcher stays a thin, predictable verb surface."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

import rein
from rein import cli, common, init_cmd, registry, store, ui
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
    # One role, and one that names no model: a bulk switch onto `codex` is refused, because the
    # scaffold's review roles name models `codex` cannot be told to run.
    assert cli.main(["agent", "codex", "--role", "implementer"]) == 0
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
    """The listing is generated from the table, so no verb can be missing from `help --all`."""
    listing = cli._build_parser(show_all=True).format_help()
    for verb, entry in cli.VERBS.items():
        assert callable(cli._resolve(entry.spec)), entry.spec
        assert verb in listing, verb
        assert entry.summary.split(" (")[0][:30] in listing, verb


def test_the_default_help_drops_agent_verb_descriptions_but_never_their_names(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The perceived surface is the default listing; the epilog keeps discovery open.

    Agents call these verbs and agents read `rein --help` too, so hiding the *names* would leave
    prompt prose as the only way to learn a verb exists.
    """
    assert cli.main(["--help"]) == 0
    default = capsys.readouterr().out
    hidden = [name for name, entry in cli.VERBS.items() if not entry.human]
    assert hidden, "the split is pointless if nothing is hidden"
    for name in hidden:
        assert name in default, name
        assert cli.VERBS[name].summary not in default, name
    assert "rein help --all" in default

    assert cli.main(["help", "--all"]) == 0
    everything = capsys.readouterr().out
    for name in hidden:
        assert cli.VERBS[name].summary.split(" (")[0][:30] in everything, name


@pytest.mark.parametrize("spelling", ["--version", "-V", "--help", "-h", "help"])
def test_the_identity_spellings_survive_a_leading_repo_flag(
    repo: Path, capsys: pytest.CaptureFixture[str], spelling: str
) -> None:
    """`--repo` comes off before anything else is read.

    Reading these at argv[0] made `rein --repo X --version` answer `unknown verb '--version'`
    with exit 2 — a version or help check that fails reads as a broken install.
    """
    assert cli.main(["--repo", str(repo), spelling]) == 0
    assert capsys.readouterr().out.strip()  # something was answered, not an error


def test_start_honours_a_repo_flag_typed_after_the_verb(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--repo` is accepted on either side of every verb, and `start` is not an exception.

    It used to be: `start` was the one verb the dispatcher did not hand the flag to, so a trailing
    `--repo` reached `resume` but not the wizard check, and the two looked at different
    repositories.
    """
    monkeypatch.chdir(repo.parent)  # cwd is not a repo
    assert cli.main(["start", "--repo", str(repo)]) == 0
    assert "Since last time" in capsys.readouterr().out


@pytest.mark.parametrize("tty", [False, True])
def test_start_collects_the_status_exactly_once(repo: Path, monkeypatch: pytest.MonkeyPatch, tty: bool) -> None:
    """`resume` collects; the wizard decision reads two documents instead of asking again.

    Asking the whole status object "is this still a template?" would run the git subprocesses,
    digests and readiness probes 0.3.11 was spent making cheap, for a boolean the config and the
    state carry — and `rein start` is what the SessionStart hook runs at every session start.
    """
    from rein import status_api

    calls: list[object] = []
    real = status_api.collect_status

    def counting(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(status_api, "collect_status", counting)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty)

    assert cli.main(["start"]) == 0
    assert len(calls) == 1


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
    assert cli.main(["next"]) == 1  # every other verb hard-stops on a newer lock format
    assert "is in format" in capsys.readouterr().err


@pytest.mark.parametrize("spelling", ["version", "--version", "-V"])
def test_the_conventional_version_spellings_all_answer(spelling: str, capsys: pytest.CaptureFixture[str]) -> None:
    """`rein --version` used to answer `unknown verb '--version'` with exit 2.

    `help` has had three spellings from the start and `version` had one, so the invocation
    everybody reaches for first reported a broken install instead of a version.
    """
    assert cli.main([spelling]) == 0
    assert capsys.readouterr().out.strip() == rein.__version__


# --- start: wizard on a fresh copy, orientation afterwards -------------------------


def test_start_initialized_prints_the_delta_and_next(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["start"]) == 0
    out = capsys.readouterr().out
    assert "Since last time" in out
    assert "next: /build" in out


def test_start_full_adds_the_board_and_json_gives_the_object(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`--full` and `--json` are why there is no second verb for the snapshot."""
    assert cli.main(["start", "--full", "--no-mark"]) == 0
    board = capsys.readouterr().out
    assert "project: demo" in board and "### Gates" in board

    assert cli.main(["start", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["project"] == "demo"


def test_start_off_a_tty_reports_instead_of_refusing(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SessionStart hook runs this: refusing here is what kept the hook on a second verb.

    The packet already leads with `rein init --name <product>`, which is the answer a template
    checkout needs — so print it and exit 0 rather than erroring.
    """
    (repo / ".rein" / "config.yaml").write_bytes(store.dump_yaml(make_config(template_mode=True)))
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert cli.main(["start"]) == 0
    assert "rein init --name" in capsys.readouterr().out


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


def test_a_stated_failure_prints_as_an_error_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Every module here words its own reason and none of them reached a reader: a schema-invalid
    `config.yaml` after an upgrade, a comparator answer the policy refused, a review over budget —
    all of them arrived as Python tracebacks, which say "rein is broken" about a repository that is
    merely out of shape.
    """
    from rein import models

    def boom(argv: list[str] | None = None) -> int:
        raise models.DocumentError("config.yaml", ["agents/actual_extractor: unexpected 'independence_group'"])

    monkeypatch.setattr(cli, "_resolve", lambda spec: boom)
    monkeypatch.setattr(cli, "_lock_check", lambda flag: 0)

    assert cli.main(["doctor"]) == common.EXIT_CANNOT_PROCEED
    err = capsys.readouterr().err
    assert "independence_group" in err
    assert "Traceback" not in err
