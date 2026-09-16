"""The harness's own notification channel (plan §J).

A person is what the loop waits on, so the number that matters is how long each stop lasts — and
that is set by how soon they find out. These tests pin the two rules that make the signal worth
having: one decision produces one notification, and a channel that is down never takes the watcher
with it.
"""

from __future__ import annotations

import json
import os
import stat
import threading
from pathlib import Path
from typing import Any

import pytest

from rein import notify, observations


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
    assert notify.read_channel() is None
    assert notify.send({"REIN_HEADLINE": "x"}) is False


def test_the_channel_lives_beside_the_project_registry(config_home: Path) -> None:
    """Not `.rein/config.yaml`: that is frozen by the mandate, and where somebody's pings go is not
    a thing a mandate should be able to freeze."""
    assert notify.config_path() == config_home / notify.CONFIG_NAME
    assert ".rein" not in str(notify.config_path())


def test_a_string_command_is_split_like_a_shell_would(config_home: Path) -> None:
    _write(config_home, 'command: notify-send "rein" --urgency=low\n')
    channel = notify.read_channel()
    assert channel is not None and list(channel.argv) == ["notify-send", "rein", "--urgency=low"]


def test_a_list_command_is_taken_as_argv(config_home: Path) -> None:
    _write(config_home, "command:\n  - /usr/bin/say\n  - your turn\n")
    channel = notify.read_channel()
    assert channel is not None and list(channel.argv) == ["/usr/bin/say", "your turn"]


def test_an_unusable_command_is_no_channel_rather_than_a_crash(config_home: Path) -> None:
    _write(config_home, "command: 42\n")
    assert notify.read_channel() is None


def _capture(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    """Record what `send` was handed, and make the channel look configured to the watcher."""
    sent: list[dict[str, str]] = []

    def fake_send(payload: Any, **_: Any) -> bool:
        sent.append(dict(payload))
        return True

    monkeypatch.setattr(notify, "send", fake_send)
    monkeypatch.setattr(notify, "read_channel", lambda: notify.Channel(argv=("true",)))
    return sent


# --- the command runs in an environment that can find it -----------------------


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "mynotify"
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def test_a_command_on_the_path_is_actually_launched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test that goes through `subprocess.run`. An empty environment is not a safe
    environment, it is a broken one: without PATH, `argv[0]` is looked up in `/bin:/usr/bin` alone,
    so a notifier from pipx, npm, Homebrew or `~/bin` is never found — and `doctor`, resolving the
    same name against the real PATH, would call that channel PASS."""
    out = tmp_path / "seen.json"
    _script(tmp_path, f'#!/bin/sh\nprintf \'{{"h":"%s","u":"%s"}}\' "$REIN_HEADLINE" "$REIN_URL" > {out}\n')
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    assert notify.send({"REIN_HEADLINE": "your turn", "REIN_URL": "http://127.0.0.1:1/"}, command=["mynotify"]) is True
    assert json.loads(out.read_text(encoding="utf-8")) == {"h": "your turn", "u": "http://127.0.0.1:1/"}


def test_the_command_environment_carries_the_session_a_desktop_notifier_needs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`notify-send` with no `DBUS_SESSION_BUS_ADDRESS` finds no session; a shell script with no
    `HOME` finds no config. The allowlist is what a command needs to be a command on this machine,
    and the payload is layered over it so `REIN_*` is never shadowed by the parent."""
    env = notify.command_env(
        {"REIN_HEADLINE": "x"},
        {
            "PATH": "/opt/bin",
            "HOME": "/home/me",
            "DBUS_SESSION_BUS_ADDRESS": "unix:/run/bus",
            "AWS_SECRET_KEY": "s3cr3t",
        },
    )

    assert env["PATH"] == "/opt/bin"
    assert env["HOME"] == "/home/me"
    assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:/run/bus"
    assert env["REIN_HEADLINE"] == "x"
    assert "AWS_SECRET_KEY" not in env


def test_doctor_and_the_runner_resolve_the_command_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that resolves a name against one PATH while the runner resolves it against another
    reports PASS for a command nobody will execute."""
    from rein import doctor

    _script(tmp_path, "#!/bin/sh\nexit 0\n")
    _write(tmp_path / "rein", "command: mynotify\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}")

    assert notify.resolve("mynotify") is not None
    assert [f.level for f in doctor.check_notification_channel()] == ["PASS"]
    assert notify.send({"REIN_HEADLINE": "x"}) is True


def test_a_command_that_cannot_be_found_is_a_warning_not_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rein import doctor

    _write(tmp_path / "rein", "command: no-such-notifier-anywhere\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert notify.resolve("no-such-notifier-anywhere") is None
    assert [f.level for f in doctor.check_notification_channel()] == ["WARN"]


# --- one decision, one notification --------------------------------------------


def test_an_unchanged_decision_is_announced_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """The id is a function of the decision, so a busy minute underneath an unchanged one is
    silent however much moved. Re-announcing is what turns a signal into noise nobody reads."""
    sent = _capture(monkeypatch)
    queue: list[Any] = [_decision(), _decision(), _decision()]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert [watcher.tick(), watcher.tick(), watcher.tick()] == [True, False, False]
    assert len(sent) == 1
    assert sent[0]["REIN_HEADLINE"] == "demo: the mandate is ready for your approval (2 blocked)"
    assert sent[0]["REIN_ACTION"] == "rein approve mandate"


def test_a_different_decision_is_announced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _capture(monkeypatch)
    queue: list[Any] = [_decision("a"), _decision("b")]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert [watcher.tick(), watcher.tick()] == [True, True]


def test_a_decision_that_goes_away_and_comes_back_is_announced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Correctly so: it is waiting again. Suppressing it would leave a person who already answered
    once with no signal the second time round."""
    _capture(monkeypatch)
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
    assert watcher.tick() is False  # and again, still without raising


# --- every wait is measured, in the arm it was spent in -------------------------


def test_a_wait_is_recorded_when_the_next_decision_arrives_not_only_at_idle(
    config_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Answering one gate and having the next open immediately is the normal shape of a cycle. A
    watcher that only closed a wait on the way to idle would drop most of them, and the mean it
    reported would be the mean of the few waits that happened to end in silence."""
    _capture(monkeypatch)
    queue: list[Any] = [_decision("a"), _decision("b"), {"id": "", "waiting_on_human": False}]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    watcher.tick(), watcher.tick(), watcher.tick()

    waits = [o for o in observations.read() if o.kind == "waited_seconds"]
    assert [o.subject for o in waits] == ["a", "b"]


def test_a_wait_with_no_channel_configured_is_still_measured(config_home: Path) -> None:
    """The control arm. "A channel shortens the wait" is a comparison, and a quantity recorded only
    when the channel is on has one arm — which is a number, not evidence."""
    queue: list[Any] = [_decision("a"), {"id": "", "waiting_on_human": False}]
    watcher = notify.Watcher(status=lambda: queue.pop(0), project="demo", url="http://x/", stop=threading.Event())

    assert watcher.tick() is False  # nothing to notify with
    watcher.tick()

    waits = [o for o in observations.read() if o.kind == "waited_seconds"]
    assert [o.arm for o in waits] == [observations.ARM_SILENT]


def test_the_two_arms_are_summarized_apart(config_home: Path) -> None:
    """Pooled, the mean moves with whichever arm was recorded more and answers a question nobody
    asked. The claim is the difference between them."""
    for arm, value in ((observations.ARM_NOTIFIED, 60.0), (observations.ARM_SILENT, 600.0)):
        observations.record("waited_seconds", project="demo", cycle_id="c", value=value, arm=arm)
    summary = observations.summarize(observations.read())

    assert summary["waited_seconds/notified"]["mean"] == 60.0
    assert summary["waited_seconds/silent"]["mean"] == 600.0


def test_the_cycle_id_is_read_when_the_wait_ends_not_when_the_server_started(
    config_home: Path,
) -> None:
    """The server outlives cycles — that is the point of it running with no browser open — and a
    wait filed under the cycle that happened to be current at boot names an archive not holding it."""
    cycles = iter(["cycle-two"])
    queue: list[Any] = [_decision("a"), {"id": "", "waiting_on_human": False}]
    watcher = notify.Watcher(
        status=lambda: queue.pop(0),
        project="demo",
        url="http://x/",
        stop=threading.Event(),
        cycle_id=lambda: next(cycles),
    )

    watcher.tick(), watcher.tick()

    waits = [o for o in observations.read() if o.kind == "waited_seconds"]
    assert [o.cycle_id for o in waits] == ["cycle-two"]


# --- a notification is not an approval -----------------------------------------


def test_the_payload_carries_no_evidence_and_no_way_to_answer() -> None:
    """It may leave the machine, so it says what is waited on and where — never the evidence, and
    never a token. Answering still goes through the UI's write authority or a terminal."""
    payload = notify.render(_decision(), project="demo", url="http://127.0.0.1:8765/")

    assert set(payload) == {"REIN_PROJECT", "REIN_DECISION_ID", "REIN_HEADLINE", "REIN_ACTION", "REIN_URL"}
    assert "?k=" not in payload["REIN_URL"]
    joined = " ".join(payload.values())
    assert "token" not in joined and "secret" not in joined


def test_a_launch_link_handed_in_is_stripped_rather_than_forwarded() -> None:
    """The guarantee belongs in this module, not in the one call site that currently gets it right.
    A launch link is a one-time write capability, and a notification is a message whose delivery
    this harness does not control — pushed to a phone, posted to a chat, written to a file."""
    payload = notify.render(_decision(), project="demo", url="http://127.0.0.1:8765/?k=SUPERSECRET")

    assert payload["REIN_URL"] == "http://127.0.0.1:8765/"
    assert "SUPERSECRET" not in " ".join(payload.values())
