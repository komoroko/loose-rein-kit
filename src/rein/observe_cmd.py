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

    # The chain is per-repository and this store is user-global, so the chained figures are only
    # offered when the repository in hand is the one the summary is about. `--project` names a
    # project in the store, which is not a path to a chain: asked for another project's readings
    # from inside this repository, the table would have put this repository's stop count beside
    # them under a heading that says "this repo", and the two scopes would have read as one.
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
    if repo is not None and project != repo.root.name:
        logger.info(
            f"showing project {project!r}, and this repository is {repo.root.name!r} — the chained "
            "stop figures are that repository's audit chain, so they are left out rather than "
            "printed beside readings they are not about."
        )
        repo = None

    entries = observations.read()
    summary = observations.summarize(entries, project=project or "")
    scope = f"project {project}" if project else f"{len({e.project for e in entries})} project(s)"
    stops, stopped, causes = _chain_cost(repo)
    print(f"{len(entries)} observation(s), {scope} — {observations.store_path()}\n")
    print(observations.render(summary, chain_stops=stops, chain_stopped=stopped, chain_causes=causes))
    return 0


def _chain_cost(repo: repo_mod.Repo | None) -> tuple[int | None, list[float], dict[str, int]]:
    """What this repository's chain says a cycle cost: how many times it stopped, for how long
    each time, and why each stop reached a person. `(None, [], {})` when there is no repository to
    ask, or its log cannot be read.

    One pass for all three. They come from the same scan of the same chains, and splitting them into
    two functions would walk every archive twice and report each unreadable one twice to the
    person reading a single table.

    Across archives for the same reason `rein lens --stats` is: `cycle-close` moves the chain that
    answers this into `docs/archive/`, and reading only the live one makes the figure go blank at
    the moment a second cycle makes it worth reading. An archive that does not verify is named and
    left out rather than folded in — a figure assembled from a log that failed its own check would
    be the one thing these must not be.
    """
    if repo is None:
        return None, [], {}
    live, defects = event_chain.scan(repo.events)
    if defects:
        logger.warning(f"{repo.events} has {len(defects)} chain defect(s); no chained stop figures")
        return None, [], {}
    sources, unreadable = events_mod.cycle_sources(repo, live)
    for rel in unreadable:
        logger.warning(f"{rel} could not be verified, so its stops are not counted")
    stops = sum(events_mod.stops(source.events) for source in sources)
    stopped = [d for source in sources for d in events_mod.stop_durations(source.events)]
    causes: dict[str, int] = {}
    for source in sources:
        for cause, count in events_mod.stop_causes(source.events).items():
            causes[cause] = causes.get(cause, 0) + count
    return stops, stopped, causes


if __name__ == "__main__":
    raise SystemExit(main())
