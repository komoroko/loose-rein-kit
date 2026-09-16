"""The harness's own notification channel (plan §J).

A person is what the loop waits on, so the number that matters is how long each stop lasts — and
that is set by how soon they find out. These tests pin the two rules that make the signal worth
having: one decision produces one notification, and a channel that is down never takes the watcher
with it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from rein import notify


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "rein"


def _write(config_home: Path, body: str) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / notify.CONFIG_NAME).write_text(body, encoding="utf-8")


def _decision(decision_id: str = "approve_gate:mandate:rein approve mandate") -> dict[str, Any]:
    return {
        "id": decision_id,
        "waiting_on_human": True,
        "kind": "approve_gate",
        "headline": "the mandate is ready for your approval",
        "action": "rein approve mandate",
        "blocking": 2,
        "open": 3,
    }


# --- the channel is the person's, not the repository's -------------------------


def test_no_channel_is_not_a_failure(config_home: Path) -> None:
    """Running without one is supported: the tab still badges and `rein next` still prints."""
    assert notify.read_command() is None
    assert notify.send({"REIN_HEADLINE": "x"}) is False


def test_the_channel_lives_beside_the_project_registry(config_home: Path) -> None:
    """Not `.rein/config.yaml`: that is frozen by the mandate, and where somebody's pings go is not
    a thing a mandate should be able to freeze."""
    assert notify.config_path() == config_home / notify.CONFIG_NAME
    assert ".rein" not in str(notify.config_path())


def test_a_string_command_is_split_like_a_shell_would(config_home: Path) -> None:
    _write(config_home, 'command: notify-send "rein" --urgency=low\n')
    assert notify.read_command() == ["notify-send", "rein", "--urgency=low"]


def test_a_list_command_is_taken_as_argv(config_home: Path) -> None:
    _write(config_home, "command:\n  - /usr/bin/say\n  - your turn\n")
    assert notify.read_command() == ["/usr/bin/say", "your turn"]


def test_an_unusable_command_is_no_channel_rather_than_a_crash(config_home: Path) -> None:
    _write(config_home, "command: 42\n")
    assert notify.read_command() is None


# --- one decision, one notification --------------------------------------------


def test_an_unchanged_decision_is_announced_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id is a function of the decision, so a busy minute underneath an unchanged one is
    silent however much moved. Re-announcing is what turns a signal into noise nobody reads."""
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda payload, **_: sent.append(dict(payload)) or True)
    queue: list[Any] = [_decision(), _decision(), _decision()]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert [watcher.tick(), watcher.tick(), watcher.tick()] == [True, False, False]
    assert len(sent) == 1
    assert sent[0]["REIN_HEADLINE"] == "demo: the mandate is ready for your approval (2 blocked)"
    assert sent[0]["REIN_ACTION"] == "rein approve mandate"


def test_a_different_decision_is_announced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda payload, **_: sent.append(dict(payload)) or True)
    queue: list[Any] = [_decision("a"), _decision("b")]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert [watcher.tick(), watcher.tick()] == [True, True]


def test_a_decision_that_goes_away_and_comes_back_is_announced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correctly so: it is waiting again. Suppressing it would leave a person who already answered
    once with no signal the second time round."""
    sent: list[dict[str, str]] = []
    monkeypatch.setattr(notify, "send", lambda payload, **_: sent.append(dict(payload)) or True)
    idle: dict[str, Any] = {"id": "", "waiting_on_human": False}
    queue: list[Any] = [_decision(), idle, _decision()]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert [watcher.tick(), watcher.tick(), watcher.tick()] == [True, False, True]


def test_an_unreadable_ssot_is_skipped_rather_than_killing_the_watcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The watcher's job is the *next* notification too. A mid-write SSOT is a thing to try again
    on, not a reason to take the dashboard's only outward signal down with it."""
    monkeypatch.setattr(notify, "send", lambda payload, **_: True)

    def boom() -> Any:
        raise OSError("the store is mid-write")

    watcher = notify.Watcher(status=boom, project="demo", url="http://x/", stop=threading.Event())
    assert watcher.tick() is False


# --- a notification is not an approval -----------------------------------------


def test_the_payload_carries_no_evidence_and_no_way_to_answer() -> None:
    """It may leave the machine, so it says what is waited on and where — never the evidence, and
    never a token. Answering still goes through the UI's write authority or a terminal."""
    payload = notify.render(_decision(), project="demo", url="http://127.0.0.1:8765/")

    assert set(payload) == {"REIN_PROJECT", "REIN_DECISION_ID", "REIN_HEADLINE", "REIN_ACTION", "REIN_URL"}
    assert "?k=" not in payload["REIN_URL"]
    joined = " ".join(payload.values())
    assert "token" not in joined and "secret" not in joined
