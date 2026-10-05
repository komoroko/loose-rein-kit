"""Approving acceptance integrates the approved work branch into the mainline — one act, not four.

`rein review complete`, `rein approve acceptance`, `rein pr-stack --ready` and a merge on the forge
were four asks for one decision. The approval now freezes the answers and carries itself out; these
tests pin the three ways it does, what it refuses to integrate, and that every ending is recorded.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import approve, build_git, common, integrate, store
from rein import repo as repo_mod
from tests._support import bind_review, make_review, make_state, seed_repo

WORK = "build/demo"


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _approved(tmp_path: Path, *, integrated: bool = False) -> repo_mod.Repo:
    """A repository whose `main` is the mainline and whose work branch is approved at its tip."""
    _git(tmp_path, "init", "-q", "-b", "main")
    # The merge integration makes is a commit, and CI's runner has no identity of its own: the repository
    # carries one, as `seed_repo(git=True)` does.
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "README.md").write_text("start\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".rein/\n.worktrees/\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    _git(tmp_path, "checkout", "-q", "-b", WORK)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('built')\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "the cycle's work")
    head = _git(tmp_path, "rev-parse", "HEAD")
    seed_repo(
        tmp_path,
        state=make_state(gates={"mandate": "approved", "acceptance": "approved"}, tasks={"T-001": "done"}),
        review=make_review(generated=True, human_status="frozen", head_sha=head),
    )
    repo = repo_mod.Repo(tmp_path)
    review = bind_review(tmp_path, make_review(generated=True, human_status="frozen", head_sha=head))
    repo.review.write_bytes(store.dump_yaml(review))
    if integrated:
        with store.Store(repo).transaction() as tx:
            tx.append("cycle_integrated", cycle_id="demo-cycle", actor="test", detail={"mode": "local"})
    return repo


def _real(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    return proc.returncode, proc.stdout + proc.stderr


def test_with_no_remote_the_work_is_merged_into_the_mainline_here(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    head = _git(tmp_path, "rev-parse", WORK)

    outcome = integrate.run(repo)

    assert outcome.mode == "local"
    assert _git(tmp_path, "merge-base", "--is-ancestor", head, "main") == ""
    assert _git(tmp_path, "rev-parse", "--abbrev-ref", "HEAD") == WORK, "the root's checkout is untouched"
    done = [e for e in store.Store(repo).read_events() if e.event == "cycle_integrated"][-1]
    assert done.detail["mode"] == "local" and done.detail["head"] == head
    assert integrate.integrated(store.Store(repo).read_events(), "demo-cycle")


def test_only_what_was_approved_is_integrated(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    (tmp_path / "src" / "late.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "src/late.py")
    _git(tmp_path, "commit", "-qm", "landed after the approval")
    before = _git(tmp_path, "rev-parse", "main")

    with pytest.raises(integrate.IntegrationError, match="the product has changed since"):
        integrate.run(repo)
    assert _git(tmp_path, "rev-parse", "main") == before


def test_committing_the_gate_s_own_documents_does_not_stop_the_integration(tmp_path: Path) -> None:
    """The workflow commits `.rein/` at each gate. Acceptance measures the review on the product's
    content and reads that commit as moving nothing; integrating compared commit ids, so it refused
    every approval made after it — and `rein next` kept recommending the `rein integrate` that
    refused."""
    repo = _approved(tmp_path)
    _git(tmp_path, "add", "-f", ".rein/review.yaml")
    _git(tmp_path, "commit", "-qm", "the acceptance gate's deliverables")
    tip = _git(tmp_path, "rev-parse", WORK)

    assert approve._integration_blockers(repo, store.Store(repo).read_config(), "acceptance") == []
    outcome = integrate.run(repo)

    assert outcome.mode == "local"
    assert _git(tmp_path, "merge-base", "--is-ancestor", tip, "main") == ""


def test_a_mainline_checked_out_in_the_root_is_never_moved_under_it(tmp_path: Path) -> None:
    """The scratch worktree was made with `--force`, so it checked out the branch the root held, and
    the merge moved `main` under the root: its index still described the old commit, and the next
    commit made there would have undone the integration."""
    repo = _approved(tmp_path)
    _git(tmp_path, "checkout", "-q", "main")
    before = _git(tmp_path, "rev-parse", "main")

    blockers = approve._integration_blockers(repo, store.Store(repo).read_config(), "acceptance")
    assert len(blockers) == 1 and approve.owner(blockers[0]) == "human"
    assert f"git switch {WORK}" == blockers[0].remedy
    with pytest.raises(integrate.IntegrationError, match="canonical checkout has the mainline"):
        integrate.run(repo)

    assert _git(tmp_path, "rev-parse", "main") == before
    assert _git(tmp_path, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_mainline_checked_out_elsewhere_is_merged_where_it_is(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    elsewhere = tmp_path.parent / f"{tmp_path.name}-main"
    _git(tmp_path, "worktree", "add", "-q", str(elsewhere), "main")
    tip = _git(tmp_path, "rev-parse", WORK)

    integrate.run(repo)

    assert _git(elsewhere, "merge-base", "--is-ancestor", tip, "HEAD") == ""
    assert _git(elsewhere, "status", "--porcelain") == "", "the checkout that holds it moved with it"


def test_a_scratch_worktree_is_never_a_branch_another_checkout_holds(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    with pytest.raises(common.StopLoop, match="could not create the scratch worktree"):
        with build_git.scratch_worktree(repo, ".worktrees", "probe", WORK, _real):
            pytest.fail("checked out the branch the root holds")


def test_a_merge_that_fails_is_recorded_with_its_own_words(tmp_path: Path) -> None:
    repo = _approved(tmp_path)

    def refusing(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        if cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
            return 1, "CONFLICT (content): Merge conflict in src/app.py"
        return _real(cmd, cwd)

    with pytest.raises(integrate.IntegrationError, match="Merge conflict"):
        integrate.run(repo, runner=refusing)
    failed = [e for e in store.Store(repo).read_events() if e.event == "integration_failed"][-1]
    assert failed.detail["mode"] == "local" and "Merge conflict" in failed.detail["reason"]
    assert not integrate.integrated(store.Store(repo).read_events(), "demo-cycle")


def _forge(tmp_path: Path, calls: list[list[str]], **answers: tuple[int, str]) -> integrate.Runner:
    """A forge: `origin` exists and its `main` is the local one; `gh` answers what the test says."""
    _git(tmp_path, "update-ref", "refs/remotes/origin/main", "main")

    def run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return 0, "https://example.invalid/o/r.git"
        if cmd[:2] in (["git", "push"], ["git", "fetch"]):
            return 0, ""
        if cmd[:3] == ["gh", "pr", "list"]:
            return answers.get("list", (0, ""))
        if cmd[:3] == ["gh", "pr", "create"]:
            return 0, "https://example.invalid/o/r/pull/7\n"
        if cmd[:3] == ["gh", "pr", "view"]:
            return answers.get("draft", (0, "true"))
        if cmd[:3] == ["gh", "pr", "ready"]:
            return 0, ""
        if cmd[:3] == ["gh", "pr", "merge"]:
            return answers.get("merge", (0, ""))
        return _real(cmd, cwd)

    return run


def test_with_a_remote_the_approved_commit_is_pushed_opened_and_merged(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    tip = _git(tmp_path, "rev-parse", WORK)
    calls: list[list[str]] = []

    outcome = integrate.run(repo, runner=_forge(tmp_path, calls))

    assert outcome == integrate.Outcome(mode="pull_request", landed=("https://example.invalid/o/r/pull/7",))
    assert ["git", "push", "origin", f"{tip}:refs/heads/{WORK}"] in calls, "the commit, not the branch's name"
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == WORK
    merge = next(c for c in calls if c[:3] == ["gh", "pr", "merge"])
    assert "--merge" in merge, "a merge commit: squash and rebase strand the recorded commits"
    assert merge[merge.index("--match-head-commit") + 1] == tip, "a head pushed since is not merged"
    issued = [c[:3] for c in calls if c[0] == "gh"]
    assert issued.index(["gh", "pr", "ready"]) < issued.index(["gh", "pr", "merge"]), "lifted, then merged"


def test_the_pull_request_is_the_open_one_into_the_mainline(tmp_path: Path) -> None:
    """The work branch outlives a cycle. Looked up by its name alone, a branch whose previous cycle's
    pull request was merged found that one, and every merge after it failed on a merged pull request."""
    repo = _approved(tmp_path)
    calls: list[list[str]] = []

    integrate.run(repo, runner=_forge(tmp_path, calls, list=(0, "https://example.invalid/o/r/pull/9\n")))

    asked = next(c for c in calls if c[:3] == ["gh", "pr", "list"])
    assert asked[asked.index("--head") + 1] == WORK and asked[asked.index("--base") + 1] == "main"
    assert asked[asked.index("--state") + 1] == "open"
    assert not any(c[:3] == ["gh", "pr", "create"] for c in calls), "the open one is used"
    assert next(c for c in calls if c[:3] == ["gh", "pr", "merge"])[3] == "https://example.invalid/o/r/pull/9"


def test_a_forge_still_waiting_on_checks_leaves_the_approval_standing(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    calls: list[list[str]] = []
    waiting = (1, "Pull request is not mergeable: required status checks are pending")

    with pytest.raises(integrate.IntegrationError, match="rein integrate"):
        integrate.run(repo, runner=_forge(tmp_path, calls, draft=(0, "false"), merge=waiting))
    state = store.Store(repo).read_state()
    assert state is not None and state.gate_status("acceptance") == "approved"


def test_a_forge_whose_mainline_moved_into_a_conflict_blocks_before_the_approval(tmp_path: Path) -> None:
    """The conflict check read the local `main`, while a forge merges into its own. A local `main`
    behind the forge's passed readiness, and the forge refused after the approval."""
    origin = tmp_path.parent / f"{tmp_path.name}-origin.git"
    _git(tmp_path.parent, "init", "-q", "--bare", "-b", "main", str(origin))
    repo = _approved(tmp_path)
    _git(tmp_path, "remote", "add", "origin", str(origin))
    _git(tmp_path, "push", "-q", "origin", "main")
    other = tmp_path.parent / f"{tmp_path.name}-other"
    _git(tmp_path.parent, "clone", "-q", str(origin), str(other))
    _git(other, "config", "user.email", "t@e.x")
    _git(other, "config", "user.name", "T")
    (other / "src").mkdir()
    (other / "src" / "app.py").write_text("print('the forge moved')\n", encoding="utf-8")
    _git(other, "add", "src/app.py")
    _git(other, "commit", "-qm", "the forge's main moved")
    _git(other, "push", "-q", "origin", "main")

    approve.refresh_integration_target(repo)
    blockers = approve._integration_blockers(repo, store.Store(repo).read_config(), "acceptance")

    assert len(blockers) == 1 and approve.owner(blockers[0]) == "machine"
    assert "no longer merges cleanly into refs/remotes/origin/main" in blockers[0]


# --- what approving says before it asks, and what it does after -------------------------------


def test_approving_acceptance_carries_itself_out(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = _approved(tmp_path)
    ran: list[str] = []
    monkeypatch.setattr(approve, "confirm_locally", lambda repo, gate, subject: None)
    monkeypatch.setattr(approve, "readiness", lambda repo, gate: [])

    def record(repo: repo_mod.Repo, gate: str, subject: Any) -> str:
        ran.append(gate)
        return "GA-X"

    def carry_out(repo: repo_mod.Repo) -> tuple[str, bool]:
        ran.append("integrate")
        return "done", True

    monkeypatch.setattr(approve, "record_approval", record)
    monkeypatch.setattr(approve, "integrate_approved", carry_out)

    assert approve.approve_locally(repo, "acceptance", {}) == 0
    assert ran == ["acceptance", "integrate"]


def test_the_screen_says_what_approving_will_do_to_the_mainline(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    assert approve.integration_note(repo) == (
        f"Approving integrates {WORK} into main: it merges the work branch into it here, in a scratch worktree."
    )


def test_a_work_branch_that_no_longer_merges_is_the_machine_s_to_fix_first(tmp_path: Path) -> None:
    """An approval whose integration then stops on a conflict hands a person back a decision they
    already made — so a conflict blocks before, and it is the machine's to clear."""
    repo = _approved(tmp_path)
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "app.py").write_text("print('elsewhere')\n", encoding="utf-8")
    _git(tmp_path, "add", "src/app.py")
    _git(tmp_path, "commit", "-qm", "main moved")
    _git(tmp_path, "checkout", "-q", WORK)

    config = store.Store(repo).read_config()
    blockers = approve._integration_blockers(repo, config, "acceptance")

    assert len(blockers) == 1 and approve.owner(blockers[0]) == "machine"
    assert "no longer merges cleanly into main" in blockers[0]


def test_a_mainline_that_does_not_exist_is_a_person_s_to_fix(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    _git(tmp_path, "branch", "-m", "main", "trunk")
    blockers = approve._integration_blockers(repo, store.Store(repo).read_config(), "acceptance")
    assert len(blockers) == 1 and approve.owner(blockers[0]) == "human"
    assert "`project.mainline`" in blockers[0]
