"""Tests for store.py — the transactional write path (plan §30.3).

The invariants under test: a mutation and its audit event land together, a concurrent writer
cannot silently win, an interrupted transaction recovers in the direction that preserves the
audit record, and the runtime directory refuses to be somewhere unsafe.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from rein import models, store
from rein import repo as repo_mod

STATE: dict[str, Any] = {
    "project": "demo",
    "cycle_id": "demo-cycle",
    "current_phase": "build",
    "gates": {
        "requirements": {"status": "pending", "receipt": None},
        "design": {"status": "pending", "receipt": None},
        "tasks": {"status": "pending", "receipt": None},
        "build": {"status": "pending", "receipt": None},
        "release": {"status": "pending", "receipt": None},
    },
    "plan": {"status": "draft"},
}


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    """A bare repo. The autouse conftest fixture already points XDG_RUNTIME_DIR at tmp_path."""
    (tmp_path / ".rein").mkdir()
    return repo_mod.Repo(tmp_path)


# --- XDG resolution -----------------------------------------------------------


def test_config_and_cache_fall_back_when_xdg_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unset is the common case on a plain login shell; a tool that only works when these
    # happen to be exported works on nobody's machine but the author's.
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    assert store.config_home() == Path.home() / ".config"
    assert store.cache_home() == Path.home() / ".cache"


def test_relative_xdg_value_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    # The spec says a relative value is invalid and must be ignored.
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    assert store.config_home() == Path.home() / ".config"


def test_runtime_fallback_reports_itself_as_less_private(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    _, private = store.runtime_home()
    assert private is False


def test_worktree_and_canonical_checkout_share_a_runtime_dir(tmp_path: Path) -> None:
    # The bug this prevents: a per-worktree `.rein/` lock inode would let two leaves
    # each hold "the" lock, and a leaf's decisions died with the worktree (plan §11.1).
    canonical = repo_mod.Repo(tmp_path / "main")
    leaf = repo_mod.Repo(tmp_path / "wt" / "leaf")
    for r in (canonical, leaf):
        r._cache["git_common_dir"] = tmp_path / "main" / ".git"
    assert store.runtime_dir(canonical) == store.runtime_dir(leaf)


# --- private directory safety -------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_symlinked_runtime_dir_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.mkdir()
    link = tmp_path / "runtime"
    os.symlink(target, link)
    with pytest.raises(store.StoreError, match="symlink"):
        store.ensure_private_dir(link)


def test_runtime_dir_is_created_0700(tmp_path: Path) -> None:
    created = store.ensure_private_dir(tmp_path / "rt")
    assert (created.stat().st_mode & 0o777) == 0o700


def test_loose_permissions_are_tightened(tmp_path: Path) -> None:
    path = tmp_path / "rt"
    path.mkdir(mode=0o755)
    assert (store.ensure_private_dir(path).stat().st_mode & 0o777) == 0o700


# --- atomic write -------------------------------------------------------------


def test_atomic_write_replaces_and_leaves_no_temp(tmp_path: Path) -> None:
    directory = tmp_path / "docs"
    target = directory / "doc.yaml"
    store.atomic_write(target, b"first", mode=0o644)
    store.atomic_write(target, b"second", mode=0o644)
    assert target.read_bytes() == b"second"
    assert [p.name for p in directory.iterdir()] == ["doc.yaml"]


def test_atomic_write_cleans_up_its_temp_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    directory = tmp_path / "docs"
    directory.mkdir()
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.atomic_write(directory / "doc.yaml", b"x")
    assert list(directory.iterdir()) == []  # the temp file is not left behind


# --- transactions -------------------------------------------------------------


def test_write_and_event_land_together(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle", actor="alice")

    assert st.read_state().current_phase == "build"  # type: ignore[union-attr]
    events = st.read_events()
    assert [e.event for e in events] == ["cycle_initialized"]
    assert events[0].seq == 1


def test_a_state_change_without_an_event_is_refused(repo: repo_mod.Repo) -> None:
    # An unexplained mutation is exactly what the audit chain exists to prevent.
    st = store.Store(repo)
    with pytest.raises(store.StoreError, match="must record why"):
        with st.transaction() as tx:
            tx.write("state", STATE)
    assert not repo.state.exists()


def test_an_event_alone_is_allowed(repo: repo_mod.Repo) -> None:
    # The reverse is fine: recording that something happened without changing a document.
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.append("knowledge_gap", cycle_id="demo-cycle", detail={"note": "unclear"})
    assert len(st.read_events()) == 1


def test_an_exception_inside_the_block_writes_nothing(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with pytest.raises(RuntimeError, match="boom"):
        with st.transaction() as tx:
            tx.write("state", STATE)
            tx.append("cycle_initialized", cycle_id="demo-cycle")
            raise RuntimeError("boom")
    assert not repo.state.exists()
    assert st.read_events() == []


def test_a_staged_document_that_fails_its_schema_is_refused(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with pytest.raises(models.DocumentError, match="current_phase"):
        with st.transaction() as tx:
            tx.write("state", {**STATE, "current_phase": "somewhere"})


def test_stale_write_is_refused(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle")

    stale = st.document_digest("state")
    # Somebody else commits in between.
    with st.transaction() as tx:
        tx.write("state", {**STATE, "current_phase": "verify"})
        tx.append("task_completed", cycle_id="demo-cycle")

    with pytest.raises(store.StaleWriteError, match="changed since it was read"):
        with st.transaction() as tx:
            tx.write("state", {**STATE, "current_phase": "done"}, expect_digest=stale)
            tx.append("task_completed", cycle_id="demo-cycle")
    assert st.read_state().current_phase == "verify"  # type: ignore[union-attr]


def test_read_digest_is_taken_from_what_the_caller_read(repo: repo_mod.Repo) -> None:
    """The regression that made the whole check a no-op: callers passed
    `store.document_digest(...)` evaluated *inside* the transaction, which re-read the file and
    compared it against itself. `read_digest` takes it from the object in hand instead."""
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle")

    was = st.read_state()
    assert was is not None
    seen = store.read_digest(was)
    assert seen == st.document_digest("state")

    with st.transaction() as tx:  # somebody else commits between the read and the write
        tx.write("state", {**STATE, "current_phase": "verify"})
        tx.append("task_completed", cycle_id="demo-cycle")

    with pytest.raises(store.StaleWriteError):
        with st.transaction() as tx:
            tx.write("state", {**STATE, "current_phase": "done"}, expect_digest=seen)
            tx.append("task_completed", cycle_id="demo-cycle")
    assert st.read_state().current_phase == "verify"  # type: ignore[union-attr]


def test_read_digest_of_an_absent_document_is_empty() -> None:
    assert store.read_digest(None) == ""


def test_retry_on_stale_re_runs_the_operation(repo: repo_mod.Repo) -> None:
    """Two leaves touching two different task entries genuinely compose, so losing the race
    means re-read and re-apply — not drop the record."""
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle")

    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        state = st.read_state()
        assert state is not None
        seen = store.read_digest(state)
        if len(attempts) == 1:  # a competing commit lands after our read
            with st.transaction() as tx:
                tx.write("state", {**STATE, "current_phase": "verify"})
                tx.append("task_completed", cycle_id="demo-cycle")
        with st.transaction() as tx:
            tx.write("state", {**state.raw, "current_phase": "done"}, expect_digest=seen)
            tx.append("task_completed", cycle_id="demo-cycle")
        return "ok"

    assert store.retry_on_stale(operation) == "ok"
    assert len(attempts) == 2


def test_retry_on_stale_gives_up_and_raises(repo: repo_mod.Repo) -> None:
    def always_stale() -> None:
        raise store.StaleWriteError("nope")

    with pytest.raises(store.StaleWriteError):
        store.retry_on_stale(always_stale, attempts=2)


def test_projected_chain_root_matches_the_root_after_commit(repo: repo_mod.Repo) -> None:
    """What lets a gate receipt record the root its own approval results in."""
    from rein import event_chain

    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle")

    with st.transaction() as tx:
        tx.append("gate_approved", cycle_id="demo-cycle", subject_ids=["requirements"])
        projected = tx.projected_chain_root()
        assert projected != st.chain_root()  # the chain does move; that was the bug

    assert st.chain_root() == projected
    assert event_chain.verify_root(st.read_events(), projected)


def test_document_digest_survives_a_reformat(repo: repo_mod.Repo) -> None:
    # The concurrency check compares parsed content, so re-indenting a file is not an edit.
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.write("state", STATE)
        tx.append("cycle_initialized", cycle_id="demo-cycle")
    before = st.document_digest("state")
    repo.state.write_text(repo.state.read_text(encoding="utf-8").replace("\n", "\n\n"), encoding="utf-8")
    assert st.document_digest("state") == before


def test_lock_is_exclusive(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    with store.FileLock(st.store_lock):
        with pytest.raises(store.LockUnavailableError, match="another rein process"):
            with store.FileLock(st.store_lock):
                pass


def test_events_appended_across_transactions_keep_one_chain(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    for _ in range(3):
        with st.transaction() as tx:
            tx.append("task_completed", cycle_id="demo-cycle")
    events = st.read_events()
    assert [e.seq for e in events] == [1, 2, 3]
    assert events[2].prev_event_digest == events[1].event_digest


def test_events_of_one_transaction_share_a_tx_id(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.append("task_started", cycle_id="demo-cycle")
        tx.append("task_completed", cycle_id="demo-cycle")
    tx_ids = {e.tx_id for e in st.read_events()}
    assert len(tx_ids) == 1


# --- journal recovery ---------------------------------------------------------


def test_recovery_discards_a_prepared_transaction(repo: repo_mod.Repo) -> None:
    # Nothing was replaced yet, so there is nothing to preserve: roll back.
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    orphan = st.runtime / "orphan.tmp"
    orphan.write_text("garbage", encoding="utf-8")
    st._write_journal({"tx_id": "abc", "phase": "prepared", "temp_paths": {"state": str(orphan)}})

    with st.transaction() as tx:
        tx.append("cycle_initialized", cycle_id="demo-cycle")

    assert not orphan.exists()
    assert not st.journal.exists()


def test_recovery_rolls_a_files_replaced_transaction_forward(repo: repo_mod.Repo) -> None:
    # The documents are already in place; the audit event is missing. It must be appended,
    # never dropped — a state change with no event is the invisible mutation we forbid.
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    store.atomic_write(repo.state, store.dump_yaml(STATE), mode=0o644)

    pending = models.Event(
        seq=0,
        id="11111111-1111-4111-8111-111111111111",
        tx_id="22222222-2222-4222-8222-222222222222",
        ts="2026-07-23T18:10:00+09:00",
        event="gate_approved",
        cycle_id="demo-cycle",
        actor="alice",
    )
    st._write_journal({"tx_id": pending.tx_id, "phase": "files_replaced", "event_payloads": [pending.to_mapping()]})

    with st.transaction() as tx:
        tx.append("task_completed", cycle_id="demo-cycle")

    events = st.read_events()
    assert [e.event for e in events] == ["gate_approved", "task_completed"]
    assert events[0].actor == "alice"


def test_recovery_does_not_duplicate_an_already_appended_event(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    with st.transaction() as tx:
        tx.append("gate_approved", cycle_id="demo-cycle")
    appended = st.read_events()[0]
    st._write_journal({"tx_id": appended.tx_id, "phase": "files_replaced", "event_payloads": [appended.to_mapping()]})

    with st.transaction() as tx:
        tx.append("task_completed", cycle_id="demo-cycle")

    assert [e.event for e in st.read_events()] == ["gate_approved", "task_completed"]


def test_recovery_clears_an_event_appended_journal(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    st._write_journal({"tx_id": "abc", "phase": "event_appended", "event_payloads": []})
    with st.transaction() as tx:
        tx.append("task_completed", cycle_id="demo-cycle")
    assert not st.journal.exists()
    assert len(st.read_events()) == 1


def test_an_unknown_journal_phase_fails_closed(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    st._write_journal({"tx_id": "abc", "phase": "half_done"})
    with pytest.raises(store.StoreError, match="unknown phase"):
        with st.transaction():
            pass


def test_a_corrupt_journal_fails_closed(repo: repo_mod.Repo) -> None:
    st = store.Store(repo)
    store.ensure_private_dir(st.runtime)
    st.journal.write_text("{not json", encoding="utf-8")
    with pytest.raises(store.StoreError, match="journal is corrupt"):
        with st.transaction():
            pass


def test_reads_refuse_a_damaged_chain(repo: repo_mod.Repo) -> None:
    from rein import event_chain

    st = store.Store(repo)
    with st.transaction() as tx:
        tx.append("task_completed", cycle_id="demo-cycle")
    repo.events.write_text(repo.events.read_text(encoding="utf-8").replace("demo-cycle", "other-cycle"), "utf-8")
    with pytest.raises(event_chain.ChainError):
        st.read_events()


def test_only_the_store_writes_a_machine_written_document() -> None:
    """`gate_guard` rule 1 denies an *agent* hand-editing state.yaml / review.yaml / events.ndjson
    because "written only inside a Central Store transaction" — this is the same claim held against
    rein's own code, which is where it was actually broken.

    `cycle-close` reset state.yaml with a bare `atomic_write`, so the reset skipped the schema
    (`--name ①` reached disk) and landed separately from the event recording it. Only the store
    writes those three, and only `tx.write` reaches them, which is what makes the schema check and
    the journal unavoidable rather than customary.
    """
    import ast
    import pathlib

    machine_written = ("state", "review", "events")
    offenders: list[str] = []
    for path in sorted(pathlib.Path("src/rein").rglob("*.py")):
        if path.name == "store.py":  # the one module whose job this is
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or "atomic_write" not in ast.unparse(node.func):
                continue
            target = ast.unparse(node.args[0]) if node.args else ""
            if any(target.endswith(f".{name}") for name in machine_written):
                offenders.append(f"{path}:{node.lineno} atomic_write({target})")
    assert not offenders, "these write a machine-written document outside the store: " + "; ".join(offenders)
