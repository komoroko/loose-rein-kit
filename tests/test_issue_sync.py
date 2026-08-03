"""Verify issue_sync.py's pure logic and dry-run (offline, gh-independent)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from rein import dag, issue_sync, models
from tests._support import make_config, make_plan, make_task


def _task(
    tid: str,
    kind: str = "parallel",
    status: str = "todo",
    claim_ids: tuple[str, ...] = ("C-001",),
    risk: str = "low",
) -> dag.Task:
    return dag.Task(
        id=tid,
        title=f"{tid} title",
        kind=kind,
        blocked_by=(),
        status=status,
        claim_ids=claim_ids,
        risk=risk,
    )


def test_desired_issue_fields() -> None:
    d = issue_sync.desired_issue(_task("T-001", kind="foundation"), base_label="rein", close_on_done=True)
    assert d.title == "T-001: T-001 title"
    assert d.labels == ("rein", "kind:foundation", "status:todo", "risk:low", "claim:C-001")
    assert d.closed is False
    assert "one-way mirror" in d.body


def test_desired_issue_carries_risk_and_every_claim() -> None:
    """The mirror shows what the task is answerable for, not a free-text requirement string."""
    d = issue_sync.desired_issue(
        _task("T-001", claim_ids=("C-001", "C-002"), risk="high"), base_label="rein", close_on_done=False
    )
    assert "risk:high" in d.labels
    assert {"claim:C-001", "claim:C-002"} <= set(d.labels)
    assert "- risk: high" in d.body
    assert "- claims: C-001, C-002" in d.body


def test_done_task_is_desired_closed_only_when_enabled() -> None:
    done = _task("T-001", status="done")
    assert issue_sync.desired_issue(done, base_label="rein", close_on_done=True).closed is True
    assert issue_sync.desired_issue(done, base_label="rein", close_on_done=False).closed is False


def test_plan_creates_when_no_existing() -> None:
    tasks = (_task("T-002"), _task("T-001"))
    actions = issue_sync.plan_actions(tasks, {}, base_label="rein", close_on_done=True)
    assert [(a.op, a.task_id) for a in actions] == [("create", "T-001"), ("create", "T-002")]  # ascending id
    assert actions[0].add_labels == ("rein", "kind:parallel", "status:todo", "risk:low", "claim:C-001")


def test_label_specs_cover_families_and_the_plan_s_claims() -> None:
    graph = dag.Graph.from_tasks([dag.Task(id="T-001", title="x", kind="foundation", claim_ids=("C-001",))])
    specs = issue_sync.label_specs(graph, "rein")
    names = {s.name for s in specs}
    assert "rein" in names
    assert {f"kind:{k}" for k in dag.KIND_VALUES} <= names
    assert {f"status:{s}" for s in dag.STATUS_VALUES} <= names
    assert {"risk:low", "risk:high", "risk:critical", "claim:C-001"} <= names
    assert {f"risk:{r}" for r in models.RISK_ORDER} <= names
    assert all(len(s.color) == 6 and all(c in "0123456789abcdef" for c in s.color) for s in specs)


def test_task_id_of_prefers_body_marker_over_edited_title() -> None:
    # A human renaming the issue must not break the issue↔task link (which would create a duplicate).
    body = issue_sync._issue_body(_task("T-007"))
    assert issue_sync.task_id_of("renamed by a human", body) == "T-007"


def test_task_id_of_falls_back_to_title_prefix_for_pre_marker_issues() -> None:
    assert issue_sync.task_id_of("T-003: old issue", "no marker in this body") == "T-003"


def test_issue_body_embeds_marker() -> None:
    assert "<!-- rein:T-001 -->" in issue_sync._issue_body(_task("T-001"))


def test_every_emitted_label_family_is_a_managed_one() -> None:
    """A family that is emitted but not managed reads as missing on every run, so each sync
    re-adds the same labels forever."""
    desired = issue_sync.desired_issue(_task("T-001"), base_label="rein", close_on_done=True)
    assert all(issue_sync._managed(label, "rein") for label in desired.labels)


def test_plan_noop_when_identical() -> None:
    task = _task("T-001")
    desired = issue_sync.desired_issue(task, base_label="rein", close_on_done=True)
    existing = {
        "T-001": issue_sync.ExistingIssue(
            number=5, title=desired.title, state="OPEN", labels=desired.labels, body=desired.body
        )
    }
    assert issue_sync.plan_actions((task,), existing, base_label="rein", close_on_done=True) == []


def test_plan_updates_status_label_diff() -> None:
    task = _task("T-001", status="in-progress")
    desired = issue_sync.desired_issue(task, base_label="rein", close_on_done=True)
    existing = {
        "T-001": issue_sync.ExistingIssue(
            number=5,
            title=desired.title,
            state="OPEN",
            labels=("rein", "kind:parallel", "status:todo", "risk:low", "claim:C-001"),  # stale status
            body=desired.body,
        )
    }
    actions = issue_sync.plan_actions((task,), existing, base_label="rein", close_on_done=True)
    assert [a.op for a in actions] == ["update"]
    assert actions[0].add_labels == ("status:in-progress",)
    assert actions[0].remove_labels == ("status:todo",)


def test_plan_closes_done_open_issue() -> None:
    task = _task("T-001", status="done")
    desired = issue_sync.desired_issue(task, base_label="rein", close_on_done=True)
    existing = {
        "T-001": issue_sync.ExistingIssue(
            number=5, title=desired.title, state="OPEN", labels=desired.labels, body=desired.body
        )
    }
    actions = issue_sync.plan_actions((task,), existing, base_label="rein", close_on_done=True)
    assert [a.op for a in actions] == ["close"]


def test_plan_reopens_regressed_issue() -> None:
    task = _task("T-001", status="in-progress")
    desired = issue_sync.desired_issue(task, base_label="rein", close_on_done=True)
    existing = {
        "T-001": issue_sync.ExistingIssue(
            number=5, title=desired.title, state="CLOSED", labels=desired.labels, body=desired.body
        )
    }
    actions = issue_sync.plan_actions((task,), existing, base_label="rein", close_on_done=True)
    assert [a.op for a in actions] == ["reopen"]


def test_preflight_skips_when_disabled() -> None:
    cfg = issue_sync.GithubConfig(enabled=False, label="rein", close_on_done=True, repo="")
    ready, reason = issue_sync.preflight(cfg)
    assert ready is False
    assert "enabled" in reason


_CONFIG = make_config()
_TASKS = make_plan(
    tasks=[
        make_task("T-001", claim_ids=["C-001"]),
        make_task("T-002", kind="parallel", blocked_by=["T-001"], claim_ids=["C-001"]),
    ]
)


@pytest.fixture
def project(make_repo: Callable[..., Path]) -> Path:
    return make_repo(config=_CONFIG, plan=_TASKS)


def test_dry_run_is_offline_and_plans_creates(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # --dry-run outputs the label list and planned creations without calling gh (even when disabled).
    rc = issue_sync.main(["--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "create T-001" in out
    assert "create T-002" in out
    # Labels to create/ensure (fixed kind/status/phase + dynamic req).
    assert "kind:foundation" in out
    assert "risk:low" in out
    assert "claim:C-001" in out


def test_fetch_existing_stops_when_snapshot_may_be_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    # A saturated gh page means unseen mirror issues; planning against it would create duplicates.
    import json

    cfg = issue_sync.GithubConfig(enabled=True, label="rein", close_on_done=True, repo="")
    page = json.dumps(
        [
            {"number": i, "title": f"T-{i:03d}: t", "state": "OPEN", "labels": [], "body": ""}
            for i in range(issue_sync.FETCH_LIMIT)
        ]
    )
    monkeypatch.setattr(issue_sync, "_run", lambda args, cwd=None: (0, page))
    with pytest.raises(issue_sync.IssueSyncError, match="truncated"):
        issue_sync.fetch_existing(cfg)


def test_preflight_skips_without_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = issue_sync.GithubConfig(enabled=True, label="rein", close_on_done=True, repo="")
    monkeypatch.setattr("shutil.which", lambda name: None)
    ready, reason = issue_sync.preflight(cfg)
    assert ready is False
    assert "gh CLI" in reason


def test_preflight_skips_without_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = issue_sync.GithubConfig(enabled=True, label="rein", close_on_done=True, repo="")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(issue_sync, "_run", lambda args, cwd=None: (0, ""))  # `git remote` lists nothing
    ready, reason = issue_sync.preflight(cfg)
    assert ready is False
    assert "remote" in reason


def test_preflight_ready_with_explicit_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    # An explicit github.repo skips the remote probe entirely (works in a detached clone).
    cfg = issue_sync.GithubConfig(enabled=True, label="rein", close_on_done=True, repo="owner/repo")
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(issue_sync, "_run", lambda args, cwd=None: pytest.fail("must not probe git remote"))
    assert issue_sync.preflight(cfg) == (True, "")


def test_apply_one_creates_then_closes_done_task(monkeypatch: pytest.MonkeyPatch) -> None:
    # A done task mirrored for the first time is created and immediately closed via the URL's number.
    cfg = issue_sync.GithubConfig(enabled=True, label="rein", close_on_done=True, repo="owner/repo")
    calls: list[list[str]] = []

    def fake_run(args: list[str], cwd: str | None = None) -> tuple[int, str]:
        calls.append(args)
        return 0, "https://github.com/owner/repo/issues/42\n"

    monkeypatch.setattr(issue_sync, "_run", fake_run)
    desired = issue_sync.desired_issue(_task("T-001", status="done"), base_label="rein", close_on_done=True)
    issue_sync._apply_one(issue_sync.Action("create", "T-001", None, desired), cfg)
    assert calls[0][:3] == ["gh", "issue", "create"]
    assert "--repo" in calls[0] and "owner/repo" in calls[0]
    assert ["gh", "issue", "close", "42", "--repo", "owner/repo"] in calls
