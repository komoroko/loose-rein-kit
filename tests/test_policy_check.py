"""policy_check is the base-side CI meta-policy: it must refuse a head that weakens the base (plan §29).

Every check runs over a real git tree so the base-side read is exercised as CI does it: an exact SHA
requirement, a legacy marker reappearing, a banned config key, and a broken audit chain (E2E-21/22).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rein import policy_check
from rein import repo as repo_mod
from tests._support import chain, make_config, make_state, seed_repo


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, state=make_state(project="p"), config=make_config())
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    return repo_mod.Repo(tmp_path)


def _head(repo: repo_mod.Repo) -> str:
    return repo._git_rc("rev-parse", "HEAD")[1].strip()


def _base(repo: repo_mod.Repo) -> str:
    """The seed commit. CI always hands `check` a base it can read — a base it cannot is itself
    a violation, since there is then nothing to compare the head against."""
    return repo._git_rc("rev-list", "--max-parents=0", "HEAD")[1].split()[0]


@pytest.mark.integration
def test_a_clean_head_passes(repo: repo_mod.Repo) -> None:
    head = _head(repo)
    assert policy_check.check(repo, _base(repo), head) == []


@pytest.mark.integration
def test_a_mutable_ref_is_refused(repo: repo_mod.Repo) -> None:
    head = _head(repo)
    problems = policy_check.check(repo, "main", head)  # a branch name, not an exact SHA
    assert problems and "mutable ref is not a base" in problems[0]


def test_a_short_sha_is_refused() -> None:
    repo = repo_mod.Repo(Path("/nonexistent"))  # SHA-shape check happens before any git call
    assert policy_check.check(repo, "0" * 40, "abc123")


@pytest.mark.integration
def test_a_banned_config_key_fails(repo: repo_mod.Repo) -> None:
    # `gates.enforce_hook` is a gate-weakening bypass this release refuses to carry (plan §4.1).
    (repo.root / ".rein" / "config.yaml").write_text(
        "project:\n  name: p\ngates:\n  enforce_hook: false\n", encoding="utf-8"
    )
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "weaken the gate")
    problems = policy_check.check(repo, _base(repo), _head(repo))
    assert any("gates.enforce_hook" in p for p in problems)


@pytest.mark.integration
def test_a_broken_audit_chain_fails(repo: repo_mod.Repo) -> None:
    from rein import event_chain

    events = chain("task_completed", "task_completed")
    event_chain.append_lines(repo.events, events)
    # Corrupt the chain: drop the first line so the second's prev-link dangles.
    lines = repo.events.read_text(encoding="utf-8").splitlines()
    repo.events.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "corrupt the chain")
    problems = policy_check.check(repo, _base(repo), _head(repo))
    assert any("audit chain" in p for p in problems)


@pytest.mark.integration
def test_the_chain_is_read_from_the_head_commit_not_the_checkout(repo: repo_mod.Repo) -> None:
    """On a pull request the checkout is the merge commit, so a path-based read verified a log
    nobody proposed — and an uncommitted edit could fail a head that is in fact clean."""
    from rein import event_chain

    event_chain.append_lines(repo.events, chain("task_completed", "task_completed"))
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "sound chain")
    head = _head(repo)
    repo.events.write_text("this is not an event\n", encoding="utf-8")  # working tree only
    assert not [p for p in policy_check.check(repo, _base(repo), head) if "audit chain" in p]


def test_banned_key_scan_is_recursive() -> None:
    text = "project:\n  name: p\nbuild:\n  headless:\n    cmd: claude -p\n"
    assert "build.headless.cmd" in policy_check._banned_config_keys(text)


@pytest.mark.parametrize("key", ["accept_critical_unknown", "trust_local_git_identity"])
def test_every_named_bypass_is_on_the_base_side_list(key: str) -> None:
    """No code reads these today. The list is what stops a head adding the key and its reader together."""
    assert key in policy_check._banned_config_keys(f"project:\n  name: p\n{key}: true\n")


# --- the check defends its own invocation -------------------------------------

_POLICY_WORKFLOW = """name: ci
on:
  pull_request:
jobs:
  policy:
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - run: >-
          uv run --frozen rein policy-check
          --base-sha "${{ github.event.pull_request.base.sha }}"
          --head-sha "${{ github.event.pull_request.head.sha }}"
"""


def _with_policy_workflow(repo: repo_mod.Repo) -> str:
    workflows = repo.root / ".github" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "ci.yml").write_text(_POLICY_WORKFLOW, encoding="utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "wire policy-check")
    return _head(repo)


@pytest.mark.integration
def test_a_head_that_deletes_the_policy_job_is_refused(repo: repo_mod.Repo) -> None:
    """Nothing read the workflows, so the simplest bypass in the repository was deleting the job:
    policy-check passed the pull request that removed policy-check."""
    base = _with_policy_workflow(repo)
    (repo.root / ".github" / "workflows" / "ci.yml").unlink()
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "drop CI")
    problems = policy_check.check(repo, base, _head(repo))
    assert any("no longer runs `rein policy-check`" in p for p in problems)


@pytest.mark.integration
def test_a_head_that_feeds_the_check_a_base_it_chose_is_refused(repo: repo_mod.Repo) -> None:
    base = _with_policy_workflow(repo)
    weakened = _POLICY_WORKFLOW.replace('"${{ github.event.pull_request.base.sha }}"', '"${{ github.head_ref }}"')
    (repo.root / ".github" / "workflows" / "ci.yml").write_text(weakened, encoding="utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "choose our own base")
    problems = policy_check.check(repo, base, _head(repo))
    assert any("trusted base SHA" in p for p in problems)


@pytest.mark.integration
def test_a_head_that_removes_the_commit_stage_guard_is_refused(repo: repo_mod.Repo) -> None:
    hook = repo.root / ".pre-commit-config.yaml"
    hook.write_text("repos:\n  - repo: local\n    hooks:\n      - id: g\n        entry: rein guard\n", "utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "wire the guard")
    base = _head(repo)

    hook.write_text("repos: []\n", encoding="utf-8")
    _git(repo.root, "add", "-A")
    _git(repo.root, "commit", "-qm", "unwire the guard")
    problems = policy_check.check(repo, base, _head(repo))
    assert any("commit-stage gate guard" in p for p in problems)


@pytest.mark.integration
def test_a_repository_that_never_wired_a_hook_is_not_weakening_anything(repo: repo_mod.Repo) -> None:
    """The comparison is against the base, not an assertion that every repo must have every hook."""
    assert not [p for p in policy_check.check(repo, _base(repo), _head(repo)) if "gate guard" in p]
