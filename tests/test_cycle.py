"""Tests for cycle.py — archiving a finished delta cycle and resetting for the next.

Two properties matter. The archive carries the cycle's **evidence** with its prose (plan,
state, review, event log), because a history of conclusions with no grounds is
not a record. And the reset carries **nothing** forward but the project identity: a gate
status or a task status surviving into a new cycle would be an approval for work that has not
happened.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from rein import cycle, models, strict_yaml
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import chain, make_state, seed_repo

ALL_APPROVED = dict.fromkeys(models.GATE_ENDS, "approved")


def finished_repo(tmp_path: Path, **kwargs: object) -> repo_mod.Repo:
    """A repo whose acceptance gate is approved, each gate carrying a schema-valid receipt."""
    seed_repo(tmp_path, state=make_state(gates=ALL_APPROVED), docs=True, **kwargs)  # type: ignore[arg-type]
    return repo_mod.Repo(tmp_path)


# --- readiness ----------------------------------------------------------------


def test_an_unapproved_acceptance_gate_blocks(tmp_path: Path) -> None:
    seed_repo(tmp_path, state=make_state(gates={"acceptance": "pending"}))
    blockers = cycle.readiness(repo_mod.Repo(tmp_path))
    assert any("acceptance gate is not approved" in b for b in blockers)


def test_a_damaged_chain_blocks(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path, events=chain("cycle_initialized", "task_completed"))
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "x", 1), encoding="utf-8")
    assert any("unreadable log" in b for b in cycle.readiness(repo))


def test_a_finished_cycle_is_ready(tmp_path: Path) -> None:
    assert cycle.readiness(finished_repo(tmp_path)) == []


# --- the archive plan ---------------------------------------------------------


def test_the_plan_carries_docs_and_the_machine_record(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    rows = cycle.plan_close(repo, "payment", "2026-07-23")
    archived = {src for action, src, _ in rows if action == "archive"}

    assert "docs/10-requirements.md" in archived
    # The evidence goes with the prose: archiving conclusions and dropping their grounds
    # would leave a history nobody can re-check.
    assert ".rein/plan.yaml" in archived
    assert ".rein/state.yaml" in archived
    assert ".rein/review.yaml" in archived


def test_the_product_baseline_persists_across_cycles(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    sources = {src for _, src, _ in cycle.plan_close(repo, "payment", "2026-07-23")}
    assert "docs/00-product-brief.md" not in sources
    assert "docs/05-current-state.md" not in sources


def test_the_speculative_work_log_is_archived_with_its_cycle(tmp_path: Path) -> None:
    """It is filled in per cycle and its rows are finalized in that cycle's retrospective.

    Classified as neither, it was archived by nothing and restored by nothing: the next cycle
    opened holding the last one's rows, and `/status` went on naming them as still undecided.
    """
    repo = finished_repo(tmp_path)
    rows = {src: dst for _, src, dst in cycle.plan_close(repo, "payment", "2026-07-23")}

    assert rows["docs/speculative-work.md"] == "docs/archive/2026-07-23-payment/speculative-work.md"


def test_every_scaffold_document_is_classified_one_way_or_the_other(tmp_path: Path) -> None:
    """The two lists are the whole answer, so a document in neither is not a third policy — it is
    a per-cycle log that silently persists, or a persistent file nobody said persists."""
    from rein import data as data_mod

    prefix = len("scaffold/docs/")
    shipped = {rel[prefix:].split("/")[0] for rel, _ in data_mod.iter_files("scaffold/docs")}

    assert shipped <= set(cycle.CYCLE_DOCS) | set(cycle.PERSISTENT_DOCS)


def test_an_absent_item_is_skipped_which_is_what_makes_a_rerun_idempotent(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    (tmp_path / "docs" / "retrospective.md").unlink()
    rows = dict((src, action) for action, src, _ in cycle.plan_close(repo, "p", "2026-07-23"))
    assert rows["docs/retrospective.md"] == "skip"


def test_destinations_are_dated_and_slugged(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    rows = cycle.plan_close(repo, "payment", "2026-07-23")
    assert all(dst.startswith("docs/archive/2026-07-23-payment/") for _, _, dst in rows)


# --- the reset ----------------------------------------------------------------


def test_the_next_state_carries_only_the_project_identity(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    previous = store_mod.Store(repo).read_state()
    assert previous is not None

    fresh = cycle.next_state(previous, "payment-2")
    assert fresh["project"] == previous.project
    assert fresh["cycle_id"] == "payment-2"
    assert "current_phase" not in fresh, "where a cycle stands is derived from the gates"
    assert fresh["plan"] == {"status": "draft"}
    assert fresh["tasks"] == {}
    gates = fresh["gates"]
    assert isinstance(gates, dict)
    assert all(g["status"] == "pending" and g["receipt"] is None for g in gates.values())
    assert models.schema_errors(fresh, "state") == []


# --- the snapshot -------------------------------------------------------------


def test_the_ssot_snapshot_is_taken_once_and_never_overwritten(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    assert cycle.snapshot_ssot(repo) is True
    pristine = tmp_path / ".rein" / "scaffold" / "rein" / "plan.yaml"
    assert pristine.exists()
    before = pristine.read_text(encoding="utf-8")

    (tmp_path / ".rein" / "plan.yaml").write_text("tasks: []\n", encoding="utf-8")
    cycle.snapshot_ssot(repo)
    assert pristine.read_text(encoding="utf-8") == before  # the pristine copy survived


def test_the_per_cycle_documents_are_not_copied_into_the_repository(tmp_path: Path) -> None:
    """They are packaged data, so a per-repository copy could only ever go stale — which is what
    made a document the release began shipping vanish at the first close after an upgrade."""
    repo = finished_repo(tmp_path)
    cycle.snapshot_ssot(repo)
    assert not (tmp_path / ".rein" / "scaffold" / "docs").exists()


def test_a_document_this_release_ships_is_restored_even_in_an_older_repository(tmp_path: Path) -> None:
    """The regression: the snapshot was taken once at `init` and never gained anything, so a
    per-cycle document added by a later release was archived and then silently not restored."""
    from rein import data as data_mod

    repo = finished_repo(tmp_path)
    # A repository initialized before this release: it has the old snapshot directory and no copy
    # of whatever the payload has since added.
    (tmp_path / ".rein" / "scaffold" / "docs").mkdir(parents=True, exist_ok=True)
    for name in cycle.CYCLE_DOCS:
        doc = tmp_path / "docs" / name
        if doc.is_dir():
            shutil.rmtree(doc)
        elif doc.exists():
            doc.unlink()

    restored = cycle._restore(repo)

    prefix = len("scaffold/docs/")
    shipped = {rel[prefix:] for rel, _ in data_mod.iter_files("scaffold/docs")}
    expected = {rel for rel in shipped if rel.split("/")[0] in set(cycle.CYCLE_DOCS)}
    assert {r[len("docs/") :] for r in restored if r.startswith("docs/")} == expected
    assert (tmp_path / "docs" / "speculative-work.md").exists()
    assert (tmp_path / "docs" / "tasks" / "T-template.md").exists()


def test_restoring_never_overwrites_a_document_the_archive_could_not_take(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path)
    kept = tmp_path / "docs" / "retrospective.md"
    kept.write_text("work the git mv could not take\n", encoding="utf-8")

    cycle._restore(repo)

    assert kept.read_text(encoding="utf-8") == "work the git mv could not take\n"


# --- the whole close ----------------------------------------------------------


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.mark.integration
def test_close_archives_resets_and_records(tmp_path: Path) -> None:
    repo = finished_repo(tmp_path, git=True, events=chain("cycle_initialized"))
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    cycle.snapshot_ssot(repo)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")

    assert cycle.main(["--name", "payment", "--repo", str(tmp_path)]) == 0

    archive = next((tmp_path / "docs" / "archive").iterdir())
    assert (archive / "10-requirements.md").exists()
    assert (archive / "rein" / "plan.yaml").exists()
    assert (archive / "rein" / "events.ndjson").exists()

    state = store_mod.Store(repo).read_state()
    assert state is not None
    assert state.cycle_id == "payment"
    assert state.stage == "drafting"
    assert state.approved_gates == ()

    # The closing event is the last entry of the chain being archived; the new chain opens with
    # the cycle that follows it.
    archived_log = (archive / "rein" / "events.ndjson").read_text(encoding="utf-8")
    assert "cycle_closed" in archived_log
    assert [e.event for e in store_mod.Store(repo).read_events()] == ["cycle_initialized"]

    # The fresh scaffolds are back for the next cycle.
    assert (tmp_path / "docs" / "10-requirements.md").exists()


@pytest.mark.integration
def test_the_reset_state_is_schema_valid_and_lands_with_its_event(tmp_path: Path) -> None:
    """The reset used to call `atomic_write` on state.yaml directly, so the schema never saw the
    document and the write was a separate step from the event recording it. Both go through one
    transaction now; the structural half of this is
    `test_store.test_only_the_store_writes_a_machine_written_document`."""
    repo = finished_repo(tmp_path, git=True, events=chain("cycle_initialized"))
    _git(tmp_path, "config", "user.email", "t@e.x")
    _git(tmp_path, "config", "user.name", "T")
    cycle.snapshot_ssot(repo)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "baseline")

    assert cycle.main(["--name", "payment", "--repo", str(tmp_path)]) == 0

    raw = strict_yaml.load_mapping(repo.state.read_text(encoding="utf-8"), what="state.yaml")
    assert models.schema_errors(raw, "state") == []
    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["cycle_initialized"]
    assert events[0].cycle_id == "payment"
    # No journal left behind: the write and its event committed as one unit.
    assert not store_mod.Store(repo).journal.exists()


@pytest.mark.integration
def test_close_refuses_when_not_ready(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path, state=make_state(gates={"acceptance": "pending"}), docs=True, git=True)
    assert cycle.main(["--name", "payment", "--repo", str(tmp_path)]) == 1
    assert "cannot close this cycle" in capsys.readouterr().err


def test_dry_run_writes_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    repo = finished_repo(tmp_path)
    before = store_mod.Store(repo).document_digest("state")
    assert cycle.main(["--name", "payment", "--dry-run", "--repo", str(tmp_path)]) == 0
    assert "dry run" in capsys.readouterr().out
    assert store_mod.Store(repo).document_digest("state") == before


@pytest.mark.parametrize("name", ["Payment Refactor!", "-leading-dash", "яя", "①"])
def test_a_bad_slug_is_refused(tmp_path: Path, name: str) -> None:
    """The check was `slug.replace("-", "").isalnum()`, an approximation of the schema's pattern
    that accepted a leading dash and every Unicode letter `str.isalnum` counts — so `--name -foo`
    got as far as writing a cycle_id that state.yaml and the audit log both reject."""
    finished_repo(tmp_path)
    # `--name=<value>`: argparse would read a leading-dash value as another option and exit itself,
    # which is the right answer but not the one under test here.
    assert cycle.main([f"--name={name}", "--repo", str(tmp_path)]) == 2
