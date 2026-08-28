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

An entry records **who answered as well as what they answered**. It used to hold the stage name
and the bytes and nothing else, which conflated four different identities — the question (this
stage's key), the answer, the launch that produced it, and the human judgement built on top. The
cost of that conflation was not bookkeeping: `review._independence_record` reads the model id off
the launch's own usage report, so a replayed stage contributed none, `binding.independence` lost
its `model`, and `review_policy.independence_observed` — the gate-④ check that a provider did not
silently serve one model to both halves of a critical review — went quiet. A cache that disables a
safety check when it hits is not a cache, it is a hole. So the launch's `usage.Usage` travels with
the answer and is replayed with it, into a ledger kept separate from what this run actually paid.

Two more properties fall out of caching the adapter's *response* rather than a parsed result:

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rein import digests, review_policy
from rein import usage as usage_mod

#: Where a stage answer is kept, relative to the repository root.
CACHE_DIR = ".rein/work/review-cache"


@dataclass(frozen=True)
class Entry:
    """One stored stage answer and the launch that produced it."""

    answer: str
    usage: usage_mod.Usage = field(default_factory=usage_mod.Usage)


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

    def has(self, stage: str, key: str) -> bool:
        """Is this exact question already answered? Asked *without* claiming the entry.

        `read` records what this run used, which is what `prune` keeps. This is for deciding
        something before the stages run — whether one shared reading is worth priming — and a
        decision must not make the thing it asked about look used.
        """
        return self.enabled and self._path(stage, key).is_file()

    def read(self, stage: str, key: str) -> Entry | None:
        """The stored answer to this exact question and what produced it, or None for a miss.

        An entry written before executions were recorded has no `execution` and is treated as a
        miss: replaying it would put back exactly the provenance hole this records, and there is
        nothing to migrate — `.rein/work/` is gitignored and dies with its worktree.
        """
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
        answer, execution = entry.get("answer"), entry.get("execution")
        if not isinstance(answer, str) or not isinstance(execution, dict):
            return None
        with self._lock:
            self._used.add(path)
        return Entry(answer=answer, usage=usage_mod.Usage.from_detail(execution))

    def write(self, stage: str, key: str, answer: str, spent: usage_mod.Usage) -> None:
        """Store one stage's answer beside the launch that gave it. Called once the stage validates."""
        path = self._path(stage, key)
        with self._lock:
            self._used.add(path)
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            entry = {"stage": stage, "key": key, "answer": answer, "execution": spent.to_detail()}
            path.write_text(json.dumps(entry), encoding="utf-8")
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
    """A reviewer that keeps the last `Answer` it passed through, so the caller can store it.

    A stage takes a `Reviewer` and hands back a parsed, validated result; the raw answer — and the
    launch that produced it — is what the cache holds, and neither is otherwise reachable from
    outside the call.
    """

    def __init__(self, reviewer: review_policy.Reviewer) -> None:
        self._reviewer = reviewer
        self.reply: review_policy.Answer | None = None

    def __call__(self, request: Mapping[str, Any]) -> review_policy.Answer:
        self.reply = self._reviewer(request)
        return self.reply


def replay(answer: str) -> review_policy.Reviewer:
    """A reviewer that returns `answer` without launching anything, so it cost nothing."""

    def call(request: Mapping[str, Any]) -> review_policy.Answer:  # noqa: ARG001 — the answer is in hand
        return review_policy.Answer(answer)

    return call
