"""Tests for build_git.py — the git/worktree mechanics the build loop drives.

These run against a real `git`, not a fake: what is under test is whether a branch, a worktree
and a merge actually behave the way the loop assumes, which a stubbed runner cannot answer.

The line this file defends: **an interrupted run's work is picked back up, not merely kept.**
Preserving a crashed leaf's commits on a salvage branch was already true; nothing read them
back, so a build restarted from another terminal re-implemented the task from zero on a fresh
branch off the work branch. The salvage branch is now merged into the new worktree, and what
happened either way is reported to whoever records the handoff.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rein import build_git, common, models
from rein import repo as repo_mod


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def commit(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", f"add {name}")


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]]:
    """A repo on a work branch, plus a workspace wired to a recording salvage sink."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    commit(root, "README.md", "base\n")
    git(root, "checkout", "-q", "-b", "build/x")
    salvaged: list[tuple[str, str, str]] = []
    ws = build_git.GitWorkspace(
        repo_mod.Repo(root),
        "build/x",
        dry_run=False,
        worktree_dir=".worktrees",
        branch_pattern="{branch}-{task_id}",
        run=common.run,
        on_salvage=lambda task_id, branch, state: salvaged.append((task_id, branch, state)),
    )
    return root, ws, salvaged


def test_an_interrupted_attempts_work_is_carried_into_the_next_one(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """The whole point of the handoff: the next attempt starts from the work already done."""
    root, ws, salvaged = workspace
    ws.add_worktree("T-001")
    commit(root / ".worktrees" / "T-001", "feature.py", "half a feature\n")

    # The run dies here; a new terminal re-enters the same task.
    branch = ws.add_worktree("T-001")
    assert (root / ".worktrees" / "T-001" / "feature.py").read_text(encoding="utf-8") == "half a feature\n"
    assert branch == "build/x-T-001"
    assert [(t, s) for t, _, s in salvaged] == [("T-001", "pending"), ("T-001", "restored")]


def test_the_salvage_branch_is_named_in_what_is_reported(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    root, ws, salvaged = workspace
    ws.add_worktree("T-001")
    commit(root / ".worktrees" / "T-001", "feature.py", "work\n")
    ws.add_worktree("T-001")
    assert all(b.startswith("build/x-T-001-salvage-") for _, b, _ in salvaged)
    assert git(root, "rev-parse", "--verify", "--quiet", salvaged[0][1])  # the branch still exists


def test_a_conflicting_salvage_is_reported_rather_than_forced(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """Failing the task here would strand the very run meant to recover it, so the merge is
    abandoned, the branch keeps the work, and the implementer is told where to find it."""
    root, ws, salvaged = workspace
    ws.add_worktree("T-001")
    commit(root / ".worktrees" / "T-001", "shared.py", "the leaf's version\n")
    commit(root, "shared.py", "the work branch's version\n")  # the same file moved underneath it

    ws.add_worktree("T-001")
    assert salvaged[-1][2] == "conflict"
    worktree = root / ".worktrees" / "T-001"
    assert (worktree / "shared.py").read_text(encoding="utf-8") == "the work branch's version\n"
    assert git(worktree, "status", "--porcelain") == ""  # the aborted merge left nothing behind


def test_a_first_attempt_has_nothing_to_restore(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    _, ws, salvaged = workspace
    ws.add_worktree("T-001")
    assert salvaged == []


def test_a_fully_merged_branch_is_not_resurrected(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """Its content is already in the work branch; salvaging it would be noise, not safety."""
    root, ws, salvaged = workspace
    branch = ws.add_worktree("T-001")
    commit(root / ".worktrees" / "T-001", "feature.py", "done\n")
    assert ws.merge_leaf("T-001", branch)

    ws.add_worktree("T-001")
    assert salvaged == []


def test_the_ssot_exclusion_lives_in_exactly_one_place() -> None:
    """Four answers have to agree about what "the tree" excludes: the fingerprint, the paths a task
    is credited with, the commit it produces, and the change a review is bound to. They were four
    separate spellings of `.rein/` — two module constants and four inline `:(exclude).rein`
    pathspecs, plus the copy inside the implementer's own instructions — so any one of them could
    drift and nothing would notice until a fact was invalidated by having been recorded.
    """
    import re

    from rein import build_prompts

    assert build_git._EXCLUDED == (repo_mod.SSOT_DIR,)
    # The instruction an agent actually types renders the same pathspec the loop applies for it.
    assert build_prompts._pathspec() == ". ':(exclude).rein'"
    assert repo_mod.SSOT_PATHSPEC == (".", ":(exclude).rein")

    literal = re.compile(r":\(exclude\)")
    offenders = [
        path.name
        for path in sorted(Path(repo_mod.__file__).parent.rglob("*.py"))
        if path.name != "repo.py" and literal.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"{offenders} spell the exclusion themselves instead of using repo.SSOT_PATHSPEC"


def test_the_review_excludes_more_than_the_tree_does(tmp_path: Path) -> None:
    """What "the tree" excludes and what "the product under review" excludes are different answers.

    The tree's is `.rein/` and only `.rein/` — a task legitimately writes `docs/`, and a
    fingerprint that ignored it would credit the task with nothing. The review's is wider: the
    plan's own prose is the answer sheet, and handing it to the blind extractor is the failure
    `assert_blind` cannot see, because the plan arrives inside the diff rather than beside it.
    """
    from rein import review
    from tests._support import make_state

    (tmp_path / ".rein").mkdir()
    repo = repo_mod.Repo(tmp_path)
    raw = make_state()
    raw["plan"]["sources"] = {"docs/10-requirements.md": "sha256:" + "a" * 64}
    state = models.State(raw)
    exclude = review.not_the_product(repo, state)

    assert repo_mod.SSOT_DIR in exclude
    assert "docs/10-requirements.md" in exclude, "the frozen prose gate ③ pinned"
    assert "docs/tasks/" in exclude and "docs/decisions/" in exclude, "tickets and ADRs"
    assert not any(e == "docs/" for e in exclude), "a README is a deliverable and stays reviewable"
