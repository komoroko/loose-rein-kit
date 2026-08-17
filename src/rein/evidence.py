"""The evidence ledger: what was mechanically established, and against exactly which content.

The build loop used to re-establish the same facts over and over — `test` re-ran from the top on
every retry even when only `check` had gone red, the integration gate re-ran the whole DoD on a
tree a leaf had already verified, and the same security finding was carried forward by *id* with
nothing recording which base it was found against. Those look like three problems. They are one:
**nothing recorded that a fact had already been established, or against what.**

The ledger is that record, and it is deliberately split in two halves with different lifetimes:

  **The auditable half** lives in ``state.yaml`` as ``tasks.<id>.evidence`` — the content
  fingerprint a task's ``done`` was decided on, and which gate steps were green against it. It is
  written inside the same Store transaction that marks the task done, so the audit chain already
  explains it. This is what makes ``done`` mean "the evidence was there", not "the agent exited 0".

  **The reuse half** is this module: a content-addressed cache outside the working tree
  (``$XDG_CACHE_HOME/rein/<repo_id>/evidence.jsonl``). It never decides anything. A hit skips a
  re-run; a miss costs one. Losing the whole file costs time and nothing else, which is why it is
  not an SSOT document and why it appends no audit events — an event per `make test` would flood a
  log that deliberately never rotates.

Three rules keep the cache from ever being able to lie:

  1. **Content, not names.** The subject is a content fingerprint (:meth:`GitWorkspace.fingerprint`).
     A tree that moved by a byte gets a different key, so a stale result is unreachable rather than
     wrong. This is also what closes the review-base carryover: a judgement recorded against base A
     simply has no key that base B can look up.
  2. **Successes only.** A red step is never recorded. Re-running a failure costs one run; skipping
     a re-run because a *previous* environment failed would be a machine's bad afternoon turned into
     a verdict.
  3. **An unknown subject never matches.** A fingerprint the git layer could not compute comes back
     as ``""``, and both :meth:`Ledger.hit` and :meth:`Ledger.record` refuse it outright. Fail
     closed: run the step.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rein import digests, event_chain
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: The cache file inside the repository's cache directory.
FILENAME = "evidence.jsonl"

#: How many facts to keep. Old entries are harmless (an unmatched key is simply never read), so
#: the cap is about file size, not correctness — the newest entries are the ones a live run asks for.
MAX_ENTRIES = 2048

#: Fact kinds. `gate_step` is one quality-gate command against one tree; `acceptance` is one
#: task acceptance criterion's evidence command (phase 3).
KIND_GATE_STEP = "gate_step"
KIND_ACCEPTANCE = "acceptance"


def fact_key(kind: str, subject: str, tool: Sequence[str]) -> str:
    """The content address of one fact: what kind, about which content, established with what.

    Everything that could make the result differ has to be in here. `tool` carries the step's
    name *and* its argv *and* the pinned image digest it ran in, because "make test was green"
    is not a fact about the code alone — a different image is a different claim.
    """
    return digests.of({"kind": kind, "subject": subject, "tool": list(tool)})


@dataclass(frozen=True)
class Fact:
    """One established fact, as it sits in the cache file."""

    key: str
    kind: str
    subject: str
    tool: tuple[str, ...]
    at: str

    def to_mapping(self) -> dict[str, object]:
        return {"key": self.key, "kind": self.kind, "subject": self.subject, "tool": list(self.tool), "at": self.at}


@dataclass
class Ledger:
    """The reuse half. Load once per run, ask before running, record after a green.

    `enabled=False` turns every lookup into a miss and every record into a no-op, which is what
    a dry run and `--no-cache` both want: the control flow stays identical and nothing is reused.
    """

    path: Path | None
    enabled: bool = True
    _facts: dict[str, Fact] = field(default_factory=dict, repr=False)
    _dirty: bool = False
    hits: int = 0
    misses: int = 0

    @classmethod
    def for_repo(cls, repo: repo_mod.Repo, *, enabled: bool = True) -> Ledger:
        """The ledger for one repository, loaded from its cache directory.

        A cache directory that cannot be read or is owned by somebody else is not an error worth
        stopping a build for — it disables reuse and says so once.
        """
        if not enabled:
            return cls(path=None, enabled=False)
        try:
            directory = store_mod.ensure_private_dir(store_mod.cache_dir(repo))
        except (store_mod.StoreError, OSError) as exc:
            logger.debug(f"evidence cache unavailable ({exc}); every step will run")
            return cls(path=None, enabled=False)
        ledger = cls(path=directory / FILENAME)
        ledger._load()
        return ledger

    # -- lookup and record ----------------------------------------------------

    def hit(self, kind: str, subject: str, tool: Sequence[str]) -> bool:
        """Has this exact fact already been established? An unknown subject is always a miss."""
        if not self.enabled or not subject:
            return False
        found = fact_key(kind, subject, tool) in self._facts
        if found:
            self.hits += 1
        else:
            self.misses += 1
        return found

    def record(self, kind: str, subject: str, tool: Sequence[str]) -> None:
        """Record a fact that was just established green. Failures are deliberately never recorded."""
        if not self.enabled or not subject:
            return
        key = fact_key(kind, subject, tool)
        if key in self._facts:
            return
        self._facts[key] = Fact(key=key, kind=kind, subject=subject, tool=tuple(tool), at=event_chain.now_iso())
        self._dirty = True

    # -- persistence ----------------------------------------------------------

    def _load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            logger.debug(f"evidence cache unreadable ({exc}); every step will run")
            self.enabled = False
            return
        for line in text.splitlines():
            fact = _parse(line)
            if fact is not None:
                self._facts[fact.key] = fact

    def flush(self) -> None:
        """Write the ledger back, newest-last and capped. Any failure is silent by design.

        A cache that cannot be written must not be able to fail a build: the next run simply
        re-establishes what it needs.
        """
        if self.path is None or not self._dirty:
            return
        kept = list(self._facts.values())[-MAX_ENTRIES:]
        body = "".join(json.dumps(f.to_mapping(), sort_keys=True, separators=(",", ":")) + "\n" for f in kept)
        try:
            store_mod.atomic_write(self.path, body.encode("utf-8"), mode=0o600)
            self._dirty = False
        except (store_mod.StoreError, OSError) as exc:  # pragma: no cover - filesystem edge
            logger.debug(f"could not write the evidence cache ({exc})")

    def summary(self) -> str:
        """One line for the run's console output. Empty when the ledger is off."""
        if not self.enabled:
            return ""
        return f"evidence: {self.hits} reused, {self.misses} run"


def _parse(line: str) -> Fact | None:
    """One cache line, or None when it is damaged. A damaged line is dropped, never fatal."""
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    key, kind, subject = raw.get("key"), raw.get("kind"), raw.get("subject")
    tool, at = raw.get("tool"), raw.get("at")
    if not isinstance(key, str) or not isinstance(kind, str) or not isinstance(subject, str):
        return None
    if not isinstance(tool, list) or not all(isinstance(part, str) for part in tool):
        return None
    return Fact(key=key, kind=kind, subject=subject, tool=tuple(tool), at=at if isinstance(at, str) else "")


def cache_enabled_by_env() -> bool:
    """False when `REIN_NO_CACHE` is set to anything non-empty.

    An escape hatch for "I do not believe the cache" that needs no config change and cannot be
    set by a task's own ticket — it is an operator's environment, not a knob in a frozen document.
    """
    return not os.environ.get("REIN_NO_CACHE", "")
