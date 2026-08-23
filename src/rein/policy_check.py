"""The CI meta-policy: a base-side verifier that reads the head tree and refuses a weakening (plan §29).

A pull request cannot be trusted to check itself — the head could edit the very policy that judges it.
So CI runs *this* from the trusted base side, handing it the two commit SHAs from its own event context
(`github.event.pull_request.base.sha` / `head.sha`), never a branch name and never a base the head
declared. `policy-check` then reads the head tree read-only and fails closed on anything that would
re-open a closed boundary (E2E-21):

- a non-exact ref (a branch, a short SHA, `HEAD`) for either side — a mutable base is no base at all;
- a banned config key that unbinds a gate (`gates.enforce_hook`, `build.headless.cmd`, `schema_version`,
  `post_build.security_review`, `skip_grounding`) — the Absolute-Block bypasses (plan §4.1, §15.4);
- a damaged audit chain (deletion, reorder, truncation, or a wholesale re-hash — E2E-22).

The one honesty this file owes the reader: this check is a bootstrap — there is no prior base
verifier to check the commit that adds this file, so that first run is *not* self-verified and the
workflow and README say so (plan §29.3).
"""

from __future__ import annotations

import argparse
import logging
import re

from rein import event_chain, models, strict_yaml
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

_EXACT_SHA = re.compile(r"^[0-9a-f]{40}$")

#: Config keys whose mere presence weakens a gate — the compatibility/bypass shims this release
#: refuses to carry (plan §4.1). `gates.enforce_hook` and friends are nested, so the scan is recursive by dotted path.
_BANNED_KEYS: frozenset[str] = frozenset(
    {
        "schema_version",
        "gates.enforce_hook",
        "build.headless.cmd",
        "post_build.security_review",
        "skip_grounding",
        # Named bypasses the release refuses to have. No code reads them and the config schema is
        # `additionalProperties: false`, so a key alone does nothing — but this list is the base
        # side's statement of what may not come back, and a head that adds the key *and* the code
        # to read it arrives as one change. Silence here would make that one review away from
        # landing.
        "accept_critical_unknown",
        "trust_local_git_identity",
    }
)

#: Files that carry an enforcement boundary rather than product code. A head that removes what a
#: base tree had is removing a check on itself, which is the one move this file exists to refuse.
_ENFORCEMENT_MARKERS: tuple[tuple[str, str, str], ...] = (
    (".pre-commit-config.yaml", "rein guard", "the commit-stage gate guard"),
    (".claude/settings.json", "rein guard", "the edit-stage gate guard hook"),
)

#: What a workflow invoking the base-side check has to keep. `pull_request` because that is the
#: only event where CI knows a trusted base; the two SHA expressions because a base the head can
#: name is not a base (a branch, `github.head_ref`, an input) — which is exactly the substitution
#: `check` refuses at run time, refused here before it can run at all.
_POLICY_INVOCATION = "rein policy-check"
_POLICY_REQUIRED: tuple[tuple[str, str], ...] = (
    ("pull_request", "the pull_request trigger"),
    ("github.event.pull_request.base.sha", "the trusted base SHA from the event context"),
    ("github.event.pull_request.head.sha", "the head SHA from the event context"),
)


class PolicyCheckError(Exception):
    """policy-check could not run at all (a bad SHA, an unreadable tree) — distinct from a violation."""


def _show(repo: repo_mod.Repo, sha: str, path: str) -> str | None:
    """The content of `path` in the tree at `sha`, or None when it is not present there."""
    rc, out = repo._git_rc("show", f"{sha}:{path}")
    return out if rc == 0 else None


def _tree_paths(repo: repo_mod.Repo, sha: str) -> list[str]:
    rc, out = repo._git_rc("ls-tree", "-r", "--name-only", sha)
    if rc != 0:
        raise PolicyCheckError(f"cannot read the tree at {sha}: {out.strip()}")
    return [line for line in out.splitlines() if line]


def _banned_config_keys(text: str) -> list[str]:
    """Dotted paths present in a config document that are on the banned list (recursive)."""
    try:
        document = strict_yaml.load_mapping(text, what="config.yaml (head tree)")
    except strict_yaml.StrictParseError as exc:
        return [f"config.yaml does not parse under the strict loader: {exc}"]
    found: list[str] = []

    def walk(node: object, prefix: str) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            dotted = f"{prefix}.{key}" if prefix else str(key)
            if dotted in _BANNED_KEYS:
                found.append(dotted)
            walk(value, dotted)

    walk(document, "")
    return found


def _enforcement_removals(repo: repo_mod.Repo, base_sha: str, head_sha: str) -> list[str]:
    """Enforcement the base tree had and the head no longer does.

    Compared against the base rather than asserted outright: a repository that never wired a
    given hook is not weakening anything by still not having it, but removing one is exactly
    the move a base-side check exists to catch.
    """
    violations: list[str] = []
    for path, marker, what in _ENFORCEMENT_MARKERS:
        base_text = _show(repo, base_sha, path)
        if base_text is None or marker not in base_text:
            continue
        head_text = _show(repo, head_sha, path)
        if head_text is None:
            violations.append(f"the head tree deletes {path}, which carried {what}")
        elif marker not in head_text:
            violations.append(f"the head tree removes {what} from {path}")
    return violations


def _policy_job_weakening(repo: repo_mod.Repo, base_sha: str, head_sha: str) -> list[str]:
    """A head that stops CI from running this very check, or feeds it a base it can choose.

    Nothing looked at the workflows at all, so the simplest bypass in the repository was to
    delete the job: `policy-check` passed the pull request that removed `policy-check`. It reads
    the raw text rather than parsing the YAML because what matters is whether the invocation and
    its trusted inputs survive, not where in the file they live.
    """
    base_paths = _tree_paths_or_none(repo, base_sha)
    if base_paths is None:
        return [f"cannot read the base tree at {base_sha[:12]} — with no base there is nothing to compare against"]
    invoking = {p for p in base_paths if _is_workflow(p) and _POLICY_INVOCATION in (_show(repo, base_sha, p) or "")}
    if not invoking:
        return []  # the base never ran it here; requiring it now is a different conversation

    head_texts = {p: _show(repo, head_sha, p) or "" for p in _tree_paths(repo, head_sha) if _is_workflow(p)}
    surviving = [text for text in head_texts.values() if _POLICY_INVOCATION in text]
    if not surviving:
        return [
            "the head tree no longer runs `rein policy-check` in any workflow — a pull request "
            "that removes the base-side check is removing the only check it cannot fake"
        ]
    violations: list[str] = []
    for token, what in _POLICY_REQUIRED:
        if not any(token in text for text in surviving):
            violations.append(f"the head's `rein policy-check` workflow no longer carries {what} ({token})")
    return violations


def _tree_paths_or_none(repo: repo_mod.Repo, sha: str) -> list[str] | None:
    """`_tree_paths`, but None instead of raising — for the base side, whose absence is reportable."""
    rc, out = repo._git_rc("ls-tree", "-r", "--name-only", sha)
    return [line for line in out.splitlines() if line] if rc == 0 else None


def _is_workflow(path: str) -> bool:
    return path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml"))


def trusted_base(
    repo: repo_mod.Repo, base_sha: str, head_sha: str, base_ref: str, default_branch: str
) -> tuple[str, list[str]]:
    """The commit this check may compare against, and why it is not always the pull request's base.

    Normally the base *is* trusted: CI takes it from its own event context and the head cannot
    name it. A **stacked** pull request breaks that. Its base is the slice below it, a branch the
    head's author created — so "the head did not remove what the base had" says nothing, because
    the base is theirs too and the removal could have happened one slice earlier.

    For those, the trusted base is `merge-base(<default branch>, head)`: the last commit both the
    head and the repository's own default branch agree on, which is by construction a commit the
    author did not write. That is a *stronger* check than the flat case, not a workaround — it
    catches a weakening introduced anywhere in the stack rather than only in the top slice.

    The default branch comes from CI's event context (`github.event.repository.default_branch`),
    which the head cannot forge either. Without it there is no trusted base to find, and a stacked
    pull request must fail rather than fall back to one its author controls.
    """
    if not base_ref or not models.is_stack_branch(base_ref):
        return base_sha, []
    if not default_branch:
        return "", [
            f"the base {base_ref!r} is a stack branch — a base the head's author created — and no "
            "--default-branch was given, so there is no trusted commit to compare against. CI has to "
            "pass `github.event.repository.default_branch`."
        ]
    rc, out = repo._git_rc("merge-base", default_branch, head_sha)
    resolved = out.strip()
    if rc != 0 or not _EXACT_SHA.match(resolved):
        return "", [
            f"the base {base_ref!r} is a stack branch and no merge base with {default_branch!r} could be "
            "found — fetch enough history (actions/checkout with fetch-depth: 0) for the check to have one"
        ]
    return resolved, []


def check(
    repo: repo_mod.Repo,
    base_sha: str,
    head_sha: str,
    *,
    base_ref: str = "",
    default_branch: str = "",
) -> list[str]:
    """Every policy violation in the head tree; empty means CI may pass. Never short-circuits.

    `base_sha` is used to prove the caller supplied an exact commit from a trusted context, and to
    compare what enforcement the base tree carried against what the head still does — it is never
    read *from* the head, which is the whole point of a base-side check (plan §29.3).

    `base_ref` is what lets this tell a flat pull request from a slice of a stack; see
    :func:`trusted_base` for why the two cannot be compared against the same commit.
    """
    violations: list[str] = []
    for label, sha in (("--base-sha", base_sha), ("--head-sha", head_sha)):
        if not _EXACT_SHA.match(sha):
            violations.append(f"{label} {sha!r} is not an exact 40-hex commit SHA — a mutable ref is not a base")
    if violations:
        return violations  # nothing else can be trusted until the SHAs are exact

    base_sha, resolution_problems = trusted_base(repo, base_sha, head_sha, base_ref, default_branch)
    if resolution_problems:
        return resolution_problems  # comparing against an untrusted base is worse than not comparing

    config_text = _show(repo, head_sha, ".rein/config.yaml")
    if config_text is not None:
        for key in _banned_config_keys(config_text):
            violations.append(f"the head config carries a banned key that would weaken a gate: {key}")

    violations += _policy_job_weakening(repo, base_sha, head_sha)
    violations += _enforcement_removals(repo, base_sha, head_sha)

    # From the head tree, not the checkout: on a `pull_request` the checked-out commit is the
    # merge commit, so reading the working tree meant this check ran against a log that was not
    # the one being proposed.
    events_text = _show(repo, head_sha, ".rein/events.ndjson")
    _, defects = event_chain.scan_text(events_text or "")
    if defects:
        violations.append(
            f"the audit chain has {len(defects)} defect(s) — deletion, reorder, truncation, or a "
            "re-hash breaks the chain a gate receipt pins (E2E-22)"
        )
    return violations


#: What a workflow has to pass for a *stacked* pull request to be judgeable at all. Not in
#: `_POLICY_REQUIRED`: a repository that never publishes a stack is unaffected by its absence, and
#: making it required there would fail every adopter's CI for a feature they do not use. It is
#: enforced where it matters instead — `pr-stack` refuses to publish a stack CI cannot judge.
STACK_REQUIRED: tuple[tuple[str, str], ...] = (
    ("github.event.pull_request.base.ref", "the base branch name, which is how a stack base is recognised"),
    ("github.event.repository.default_branch", "the default branch, the only trusted base a stack has"),
)


def workflows_missing_stack_inputs(repo: repo_mod.Repo) -> list[str]:
    """Workflows that run the base-side check but could not judge a stacked pull request.

    Read from the working tree rather than a git object: this answers "may I publish a stack from
    this checkout", which is a question about the files as they are now.
    """
    directory = repo.root / ".github" / "workflows"
    if not directory.is_dir():
        return []
    missing: list[str] = []
    for path in sorted(directory.iterdir()):
        if not _is_workflow(f".github/workflows/{path.name}"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _POLICY_INVOCATION not in text:
            continue
        absent = [why for token, why in STACK_REQUIRED if token not in text]
        if absent:
            missing.append(f".github/workflows/{path.name} does not pass {' or '.join(absent)}")
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein policy-check",
        description="base-side CI meta-policy: read the head tree and refuse a weakening",
    )
    parser.add_argument("--base-sha", required=True, help="the PR base commit SHA (from CI, exact — never a branch)")
    parser.add_argument("--head-sha", required=True, help="the PR head commit SHA (from CI, exact)")
    parser.add_argument(
        "--base-ref",
        default="",
        help="the PR base branch name (from CI) — how a stacked PR's untrusted base is recognised",
    )
    parser.add_argument(
        "--default-branch",
        default="",
        help="the repository's default branch (from CI) — the trusted base a stacked PR is measured against",
    )
    args = parser.parse_args(argv)

    from rein import common

    common.configure_logging()
    try:
        repo = repo_mod.get(None)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        violations = check(
            repo, args.base_sha, args.head_sha, base_ref=args.base_ref, default_branch=args.default_branch
        )
    except PolicyCheckError as exc:
        logger.error(str(exc))
        return 1
    if violations:
        logger.error("policy-check failed (%d violation(s)):", len(violations))
        for violation in violations:
            logger.error("  - %s", violation)
        return 1
    print(f"policy-check clear: head {args.head_sha[:12]} does not weaken the base policy")
    return 0
