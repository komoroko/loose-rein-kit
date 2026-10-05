"""`rein integrate` — what approving acceptance does: put the approved work branch into the mainline.

  rein integrate

Approving acceptance is the decision; this is its execution, and it is the same act. It used to be
three more: lifting the drafts (`rein pr-stack --ready`, confirmed at a terminal), and merging by
hand on the forge — after `rein review complete` had frozen the answers the approval was about to
bind anyway. One person deciding one thing was asked four times, and the last of those asks was
outside the harness, where nothing recorded that it had been made.

So `rein approve acceptance` (and the dashboard's approval) calls :func:`run` straight after the
receipt is written, and this verb exists only to finish an integration that did not complete —
the forge refused while its required checks were pending, the network dropped. It asks nothing:
the decision is on the record, and re-running it is carrying that decision out, not making a new
one. What it refuses is integrating anything other than what was approved: the approved review has
to speak for the work branch's tip, measured on content the way acceptance measured it
(`review_reading.freshness`), and that tip — one commit, resolved once — is what is merged.

What could stop it is asked before the approval too (:func:`obstacles`, read by acceptance's
readiness), so an approval is not handed back to a person over something the machine could see.

Three ways in, chosen by what the cycle already has, never by a flag:

* **stack** — the cycle was published as a stack (`rein pr-stack --push` recorded its pull
  requests). Each body is rewritten with the approved facts, each draft is lifted, and the stack is
  merged whole, as merge commits (`gh stack merge`): a partial or rebased merge strands every
  `completed_commit` above the cut.
* **pull request** — the repository has an `origin` remote. The approved tip is pushed to the work
  branch, the branch's *open* pull request against the mainline is found or opened (body:
  `rein pr-draft`'s), lifted out of draft if it is one, and merged as a merge commit — only while
  its head is still the approved tip (`--match-head-commit`).
* **local** — no remote. The approved tip is merged into the mainline where the mainline is checked
  out, or in a scratch worktree when nothing has it. Never past a checkout that holds it: moving a
  branch under a checkout leaves that checkout's index describing the commit before, and its next
  commit undoes the merge.

A forge merges into *its* mainline, so the two remote ways measure against `origin/<mainline>`,
fetched by the approval (:func:`refresh`) and again here.

Every outcome is in the chain: `cycle_integrated` when the mainline has the work, and
`integration_failed` with the forge's or git's own words when it does not.
"""

from __future__ import annotations

import argparse
import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rein import build_git, common, models, pr_draft, pr_stack, review_reading
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

Runner = Callable[..., tuple[int, str]]

#: The scratch worktree a local integration merges in, when no worktree holds the mainline.
_WORKTREE = "_integrate"


class IntegrationError(common.ReinError):
    """Integration did not happen. The message says why, and what to do next."""


@dataclass(frozen=True)
class Outcome:
    mode: str
    #: What the mainline now has: the merge commit (local), or the pull request(s) merged.
    landed: tuple[str, ...]


@dataclass(frozen=True)
class Obstacle:
    """Something that would stop the integration, whose it is to clear, and what clears it."""

    text: str
    #: `machine` or `human`, as `approve.Blocker` reads it.
    by: str
    remedy: str


def integrated(events: tuple[models.Event, ...] | list[models.Event], cycle_id: str) -> bool:
    """Has this cycle's approved work reached the mainline?"""
    return any(e.event == "cycle_integrated" and e.cycle_id == cycle_id for e in events)


def mode_of(repo: repo_mod.Repo, docs: pr_stack.Documents, *, run: Runner = common.run) -> str:
    """`stack`, `pull_request` or `local` — read off what the cycle and the repository already have."""
    if any(record.url for record in pr_stack.ledger(docs.events)):
        return "stack"
    rc, _ = run(["git", "remote", "get-url", "origin"], cwd=str(repo.root))
    return "pull_request" if rc == 0 else "local"


def target_ref(config: models.Config, mode: str) -> str:
    """The ref the integration merges into: the mainline here, or the forge's copy of it."""
    return config.mainline if mode == "local" else f"refs/remotes/origin/{config.mainline}"


def refresh(repo: repo_mod.Repo, *, runner: Runner = common.run) -> None:
    """Bring `origin/<mainline>` up to the forge's, when the forge is where the integration lands.

    The one network read the question needs, made by the act that integrates — approving, and
    `rein integrate` — and never by readiness, which every status read asks.
    """
    docs = pr_stack.Documents.read(repo)
    mode = mode_of(repo, docs, run=runner)
    if mode == "local":
        return
    mainline = docs.config.mainline
    rc, out = runner(
        ["git", "fetch", "origin", f"refs/heads/{mainline}:refs/remotes/origin/{mainline}"],
        cwd=str(repo.root),
        timeout=pr_stack.NETWORK_TIMEOUT_SEC,
    )
    if rc != 0:
        raise IntegrationError(
            f"reading origin's {mainline} failed: {out[-1000:]}. Approving integrates into it, so it has to be read"
        )


def obstacles(
    repo: repo_mod.Repo, docs: pr_stack.Documents, *, run: Runner = common.run, tip: str | None = None
) -> list[Obstacle]:
    """What would stop integrating the work branch as it stands. Asked before the approval and again by it.

    `tip` is the commit that will be merged, when the caller has already resolved it; otherwise the
    work branch's tip now. No network: the remote ways read `origin/<mainline>` as last fetched
    (:func:`refresh`).
    """
    config, review = docs.config, docs.review
    mode = mode_of(repo, docs, run=run)
    target = target_ref(config, mode)
    found: list[Obstacle] = []
    if not _rev_parse(repo, target):
        if mode == "local":
            return [
                Obstacle(
                    f"the mainline `{config.mainline}` (`project.mainline`) does not exist here, so there is nothing "
                    "for an approval to integrate the cycle into",
                    by="human",
                    remedy="rein doctor",
                )
            ]
        return [
            Obstacle(
                f"origin's `{config.mainline}` has not been read here, and it is what the forge merges into",
                by="machine",
                remedy=f"git fetch origin {config.mainline}",
            )
        ]
    tip = _rev_parse(repo, config.work_branch) if tip is None else tip
    if not tip:
        return [Obstacle(f"the work branch `{config.work_branch}` does not resolve", by="human", remedy="rein doctor")]
    if review is not None and review.is_generated:
        # The tip is what gets merged, so the review has to speak for the tip — not for whatever the
        # root has checked out, which is what readiness's own freshness reads.
        if reason := review_reading.freshness(repo, review, docs.state, at=tip).reason:
            found.append(Obstacle(reason, by="machine", remedy="rein review generate"))
    conflicted, why = _conflicts(repo, tip, target)
    if why:
        found.append(Obstacle(why, by="human", remedy="rein doctor"))
    elif conflicted:
        found.append(
            Obstacle(
                f"{config.work_branch} no longer merges cleanly into {target}. Do not rebase: merge "
                f"{config.mainline} into the work branch, then take the review again so that what is "
                "approved is what lands",
                by="machine",
                remedy=f"git merge {target}; rein review generate",
            )
        )
    if mode == "local":
        holder = _holder(repo, config.mainline, run)
        if holder and Path(holder).resolve() == Path(repo.root).resolve():
            # The approval writes this checkout's `.rein/` and the merge would land under it; both
            # are what the root is for, and the root is where the work branch belongs.
            found.append(
                Obstacle(
                    f"the canonical checkout has the mainline `{config.mainline}` checked out. The cycle's "
                    "state lives there and approving writes it, so integrating would merge into a checkout "
                    f"the approval has just changed. Check out `{config.work_branch}` there",
                    by="human",
                    remedy=f"git switch {config.work_branch}",
                )
            )
        elif holder and _dirty(holder, run):
            found.append(
                Obstacle(
                    f"the mainline `{config.mainline}` is checked out at {holder} with uncommitted changes, and the "
                    "merge is made where the mainline is checked out",
                    by="human",
                    remedy=f"git -C {holder} status",
                )
            )
    return found


def run(repo: repo_mod.Repo, *, runner: Runner = common.run) -> Outcome:
    """Integrate the approved work branch into the mainline, and record what happened.

    Refuses before touching anything when acceptance is not approved or the cycle is already
    integrated. Everything after that is recorded: a stop is `integration_failed` with its reason.
    """
    docs = pr_stack.Documents.read(repo)
    state, config = docs.state, docs.config
    if state.gate_status("acceptance") != "approved":
        raise IntegrationError("acceptance is not approved — integrating is what approving it does")
    if integrated(docs.events, state.cycle_id):
        raise IntegrationError(f"cycle {state.cycle_id} is already integrated into {config.mainline}")
    mode = mode_of(repo, docs, run=runner)
    # Resolved once: everything below checks and merges this commit, never the branch's name, so
    # nothing that lands on the branch while this runs can ride along.
    tip = _rev_parse(repo, config.work_branch)
    try:
        refresh(repo, runner=runner)
        if stopped := obstacles(repo, docs, run=runner, tip=tip):
            raise IntegrationError(
                "the approved work cannot be integrated as it stands:\n  - "
                + "\n  - ".join(f"{o.text} (`{o.remedy}`)" for o in stopped)
                + "\nWhat landed after the approval goes back to a person with `rein revise --to acceptance`."
            )
        if mode == "stack":
            landed = _stack(repo, docs, runner)
        elif mode == "pull_request":
            landed = _pull_request(repo, docs, tip, runner)
        else:
            landed = _local(repo, docs, tip, runner)
    except IntegrationError as exc:
        _record(repo, state.cycle_id, "integration_failed", {"mode": mode, "reason": str(exc)[:2000]})
        raise
    _record(
        repo,
        state.cycle_id,
        "cycle_integrated",
        {"mode": mode, "mainline": config.mainline, "head": tip, "landed": list(landed)},
    )
    return Outcome(mode=mode, landed=landed)


def _stack(repo: repo_mod.Repo, docs: pr_stack.Documents, runner: Runner) -> tuple[str, ...]:
    base = docs.config.mainline
    slices = pr_stack.derive(repo, docs, base=base)
    checked = pr_stack.preconditions(repo, docs, slices, mode="ready", base=base)
    if not checked.ok:
        raise IntegrationError("the stack cannot be integrated:\n  - " + "\n  - ".join(checked.errors))
    try:
        bodies = pr_stack.write_bodies(repo, docs, slices, base=base)
        pr_stack.lift(repo, docs, slices, bodies, run=runner)
    except (pr_stack.PublishError, OSError) as exc:
        raise IntegrationError(str(exc)) from None
    top = next((record.url for record in reversed(pr_stack.ledger(docs.events)) if record.url), "")
    rc, out = runner(["gh", "stack", "merge", top, "--merge"], cwd=str(repo.root), timeout=pr_stack.NETWORK_TIMEOUT_SEC)
    if rc != 0:
        raise IntegrationError(
            f"`gh stack merge {top} --merge` failed: {out[-1000:]}. `rein integrate` again once it can"
        )
    return tuple(record.url for record in pr_stack.ledger(docs.events) if record.url)


def _pull_request(repo: repo_mod.Repo, docs: pr_stack.Documents, tip: str, runner: Runner) -> tuple[str, ...]:
    branch, base, root = docs.config.work_branch, docs.config.mainline, str(repo.root)
    timeout = pr_stack.NETWORK_TIMEOUT_SEC
    rc, out = runner(["git", "push", "origin", f"{tip}:refs/heads/{branch}"], cwd=root, timeout=timeout)
    if rc != 0:
        raise IntegrationError(f"pushing {tip[:12]} to origin's {branch} failed: {out[-1000:]}")
    # The open one into the mainline, asked for by both: the work branch outlives a cycle, so a branch
    # name alone also finds the pull request a previous cycle merged.
    rc, url = runner(
        [
            "gh", "pr", "list", "--head", branch, "--base", base, "--state", "open",
            "--json", "url", "--jq", ".[0].url // empty",
        ],
        cwd=root,
        timeout=timeout,
    )  # fmt: skip
    if rc != 0:
        raise IntegrationError(f"listing the open pull requests from {branch} into {base} failed: {url[-1000:]}")
    url = url.strip()
    if not url:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
            handle.write(pr_draft.build_body(repo, base=base))
            body = handle.name
        try:
            rc, out = runner(
                [
                    "gh", "pr", "create", "--base", base, "--head", branch,
                    "--title", f"{docs.state.cycle_id}: integrate {branch}", "--body-file", body,
                ],
                cwd=root,
                timeout=timeout,
            )  # fmt: skip
        finally:
            Path(body).unlink(missing_ok=True)
        if rc != 0 or not out.strip():
            raise IntegrationError(f"opening the pull request for {branch} failed: {out[-1000:]}")
        url = out.strip().splitlines()[-1]
    rc, draft = runner(["gh", "pr", "view", url, "--json", "isDraft", "--jq", ".isDraft"], cwd=root, timeout=timeout)
    if rc != 0:
        raise IntegrationError(f"reading whether {url} is a draft failed: {draft[-1000:]}")
    if draft.strip() == "true":
        rc, out = runner(["gh", "pr", "ready", url], cwd=root, timeout=timeout)
        if rc != 0:
            raise IntegrationError(f"lifting {url} out of draft failed: {out[-1000:]}")
    rc, out = runner(["gh", "pr", "merge", url, "--merge", "--match-head-commit", tip], cwd=root, timeout=timeout)
    if rc != 0:
        raise IntegrationError(
            f"merging {url} at {tip[:12]} failed: {out[-1000:]}. A forge that is waiting on required checks says "
            "so here; `rein integrate` again once they pass. One whose head is no longer the approved commit "
            "refuses the merge, and that is not retried"
        )
    return (url,)


def _local(repo: repo_mod.Repo, docs: pr_stack.Documents, tip: str, runner: Runner) -> tuple[str, ...]:
    branch, base = docs.config.work_branch, docs.config.mainline
    message = f"{docs.state.cycle_id}: integrate {branch}"

    def merge(path: str) -> str:
        rc, out = runner(["git", "merge", "--no-ff", "-m", message, tip], cwd=path)
        if rc != 0:
            runner(["git", "merge", "--abort"], cwd=path)
            raise IntegrationError(f"merging {branch} ({tip[:12]}) into {base} failed: {out[-1000:]}")
        return _rev_parse(repo, base)

    if holder := _holder(repo, base, runner):
        return (merge(holder),)
    try:
        with build_git.scratch_worktree(repo, docs.config.worktree_dir, _WORKTREE, base, runner) as path:
            return (merge(path),)
    except common.StopLoop as exc:
        raise IntegrationError(str(exc)) from None


def _holder(repo: repo_mod.Repo, branch: str, run: Runner) -> str:
    """The worktree that has `branch` checked out, or "" — the only place a merge into it may be made."""
    return next((path for path, held in build_git.worktree_heads(repo, run) if held == branch), "")


def _dirty(path: str, run: Runner) -> bool:
    rc, out = run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=path)
    return rc != 0 or bool(out.strip())


def _conflicts(repo: repo_mod.Repo, ours: str, theirs: str) -> tuple[bool, str]:
    """Would merging `theirs` into `ours` conflict? `(conflicted, why it could not be told)`.

    A gate, so a git that cannot answer is not "no conflict" (`pr_stack.conflicts_with`, which only
    warns, reads it that way).
    """
    rc, out = repo._git_rc("merge-tree", "--write-tree", "--name-only", ours, theirs)
    if rc == 0:
        return False, ""
    if rc == 1:
        return True, ""
    return False, f"whether the work branch still merges into the mainline could not be measured: {out[-300:]}"


def _rev_parse(repo: repo_mod.Repo, ref: str) -> str:
    """A read of this repository, so the repository's own git — the runner is for what is done to it."""
    rc, out = repo._git_rc("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    return out.strip() if rc == 0 else ""


def _record(repo: repo_mod.Repo, cycle_id: str, event: str, detail: dict[str, object]) -> None:
    with store_mod.Store(repo).transaction() as tx:
        tx.append(event, cycle_id=cycle_id, actor="rein integrate", detail=detail)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein integrate",
        description="finish putting the approved work branch into the mainline (approving acceptance starts it)",
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        outcome = run(repo)
    except (IntegrationError, pr_stack.StackError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    print(f"integrated ({outcome.mode}): {', '.join(outcome.landed)}")
    return 0
