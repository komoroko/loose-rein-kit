"""The harness's own notification channel — what makes "it's your turn" reach a person.

The loop's throughput is not what a human waits on; a human is what the loop waits on. Two gates
say how *often* the work stops, and nothing there says how long each stop lasts. That is set by how
soon the person finds out, and until now finding out was somebody else's job: each agent CLI
realized the `notify-and-wait` capability its own way (Claude Code has a push notification, the
rest "say so and end the turn"), and the dashboard signalled through the browser tab, which has to
be open to signal anything. So the one number a Human-on-the-Loop harness exists to keep small was
set outside it, by which CLI was in use and whether a window happened to be up.

It is owned here instead. :class:`Watcher` runs in the dashboard server for as long as the server
does — no browser required — derives the same `status.decision` the page and `rein next` derive,
and runs the configured command when the decision *changes*. One decision, one notification: the
id already changes only when the decision does, so a busy minute underneath an unchanged decision
is silent.

**The channel is the person's, not the repository's.** It lives in the user-global config next to
the project registry (`$XDG_CONFIG_HOME/rein/notify.yaml`), for the same reason principals and
credentials do: `.rein/config.yaml` is frozen by the mandate, and a notification channel is not a
thing a mandate should be able to freeze. Somebody who changes laptops mid-cycle changes where
their pings go, not what the loop was authorized to build.

**A notification is not an approval.** It carries what is waited on, which decision, and where to
answer — never the evidence, and never a way to answer. `REIN_URL` is the dashboard's base address
with no launch link on it, so a browser that does not already hold a session gets the read-only
page: answering keeps going through the UI's write authority or a terminal (`AGENTS.md` gate
rules). That is a real limit, not an oversight — the notification may leave this machine, and a
write capability that travelled with it would be the gate's authority following the message. What
a channel can do is tell somebody to come back.

**The wait is timed whether or not a channel exists.** `Watcher` runs either way and records every
wait in the arm it was spent in (`notified` / `silent`, :mod:`rein.observations`). A channel that
was only measured when it was switched on could never be shown to have helped.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rein import observations
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: How long the notify command may take before it is killed. A channel that hangs must not hold the
#: watcher thread: the next decision would go unannounced while the last one's curl waited on DNS.
COMMAND_TIMEOUT_SEC = 20

#: How often the watcher re-derives the decision. Slower than the page's own stream on purpose —
#: this is the "come back to your desk" path, and a few seconds of latency on it is not the thing
#: that makes a human wait.
POLL_SECONDS = 5.0

CONFIG_NAME = "notify.yaml"

#: What the notify command inherits from the dashboard's own environment, and nothing else.
#:
#: An empty environment is not a safe environment, it is a broken one. Without `PATH`, `argv[0]` is
#: looked up in `os.defpath` alone (`/bin:/usr/bin`), so a notifier installed by pipx, npm, Homebrew
#: or the user's own `~/bin` is not found at all — and `doctor`, which resolves the name against the
#: real `PATH`, would report PASS for a channel that can never run. Without `DBUS_SESSION_BUS_ADDRESS`
#: or `DISPLAY` a desktop notifier finds no session; without `HOME` a script finds no config.
#:
#: So the rule is an allowlist rather than a blank slate: the variables a command needs to *be a
#: command on this machine*, and none of the ones that carry credentials. Anything else the channel
#: needs, it reads from its own config — which is the same posture `.rein/config.yaml` takes toward
#: secrets. The payload is layered on top and wins, so `REIN_*` is never shadowed by the parent.
INHERITED_ENV: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "DISPLAY",
    "WAYLAND_DISPLAY",
    "XAUTHORITY",
    "DBUS_SESSION_BUS_ADDRESS",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_DIRS",
    "XDG_CONFIG_HOME",
    "SSH_AUTH_SOCK",
)


def command_env(payload: Mapping[str, str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the channel is run in: the allowlist above, then the payload over it."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in INHERITED_ENV if name in source}
    env.setdefault("PATH", os.defpath)
    env.update(payload)
    return env


def resolve(program: str, environ: Mapping[str, str] | None = None) -> str | None:
    """Where `program` would be found when the channel runs, or None.

    Shared with `doctor` on purpose. A check that resolves a name against one `PATH` while the
    runner resolves it against another reports on a command nobody will execute.
    """
    if Path(program).is_absolute() or os.sep in program:
        return program if Path(program).exists() else None
    return shutil.which(program, path=command_env({}, environ).get("PATH"))


def config_path() -> Path:
    """Where the channel is configured — user-global, beside the project registry."""
    return store_mod.config_home() / "rein" / CONFIG_NAME


@dataclass(frozen=True)
class Channel:
    """A configured notification channel: what to run, and nothing it is not allowed to carry."""

    argv: tuple[str, ...]

    def program(self) -> str:
        return self.argv[0]


def read_channel() -> Channel | None:
    """The configured channel, or None when none is set up.

    None is not a failure. Running without a channel is the supported default: the dashboard still
    badges its tab and `rein next` still prints the decision. What is missing is only the path that
    reaches somebody who is not looking — and the wait is still measured, in the `silent` arm, which
    is what makes "the channel shortens it" a claim anybody can check.
    """
    path = config_path()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"{path} could not be read, so no notification will be sent: {exc}")
        return None
    command = raw.get("command") if isinstance(raw, Mapping) else None
    if isinstance(command, str) and command.strip():
        return Channel(argv=tuple(shlex.split(command)))
    if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
        return Channel(argv=tuple(str(part) for part in command))
    if command is not None:
        logger.warning(f"{path}: `command` must be a string or a list of strings — no notification will be sent")
    return None


def render(decision: Mapping[str, Any], *, project: str, url: str) -> dict[str, str]:
    """What the channel is handed. Three facts and a place, and nothing that could stand in for the
    decision itself: a person still has to go and read it before they can answer it.

    `REIN_URL` is the dashboard's base address, deliberately without the launch link. The launch
    link is a one-time write capability handed over in the terminal that started the server, and a
    notification is a message this harness does not control the delivery of — pushed to a phone,
    posted to a chat, written to a file. Putting the capability in it would mean the write
    authority for a gate travels wherever the channel happens to go, which is the one thing the
    gate's whole design is about. So what arrives is "it is your turn, here": whoever answers does
    it from a browser that already holds the session, or from the terminal.
    """
    blocking = int(decision.get("blocking") or 0)
    # Stripped here rather than trusted from the caller. This module promises the payload carries
    # no way to answer, and a promise kept by one call site in `ui.py` is one line away from being
    # broken by the next one; the guarantee belongs where the promise is written.
    safe_url = url.split("?", 1)[0] if "?" in url else url
    behind = f" ({blocking} blocked)" if blocking else ""
    return {
        "REIN_PROJECT": project,
        "REIN_DECISION_ID": str(decision.get("id") or ""),
        "REIN_HEADLINE": f"{project}: {decision.get('headline') or 'a decision is waiting'}{behind}",
        "REIN_ACTION": str(decision.get("action") or ""),
        "REIN_URL": safe_url,
    }


def send(payload: Mapping[str, str], *, command: list[str] | None = None) -> bool:
    """Run the channel once. True when it exited zero; a failure is logged, never raised.

    Never raised because the caller is a watcher thread whose job is the *next* notification too.
    A channel that is down is a channel that is down; it is not a reason to stop watching, and it
    is certainly not a reason to take the dashboard with it.
    """
    if command is not None:
        argv = list(command)
    else:
        channel = read_channel()
        argv = list(channel.argv) if channel is not None else []
    if not argv:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 — argv from the user's own config, never from a request
            argv,
            env=command_env(payload),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SEC,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(f"the notification command failed to run: {exc}")
        return False
    if proc.returncode != 0:
        logger.warning(f"the notification command exited {proc.returncode}: {proc.stderr.strip()[:200]}")
        return False
    return True


class Watcher:
    """Re-derives the pending decision on a timer, notifies when it changes, and times every wait.

    Two jobs, and only one of them depends on a channel being configured. Notifying does. Measuring
    does not, and must not: `waited_seconds` is there to answer whether a channel shortens the wait,
    and a watcher that only runs when a channel exists records one arm of that comparison and calls
    it evidence. So it runs either way, and each wait carries the arm it was spent in.

    The one piece of state that survives a tick is the decision currently being waited on and when
    it started being waited on. That is what makes "one decision, one notification" true across a
    long run — the id is a function of the decision, so re-deriving an unchanged one is silent —
    and it is also what makes a wait get recorded when the *next* decision arrives rather than only
    when the queue empties. Answering one gate and having the next open immediately is the normal
    shape of a cycle; a watcher that only closed a wait on the way to idle would drop most of them.
    """

    def __init__(
        self,
        *,
        status: Any,
        project: str,
        url: str,
        stop: threading.Event,
        cycle_id: Callable[[], str] | str = "",
        interval: float = POLL_SECONDS,
    ) -> None:
        self._status = status
        self._project = project
        # Re-read per observation, not once at construction. The server outlives a cycle — that is
        # the point of it running with no browser open — and a wait filed under the cycle that
        # happened to be current at boot names an archive that does not hold it.
        fixed = "" if callable(cycle_id) else cycle_id
        self._cycle_id: Callable[[], str] = cycle_id if callable(cycle_id) else (lambda: fixed)
        self._url = url
        self._stop = stop
        self._interval = interval
        self._last_id: str = ""
        self._since: float = 0.0
        self._arm: str = ""
        self._status_failures = 0
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Always starts. Without a channel it notifies nothing and still times the waits."""
        self._thread = threading.Thread(target=self._run, name="rein-notify", daemon=True)
        self._thread.start()

    def tick(self) -> bool:
        """One pass. True when a notification was sent. Separated from the loop so a test can pin
        the decision-changed rule without a clock."""
        try:
            decision = self._status()
        except Exception as exc:
            # The SSOT is mid-write, or unreadable. The first one is news — a watcher that has gone
            # permanently blind looks exactly like a quiet repository at DEBUG — and the rest are
            # the retry working as intended.
            if self._status_failures == 0:
                logger.warning(f"could not derive the pending decision: {exc}. Retrying every {self._interval:g}s.")
            else:
                logger.debug(f"could not derive the pending decision: {exc}")
            self._status_failures += 1
            return False
        self._status_failures = 0
        waiting = isinstance(decision, Mapping) and bool(decision.get("waiting_on_human"))
        current = str(decision.get("id") or "") if waiting and isinstance(decision, Mapping) else ""
        if current == self._last_id:
            return False
        self._close_wait()
        self._last_id = current
        self._since = time.monotonic() if current else 0.0
        if not current:
            return False
        channel = read_channel()
        if channel is None:
            self._arm = observations.ARM_SILENT
            return False
        assert isinstance(decision, Mapping)
        sent = send(render(decision, project=self._project, url=self._url), command=list(channel.argv))
        # The arm is what the wait was *spent under*, and a channel that did not deliver is a wait
        # the person was not told about. It used to be set from `channel is not None` before this
        # call, so a configured-but-broken channel — a command that is not installed, a non-zero
        # exit, a timeout — filed every one of its waits as `notified`. The comparison then had a
        # treatment arm holding waits where nobody was notified, which is the one thing it exists
        # to distinguish. `send` already knew; the arm was reading the config instead of the outcome.
        #
        # Exactly one send happens per wait (the id has to change for `tick` to get this far), so
        # this one result is that wait's condition, fixed at its start like the other arm is.
        self._arm = observations.ARM_NOTIFIED if sent else observations.ARM_SILENT
        return sent

    def _close_wait(self) -> None:
        """File the wait that just ended, in the arm it was spent in.

        The arm is fixed when the wait *starts*, not here: a channel configured halfway through a
        wait did not shorten that wait, and crediting it would bias the comparison toward the
        answer somebody was hoping for.
        """
        if not self._last_id or not self._since:
            return
        observations.record(
            "waited_seconds",
            project=self._project,
            cycle_id=self._cycle_id(),
            value=max(0.0, time.monotonic() - self._since),
            subject=self._last_id,
            arm=self._arm or observations.ARM_SILENT,
        )
        self._since = 0.0

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()
