"""`rein start` — what changed since you last looked, not just where you are.

Every surface the tool had answered "where am I" with an absolute snapshot, and the SessionStart
hook printed that snapshot into every new session. Nothing anywhere answered "what moved while I
was gone", so coming back to a repository meant re-reading the whole state and diffing it against
memory. Interruption is not only lost minutes — resuming a task costs effort and carries its own
time pressure, and re-deriving context you already had is exactly the part that is avoidable.

So this reads a **watermark** — the last event sequence this user had seen in this repository — and
reports the delta above it: gates that opened, tasks that moved, escalations that arrived, reviews
that were regenerated. The reader chooses how much they want: the delta by default, `--full` for
the absolute board as well, `--json` for the whole status object.

This is the human's one reader. `rein next` prints the single recommended command for an
integration to act on, and the dashboard renders the same object — but there is no fourth verb
that prints the snapshot alone, because a second spelling of one answer is what made this
module necessary in the first place.

**The watermark is per-person, not per-repository.** It lives beside the project registry in the
user's config home, never in `.rein/`: the SSOT is machine-written under a transaction that
records why each change happened, and "koichi has read up to event 214" is not a change to the
project's state. Putting it there would also mean two people sharing a checkout would overwrite
each other's place in the log, and that a `git pull` could move a reader's watermark.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import rein
from rein import common, event_chain, models, observations, registry, status_api, upstream
from rein import lock as lock_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: Events worth putting in front of somebody returning, grouped by the sentence they belong in.
#: Everything else is still counted, just not enumerated — a resume packet that reprints the log is
#: the wall of text it exists to replace.
HEADLINE_EVENTS: dict[str, str] = {
    "gate_approved": "gates opened",
    "gate_revised": "gates rolled back",
    "plan_frozen": "the plan froze",
    "plan_invalidated": "the plan was invalidated",
    "review_generated": "the machine review was regenerated",
    "human_review_frozen": "the human review was frozen",
    "release_approved": "the release was approved",
    "cycle_closed": "the cycle closed",
    "task_completed": "tasks completed",
    "task_failed": "tasks failed",
    # Worth a headline precisely because it asks for nothing but a re-run: someone opening a
    # fresh terminal after a session limit needs to know the build stopped for the machine's
    # reasons and left every task where it was, not that something went wrong with the work.
    "run_aborted": "build runs stopped by the machine (re-runnable)",
    # The same sentence for acceptance's pipeline, and worth a headline for the same reason: a
    # composed review that stopped on a session limit kept every reading it had already taken
    # (`review_cache`), so what a returning operator has to know is that re-running is cheap, not
    # that eight hours were lost.
    "review_aborted": "review runs stopped by the machine (re-runnable)",
    "knowledge_gap": "knowledge gaps recorded",
}


#: How many queue rows the resume packet prints. It is read at the start of every session, so it
#: has to stay a packet; the count in the heading is what tells the reader the list is partial, and
#: `rein start --full` is one flag away with the whole of it.
_RESUME_ROWS = 5


def state_home() -> Path:
    """Where the watermark file lives, mirroring registry.config_home's precedence.

    `$REIN_CONFIG_HOME` is honoured first for the same reason the registry honours it: the
    test suite must never write into a developer's real config directory.
    """
    override = os.environ.get("REIN_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return base / "rein"


def watermark_path() -> Path:
    return state_home() / "seen.json"


def observed_path() -> Path:
    """The reader's place in the **observation** store, kept apart from their place in the chain.

    Two marks because they mark two stores: the chain is per-repository and sequenced, the
    observation store is user-global and counted. Merging them into one file would mean one of the
    two questions answering in the other's units.
    """
    return state_home() / "observed.json"


def _load_marks(path: Path) -> dict[str, int]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return {str(k): int(v) for k, v in raw.items() if isinstance(v, int)} if isinstance(raw, dict) else {}


def _write_mark(path: Path, key: str, value: int, what: str) -> None:
    """Record one mark. A failure here is logged, never raised: not being able to remember where
    you were is a degraded experience, not a reason to fail the command you actually ran."""
    marks = {**_load_marks(path), key: int(value)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marks, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as exc:
        logger.warning(f"could not record the {what}: {exc}")


def read_mark(root: Path) -> int:
    """The last event seq this user had seen in `root`, or 0 — a first visit sees everything."""
    return _load_marks(watermark_path()).get(str(root.resolve()), 0)


def write_mark(root: Path, seq: int) -> None:
    _write_mark(watermark_path(), str(root.resolve()), seq, "resume watermark")


def read_observed(root: Path) -> int:
    """How many observations this user had already seen for `root`'s project, or 0."""
    return _load_marks(observed_path()).get(str(root.resolve()), 0)


def write_observed(root: Path, count: int) -> None:
    _write_mark(observed_path(), str(root.resolve()), count, "observation watermark")


def observation_delta(root: Path, project: str) -> tuple[int, int]:
    """`(how many are there now, how many are new since this reader last looked)`.

    A count rather than a sequence because the store has no seq — and it can *shrink*, since
    `rein observe --prune` trims it. A mark above the current total therefore means the store was
    trimmed, not that readings vanished, and the delta is reported as zero rather than as a
    negative: there is nothing new to read, which is the true answer to the question asked.

    No threshold. The question is "what is new since you looked", never "is it a lot" — a figure
    with a level attached to it gets managed instead of read, which is the whole reason the
    observation store is never an input to anything.
    """
    total = sum(1 for entry in observations.read() if entry.project == project)
    return total, max(0, total - read_observed(root))


@dataclass(frozen=True)
class Packet:
    """The delta since the watermark: what moved, how much, and whether anything is waiting."""

    since: int
    latest: int
    first_visit: bool
    groups: dict[str, list[models.Event]]
    other: int

    @property
    def empty(self) -> bool:
        return not self.groups and not self.other


def build(events: Sequence[models.Event], since: int) -> Packet:
    """Group the events above `since` into the handful of sentences a returning reader needs."""
    fresh = [e for e in events if e.seq > since]
    groups: dict[str, list[models.Event]] = {}
    other = 0
    for event in fresh:
        label = HEADLINE_EVENTS.get(event.event)
        if label is None:
            other += 1
            continue
        groups.setdefault(label, []).append(event)
    return Packet(
        since=since,
        latest=events[-1].seq if events else 0,
        first_visit=since == 0,
        groups=groups,
        other=other,
    )


def render(packet: Packet, status: dict[str, Any], upstream_note: str = "", observed_new: int = 0) -> str:
    """The packet as text. Short by construction — it is read at the start of every session.

    `upstream_note` is passed in rather than fetched: this function runs from the SessionStart
    hook, and a network call there is latency on every session. `rein doctor` does the fetching
    and caches it; this only ever repeats what the cache already says (see `upstream`).
    """
    lines: list[str] = []
    if packet.first_visit:
        lines.append("## Since last time — first visit (no watermark yet)")
    elif packet.latest < packet.since:
        # The log is shorter than where this reader had got to. It only ever grows, so it was
        # restored or rewound — say so rather than printing a backwards range.
        lines.append(
            f"## Since last time — the log now ends at event {packet.latest}, below your mark "
            f"({packet.since}). It was restored or rewound; check `rein events --verify`."
        )
    elif packet.empty:
        lines.append(f"## Since last time — nothing moved (still at event {packet.latest})")
    else:
        lines.append(f"## Since last time — events {packet.since + 1}..{packet.latest}")
    for label, group in packet.groups.items():
        subjects = sorted({s for e in group for s in e.subject_ids})
        detail = f" ({', '.join(subjects[:6])}{'…' if len(subjects) > 6 else ''})" if subjects else ""
        lines.append(f"- {label}: {len(group)}{detail}")
    if packet.other:
        lines.append(f"- {packet.other} other event(s) — `rein events --since {packet.since}` for the log")
    # What is waiting comes from the status queue, not from the event delta above. An escalation
    # event is only one of the ways a repository stops moving: a review bound to a commit that is
    # no longer HEAD, an undispositioned plan-review finding, and a task nobody reclassified all
    # block a gate without ever writing an escalation event. Reporting only the headline groups
    # above would tell a returning reader "nothing is waiting" while the gate refuses to open.
    pending = status.get("pending")
    rows = [r for r in pending if r.get("severity") != "info"] if isinstance(pending, list) else []
    if rows:
        blocking = sum(1 for r in rows if r["severity"] == "blocking")
        lines.append("")
        lines.append(f"**{len(rows)} item(s) waiting on you" + (f", {blocking} blocking:**" if blocking else ":**"))
        for row in rows[:_RESUME_ROWS]:
            lines.append(f"- [{row['severity']}] {row['subject']}: {row['headline']}")
        if len(rows) > _RESUME_ROWS:
            lines.append(f"- … {len(rows) - _RESUME_ROWS} more — `rein start --full`")

    decision = status.get("decision") or {}
    lines.append("")
    if decision.get("waiting_on_human"):
        lines.append(f"**Waiting on you:** {decision.get('headline', '')}")
        lines.append(f"▸ {decision.get('action', '')}")
    else:
        lines.append(status_api.render_next(status.get("next") or {}))
    # The observation store's own delta. It is here, in a reading the person is already doing, and
    # nowhere else: the alternative was a trigger, and a trigger needs a level to fire at. A level
    # is the thing `observations` refuses to have, so the occasion has to be one that already
    # exists. It asks for no answer and is not on the notification channel — that channel carries
    # decisions waiting on a person, and these readings wait on nobody.
    if observed_new:
        lines.append("")
        lines.append(f"{observed_new} new observation(s) about this harness — `rein observe`")
    if not packet.first_visit and not packet.empty:
        lines.append("")
        lines.append(f"Full state: `rein start --full`  ·  this log: `rein events --since {packet.since}`")
    if upstream_note:
        lines.append("")
        lines.append(upstream_note)
    return "\n".join(lines)


def run(root: Path | None = None, *, full: bool = False, mark: bool = True) -> str:
    """Build and render the packet for `root`, advancing the watermark unless told not to."""
    repo = repo_mod.get(str(root) if root else None)
    events, defects = event_chain.scan(repo.events)
    packet = build(events, read_mark(repo.root))
    status = status_api.collect_status(repo)
    observed_total, observed_new = observation_delta(repo.root, repo.root.name)
    text = render(packet, status, _upstream_note(repo), observed_new)
    if defects:
        text = f"⚠ the audit chain has {len(defects)} defect(s) — `rein events --verify`\n\n{text}"
    if full:
        text = f"{text}\n\n{status_api.render(status)}"
    if mark and packet.latest:
        write_mark(repo.root, packet.latest)
    if mark:
        # Unconditionally, unlike the chain mark above: a total of zero is a real place to be, and
        # a store that was pruned back to zero must not keep re-reporting the readings it no longer
        # holds. This write lands in the reader's own state, never in the observation store —
        # reading the figures may not change them, or they would be an input to something.
        write_observed(repo.root, observed_total)
    return text


def _upstream_note(repo: repo_mod.Repo) -> str:
    """What `rein doctor`'s last release check saw, or "" — read from cache, never fetched here."""
    try:
        data = lock_mod.read(repo.lock)
    except lock_mod.LockError:
        data = None  # a lock this command cannot read is `doctor`'s finding, not a reason to fail
    return upstream.cached_note(rein.__version__, lock_mod.source_of(data) if data else "")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein start", description="what changed since you last looked")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    parser.add_argument("--full", action="store_true", help="also print the whole status board")
    parser.add_argument("--json", action="store_true", help="print the whole status object as JSON")
    parser.add_argument(
        "--no-mark",
        action="store_true",
        help="report the delta without advancing the watermark (a peek, not a visit)",
    )
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        if args.json:
            status = status_api.collect_status(repo_mod.get(args.repo))
            print(json.dumps(status, indent=2, ensure_ascii=False, default=str))
            return 0
        print(run(Path(args.repo) if args.repo else None, full=args.full, mark=not args.no_mark))
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    return 0


__all__ = [
    "Packet",
    "build",
    "main",
    "observation_delta",
    "observed_path",
    "read_mark",
    "read_observed",
    "registry",
    "render",
    "run",
    "watermark_path",
    "write_mark",
    "write_observed",
]
