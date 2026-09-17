"""`rein observe` — the figures this harness keeps about itself, and the claim each one tests.

Every rule here was argued for. `rein lens --stats` answers one of those arguments; this answers
the rest, and it prints the claim beside the number because a figure with no claim attached is one
somebody reads as a score.

There are no thresholds and none are coming. A number with a ceiling on it gets managed instead of
read — which is the failure the acceptance budget demonstrated in this repository's own source
before it was removed: the ceiling's own instruction was impossible to follow, so the ceiling moved
twice and the thing it measured never did.
"""

from __future__ import annotations

import argparse
import logging

from rein import common, event_chain, observations
from rein import events as events_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="what is measured about this harness, and what each figure tests")
    parser.add_argument("--project", default=None, help="one project by name (default: every project)")
    parser.add_argument("--prune", type=int, metavar="KEEP", help="drop all but the most recent KEEP entries")
    parser.add_argument("--repo", default=None, help="repository root, used to default --project")
    args = parser.parse_args(argv)
    common.configure_logging()

    if args.prune is not None:
        dropped = observations.prune(args.prune)
        print(f"{dropped} observation(s) dropped; the most recent {args.prune} are kept")
        return 0

    # The chain is per-repository and this store is user-global, so the chained stop count is only
    # offered when a repository is in hand. `--project` alone names a project in the store, which
    # is not a path to a chain and never a reason to guess at one.
    repo = None
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        if args.repo is not None:
            logger.error(str(exc))
            return 1
        logger.debug(f"no repository here, so no chained stop count: {exc}")

    project = args.project
    if project is None and repo is not None:
        project = repo.root.name

    entries = observations.read()
    summary = observations.summarize(entries, project=project or "")
    scope = f"project {project}" if project else f"{len({e.project for e in entries})} project(s)"
    print(f"{len(entries)} observation(s), {scope} — {observations.store_path()}\n")
    print(observations.render(summary, chain_stops=_chain_stops(repo)))
    return 0


def _chain_stops(repo: repo_mod.Repo | None) -> int | None:
    """How many stops this repository's chain records, archives included — or None when there is
    no repository to ask, or its log cannot be read.

    Across archives for the same reason `rein lens --stats` is: `cycle-close` moves the chain that
    answers this into `docs/archive/`, and reading only the live one makes the figure go blank at
    the moment a second cycle makes it worth reading. An archive that does not verify is named and
    left out rather than folded in — a count assembled from a log that failed its own check would
    be the one thing this figure must not be.
    """
    if repo is None:
        return None
    live, defects = event_chain.scan(repo.events)
    if defects:
        logger.warning(f"{repo.events} has {len(defects)} chain defect(s); no chained stop count")
        return None
    sources, unreadable = events_mod.cost_sources(repo, live)
    for rel in unreadable:
        logger.warning(f"{rel} could not be verified, so its stops are not counted")
    return sum(events_mod.stops(chain) for _, chain in sources)


if __name__ == "__main__":
    raise SystemExit(main())
