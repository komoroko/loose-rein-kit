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
one. What it refuses is integrating anything other than what was approved: the work branch has to
be at the commit the approved review read.

Three ways in, chosen by what the cycle already has, never by a flag:

* **stack** — the cycle was published as a stack (`rein pr-stack --push` recorded its pull
  requests). Each body is rewritten with the approved facts, each draft is lifted, and the stack is
  merged whole, as merge commits (`gh stack merge`): a partial or rebased merge strands every
  `completed_commit` above the cut.
* **pull request** — the repository has an `origin` remote. The work branch is pushed, its pull
  request is opened against the mainline if it has none (body: `rein pr-draft`'s), lifted out of
  draft if it is one, and merged as a merge commit.
* **local** — no remote. The work branch is merged into the mainline in a scratch worktree, so the
  root's checkout and its uncommitted `.rein/` state are untouched.

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

from rein import build_git, common, models, pr_draft, pr_stack
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

Runner = Callable[..., tuple[int, str]]

#: The scratch worktree a local integration merges in.
_WORKTREE = "_integrate"


class IntegrationError(common.ReinError):
    """Integration did not happen. The message says why, and what to do next."""


@dataclass(frozen=True)
class Outcome:
    mode: str
    #: What the mainline now has: the merge commit (local), or the pull request(s) merged.
    landed: tuple[str, ...]


def integrated(events: tuple[models.Event, ...] | list[models.Event], cycle_id: str) -> bool:
    """Has this cycle's approved work reached the mainline?"""
    return any(e.event == "cycle_integrated" and e.cycle_id == cycle_id for e in events)


def mode_of(repo: repo_mod.Repo, docs: pr_stack.Documents, *, run: Runner = common.run) -> str:
    """`stack`, `pull_request` or `local` — read off what the cycle and the repository already have."""
    if any(record.url for record in pr_stack.ledger(docs.events)):
        return "stack"
    rc, _ = run(["git", "remote", "get-url", "origin"], cwd=str(repo.root))
    return "pull_request" if rc == 0 else "local"


def run(repo: repo_mod.Repo, *, runner: Runner = common.run) -> Outcome:
    """Integrate the approved work branch into the mainline, and record what happened.

    Refuses before touching anything when acceptance is not approved, when the cycle is already
    integrated, or when the work branch is not at the commit the approved review read.
    """
    docs = pr_stack.Documents.read(repo)
    state, config, review = docs.state, docs.config, docs.review
    if state.gate_status("acceptance") != "approved":
        raise IntegrationError("acceptance is not approved — integrating is what approving it does")
    if integrated(docs.events, state.cycle_id):
        raise IntegrationError(f"cycle {state.cycle_id} is already integrated into {config.mainline}")
    head = _rev_parse(repo, config.work_branch)
    approved = review.subject_head_sha if review is not None else ""
    if not head or head != approved:
        raise IntegrationError(
            f"{config.work_branch} is at {head[:12] or '(unresolvable)'}, and the approved review read "
            f"{approved[:12] or '(nothing)'}. Only what was approved is integrated: something landed after the "
            "approval, and taking it back to a person is `rein revise --to acceptance`."
        )
    mode = mode_of(repo, docs, run=runner)
    try:
        if mode == "stack":
            landed = _stack(repo, docs, runner)
        elif mode == "pull_request":
            landed = _pull_request(repo, docs, runner)
        else:
            landed = _local(repo, docs, runner)
    except IntegrationError as exc:
        _record(repo, state.cycle_id, "integration_failed", {"mode": mode, "reason": str(exc)[:2000]})
        raise
    _record(
        repo,
        state.cycle_id,
        "cycle_integrated",
        {"mode": mode, "mainline": config.mainline, "head": head, "landed": list(landed)},
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


def _pull_request(repo: repo_mod.Repo, docs: pr_stack.Documents, runner: Runner) -> tuple[str, ...]:
    branch, base, root = docs.config.work_branch, docs.config.mainline, str(repo.root)
    timeout = pr_stack.NETWORK_TIMEOUT_SEC
    rc, out = runner(["git", "push", "origin", f"{branch}:{branch}"], cwd=root, timeout=timeout)
    if rc != 0:
        raise IntegrationError(f"pushing {branch} to origin failed: {out[-1000:]}")
    rc, url = runner(["gh", "pr", "view", branch, "--json", "url", "--jq", ".url"], cwd=root, timeout=timeout)
    url = url.strip()
    if rc != 0 or not url:
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
        if rc != 0:
            raise IntegrationError(f"opening the pull request for {branch} failed: {out[-1000:]}")
        url = out.strip().splitlines()[-1] if out.strip() else branch
    rc, draft = runner(["gh", "pr", "view", url, "--json", "isDraft", "--jq", ".isDraft"], cwd=root, timeout=timeout)
    if rc == 0 and draft.strip() == "true":
        rc, out = runner(["gh", "pr", "ready", url], cwd=root, timeout=timeout)
        if rc != 0:
            raise IntegrationError(f"lifting {url} out of draft failed: {out[-1000:]}")
    rc, out = runner(["gh", "pr", "merge", url, "--merge"], cwd=root, timeout=timeout)
    if rc != 0:
        raise IntegrationError(
            f"merging {url} failed: {out[-1000:]}. A forge that is waiting on required checks says so here; "
            "`rein integrate` again once they pass"
        )
    return (url,)


def _local(repo: repo_mod.Repo, docs: pr_stack.Documents, runner: Runner) -> tuple[str, ...]:
    branch, base = docs.config.work_branch, docs.config.mainline
    if not _rev_parse(repo, base):
        raise IntegrationError(f"the mainline {base} does not exist in this repository (`project.mainline`)")
    try:
        with build_git.scratch_worktree(repo, docs.config.worktree_dir, _WORKTREE, base, runner) as path:
            rc, out = runner(
                ["git", "merge", "--no-ff", "-m", f"{docs.state.cycle_id}: integrate {branch}", branch], cwd=path
            )
            if rc != 0:
                runner(["git", "merge", "--abort"], cwd=path)
                raise IntegrationError(f"merging {branch} into {base} failed: {out[-1000:]}")
            merged = _rev_parse(repo, base)
    except common.StopLoop as exc:
        raise IntegrationError(str(exc)) from None
    return (merged,)


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
