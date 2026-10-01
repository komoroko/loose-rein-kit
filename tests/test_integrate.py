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

from rein import approve, integrate, store
from rein import repo as repo_mod
from tests._support import make_review, make_state, seed_repo

WORK = "build/demo"


def _git(root: Path, *args: str) -> str:
    identity = ["-c", "user.name=t", "-c", "user.email=t@t"]
    proc = subprocess.run(["git", *identity, *args], cwd=root, check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _approved(tmp_path: Path, *, integrated: bool = False) -> repo_mod.Repo:
    """A repository whose `main` is the mainline and whose work branch is approved at its tip."""
    _git(tmp_path, "init", "-q", "-b", "main")
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
    if integrated:
        with store.Store(repo).transaction() as tx:
            tx.append("cycle_integrated", cycle_id="demo-cycle", actor="test", detail={"mode": "local"})
    return repo


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

    with pytest.raises(integrate.IntegrationError, match="Only what was approved is integrated"):
        integrate.run(repo)
    assert _git(tmp_path, "rev-parse", "main") == before


def test_an_unapproved_or_finished_cycle_is_not_integrated(tmp_path: Path) -> None:
    repo = _approved(tmp_path, integrated=True)
    with pytest.raises(integrate.IntegrationError, match="already integrated"):
        integrate.run(repo)


def test_a_merge_that_fails_is_recorded_with_its_own_words(tmp_path: Path) -> None:
    repo = _approved(tmp_path)

    def refusing(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        if cmd[:2] == ["git", "merge"] and "--abort" not in cmd:
            return 1, "CONFLICT (content): Merge conflict in src/app.py"
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    with pytest.raises(integrate.IntegrationError, match="Merge conflict"):
        integrate.run(repo, runner=refusing)
    failed = [e for e in store.Store(repo).read_events() if e.event == "integration_failed"][-1]
    assert failed.detail["mode"] == "local" and "Merge conflict" in failed.detail["reason"]
    assert not integrate.integrated(store.Store(repo).read_events(), "demo-cycle")


def test_with_a_remote_the_pull_request_is_pushed_opened_and_merged(tmp_path: Path) -> None:
    repo = _approved(tmp_path)
    calls: list[list[str]] = []

    def forge(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        calls.append(cmd)
        if cmd[:3] == ["git", "remote", "get-url"]:
            return 0, "https://example.invalid/o/r.git"
        if cmd[:2] == ["git", "push"]:
            return 0, ""
        if cmd[:3] == ["gh", "pr", "view"] and "url" in cmd:
            return 1, "no pull requests found"
        if cmd[:3] == ["gh", "pr", "create"]:
            return 0, "https://example.invalid/o/r/pull/7\n"
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, "true"
        if cmd[:3] in (["gh", "pr", "ready"], ["gh", "pr", "merge"]):
            return 0, ""
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    outcome = integrate.run(repo, runner=forge)

    assert outcome == integrate.Outcome(mode="pull_request", landed=("https://example.invalid/o/r/pull/7",))
    issued = [c[:3] for c in calls if c[0] == "gh" or c[:2] == ["git", "push"]]
    assert ["git", "push", "origin"] in issued
    create = next(c for c in calls if c[:3] == ["gh", "pr", "create"])
    assert create[create.index("--base") + 1] == "main" and create[create.index("--head") + 1] == WORK
    assert issued.index(["gh", "pr", "ready"]) < issued.index(["gh", "pr", "merge"]), "lifted, then merged"
    merge = next(c for c in calls if c[:3] == ["gh", "pr", "merge"])
    assert "--merge" in merge, "a merge commit: squash and rebase strand the recorded commits"


def test_a_forge_still_waiting_on_checks_leaves_the_approval_standing(tmp_path: Path) -> None:
    repo = _approved(tmp_path)

    def forge(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        if cmd[:3] == ["git", "remote", "get-url"] or cmd[:2] == ["git", "push"]:
            return 0, ""
        if cmd[:3] == ["gh", "pr", "view"]:
            return 0, "https://example.invalid/o/r/pull/7" if "url" in cmd else "false"
        if cmd[:3] == ["gh", "pr", "merge"]:
            return 1, "Pull request is not mergeable: required status checks are pending"
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
        return proc.returncode, proc.stdout + proc.stderr

    with pytest.raises(integrate.IntegrationError, match="rein integrate"):
        integrate.run(repo, runner=forge)
    state = store.Store(repo).read_state()
    assert state is not None and state.gate_status("acceptance") == "approved"


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
