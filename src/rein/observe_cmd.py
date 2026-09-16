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

from rein import common, observations
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

    project = args.project
    if project is None and args.repo is not None:
        try:
            project = repo_mod.get(args.repo).root.name
        except repo_mod.RepoNotFoundError as exc:
            logger.error(str(exc))
            return 1

    entries = observations.read()
    summary = observations.summarize(entries, project=project or "")
    scope = f"project {project}" if project else f"{len({e.project for e in entries})} project(s)"
    print(f"{len(entries)} observation(s), {scope} — {observations.store_path()}\n")
    print(observations.render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
