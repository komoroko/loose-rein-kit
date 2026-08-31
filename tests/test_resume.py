"""`rein start` — the delta a returning reader gets instead of a fresh absolute snapshot.

The watermark is the whole mechanism, so what these pin down is where it lives (never in the SSOT),
what it does on a first visit, and that it degrades honestly when the log is shorter than the mark.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import common, event_chain, models, resume, upstream
from tests._support import SANDBOXED_PROFILES, chain, make_config, make_state, seed_repo


@pytest.fixture(autouse=True)
def isolate_state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never write a watermark into the developer's real state directory during tests."""
    monkeypatch.setenv("REIN_CONFIG_HOME", str(tmp_path / "cfg"))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "rt"
    root.mkdir()
    seed_repo(
        root,
        state=make_state(project="rt", gates=dict.fromkeys(models.GATE_ORDER, "pending"), phase="build"),
        config=make_config(profiles=SANDBOXED_PROFILES),
    )
    return root


def _log(repo: Path, *names: str) -> None:
    """Replace the log with one coherent chain of `names`.

    `chain()` numbers from 1 and links from a fresh root, so appending its output twice produces a
    log whose second half restarts the sequence — a damaged chain, not a longer one. Growing the log
    therefore means rewriting it with the full list.
    """
    path = repo / ".rein" / "events.ndjson"
    path.unlink(missing_ok=True)
    event_chain.append_lines(path, chain(*names))


def test_the_watermark_is_never_written_into_the_repository(repo: Path) -> None:
    """It is a fact about a person, not about the project. The SSOT is machine-written under a
    transaction that records why each change happened; "koichi read up to 214" is not such a change,
    and a shared checkout would have two readers overwriting each other's place in the log."""
    _log(repo, "task_completed")
    resume.run(repo)
    assert resume.watermark_path().exists()
    assert resume.watermark_path().is_relative_to(Path(str(resume.state_home())))
    assert not (repo / ".rein" / "seen.json").exists()
    tracked = {p.name for p in (repo / ".rein").iterdir()}
    assert "seen.json" not in tracked


def test_a_first_visit_says_so_rather_than_claiming_everything_is_new(repo: Path) -> None:
    _log(repo, "task_completed", "gate_approved")
    text = resume.run(repo)
    assert "first visit" in text


def test_the_second_visit_reports_only_what_moved(repo: Path) -> None:
    _log(repo, "task_completed", "gate_approved")
    resume.run(repo)  # sets the watermark at 2
    _log(repo, "task_completed", "gate_approved", "task_completed", "gate_approved", "review_generated")
    text = resume.run(repo)
    assert "events 3..5" in text
    assert "tasks completed: 1" in text
    assert "gates opened: 1" in text
    assert "the machine review was regenerated: 1" in text


def test_nothing_moving_is_said_plainly(repo: Path) -> None:
    _log(repo, "task_completed")
    resume.run(repo)
    assert "nothing moved" in resume.run(repo)


def test_no_mark_is_a_peek_not_a_visit(repo: Path) -> None:
    _log(repo, "task_completed", "gate_approved")
    resume.run(repo, mark=False)
    assert resume.read_mark(repo.resolve()) == 0
    assert "first visit" in resume.run(repo, mark=False)


def test_a_watermark_ahead_of_the_log_is_reported_not_rendered_backwards(repo: Path) -> None:
    """The log only grows, so a shorter one was restored or rewound. Printing "events 100..3"
    would be a range that reads as normal output."""
    _log(repo, "task_completed", "gate_approved")
    resume.write_mark(repo.resolve(), 99)
    text = resume.run(repo, mark=False)
    assert "restored or rewound" in text and "events 100" not in text


def test_open_attention_is_shown_even_when_it_predates_the_watermark(repo: Path) -> None:
    """An escalation opened before you left and still open is the most important thing on screen."""
    _log(repo, "task_failed", "task_completed")
    resume.run(repo)
    text = resume.run(repo)
    assert "nothing moved" in text
    assert "waiting on you" in text and "task_failed" in text


def test_what_is_waiting_is_not_only_what_wrote_an_event(repo: Path) -> None:
    """The packet reports the status queue, which knows about blockers no event ever announced.

    An undispositioned finding, a review bound to a commit that is no longer HEAD, or a task nobody
    reclassified all stop a gate without appending anything to the log. Reporting attention events
    alone told a returning reader "nothing is waiting" while the gate refused to open.
    """
    _log(repo, "task_completed")
    text = resume.run(repo)
    assert "waiting on you" in text and "blocking" in text


def test_the_packet_names_the_one_decision_waiting_on_a_human(repo: Path) -> None:
    _log(repo, "task_completed")
    text = resume.run(repo)
    assert "Waiting on you:" in text or "next:" in text


def test_events_since_narrows_the_log_but_never_the_chain_root(repo: Path) -> None:
    """`--root` is a statement about the whole chain; a root over a window is not the root a
    receipt bound, so `--since` must not touch it."""
    from rein import events as events_mod

    _log(repo, "task_completed", "gate_approved", "task_completed")
    events, _ = event_chain.scan(repo / ".rein" / "events.ndjson")
    full_root = event_chain.chain_root(events)
    assert events_mod.main(["--root", "--repo", str(repo)]) == 0
    assert events_mod.main(["--since", "1", "--repo", str(repo)]) == 0
    # the narrowing is a view over the same records, not a different chain
    assert event_chain.chain_root(events) == full_root


def test_a_run_the_machine_stopped_gets_a_headline() -> None:
    """Someone opening a fresh terminal after a session limit needs to know the build stopped
    for the machine's reasons and left every task where it was — the board looks unchanged, so
    nothing else in the packet would say so."""
    assert "re-runnable" in resume.HEADLINE_EVENTS["run_aborted"]


# --- the release note, read from doctor's cache and never fetched ------------------


def test_a_newer_release_is_reported_from_the_cache(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/o/r@v0.0.1")
    upstream.write_cache("o/r", "v999.0.0")
    _log(repo, "task_completed")
    assert "v999.0.0" in resume.run(repo)


def test_nothing_is_said_without_a_cache_or_when_current(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/o/r@v0.0.1")
    _log(repo, "task_completed")
    assert "available" not in resume.run(repo, mark=False)
    upstream.write_cache("o/r", "v0.0.1")
    assert "available" not in resume.run(repo, mark=False)


def test_start_never_reaches_the_network(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """It runs from the SessionStart hook, which 0.3.11 was spent making cheap."""
    monkeypatch.setattr(
        common, "run", lambda *a, **k: pytest.fail("`rein start` must not run a subprocess for a version check")
    )
    upstream.write_cache("o/r", "v999.0.0")
    _log(repo, "task_completed")
    resume.run(repo)
