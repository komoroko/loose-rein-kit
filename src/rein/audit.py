"""`rein audit run` — the one security answer gate ⑤ cannot carry from gate ④.

Everything else the release gate needs about security it already has: the structured review is
bound to the reviewed HEAD, its blocking findings hold this gate shut too, and a later commit
leaves it stale. Re-reading the code at gate ⑤ would ask the same reviewer the same question about
the same commit.

A dependency audit is the exception, and `verify.md` has said so since it existed: *the same commit
audited last month and today can differ, the vulnerability database having moved while the code did
not.* It is the one answer that is not a function of the tree.

It was also, until this module, the one answer nothing produced. The instruction lived in a prompt
— run `make audit`, record the date and the commit — and no code ran it, no document held it, and
no readiness check asked for it. A promise a phase command makes and nothing keeps is worse than no
promise: `/verify` could be completed, gate ⑤ approved, and the release shipped with the audit
having been "done" in a chat window.

**Its findings are not this loop's to repair, and that is the routing rule rather than a shortcut.**
A dependency bump changes the closure the gate-③ pinned sandbox image was built from — so it is a
change to the frozen environment, which `repair.route` classifies as a *plan* change and puts in a
human's hands. What this does is make the answer exist, bind it, and expire it.
"""

from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from rein import common, digests, executors, models
from rein import diff_facts as diff_facts_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: How long a recorded audit speaks for when the config does not say. A week, because the thing
#: that moves is a published database rather than anything in the repository — short enough that a
#: release is never signed on a month-old reading, long enough not to be a daily chore.
DEFAULT_MAX_AGE_DAYS = 7


class AuditError(common.ReinError):
    """The audit could not be run, or is not configured."""


def dependency_digest(repo: repo_mod.Repo, commit: str = "HEAD") -> str:
    """A digest over this tree's dependency manifests and lockfiles. "" when git cannot answer.

    What an audit is actually about. Binding it to the commit would retire the answer on every
    source edit, which is a re-run bought for nothing; binding it to the whole product digest does
    the same. The set is `diff_facts`' own — the files that already make a change carry a
    `dependency` signal — so what counts as a dependency is decided in one place.
    """
    rc, out = repo._git_rc("ls-tree", "-r", "-z", commit)
    if rc != 0:
        return ""
    entries = [e for e in digests.parse_ls_tree(out) if diff_facts_mod._DEPENDENCY_FILES.search(e.path)]
    return digests.tree_digest(entries)


def configured(config: models.Config | None) -> dict[str, Any]:
    """The `security.dependency_audit` block, or {} when this project declares none."""
    if config is None:
        return {}
    section = config.raw.get("security")
    block = section.get("dependency_audit") if isinstance(section, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def max_age_days(config: models.Config | None) -> int:
    return common.as_int(configured(config).get("max_age_days"), DEFAULT_MAX_AGE_DAYS)


def staleness(record: object, *, dependencies: str, now: datetime, max_age: int) -> str:
    """Why this audit does not speak for the release, or "" when it does.

    Two ways to be stale, and they are different facts. **The dependencies moved**: the audit is a
    statement about a set this release no longer has. **Time moved**: nothing in the repository
    changed and the answer expired anyway — the property that makes this the one piece of evidence
    here that is not tree-bound, and the reason it records a date beside the digest.
    """
    if not isinstance(record, dict) or not record.get("ran_at"):
        return "no dependency audit has been run — `rein audit run`"
    if record.get("passed") is not True:
        return (
            "the last dependency audit failed: "
            + (str(record.get("summary", "")).strip()[:400] or "(it recorded no summary)")
        )
    bound = str(record.get("dependencies", ""))
    if dependencies and bound and bound != dependencies:
        return (
            "the dependency audit was taken over a different set of manifests and lockfiles than "
            "this release has. Re-run `rein audit run`."
        )
    try:
        ran = datetime.fromisoformat(str(record["ran_at"]))
    except ValueError:
        return "the dependency audit records an unreadable `ran_at` — re-run `rein audit run`"
    if ran.tzinfo is None:
        ran = ran.replace(tzinfo=timezone.utc)
    if now - ran > timedelta(days=max_age):
        return (
            f"the dependency audit is {(now - ran).days} days old and expires after {max_age}. "
            "Nothing in the repository has to have changed for that to matter — the database it "
            "read moved. Re-run `rein audit run`."
        )
    return ""


def run(repo: repo_mod.Repo, config: models.Config | None) -> dict[str, Any]:
    """Run the configured audit and return the record to store. Raises when none is configured."""
    block = configured(config)
    command = [str(part) for part in block.get("command", [])]
    if not command:
        raise AuditError(
            "this project declares no `security.dependency_audit.command`, so gate 5 has no "
            "dependency answer to carry. Add one (pip-audit, npm audit, cargo audit, `make audit`) "
            "— it is the only security question that is not a function of the tree, and the only "
            "one a review cannot answer once."
        )
    profile = None
    if config is not None:
        named = str(block.get("executor_profile", ""))
        profile = config.profiles.get(named) if named else config.profile_for("quality_gate")
    profile = profile or models.ExecutorProfile("host", {"kind": "host"})
    spec = executors.ExecutionSpec(command=tuple(command), profile=profile, mounts=(), workdir=str(repo.root))
    result = executors.for_profile(profile).run(spec)
    rc, head = repo._git_rc("rev-parse", "HEAD")
    return {
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        # The commit is for the reader; `dependencies` is what the record is bound to.
        "commit": head.strip() if rc == 0 else "",
        "dependencies": dependency_digest(repo),
        "passed": result.exit_code == 0,
        "summary": result.output[-4000:],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein audit", description="the dependency audit gate 5 requires")
    sub = parser.add_subparsers(dest="cmd", required=True)
    runner = sub.add_parser("run", help="run the configured dependency audit and record what it said")
    runner.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    sub.add_parser("show", help="print the recorded audit and whether it still speaks for this release")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(getattr(args, "repo", None))
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    store = store_mod.Store(repo)
    try:
        config = store.read_config()
        state = store.read_state()
    except (models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    if state is None or not state.cycle_id:
        logger.error("no .rein/state.yaml — run `rein init` first")
        return 1

    if args.cmd == "show":
        record = state.raw.get("dependency_audit")
        reason = staleness(
            record,
            dependencies=dependency_digest(repo),
            now=datetime.now(timezone.utc),
            max_age=max_age_days(config),
        )
        print(f"dependency audit: {reason or 'current for this release'}")
        if isinstance(record, dict) and record.get("summary"):
            print(str(record["summary"])[:2000])
        return 0 if not reason else 1

    try:
        record = run(repo, config)
    except (AuditError, executors.ExecutorError) as exc:
        logger.error(str(exc))
        return 1
    with store.transaction() as tx:
        current = tx.store.read_state()
        assert current is not None
        tx.write("state", {**current.raw, "dependency_audit": record})
        tx.append(
            "dependency_audit_run",
            cycle_id=current.cycle_id,
            detail={"passed": record["passed"], "commit": record["commit"]},
        )
    if record["passed"]:
        print(f"dependency audit: clean at {record['commit'][:12]}, recorded {record['ran_at']}")
        return 0
    print(f"dependency audit: findings at {record['commit'][:12]}\n{record['summary']}")
    return 1
