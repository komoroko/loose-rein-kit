"""`rein cycle-close --name <slug>` — archive a finished delta cycle and reset for the next.

An ongoing repository runs Loose Rein as a series of delta cycles: each cycle's requirements,
design, tasks, and tests describe one change, not the whole product. Closing a cycle:

  1. Moves the filled deliverables to `docs/archive/<date>-<slug>/` (via `git mv`), **together
     with the cycle's `plan.yaml`, `state.yaml`, `review.yaml`, and `events.ndjson`** (plan
     §27). The four SSOT documents go with the docs because they *are* the record of what was
     decided and on what evidence — archiving the prose and dropping the evidence would leave a
     history of conclusions with no grounds.
  2. Restores fresh scaffolds from the snapshot taken at `init`, while the docs were pristine.
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

from rein import common, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

DOCS_DIR = "docs"
SCAFFOLD_DOCS = ".rein/scaffold/docs"
SCAFFOLD_REIN = ".rein/scaffold/rein"
ARCHIVE_DIR = "docs/archive"

#: Per-cycle deliverables under docs/. Everything else persists across cycles.
CYCLE_DOCS: tuple[str, ...] = (
    "10-requirements.md",
    "20-design.md",
    "decisions",
    "tasks",
    "test",
    "retrospective.md",
)

#: The cycle's machine record. Archived under `<archive>/rein/` (plan §27).
CYCLE_STATE: tuple[str, ...] = ("plan.yaml", "state.yaml", "review.yaml", "events.ndjson")


def snapshot_scaffold(repo: repo_mod.Repo) -> bool:
    """Copy the pristine docs and SSOT documents aside, once. True if anything was taken.

    Called by `init` while everything is still pristine. A no-op per target once its snapshot
    exists — re-running init after the docs are filled must never overwrite the pristine copy.
    """
    took = False
    docs_dst = repo.path(SCAFFOLD_DOCS)
    docs_src = repo.path(DOCS_DIR)
    if not docs_dst.exists() and docs_src.is_dir():
        docs_dst.mkdir(parents=True)
        for item in sorted(docs_src.iterdir()):
            if item.name == "archive":
                continue
            (shutil.copytree if item.is_dir() else shutil.copy2)(item, docs_dst / item.name)
        took = True

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

    if state.gate_status("release") != "approved":
        blockers.append("the release gate (5) is not approved — a cycle closes on a signed release decision")
    events, defects = event_chain.scan(repo.events)
    if defects:
        blockers.append(f"the audit chain has {len(defects)} defect(s); the archive would record an unreadable log")

    for gate in models.GATE_ORDER:
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
    """Recreate fresh scaffolds from the snapshot, never overwriting an existing file."""
    restored: list[str] = []
    for name in CYCLE_DOCS:
        src = repo.path(SCAFFOLD_DOCS) / name
        dst = repo.path(DOCS_DIR) / name
        if not src.exists() or dst.exists():
            continue
        (shutil.copytree if src.is_dir() else shutil.copy2)(src, dst)
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
        "current_phase": "brief",
        "updated_at": event_chain.now_iso(),
        "gates": {gate: {"status": "pending", "receipt": None} for gate in models.GATE_ORDER},
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
