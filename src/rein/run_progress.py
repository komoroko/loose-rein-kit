"""What a gate-④ generation is doing *right now* — the one writer of it, and the one reader.

`rein review generate` says a great deal on its console and nothing anywhere else, so a human
watching the dashboard while a composed review runs sees "no machine review has been generated"
for however long it takes — thirteen hours on one measured run — with no way to tell a run in
flight from a run that never started. `run_record` is the wrong home for it: that is the audit
chain, appended once when a run ends, and filling a hash-chained log with a row per stage would
put commands-issued where changes-made belong.

So the live figure lives here, in a file under `.rein/work/` beside `review_cache` — gitignored,
dying with its worktree, and holding nothing a gate receipt binds. Two properties make it usable
from the dashboard without any new machinery:

- **`ui._WATCHED` stats it.** The SSE stream's fingerprint moves when this file does, so the next
  tick pushes a `status` payload and every open tab re-renders. No endpoint, no polling, no fetch.
- **Staleness is decided here, not in the browser.** A run killed with `SIGKILL` leaves its last
  line behind saying `running`; `read` compares `updated_at` against the clock and says so, rather
  than handing a timestamp to a client whose clock is not this machine's.

`outcome` is `run_record`'s own vocabulary plus :data:`RUNNING`, so a reader that knows one knows
the other. The file is left behind when a run ends rather than deleted: "the last run failed at
stage X" is worth as much as "a run is in flight", and a `running` file whose writer is gone is
exactly what `stale` is for.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rein import common, event_chain
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: Where the live figure is kept, relative to the repository root.
FILE = ".rein/work/review-run.json"

#: The `outcome` of a run that has not ended. Every other value is `run_record`'s.
RUNNING = "running"

#: How long a `running` file may go unwritten before a reader stops believing it. Three heartbeats:
#: the run prints one every :data:`common.HEARTBEAT_SEC` while a launch is in flight, so a writer
#: that is alive has moved well inside this, and one that has not is a process nobody can see.
STALE_AFTER_SEC = common.HEARTBEAT_SEC * 3


def path(root: Path) -> Path:
    return root / FILE


class Writer:
    """The live figure for one run. Every method swallows its own I/O errors.

    A progress file is a courtesy to a watching human; a run must never fail because one could not
    be written. That is the same posture `run_record` takes for the same reason, and it is why
    nothing downstream reads this back.

    Written from several threads — the readings run `readers` at a time and each runs its security
    stage on a worker — so the counter and the snapshot move under one lock.
    """

    def __init__(self, root: Path, *, run_id: str, total: int, stages: Sequence[Mapping[str, Any]] = ()) -> None:
        self._path = path(root)
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {
            "run_id": run_id,
            "started_at": event_chain.now_iso(),
            "outcome": RUNNING,
            "total": total,
            "done": 0,
            "stages": [dict(row) for row in stages],
        }
        self._flush()

    def landed(self, unit: str, stage: str, *, reused: bool, billed: Mapping[str, Any] | None = None) -> None:
        """One stage finished. `unit` is the reading it belonged to, `stage` what it was."""
        with self._lock:
            self._state["done"] = int(self._state["done"]) + 1
            self._state["last"] = {"unit": unit, "stage": stage, "reused": reused}
            if billed is not None:
                self._state["billed_by_role"] = dict(billed)
            self._flush()

    def ended(self, outcome: str) -> None:
        """However the run ended, in `run_record`'s vocabulary. The file stays; the state moves."""
        with self._lock:
            self._state["outcome"] = outcome
            self._flush()

    def _flush(self) -> None:
        self._state["updated_at"] = event_chain.now_iso()
        try:
            store_mod.atomic_write(self._path, json.dumps(self._state, ensure_ascii=False).encode("utf-8"))
        except OSError as exc:
            logger.debug(f"could not write the review-run progress file: {exc}")


def read(root: Path) -> dict[str, Any] | None:
    """The live figure, with `stale` decided here. None when no run has ever written one.

    Tolerant like every other read behind the dashboard: a half-written or hand-edited file is
    "nothing to show", never a 500 on the pane it appears in.
    """
    try:
        raw = path(root).read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        state = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(state, dict):
        return None
    state["stale"] = state.get("outcome") == RUNNING and _older_than(str(state.get("updated_at", "")))
    return state


def _older_than(updated_at: str) -> bool:
    """Has `updated_at` gone quiet for longer than a live run ever does? Unreadable = yes.

    A timestamp nothing can parse is one nothing can vouch for, and "believe it is still running"
    is the wrong direction to round: it puts a spinner in front of a human for a process that is
    not there.
    """
    since = common.seconds_since(updated_at)
    return since is None or since > STALE_AFTER_SEC
