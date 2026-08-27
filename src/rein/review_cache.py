"""Per-stage reuse for the gate-④ pipeline: what a stage answered, keyed by what it was asked.

The pipeline used to reuse at the wrong granularity. One `subject` digest — the tree, the plan,
the config, the sandbox, the coverage manifest and the task facts, all in one key — decided
whether *all three* reviewer stages ran. Each stage is a function of a different subset of that
key, so editing `plan.yaml` re-read the code with the blind extractor and re-ran the security
review, neither of which has ever seen a plan; and promoting a task to `done` after a human
recorded evidence re-ran all three, when the only thing that reads task status is the orientation
brief, which no model produces.

The unit of reuse here is **one stage's answer**, keyed by **that stage's own inputs**, written
out field by field so the key says what the stage is a function of.

Two properties fall out of caching the adapter's *response* rather than a parsed result:

- **A stage that succeeded is not paid for twice.** The pipeline writes nothing when a later
  stage fails, so an extraction measured at over six minutes was discarded because the comparator
  came back malformed. An entry is written the moment its own stage validates, so the next run
  resumes rather than restarts.
- **A cache hit is validated exactly like a fresh answer.** The stored bytes go back through the
  stage's own `run_*`, so anchors are re-checked against the commit and the never-lists still
  apply. Nothing enters a review because it was on disk.

Entries live under `.rein/work/`, which is gitignored, dies with a worktree, and is where the
dossier already goes. There is no expiry and no configuration: a completed generation deletes
every entry it did not use, so the directory holds this review's three answers and, after a
failure, whatever a resume still needs.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rein import digests, review_policy

#: Where a stage answer is kept, relative to the repository root.
CACHE_DIR = ".rein/work/review-cache"


def stage_key(stage: str, inputs: Mapping[str, Any]) -> str:
    """The digest of everything `stage` is a function of, with the stage's own name in it.

    The name is in the key so two stages that happen to be asked about the same bytes cannot share
    an answer — the extractor and the security reviewer read the same diff, and they are not
    interchangeable.

    **The stage contract is deliberately not an input.** It travels in the request, so a stage
    really is a function of it, and an installed rein that reworded one would read the same code
    differently. Keying on it would mean `rein upgrade` re-runs every open review — and a
    regeneration that moves the machine half resets the human one, discarding a reviewer's answers
    about code nobody had touched. That is a worse failure than reusing a reading taken under last
    release's wording, and `--force` is how to re-read after an upgrade on purpose.
    """
    return digests.of({"stage": stage, **dict(inputs)})


class StageCache:
    """The stage answers for one repository, and the record of which ones this run used.

    Shared by the two threads `review.generate` runs — the extraction chain and the security
    review — so the used-set is guarded. The entry files are per-stage and never contend.
    """

    def __init__(self, root: Path, *, enabled: bool = True) -> None:
        self.dir = root / CACHE_DIR
        #: `--force` says "read it again anyway": nothing is read back, but what runs is still
        #: stored, so the run after a forced one reuses it.
        self.enabled = enabled
        self._used: set[Path] = set()
        self._lock = threading.Lock()

    def _path(self, stage: str, key: str) -> Path:
        return self.dir / f"{stage}-{key.removeprefix('sha256:')}.json"

    def read(self, stage: str, key: str) -> str | None:
        """The stored answer to this exact question, or None when there is not one."""
        if not self.enabled:
            return None
        path = self._path(stage, key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            # An unreadable entry is a cache that cannot answer, which is what a miss is. The write
            # that follows replaces it, so nothing accumulates.
            return None
        answer = entry.get("answer")
        if not isinstance(answer, str):
            return None
        with self._lock:
            self._used.add(path)
        return answer

    def write(self, stage: str, key: str, answer: str) -> None:
        """Store one stage's answer. Called only once that stage has validated it."""
        path = self._path(stage, key)
        with self._lock:
            self._used.add(path)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"stage": stage, "answer": answer}), encoding="utf-8")
        except OSError:
            # A cache that cannot be written costs a re-run, and a re-run is the behaviour this
            # module improves on rather than one it depends on. A review must not fail over a
            # scratch directory.
            pass

    def drop(self, stage: str, key: str) -> None:
        """Forget one entry — for a stored answer that no longer validates.

        Without this, one bad answer would wedge the review: the same bytes would come back on
        every run and be refused the same way, and only `--force` would clear it.
        """
        path = self._path(stage, key)
        with self._lock:
            self._used.discard(path)
        path.unlink(missing_ok=True)

    def prune(self) -> None:
        """Delete every entry this run did not use. Called once, after a generation succeeds.

        Not on the failure path, on purpose: the entries a failed run left behind are exactly what
        the next one resumes from.
        """
        if not self.dir.is_dir():
            return
        with self._lock:
            used = set(self._used)
        for path in self.dir.glob("*.json"):
            if path not in used:
                path.unlink(missing_ok=True)


class Recorder:
    """A reviewer that keeps the last answer it passed through, so the caller can store it.

    A stage takes a `Reviewer` and hands back a parsed, validated result; the raw answer is what
    the cache holds, and it is not otherwise reachable from outside the call.
    """

    def __init__(self, reviewer: review_policy.Reviewer) -> None:
        self._reviewer = reviewer
        self.answer: str | None = None

    def __call__(self, request: Mapping[str, Any]) -> str:
        self.answer = self._reviewer(request)
        return self.answer


def replay(answer: str) -> review_policy.Reviewer:
    """A reviewer that returns `answer` without launching anything."""

    def call(request: Mapping[str, Any]) -> str:  # noqa: ARG001 — the answer is already in hand
        return answer

    return call
