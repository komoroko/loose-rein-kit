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
answer — never the evidence, and never a way to answer. Answering keeps going through the UI's
write authority or a terminal (`AGENTS.md` gate rules), so the notification may leave the machine
without widening anything: what a channel can do with it is tell somebody to come back.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

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


def config_path() -> Path:
    """Where the channel is configured — user-global, beside the project registry."""
    return store_mod.config_home() / "rein" / CONFIG_NAME


def read_command() -> list[str] | None:
    """The configured command, or None when no channel is set up.

    None is not a failure. Running without a channel is the supported default: the dashboard still
    badges its tab and `rein next` still prints the decision. What is missing is only the path that
    reaches somebody who is not looking.
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
        return shlex.split(command)
    if isinstance(command, list) and all(isinstance(part, str) for part in command) and command:
        return list(command)
    if command is not None:
        logger.warning(f"{path}: `command` must be a string or a list of strings — no notification will be sent")
    return None


def render(decision: Mapping[str, Any], *, project: str, url: str) -> dict[str, str]:
    """What the channel is handed. Three facts and a place, and nothing that could stand in for the
    decision itself: a person still has to go and read it before they can answer it."""
    blocking = int(decision.get("blocking") or 0)
    behind = f" ({blocking} blocked)" if blocking else ""
    return {
        "REIN_PROJECT": project,
        "REIN_DECISION_ID": str(decision.get("id") or ""),
        "REIN_HEADLINE": f"{project}: {decision.get('headline') or 'a decision is waiting'}{behind}",
        "REIN_ACTION": str(decision.get("action") or ""),
        "REIN_URL": url,
    }


def send(payload: Mapping[str, str], *, command: list[str] | None = None) -> bool:
    """Run the channel once. True when it exited zero; a failure is logged, never raised.

    Never raised because the caller is a watcher thread whose job is the *next* notification too.
    A channel that is down is a channel that is down; it is not a reason to stop watching, and it
    is certainly not a reason to take the dashboard with it.
    """
    argv = command if command is not None else read_command()
    if not argv:
        return False
    try:
        proc = subprocess.run(  # noqa: S603 — argv from the user's own config, never from a request
            argv,
            env={**dict(payload)},
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
    """Re-derives the pending decision on a timer and notifies when it changes.

    Started by the dashboard server and stopped with it. It holds no state beyond the last decision
    id it announced, which is what makes "one decision, one notification" true across a long run:
    the id is a function of the decision, so re-deriving an unchanged one is silent however much
    moved underneath it, and a decision that goes away and comes back announces itself again —
    correctly, because it is waiting again.
    """

    def __init__(
        self,
        *,
        status: Any,
        project: str,
        url: str,
        stop: threading.Event,
        interval: float = POLL_SECONDS,
    ) -> None:
        self._status = status
        self._project = project
        self._url = url
        self._stop = stop
        self._interval = interval
        self._last_id: str | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """No-op when no channel is configured: a thread that can only decide to do nothing is
        one more thing to reason about in a crash report, for no signal anybody receives."""
        if read_command() is None:
            return
        self._thread = threading.Thread(target=self._run, name="rein-notify", daemon=True)
        self._thread.start()

    def tick(self) -> bool:
        """One pass. True when a notification was sent. Separated from the loop so a test can pin
        the decision-changed rule without a clock."""
        try:
            decision = self._status()
        except Exception as exc:  # the SSOT is mid-write, or unreadable — try again next tick
            logger.debug(f"could not derive the pending decision: {exc}")
            return False
        if not isinstance(decision, Mapping) or not decision.get("waiting_on_human"):
            self._last_id = None  # nothing is waiting; the next thing that waits is news again
            return False
        current = str(decision.get("id") or "")
        if not current or current == self._last_id:
            return False
        self._last_id = current
        return send(render(decision, project=self._project, url=self._url))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            self.tick()
