"""`rein cycle-close --name <slug>` — archive a finished delta cycle and reset for the next.

An ongoing repository runs Loose Rein as a series of delta cycles: each cycle's requirements,
design, tasks, and tests describe one change, not the whole product. Closing a cycle:

  1. Moves the filled deliverables to `docs/archive/<date>-<slug>/` (via `git mv`), **together
     with the cycle's `plan.yaml`, `state.yaml`, `review.yaml`, and `events.ndjson`** (plan
     §27). The four SSOT documents go with the docs because they *are* the record of what was
     decided and on what evidence — archiving the prose and dropping the evidence would leave a
     history of conclusions with no grounds.
  2. Restores fresh scaffolds: the per-cycle documents from the payload this release ships, the
     SSOT documents from the snapshot `init` took while they were pristine.
  3. Resets state to a new cycle: every gate pending, phase back to `brief`, a fresh chain.

`00-product-brief.md` and `05-current-state.md` persist — they are the product, not the cycle.

Closing is a human decision, like opening a gate; the agent never runs this on its own. It
refuses to close a cycle whose release gate is not approved, whose audit chain is damaged, or
whose approved gates carry no receipt: an archive is a record, and a record assembled from an
inconsistent state is worse than none.
"""

from __future__ import annotations

import argparse
import logging
import shutil
from datetime import date

from rein import common, data, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

DOCS_DIR = "docs"
#: Where the packaged per-cycle documents live inside the payload. `init` seeds `docs/` from here
#: and `cycle-close` restores from here, so "what a fresh cycle's documents are" has one answer and
#: it is the running release's.
SCAFFOLD_DOCS_PAYLOAD = "scaffold/docs"
SCAFFOLD_REIN = ".rein/scaffold/rein"
ARCHIVE_DIR = "docs/archive"

#: Per-cycle deliverables under docs/: archived with the cycle, then restored from the payload for
#: the next one. Everything in :data:`PERSISTENT_DOCS` is the other answer.
#:
#: Every scaffold document is one or the other, and `scripts/template_lint.py` holds the two lists
#: against `src/rein/data/scaffold/docs/` so that shipping a scaffold file without classifying it
#: fails there. Unclassified is not a third policy: a per-cycle log left out of this list is never
#: archived and never reset, so the next cycle opens holding the last one's rows while `/status`
#: goes on naming them as still undecided. `speculative-work.md` is a per-cycle log, so it is here.
CYCLE_DOCS: tuple[str, ...] = (
    "10-requirements.md",
    "20-design.md",
    "decisions",
    "tasks",
    "test",
    "retrospective.md",
    "speculative-work.md",
)

#: Scaffold documents that are **not** per-cycle: the product's identity and the brownfield
#: intake, which describe the repository rather than the change being made in it.
PERSISTENT_DOCS: tuple[str, ...] = (
    "00-product-brief.md",
    "05-current-state.md",
)

#: The cycle's machine record. Archived under `<archive>/rein/` (plan §27).
CYCLE_STATE: tuple[str, ...] = ("plan.yaml", "state.yaml", "review.yaml", "events.ndjson")


def snapshot_ssot(repo: repo_mod.Repo) -> bool:
    """Copy the pristine SSOT documents aside, once. True if anything was taken.

    **Only the SSOT.** `docs/` used to be snapshotted here too, and that copy was the bug: it was
    taken once, at `init`, and nothing ever added to it — so a per-cycle document a later release
    began shipping was archived by `cycle-close` and then not restored, because the snapshot taken
    by an older release had no copy of it to restore from. Silently, since :func:`_restore` skips
    what is absent. The per-cycle documents are packaged data, byte-identical to what `init` seeds,
    so a per-repository copy of them was a duplicate that could only ever go stale; they are
    restored from the payload now (:data:`SCAFFOLD_DOCS_PAYLOAD`), which is the same thing
    `rein sync` does for the prompts, the schemas and the rules.

    `plan.yaml` and `review.yaml` stay here because they are *not* the payload: `init` fills the
    plan's cycle id and work branch, so this repository's pristine copy is the only one there is.

    A no-op per target once its snapshot exists — re-running init after the documents are filled
    must never overwrite the pristine copy.
    """
    took = False
    state_dst = repo.path(SCAFFOLD_REIN)
    state_dst.mkdir(parents=True, exist_ok=True)
    for name in ("plan.yaml", "state.yaml", "review.yaml"):
        src = repo.rein_dir / name
        dst = state_dst / name
        if src.is_file() and not dst.exists():
            shutil.copy2(src, dst)
            took = True
    return took


def readiness(repo: repo_mod.Repo) -> list[str]:
    """Every reason this cycle may not be closed yet (plan §27's final check)."""
    store = store_mod.Store(repo)
    blockers: list[str] = []
    try:
        state = store.read_state()
    except models.DocumentError as exc:
        return [str(exc)]
    if state is None:
        return ["no .rein/state.yaml — there is no cycle to close"]

    if state.gate_status("acceptance") != "approved":
        blockers.append("the acceptance gate is not approved — a cycle closes on a signed decision to take the change")
    events, defects = event_chain.scan(repo.events)
    if defects:
        blockers.append(f"the audit chain has {len(defects)} defect(s); the archive would record an unreadable log")

    for gate in state.gate_ids:
        receipt = state.gate_receipt(gate)
        if state.gate_status(gate) != "approved" or receipt is None:
            continue
        if not receipt.get("approval_id"):
            blockers.append(
                f"gate '{gate}' is approved with no approval id — the archive would record an approval "
                "nobody can trace back to a confirmation"
            )
    return blockers


def plan_close(repo: repo_mod.Repo, slug: str, today: str) -> list[tuple[str, str, str]]:
    """The deterministic archive plan: (action, source, destination) rows.

    `action` is "archive" for something present and "skip" for something already gone, which
    is what makes a re-run idempotent.
    """
    base = f"{ARCHIVE_DIR}/{today}-{slug}"
    rows: list[tuple[str, str, str]] = []
    for name in CYCLE_DOCS:
        src = f"{DOCS_DIR}/{name}"
        rows.append(("archive" if repo.path(src).exists() else "skip", src, f"{base}/{name}"))
    for name in CYCLE_STATE:
        src = f".rein/{name}"
        rows.append(("archive" if repo.path(src).exists() else "skip", src, f"{base}/rein/{name}"))
    return rows


def _archive(repo: repo_mod.Repo, rows: list[tuple[str, str, str]]) -> list[str]:
    """Execute the plan with `git mv`, falling back to a plain move for untracked files."""
    moved: list[str] = []
    for action, src, dst in rows:
        if action != "archive":
            continue
        repo.path(dst).parent.mkdir(parents=True, exist_ok=True)
        rc, _ = common.run(["git", "mv", src, dst], cwd=str(repo.root))
        if rc != 0:
            shutil.move(str(repo.path(src)), str(repo.path(dst)))
        moved.append(src)
    return moved


def _restore(repo: repo_mod.Repo) -> list[str]:
    """Recreate fresh scaffolds for the next cycle, never overwriting an existing file.

    The per-cycle documents come from the payload, file by file, so every one this release ships
    is restored whether or not the repository existed when it was added — the drift that made a
    document vanish at the first close after an upgrade. `CYCLE_DOCS` names directories as well as
    files (`tasks`, `test`, `decisions`), so membership is tested on the first path segment and the
    tree below it is written out entry by entry.

    A file already on disk is left alone: the archive is a `git mv`, so anything still there is
    something the move could not take, and overwriting it would destroy work.
    """
    restored: list[str] = []
    cycle_docs = set(CYCLE_DOCS)
    prefix = len(SCAFFOLD_DOCS_PAYLOAD) + 1
    for rel, blob in data.iter_files(SCAFFOLD_DOCS_PAYLOAD):
        doc_rel = rel[prefix:]
        if doc_rel.split("/")[0] not in cycle_docs:
            continue
        dst = repo.path(DOCS_DIR) / doc_rel
        if dst.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(blob)
        restored.append(str(dst.relative_to(repo.root)))
    for name in ("plan.yaml", "review.yaml"):
        src = repo.path(SCAFFOLD_REIN) / name
        dst = repo.rein_dir / name
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)
            restored.append(str(dst.relative_to(repo.root)))
    return restored


def next_state(previous: models.State, slug: str) -> dict[str, object]:
    """A fresh state document for the next cycle, carrying only the project identity forward.

    Nothing else survives: a gate status, a receipt, or a task status carried into a new cycle
    would be an approval for work that has not happened.
    """
    return {
        "project": previous.project,
        "cycle_id": slug,
        "updated_at": event_chain.now_iso(),
        # The two ends only. What this cycle will build is not known yet, so neither is how many
        # irreversible points it has — `approve mandate` adds those when it freezes the plan.
        "gates": {gate: {"status": "pending", "receipt": None} for gate in models.GATE_ENDS},
        "plan": {"status": "draft"},
        "execution": {"status": "idle"},
        "tasks": {},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="archive the finished delta cycle and reset for the next")
    parser.add_argument("--name", required=True, help="a slug for the archive directory and the next cycle id")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; write nothing")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    slug = args.name.strip().lower()
    # The schema's rule, not an approximation of it: `replace("-", "").isalnum()` accepted a
    # leading dash and every Unicode letter `str.isalnum` counts, so `--name -foo` and `--name яя`
    # both got as far as writing a cycle_id that state.yaml and the audit log then reject.
    if not models.CYCLE_ID_RE.match(slug):
        logger.error(f"--name {args.name!r} must match {models.CYCLE_ID_RE.pattern} (lowercase, digits, dashes)")
        return 2

    today = date.today().isoformat()
    rows = plan_close(repo, slug, today)
    print(f"Archive plan for cycle '{slug}' → {ARCHIVE_DIR}/{today}-{slug}/")
    for action, src, dst in rows:
        print(f"  {action:8} {src}" + (f" → {dst}" if action == "archive" else ""))

    blockers = readiness(repo)
    if blockers:
        logger.error("cannot close this cycle:\n" + "\n".join(f"  - {b}" for b in blockers))
        return 1
    if args.dry_run:
        print("\n(dry run — nothing was written)")
        return 0

    store = store_mod.Store(repo)
    previous = store.read_state()
    if previous is None:
        logger.error("no .rein/state.yaml")
        return 1

    # The archive is assembled and recorded BEFORE the reset, so the closing event is the last
    # entry of the chain being archived rather than the first of a chain that has no history.
    with store.transaction() as tx:
        tx.append(
            "cycle_closed",
            cycle_id=previous.cycle_id,
            subject_ids=[slug],
            detail={"archive": f"{ARCHIVE_DIR}/{today}-{slug}", "chain_root": store.chain_root()},
        )

    moved = _archive(repo, rows)
    restored = _restore(repo)

    # Through the Central Store, not `atomic_write`. state.yaml is the document `gate_guard` rule 1
    # and AGENTS.md both describe as written only inside a transaction, and this — the reset that
    # opens a cycle — was the one place writing it raw. Two things followed: the schema never saw
    # the document, so a cycle id it rejects (`--name ①` was accepted upstream) reached disk; and
    # the reset and the event recording it were two separate steps, so a crash between them left a
    # fresh state.yaml with nothing in the log to say a cycle had been opened.
    with store.transaction() as tx:
        tx.write("state", next_state(previous, slug))
        tx.append(
            "cycle_initialized",
            cycle_id=slug,
            detail={"previous_cycle": previous.cycle_id, "archived_from": f"{today}-{slug}"},
        )

    print(f"\narchived {len(moved)} item(s), restored {len(restored)} scaffold(s)")
    print(f"cycle '{slug}' is open at phase 'brief'. Commit the archive, then write the next brief.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
