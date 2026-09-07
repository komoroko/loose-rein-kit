"""The one security answer gate ⑤ cannot carry from gate ④.

`verify.md` has said since it existed that the dependency audit is the one thing that must
actually be run at the release gate, because it is the only security question that is not a
function of the tree. Nothing ran it: the instruction lived in a prompt, no document held the
answer, and no readiness check asked for one — so a release could be signed with the audit having
been "done" in a chat window.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from rein import audit, models
from rein import repo as repo_mod
from tests._support import make_config, seed_repo

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _record(**over: Any) -> dict[str, Any]:
    return {
        "ran_at": (NOW - timedelta(days=1)).isoformat(timespec="seconds"),
        "commit": "a" * 40,
        "dependencies": "sha256:" + "b" * 64,
        "passed": True,
        **over,
    }


def _stale(record: object, *, dependencies: str = "sha256:" + "b" * 64, max_age: int = 7) -> str:
    return audit.staleness(record, dependencies=dependencies, now=NOW, max_age=max_age)


def test_a_current_audit_speaks_for_the_release() -> None:
    assert _stale(_record()) == ""


def test_no_audit_is_not_a_clean_audit() -> None:
    assert "no dependency audit has been run" in _stale(None)
    assert "no dependency audit has been run" in _stale({})


def test_a_failing_audit_carries_what_it_said() -> None:
    """The gate reads the result, and the human reads the reason beside the release decision."""
    said = _stale(_record(passed=False, summary="GHSA-xxxx in urllib3 <2.5.0"))
    assert "GHSA-xxxx" in said


def test_an_audit_expires_though_nothing_in_the_repository_moved() -> None:
    """The property that makes this different from every other piece of evidence here: the
    database it read moved while the code did not."""
    old = _record(ran_at=(NOW - timedelta(days=30)).isoformat(timespec="seconds"))
    assert "expires after 7" in _stale(old)


def test_a_changed_dependency_set_retires_the_answer() -> None:
    assert "a different set of manifests and lockfiles" in _stale(_record(), dependencies="sha256:" + "c" * 64)


def test_editing_source_does_not_retire_it() -> None:
    """Binding to the commit would buy a re-run on every source edit, for an answer about
    dependencies that did not change."""
    assert _stale(_record(commit="a" * 40), dependencies="sha256:" + "b" * 64) == ""


def test_the_dependency_digest_is_over_manifests_and_nothing_else(tmp_path: Path) -> None:
    """What counts as a dependency is `diff_facts`' own set — decided in one place, because the
    same question is asked when a change is priced for risk."""

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )

    seed_repo(tmp_path)
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    git("init", "-q", "-b", "main")
    git("add", "-A")
    git("commit", "-qm", "seed")
    repo = repo_mod.Repo(tmp_path)
    before = audit.dependency_digest(repo)
    assert before

    (tmp_path / "app.py").write_text("x = 2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "source only")
    assert audit.dependency_digest(repo) == before, "source is not a dependency change"

    (tmp_path / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-qm", "lockfile")
    assert audit.dependency_digest(repo) != before


def test_a_project_that_declares_no_audit_is_told_so() -> None:
    """ "We have no way to ask" is not "there is nothing wrong"."""
    assert audit.configured(models.Config(make_config())) == {}
    with pytest.raises(audit.AuditError, match="declares no"):
        audit.run(repo_mod.Repo(Path(".")), models.Config(make_config()))


# --- what the audit may record, and what it must refuse to ---------------------


def _audited(command: list[str], *, profile: str = "", profiles: dict[str, Any] | None = None) -> models.Config:
    block: dict[str, Any] = {"command": command}
    if profile:
        block["executor_profile"] = profile
    raw = make_config(profiles=profiles)
    raw["security"] = {"dependency_audit": block}
    return models.Config(raw)


def _answering(exit_code: int, output: str) -> Any:
    class _Executor:
        def run(self, spec: Any) -> Any:
            from rein import executors

            return executors.ExecutionResult(exit_code=exit_code, output=output, image_digest="")

    return lambda profile: _Executor()


def test_a_sandboxed_profile_is_refused_before_it_is_launched() -> None:
    """An audit reads a published database and `executors` grants no sandbox egress at all —
    `network_profile` may only be `none`. That configuration cannot answer on any machine, on any
    day, so it is refused rather than left to fail as though the dependencies were what was wrong.
    """
    boxed = {"boxed": {"kind": "oci", "image": "x@sha256:" + "a" * 64}}
    config = _audited(["pip-audit"], profile="boxed", profiles=boxed)
    with pytest.raises(audit.AuditError, match="sandboxed"):
        audit.run(repo_mod.Repo(Path(".")), config)


def test_a_profile_that_is_not_declared_is_not_quietly_the_host() -> None:
    """Running a security answer somewhere other than where it was configured to run would be
    guessing at where it came from."""
    with pytest.raises(audit.AuditError, match="no such profile"):
        audit.run(repo_mod.Repo(Path(".")), _audited(["pip-audit"], profile="ghost"))


def test_a_machine_that_could_not_answer_records_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """ "Could not ask" is not "the answer is bad", and only one of them is a fact about this
    release's dependencies. Recorded as a failed audit it holds gate ⑤ shut in the one place here
    with no dispute route — the same mistake this release fixed for a memory kill."""
    from rein import executors

    monkeypatch.setattr(executors, "for_profile", _answering(1, "pip-audit: Temporary failure in resolving 'pypi.org'"))
    with pytest.raises(audit.AuditError, match="could not be run"):
        audit.run(repo_mod.Repo(Path(".")), _audited(["pip-audit"]))


def test_a_finding_about_the_dependencies_is_recorded_as_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """The other side of the same line: an audit that ran and found something is exactly what the
    gate is asking about, and it is recorded with what it said."""
    from rein import executors

    monkeypatch.setattr(executors, "for_profile", _answering(1, "GHSA-xxxx: urllib3 1.26.4 is vulnerable"))
    record = audit.run(repo_mod.Repo(Path(".")), _audited(["pip-audit"]))

    assert record["passed"] is False
    assert "GHSA-xxxx" in record["summary"]
