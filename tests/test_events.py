"""Tests for events.py — the read-only view over the hash-chained audit log.

The behaviour under test is mostly about what the command *refuses* to do. An audit log an
operator can append to or resolve by hand is not evidence, so there are no such verbs and their
absence is asserted here.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from rein import event_chain, events, models
from tests._support import chain, seed_repo


def _seed(tmp_path: Path, *names: str) -> Path:
    seed_repo(tmp_path, events=chain(*names) if names else None)
    return tmp_path


# --- what the CLI no longer offers --------------------------------------------


def test_there_is_no_way_to_append_or_resolve_by_hand() -> None:
    # An audit log a human can write into is not an audit log. Dispositions live in
    # review.yaml and are signed; "resolve" implied a record could be ticked off.
    for gone in ("append_event", "log_escalation", "open_escalations", "rotate_if_large", "refresh_state_view"):
        assert not hasattr(events, gone), f"events.{gone} should not exist"


def test_the_cli_exposes_only_read_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        events.main(["--help"])
    helptext = capsys.readouterr().out
    for verb in ("--render", "--summary", "--verify", "--root", "--cost"):
        assert verb in helptext
    for gone in ("--add", "--resolve", "--refresh-state"):
        assert gone not in helptext


# --- rendering ----------------------------------------------------------------


def test_render_of_an_empty_log() -> None:
    assert events.render([]) == "no events yet"


def test_render_lists_the_chain_in_append_order(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_started", "task_completed")
    assert events.main(["--repo", str(root)]) == 0
    out = capsys.readouterr().out
    assert out.index("cycle_initialized") < out.index("task_started") < out.index("task_completed")
    assert "| 1 |" in out and "| 3 |" in out


def test_summary_counts_kinds_and_reports_the_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "task_completed", "task_completed", "task_failed")
    assert events.main(["--repo", str(root), "--summary"]) == 0
    out = capsys.readouterr().out
    assert "task_completed×2" in out
    assert "chain root: sha256:" in out


def test_summary_names_the_events_awaiting_a_human_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "task_completed", "task_failed", "knowledge_gap")
    events.main(["--repo", str(root), "--summary"])
    out = capsys.readouterr().out
    assert "needing a human decision: 2" in out
    assert "task_failed" in out and "knowledge_gap" in out


def test_root_prints_only_the_digest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized")
    assert events.main(["--repo", str(root), "--root"]) == 0
    assert capsys.readouterr().out.strip().startswith("sha256:")


def test_the_empty_root_is_a_real_digest_not_a_blank(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # "no events yet" and "field absent" must not look the same to a receipt that binds a root.
    root = _seed(tmp_path)
    events.main(["--repo", str(root), "--root"])
    assert capsys.readouterr().out.strip() == event_chain.EMPTY_CHAIN_ROOT


# --- verification -------------------------------------------------------------


def test_verify_passes_on_an_intact_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_completed")
    assert events.main(["--repo", str(root), "--verify"]) == 0
    assert "PASS event-chain" in capsys.readouterr().out


def test_verify_reports_every_defect_and_exits_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _seed(tmp_path, "cycle_initialized", "task_started", "task_completed")
    log = root / ".rein" / "events.ndjson"
    lines = log.read_text(encoding="utf-8").splitlines()
    log.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")  # the middle record removed

    assert events.main(["--repo", str(root), "--verify"]) == 1
    out = capsys.readouterr().out
    assert "FAIL event-chain" in out
    assert "seq_gap" in out or "broken_link" in out
    assert "Restore it from git" in out  # the repair is restore, never rewrite-to-agree


def test_a_damaged_chain_is_not_rendered_as_though_it_were_the_record(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A table drawn from a broken log looks exactly like one drawn from a good log."""
    root = _seed(tmp_path, "cycle_initialized", "task_completed")
    log = root / ".rein" / "events.ndjson"
    log.write_text(log.read_text(encoding="utf-8").replace("demo-cycle", "other-cycle", 1), encoding="utf-8")

    assert events.main(["--repo", str(root)]) == 1
    assert capsys.readouterr().out.strip() == ""


def test_attention_events_are_a_subset_of_the_vocabulary() -> None:
    from rein import models

    assert events.ATTENTION_EVENTS < models.EVENT_VALUES


# --- what is still open -------------------------------------------------------
#
# The retirement rules, at the level they are decided. Two kinds, and the difference matters: a
# task's own later success retires a verdict about that task, while a later event in the chain
# retires a report the chain itself has since undone. Neither touches events.ndjson.


def _chain(*specs: tuple[str, tuple[str, ...]]) -> list[models.Event]:
    built: list[models.Event] = []
    previous: models.Event | None = None
    for name, subjects in specs:
        detail: dict[str, object] | None = {"status": "done"} if name == "task_completed" else None
        linked = event_chain.link(previous, event_chain.make(name, "demo-cycle", subject_ids=subjects, detail=detail))
        built.append(linked)
        previous = linked
    return built


def test_a_later_review_generated_retires_the_failure_that_preceded_it() -> None:
    """A pipeline that failed once and succeeded on the retry left a permanent "waiting for you"
    row, because these two had no retirement condition at all."""
    chain = _chain(("review_failed", ()), ("actual_extraction_failed", ()), ("review_generated", ()))
    assert events.open_attention(chain) == []


def test_a_failure_after_the_last_success_still_stands() -> None:
    """Order is the chain's, not the clock's: only a *later* answer closes anything."""
    chain = _chain(("review_generated", ()), ("review_failed", ()))
    assert [e.event for e in events.open_attention(chain)] == ["review_failed"]


def test_an_event_does_not_supersede_itself() -> None:
    chain = _chain(("plan_invalidated", ()))
    assert [e.event for e in events.open_attention(chain)] == ["plan_invalidated"]


def test_a_task_failed_is_retired_by_that_task_reaching_done() -> None:
    chain = _chain(("task_failed", ("T-001",)))
    assert events.open_attention(chain, {"T-001": "done"}) == []
    assert [e.event for e in events.open_attention(chain, {"T-001": "blocked"})] == ["task_failed"]


def test_a_batch_failure_stands_until_every_task_it_named_is_done() -> None:
    chain = _chain(("task_failed", ("T-001", "T-002")))
    assert [e.event for e in events.open_attention(chain, {"T-001": "done"})] == ["task_failed"]


def test_a_review_failure_is_not_retired_by_a_task_it_happened_to_name() -> None:
    """`review_failed` is not a verdict about a task, so a task status is not authoritative over it."""
    chain = _chain(("review_failed", ("T-001",)))
    assert [e.event for e in events.open_attention(chain, {"T-001": "done"})] == ["review_failed"]


def test_an_aborted_review_asks_for_a_re_run_and_never_for_a_decision() -> None:
    """A supervised run waiting out a session limit filed two attention rows per attempt against
    a condition it was itself already answering. `run_aborted` had drawn this line for the build
    loop; the review pipeline recorded every failure as a decision."""
    chain = _chain(("review_aborted", ()), ("review_aborted", ()))
    assert events.open_attention(chain) == []
    assert "review_aborted" not in events.ATTENTION_EVENTS


def test_one_condition_is_one_row_however_many_times_it_was_recorded() -> None:
    chain = _chain(("review_failed", ()), ("review_failed", ()), ("task_failed", ("T-004",)))
    conditions = events.open_conditions(chain)
    assert [(e.event, seen) for e, seen in conditions] == [("review_failed", 2), ("task_failed", 1)]
    # The newest record of each, because that is the one whose `detail` describes what is true now.
    assert conditions[0][0].seq == max(e.seq for e in chain if e.event == "review_failed")


def test_the_grouping_narrows_the_same_list_open_attention_does() -> None:
    """A condition every occurrence of which has been answered is not a condition."""
    chain = _chain(("review_failed", ()), ("review_failed", ()), ("review_generated", ()))
    assert events.open_attention(chain) == []
    assert events.open_conditions(chain) == []


def test_every_surface_counts_the_same_conditions(tmp_path: Path) -> None:
    """`rein start`'s board said three and `rein events --summary` said thirty-nine about one
    question, because the grouping lived in the status board alone.

    The rule is `open_conditions` and nothing derives its own: the summary's count, the queue's
    rows, and the recommendation's number are the same number or this is a bug.
    """
    from rein import status_api

    chain = _chain(*[("review_failed", ())] * 8, ("task_failed", ("T-004",)))
    conditions = events.open_conditions(chain)
    summary = events.render_summary(list(chain))
    rows = status_api.pending_queue(
        probe_gate=None,
        gate_blockers=None,
        chain_defects=0,
        unsandboxed_profiles=[],
        unsandboxed_build_targets=[],
        attention=conditions,
        task_rows=[],
    )
    assert f"needing a human decision: {len(conditions)}" in summary
    assert len([r for r in rows if r["kind"] == "escalation"]) == len(conditions) == 2
    assert "\u00d78" in summary, "the count that was collapsed is still stated, not hidden"


# --- what a cycle cost --------------------------------------------------------
#
# `run_measured` carries what the provider billed each role. Nothing read it, so "where did the
# tokens go" had no answer inside the repository that had been recording it all along.


def _measured(cycle: str, role: str, tokens: int) -> models.Event:
    paid = {"launches": 1, "measured": True, "input_tokens": tokens, "output_tokens": 1, "cost_usd": 1.0}
    return event_chain.make(
        "run_measured",
        cycle,
        detail={"kind": "build", "run_id": "r", "outcome": "done", "billed_by_role": {role: paid}},
    )


def _archive(root: Path, slug: str, *evts: models.Event) -> Path:
    where = root / "docs/archive" / slug / "rein"
    where.mkdir(parents=True, exist_ok=True)
    path = where / "events.ndjson"
    event_chain.append_lines(path, [event_chain.link(None, evts[0]), *evts[1:]])
    return path


def test_cost_counts_this_cycle_and_the_archived_ones(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`cycle-close` moves the chain that answers the question into `docs/archive/`, so a report
    that read only the live chain would go blank exactly when the comparison becomes interesting.
    """
    seed_repo(tmp_path, events=[event_chain.link(None, _measured("live-cycle", "implementer", 1234))])
    _archive(tmp_path, "2026-01-01-first", _measured("old-cycle", "comparator", 5678))

    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "live-cycle — 1 run(s)" in out
    assert "old-cycle — 1 run(s)" in out
    assert "[docs/archive/2026-01-01-first]" in out
    assert "implementer" in out and "comparator" in out
    # Oldest first: an archive directory is `<YYYY-MM-DD>-<slug>`, and the cycle still open is the
    # end of the trend, not the start of it. Reading the newest bill first is reading a number;
    # reading it last is reading a direction.
    assert out.index("old-cycle") < out.index("live-cycle")


def test_a_damaged_archive_is_named_rather_than_silently_dropped(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Leaving it out quietly would make the totals read as the whole history — the same lie as
    pricing an unmeasured role at zero. One bad archive must also not take the live cycle down."""
    seed_repo(tmp_path, events=[event_chain.link(None, _measured("live-cycle", "implementer", 1234))])
    broken = _archive(tmp_path, "2026-01-01-first", _measured("old-cycle", "comparator", 5678))
    broken.write_text(broken.read_text(encoding="utf-8").replace("5678", "9999"), encoding="utf-8")

    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "live-cycle — 1 run(s)" in out
    assert "old-cycle" not in out
    assert "docs/archive/2026-01-01-first/rein/events.ndjson: the audit chain is damaged" in out


def test_cost_is_not_narrowed_by_since(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A cycle's total computed over a window is not that cycle's total — the reason `--root`
    already ignores `--since`. Spending's axis is the cycle, not the sequence number."""
    first = event_chain.link(None, _measured("live-cycle", "implementer", 1000))
    second = event_chain.link(first, _measured("live-cycle", "implementer", 1000))
    seed_repo(tmp_path, events=[first, second])

    assert events.main(["--cost", "--since", "1", "--repo", str(tmp_path)]) == 0
    assert "live-cycle — 2 run(s)" in capsys.readouterr().out


def test_cost_on_a_repository_that_has_launched_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _seed(tmp_path, "cycle_initialized")
    assert events.main(["--cost", "--repo", str(tmp_path)]) == 0
    assert "no run has recorded what it cost yet" in capsys.readouterr().out


# --- how often the work stopped, asked of the chain ----------------------------


def test_stops_counts_gate_stops_and_distinct_escalations() -> None:
    """The count and the duration of a stop have different availability, and living in one store
    made them look like one question. `notify.Watcher` only runs under `rein ui`, so a cycle driven
    from the terminal records no duration — but every stop that ended is in the chain already."""
    built = _chain(
        ("gate_approved", ("mandate",)),
        ("changes_requested", ("acceptance",)),
        ("gate_approved", ("acceptance",)),
        ("task_started", ("T-1",)),  # nobody stopped for this
    )
    assert events.stops(built) == 3


def test_a_repeated_escalation_is_one_stop() -> None:
    """Eight supervised attempts against one session limit file eight rows and are one thing to
    decide. `open_conditions` groups by `(kind, subjects)` for that reason; counting stops by any
    other rule would make two surfaces answer one question differently."""
    built = _chain(*[("task_failed", ("T-1",))] * 8)
    assert events.stops(built) == 1

    both = _chain(("task_failed", ("T-1",)), ("task_failed", ("T-2",)))
    assert events.stops(both) == 2


def test_a_revision_is_not_counted_on_top_of_the_refusal_that_caused_it() -> None:
    """`/revise` reopens a gate a person has usually just refused, and that refusal is already
    `changes_requested`. `decision_declared` is out for a different reason: five call sites use it
    for salvage branches, the PR-stack ledger and task declarations, so it does not mean one thing."""
    built = _chain(
        ("changes_requested", ("mandate",)),
        ("gate_revised", ("mandate",)),
        ("decision_declared", ("T-1",)),
    )
    assert events.stops(built) == 1


def test_a_failure_the_loop_recovered_from_by_itself_is_not_a_stop() -> None:
    """A stop is a human contact point. The loop failing a task twice and passing on the third
    attempt never reached anybody, and every surface that asks "what awaits you" says so —
    `stops` read raw `ATTENTION_EVENTS` with no retirement and counted it anyway.
    """
    recovered = _chain(("task_failed", ("T-1",)), ("task_failed", ("T-1",)), ("task_completed", ("T-1",)))

    assert events.open_conditions(recovered, events.task_outcomes(recovered)) == []
    assert events.stops(recovered) == 0


def _detailed(*specs: tuple[str, tuple[str, ...], dict[str, object]]) -> list[models.Event]:
    built: list[models.Event] = []
    previous: models.Event | None = None
    for name, subjects, detail in specs:
        linked = event_chain.link(previous, event_chain.make(name, "demo-cycle", subject_ids=subjects, detail=detail))
        built.append(linked)
        previous = linked
    return built


def test_the_causes_are_a_breakdown_of_the_one_stop_count() -> None:
    """CR-41: each stop is asked once why it reached a person, and the answers add up to `stops` —
    a second way of counting would make two figures disagree about one question."""
    built = _detailed(
        ("gate_approved", ("mandate",), {}),
        ("knowledge_gap", ("T-1",), {"kind": "awaiting_operator", "message": "no browser"}),
        ("task_failed", ("T-2",), {"step": "test", "retries_left": 0}),
        ("knowledge_gap", ("T-3",), {"kind": "scope_violation", "message": "x"}),
        ("task_failed", ("T-4",), {"status": "blocked", "escalation": "gate_violation"}),
        ("knowledge_gap", ("T-5",), {"kind": "something-new", "message": "x"}),
        ("task_failed", ("T-6",), {"step": "test"}),
        ("task_completed", ("T-6",), {"status": "done"}),  # recovered by itself: not a stop at all
    )
    causes = events.stop_causes(built)
    assert causes == {
        "decision": 1,
        "precondition": 1,
        "code": 1,
        "plan": 1,
        "boundary": 1,
        events.UNCLASSIFIED: 1,
    }
    assert sum(causes.values()) == events.stops(built)


def test_a_stop_whose_record_names_no_known_cause_is_not_folded_into_another() -> None:
    """A stop moved into a neighbouring column would argue for a change it says nothing about."""
    built = _detailed(("knowledge_gap", ("T-1",), {"message": "the agent declared a gap"}))
    assert events.stop_causes(built) == {events.UNCLASSIFIED: 1}


#: The one module allowed to read the stop count and its causes: the report that prints them.
STOP_READERS = {"observe_cmd"}


def _modules_reading_stops() -> set[str]:
    """Every module under `src/rein` that names `stops` or `stop_causes`, whatever the import form."""
    import ast

    names = frozenset({"stops", "stop_causes"})
    source_root = Path(__file__).resolve().parent.parent / "src" / "rein"
    found: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        if path.stem == "events":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute) and node.attr in names and isinstance(node.value, ast.Name)) or (
                isinstance(node, ast.ImportFrom) and any(a.name in names for a in node.names)
            ):
                found.add(path.stem)
    return found


def test_only_the_report_reads_the_stop_count() -> None:
    """Never a ceiling: a count something consults to decide is the limit 論点 A rejects. A reader
    in a gate, a review or a lens selection fails here."""
    assert _modules_reading_stops() == STOP_READERS


def test_a_task_that_never_came_back_is_still_a_stop() -> None:
    """The other half: retirement is the task's own later success, not the passage of time."""
    assert events.stops(_chain(("task_failed", ("T-1",)))) == 1


# --- how long the work sat, asked of the same chain ----------------------------


def _at(built: list[models.Event], *stamps: str) -> list[models.Event]:
    """The same chain with its timestamps chosen. The digests go stale and nothing minds:
    `stop_durations` reads `ts` and the chain's order, and verification is `event_chain`'s job."""
    return [replace(event, ts=stamp) for event, stamp in zip(built, stamps, strict=True)]


def test_a_stop_lasts_from_the_loops_last_event_to_the_humans() -> None:
    """Nothing is written to record this. The pending gate was already the record of the stop —
    which is why no escalation is written beside one — and the chain is ordered, so the last thing
    the loop did before a person acted is when the work stopped."""
    chain = _at(
        _chain(("review_generated", ()), ("gate_approved", ("acceptance",))),
        "2026-01-05T09:00:00+09:00",
        "2026-01-05T10:30:00+09:00",
    )

    assert events.stop_durations(chain) == [5400.0]


def test_two_gates_answered_in_one_sitting_both_held_the_same_stop() -> None:
    """A cycle's crossings are a fan with no order among them, so they are presented together and
    answered together. Reading the second one's stop from the first one's approval would report it
    as instant — the work had been sitting just as long for both."""
    chain = _at(
        _chain(("task_completed", ("T-1",)), ("gate_approved", ("T-001",)), ("gate_approved", ("T-002",))),
        "2026-01-05T09:00:00+09:00",
        "2026-01-05T10:00:00+09:00",
        "2026-01-05T10:00:05+09:00",
    )

    assert events.stop_durations(chain) == [3600.0, 3605.0]


def test_a_refusal_ends_one_stop_and_the_work_that_follows_begins_the_next() -> None:
    """`changes_requested` is a person acting, so it closes the stop it was the end of. What
    starts the next one is the loop moving again, not the refusal."""
    chain = _at(
        _chain(
            ("review_generated", ()),
            ("changes_requested", ("acceptance",)),
            ("review_generated", ()),
            ("gate_approved", ("acceptance",)),
        ),
        "2026-01-05T09:00:00+09:00",
        "2026-01-05T10:00:00+09:00",
        "2026-01-05T11:00:00+09:00",
        "2026-01-05T11:30:00+09:00",
    )

    assert events.stop_durations(chain) == [3600.0, 1800.0]


def test_a_stop_stamped_before_the_work_it_followed_is_refused_not_clamped(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """These timestamps come from whatever machine wrote each event, so two of them can disagree.
    Read as zero, the disagreement would disappear into the mean, which is the one place nobody
    could find it."""
    chain = _at(
        _chain(("review_generated", ()), ("gate_approved", ("acceptance",))),
        "2026-01-05T10:00:00+09:00",
        "2026-01-05T09:00:00+09:00",
    )

    with caplog.at_level("WARNING"):
        assert events.stop_durations(chain) == []
    assert "stamped before" in caplog.text


def test_an_unreadable_timestamp_loses_its_stop_and_not_the_others(caplog: pytest.LogCaptureFixture) -> None:
    chain = _at(
        _chain(("review_generated", ()), ("gate_approved", ("T-001",)), ("gate_approved", ("acceptance",))),
        "2026-01-05T09:00:00+09:00",
        "not a timestamp",
        "2026-01-05T10:00:00+09:00",
    )

    with caplog.at_level("WARNING"):
        assert events.stop_durations(chain) == [3600.0]
    assert "unreadable timestamp" in caplog.text


def test_a_condition_still_open_has_no_end_to_measure_to() -> None:
    """`stops` counts it — somebody has to deal with it — and it has not finished, so there is no
    span. Only gate stops are timed, and the report says how many it timed for that reason."""
    chain = _chain(("task_started", ("T-1",)), ("task_failed", ("T-1",)))

    assert events.stops(chain) == 1
    assert events.stop_durations(chain) == []


def test_a_chain_that_opens_with_a_gate_stop_times_nothing_before_it() -> None:
    """An archived chain can begin anywhere. There is no earlier event to measure from, and
    inventing one would put the cycle's whole age into its first stop."""
    chain = _at(_chain(("gate_approved", ("mandate",))), "2026-01-05T09:00:00+09:00")

    assert events.stop_durations(chain) == []


def test_the_chain_answers_for_its_own_task_outcomes() -> None:
    """`state.yaml` is the authority while a cycle is live and exactly what an archive lacks. The
    status travels on the event, so one retirement rule can serve the live chain and the archives."""
    chain = _chain(("task_started", ("T-1",)), ("task_completed", ("T-1",)))

    assert events.task_outcomes(chain)["T-1"] == "done"
