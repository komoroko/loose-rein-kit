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
from collections.abc import Callable
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


def test_the_tree_exclusion_lives_in_exactly_one_place(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """Four answers have to agree about what "the tree" excludes: the fingerprint, the paths a task
    is credited with, the commit it produces, and the change a review is bound to. They were four
    separate spellings of `.rein/` — two module constants and four inline `:(exclude).rein`
    pathspecs, plus the copy inside the implementer's own instructions — so any one of them could
    drift and nothing would notice until a fact was invalidated by having been recorded.

    The set is `.rein/` **and the leaf worktree root**. The second was missing, so a serial task
    running beside a parked leaf was credited with that leaf's entire worktree.
    """
    import re

    from rein import build_prompts

    _, ws, _ = workspace
    assert ws.excluded == (repo_mod.SSOT_DIR, ".worktrees/")
    assert ws.pathspec == (".", ":(exclude).rein", ":(exclude).worktrees")
    # The instruction an agent actually types renders the same pathspec the loop applies for it.
    assert build_prompts._pathspec(ws.pathspec) == ". ':(exclude).rein' ':(exclude).worktrees'"
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


def test_the_diff_of_a_dirty_tree_carries_a_file_git_has_never_seen(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """`dirty_paths` reads `git status -uall` and names untracked files; `git diff` cannot show
    one. So a brand-new test file the implementer had not staged was listed as changed and then
    silently absent from the diff — rc 0, no error, and the negative control built on that diff
    re-established the base's own suite and reported that no test in the change exercised it."""
    root, ws, _ = workspace
    base = git(root, "rev-parse", "HEAD")
    (root / "tests").mkdir()
    (root / "tests" / "test_new.py").write_text("def test_new():\n    assert False\n", encoding="utf-8")
    (root / "README.md").write_text("base\nmodified\n", encoding="utf-8")

    paths = ws.dirty_paths(str(root))
    assert "tests/test_new.py" in paths, "an untracked file is part of the dirty tree"

    before = ws.fingerprint(str(root))
    diff = ws.diff_from(base, str(root), paths)
    assert diff is not None
    assert "tests/test_new.py" in diff and "assert False" in diff
    assert "new file mode" in diff

    # And the reading is not a write. `fingerprint` hashes what `git ls-files -o` still calls
    # untracked, so leaving the file staged would move the digest of a tree nobody touched — the
    # digest the evidence ledger keys every gate step on.
    assert ws.fingerprint(str(root)) == before
    assert "tests/test_new.py" in ws.dirty_paths(str(root))
    assert git(root, "status", "--porcelain", "-uall").splitlines()[-1].startswith("??")


def test_a_path_that_is_gone_does_not_fail_the_whole_diff(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """`git add` fails the whole invocation on a pathspec matching nothing, and a deleted file is
    in the diff without help — so what is no longer in the worktree is filtered out first."""
    root, ws, _ = workspace
    commit(root, "doomed.py", "x = 1\n")
    base = git(root, "rev-parse", "HEAD")
    (root / "doomed.py").unlink()
    (root / "fresh.py").write_text("y = 2\n", encoding="utf-8")

    diff = ws.diff_from(base, str(root), ["doomed.py", "fresh.py"])
    assert diff is not None
    assert "deleted file mode" in diff
    assert "fresh.py" in diff and "y = 2" in diff


def test_a_diff_git_would_not_give_up_is_not_an_empty_diff(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """`rc != 0` used to return `""`, which the negative control reads as "the test half was
    empty" — a verdict about a change git never handed over. None says so instead."""
    root, ws, _ = workspace
    (root / "fresh.py").write_text("y = 2\n", encoding="utf-8")

    assert ws.diff_from("not-a-commit", str(root), ["fresh.py"]) is None


def test_a_failed_intent_to_add_takes_back_what_it_managed_to_stage(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`git add` can stage some of its arguments and still exit non-zero. Reporting "nothing was
    staged" then left intent-to-add entries in the index for a tree nobody edited, which moves the
    fingerprint the evidence ledger keys every gate step on."""
    root, ws, _ = workspace
    base = git(root, "rev-parse", "HEAD")
    (root / "kept.py").write_text("y = 2\n", encoding="utf-8")
    before = ws.fingerprint(str(root))
    real = ws._run

    def half_staged(argv: list[str], cwd: str) -> tuple[int, str]:
        if argv[:3] == ["git", "add", "--intent-to-add"]:
            real(argv, cwd=cwd)  # git staged what it could …
            return 1, "fatal: pathspec 'gone.py' did not match any files"  # … and then failed
        return real(argv, cwd=cwd)

    monkeypatch.setattr(ws, "_run", half_staged)

    assert ws.diff_from(base, str(root), ["kept.py"]) is None, "a diff that may be missing a file is not the change"
    monkeypatch.undo()
    assert ws.fingerprint(str(root)) == before, "the index is back where it was"
    assert git(root, "status", "--porcelain", "-uall").splitlines()[-1].startswith("??")


def test_a_leafs_worktree_is_not_the_serial_tasks_dirty_tree(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """A parked leaf's worktree sits inside the repository root, untracked, holding its own work.

    Every reading of "what changed" taken from the root would otherwise swallow it: the serial
    task running next is credited with the leaf's entire tree, blocked for a scope it never
    reached into, and — through `finalize_commit`'s `git add -A` — hands `T-NNN: <title>` a
    gitlink to somebody else's worktree.
    """
    root, ws, _ = workspace
    ws.add_worktree("T-002")
    (root / ".worktrees" / "T-002" / "leaf.py").write_text("someone else's work\n", encoding="utf-8")
    (root / "mine.py").write_text("this task's work\n", encoding="utf-8")

    assert ws.dirty_paths(str(root)) == ["mine.py"]

    before = ws.fingerprint(str(root))
    (root / ".worktrees" / "T-002" / "leaf.py").write_text("the leaf moved on\n", encoding="utf-8")
    assert ws.fingerprint(str(root)) == before, "a sibling leaf writing must not move this tree's fingerprint"

    assert ws.finalize_commit(str(root), "T-001: serial work")
    assert git(root, "show", "--name-only", "--format=", "HEAD").split() == ["mine.py"]


def test_a_git_that_cannot_answer_is_not_an_answer_of_nothing(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """Every path query below parsed its output under `if rc == 0` with no `else`.

    So a git that *could not answer* — an index another process holds, a pathspec a mis-set
    `execution.worktree_dir` makes invalid, a repository the runner could not enter — read exactly
    like a git that answered *nothing*. Three answers rest on that: whether the run may start at
    all (`build_loop._tree_problems`), what a task is credited with changing, and whether its diff
    stayed inside its declared scope. A precondition that passes because the check broke is the one
    way a precondition must never pass.
    """
    root, ws, _ = workspace
    (root / "stray.py").write_text("x = 1\n", encoding="utf-8")
    assert ws.dirty_paths(str(root)) == ["stray.py"]  # the same question, answered

    calls: list[list[str]] = []

    def refusing(cmd: list[str], cwd: str | None = None, **kwargs: object) -> tuple[int, str]:
        calls.append(cmd)
        return 128, "fatal: unable to read index"

    ws._run = refusing  # type: ignore[assignment]
    queries: tuple[Callable[[], list[str]], ...] = (
        lambda: ws.dirty_paths(str(root)),
        lambda: ws.changed_since("HEAD~1"),
        lambda: ws.branch_changed_paths("T-001"),
    )
    for query in queries:
        with pytest.raises(common.StopLoop) as caught:
            query()
        assert "could not determine" in str(caught.value)
        assert "unable to read index" in str(caught.value)
        assert caught.value.code == common.EXIT_CANNOT_PROCEED
    assert len(calls) == 3


def test_a_capped_listing_is_a_prefix_and_not_the_answer(
    workspace: tuple[Path, build_git.GitWorkspace, list[tuple[str, str, str]]],
) -> None:
    """`fingerprint` has always refused a truncated output; the path queries took one as fact.

    A capped `git status` is a prefix of the uncommitted paths, so the tail — which may hold the
    only path that violates a scope — reads as absent rather than as unknown.
    """
    root, ws, _ = workspace
    ws._run = lambda cmd, cwd=None, **kw: (0, "?? a.py\n" + common.TRUNCATION_MARKER)  # type: ignore[assignment]
    with pytest.raises(common.StopLoop) as caught:
        ws.dirty_paths(str(root))
    assert "prefix rather than the answer" in str(caught.value)
