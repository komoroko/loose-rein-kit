"""Which implementer session finished which task, so the task after it can start from there.

A task that has exactly one upstream task starts where that task's implementer stopped: the new
launch **forks** the upstream's session rather than reading the ticket, the design slice and the
code from cold. A fork shares everything read before it and none of what the new launch then
concludes, so two leaves that share one upstream still reach their own answers, and the upstream's
session is left as it was.

This is a cache, not a record. It lives beside the reuse half of the evidence ledger
(``$XDG_CACHE_HOME/rein/<repo_id>/sessions.json``), outside the working tree, and nothing decides
anything by it. A session that is not here costs one cold start — which is what every task paid
before this existed — and the loop says so on the console rather than guessing an id. Entries are
keyed by cycle, so a session never crosses into a cycle whose plan it never read.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

from rein import repo as repo_mod
from rein import store as store_mod

#: The cache file inside the repository's cache directory.
FILENAME = "sessions.json"

#: How many task sessions to keep. An old entry is harmless (a finished cycle's keys are never
#: asked for again), so the cap is about file size — the newest entries are the live cycle's.
MAX_ENTRIES = 512


def _key(cycle: str, task_id: str) -> str:
    return f"{cycle}/{task_id}"


@dataclass
class Sessions:
    """task → the implementer session that produced its landed tree, for one repository."""

    path: Path | None
    _entries: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    #: Why the cache is off, "" while it is on. Said once by the caller, never swallowed.
    unavailable: str = ""

    @classmethod
    def for_repo(cls, repo: repo_mod.Repo, *, enabled: bool = True) -> Sessions:
        if not enabled:
            return cls(path=None, unavailable="disabled for this run")
        try:
            directory = store_mod.ensure_private_dir(store_mod.cache_dir(repo))
        except (store_mod.StoreError, OSError) as exc:
            return cls(path=None, unavailable=f"the cache directory cannot be used ({exc})")
        sessions = cls(path=directory / FILENAME)
        sessions._load()
        return sessions

    def get(self, cycle: str, task_id: str, adapter: str) -> str:
        """The session that finished `task_id` under `adapter` in `cycle`, or "" when there is none.

        A session another CLI opened is no session at all to this one, so the adapter is part of
        the answer rather than an afterthought.
        """
        with self._lock:
            entry = self._entries.get(_key(cycle, task_id), {})
        return entry.get("session", "") if entry.get("adapter") == adapter else ""

    def put(self, cycle: str, task_id: str, adapter: str, session: str) -> None:
        """Record the session that just finished `task_id`, and write the cache through.

        Written at once rather than at the end of the run: the task after this one is usually
        launched by a later `rein build`, and a run that stops for capacity between the two is the
        normal case, not the edge.
        """
        if self.path is None or not session:
            return
        with self._lock:
            key = _key(cycle, task_id)
            self._entries.pop(key, None)
            self._entries[key] = {"adapter": adapter, "session": session}
            kept = dict(list(self._entries.items())[-MAX_ENTRIES:])
            self._entries = kept
            body = json.dumps(kept, sort_keys=False, separators=(",", ":")) + "\n"
            try:
                store_mod.atomic_write(self.path, body.encode("utf-8"), mode=0o600)
            except (store_mod.StoreError, OSError) as exc:
                self.path, self.unavailable = None, f"the session cache could not be written ({exc})"

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            self.path, self.unavailable = None, f"{FILENAME} could not be read ({exc})"
            return
        if not isinstance(raw, dict):
            self.path, self.unavailable = None, f"{FILENAME} is not a mapping"
            return
        for key, entry in raw.items():
            if (
                isinstance(key, str)
                and isinstance(entry, dict)
                and isinstance(entry.get("adapter"), str)
                and isinstance(entry.get("session"), str)
            ):
                self._entries[key] = {"adapter": entry["adapter"], "session": entry["session"]}
