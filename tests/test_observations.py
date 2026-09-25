"""What the harness measures about itself (plan §K).

Several rules here rest on a measurement nobody was taking. These pin the two properties that make
the store worth having: every figure is attached to a claim it could falsify, and nothing reads it
back — a cycle's outcome must not depend on what earlier cycles happened to record, or the same
repository answers differently on another machine.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from rein import event_chain, models, observations
from rein import observe_cmd as observe_mod
from tests._support import seed_repo


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "rein" / observations.STORE_NAME


# --- every figure has a claim it could falsify ----------------------------------


def test_every_kind_states_the_claim_it_tests() -> None:
    """A figure with no claim beside it is one somebody reads as a score. Measured is what would
    move if a design decision here were wrong, never what was easy to collect."""
    assert set(observations.CLAIMS) == set(observations.KINDS)
    for kind, claim in observations.CLAIMS.items():
        assert claim.strip(), kind


def test_an_unknown_kind_is_refused_rather_than_recorded(store: Path) -> None:
    """Closed for the same reason the event vocabulary is: a store anybody can add a key to is one
    nobody can aggregate."""
    assert observations.record("whatever_was_handy", project="p", cycle_id="c") is False
    assert not store.exists()


# --- it is a store, not an input -------------------------------------------------


def test_recording_never_raises_even_when_the_store_cannot_be_written(
    store: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every caller is doing something else — opening a gate, finishing a review. A store that can
    fail a gate would be an input to the thing it is measuring."""
    monkeypatch.setattr("rein.observations.Path.mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c") is False


def test_a_torn_line_is_skipped_rather_than_refusing_the_file(store: Path) -> None:
    """An append-only file written by long-lived processes eventually holds one. Refusing the whole
    file over it would lose every reading before it, and this is material for a judgement — not
    evidence anybody signs."""
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c-1")
    with store.open("a", encoding="utf-8") as handle:
        handle.write('{"kind": "reach_ov\n')
    observations.record("judgement_raised", project="p", cycle_id="c-1", value=3)

    kinds = [entry.kind for entry in observations.read()]
    assert kinds == ["reach_overruled", "judgement_raised"]


def test_a_well_formed_line_carrying_an_unaveragable_value_is_skipped_too(store: Path) -> None:
    """JSON that parses is not a reading that counts. `float(None)` raises, and it was raising
    outside the try — one such line and both `rein observe` and `--prune` fell over, against a
    docstring promising the opposite."""
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="p", cycle_id="c-1")
    with store.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"kind": "waited_seconds", "project": "p", "cycle_id": "c", "value": None}) + "\n")

    assert [entry.kind for entry in observations.read()] == ["reach_overruled"]
    assert observations.prune(1) == 0


def test_recording_a_value_that_is_not_a_number_is_refused_rather_than_raised(store: Path) -> None:
    """ "Never raises" has to survive the caller that hands this a None it computed from a clock
    that was not running. The reading is lost; the gate the caller was opening is not."""
    refused = observations.record(
        "waited_seconds",
        project="p",
        cycle_id="c",
        value=None,  # type: ignore[arg-type]
        arm=observations.ARM_SILENT,
    )
    assert refused is False
    assert observations.read() == []


def test_the_store_holds_counts_and_classes_never_content(store: Path) -> None:
    """It is user-global and crosses projects, so what is in it has to be safe to keep there.
    Whatever needs the content is in that cycle's own archive, which the cycle id finds."""
    observations.record("judgement_raised", project="demo", cycle_id="c-1", value=2, subject="T-004")
    written = json.loads(store.read_text(encoding="utf-8").strip())

    assert set(written) == {"kind", "project", "cycle_id", "value", "at", "subject", "arm"}
    assert isinstance(written["value"], (int, float))


# --- reading them back ------------------------------------------------------------


def test_a_summary_totals_by_kind_and_can_be_scoped_to_one_project(store: Path) -> None:
    observations.record("judgement_raised", project="a", cycle_id="c-1", value=2)
    observations.record("judgement_raised", project="a", cycle_id="c-2", value=4)
    observations.record("judgement_raised", project="b", cycle_id="c-1", value=9)

    everything = observations.summarize(observations.read())
    just_a = observations.summarize(observations.read(), project="a")

    assert everything["judgement_raised"]["total"] == 15
    assert just_a["judgement_raised"] == {"count": 2, "total": 6, "mean": 3}


def test_the_report_carries_no_threshold(store: Path) -> None:
    """A number with a ceiling on it gets managed instead of read — which is what the acceptance
    budget demonstrated here before it was removed."""
    observations.record("acceptance_reopened", project="a", cycle_id="c-1")
    out = observations.render(observations.summarize(observations.read()))

    assert "No thresholds" in out
    assert observations.CLAIMS["acceptance_reopened"] in out


def test_pruning_keeps_the_most_recent(store: Path) -> None:
    for n in range(10):
        observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="a", cycle_id=f"c-{n}")

    assert observations.prune(4) == 6
    kept = observations.read()
    assert [entry.cycle_id for entry in kept] == [f"c-{n}" for n in range(6, 10)]


def test_pruning_below_the_count_drops_nothing(store: Path) -> None:
    observations.record("reach_overruled", arm=observations.ARM_TOO_LOCAL, project="a", cycle_id="c-1")
    assert observations.prune(100) == 0
    assert len(observations.read()) == 1


# --- selection by reach, measured on both sides -----------------------------------


def test_an_armed_kind_refuses_a_reading_with_no_arm(store: Path) -> None:
    """A reading that cannot be placed in the comparison it exists for is not a reading. Filing it
    under "" would pool it with readings that never shared a condition."""
    assert observations.record("reach_overruled", project="p", cycle_id="c") is False
    assert observations.record("waited_seconds", project="p", cycle_id="c", value=1) is False
    assert observations.read() == []


def test_an_unarmed_kind_refuses_an_arm(store: Path) -> None:
    """`notified` against a count of reopened acceptances is a comparison nobody is making."""
    assert observations.record("acceptance_reopened", project="p", cycle_id="c", arm="notified") is False
    assert observations.read() == []


def test_an_arm_belonging_to_another_kind_is_refused(store: Path) -> None:
    """The vocabulary is per kind, not one shared set: a `waited_seconds` arm on a reach reading
    would put it in the notification comparison."""
    assert observations.record("reach_overruled", project="p", cycle_id="c", arm="notified") is False
    assert observations.record("waited_seconds", project="p", cycle_id="c", value=1, arm="too_local") is False
    assert observations.read() == []


def test_the_two_directions_of_a_misjudged_reach_are_never_pooled(store: Path) -> None:
    """One criterion, two ways to be wrong. A pooled figure reads as "the criterion was wrong N
    times" and says nothing about which way to move it."""
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-2", arm=observations.ARM_TOO_MANDATE)

    summary = observations.summarize(observations.read())

    assert summary["reach_overruled/too_local"]["total"] == 2
    assert summary["reach_overruled/too_mandate"]["total"] == 1
    assert "reach_overruled" not in summary


def test_a_reading_written_before_the_kind_was_armed_keeps_its_arm(store: Path) -> None:
    """0.6.0 wrote `reach_overruled` unarmed and `change_request.add` was the only thing that wrote
    it, so those readings are `too_local` — provenance, not a guess. Coercing them to "" instead
    opened a third bucket printed under a claim about arms it did not have, and the missing-arm
    warning, which looks only at real arms, stayed silent about a record that had none."""
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"kind": "reach_overruled", "project": "a", "cycle_id": "c-0", "value": 1.0, "subject": "D-001"})
        + "\n",
        encoding="utf-8",
    )

    entries = observations.read()

    assert [e.arm for e in entries] == [observations.ARM_TOO_LOCAL]
    assert set(observations.summarize(entries)) == {"reach_overruled/too_local"}


def test_a_reading_in_no_arm_of_its_kind_is_dropped_not_pooled(store: Path, caplog: Any) -> None:
    """A reading nobody can place is not a reading. Filing it under "" would pool it into a figure
    whose whole point is that its readings shared a condition."""
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(
        json.dumps({"kind": "waited_seconds", "project": "a", "cycle_id": "c-0", "value": 5.0, "arm": "whenever"})
        + "\n",
        encoding="utf-8",
    )

    with caplog.at_level(logging.WARNING):
        entries = observations.read()

    assert entries == []
    assert "belongs to no arm" in caplog.text


def test_one_sided_selection_by_reach_says_so(store: Path) -> None:
    """The whole reason for the arms: a figure with only the "too loose" side on record only ever
    reads as "ask more", and the criterion exists to ask less. The report names the gap itself
    rather than leaving somebody to remember it."""
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)

    out = observations.render(observations.summarize(observations.read()))

    assert "no readings in: too_mandate" in out
    assert "ended up `local` before the freeze" in out


def test_both_sides_on_record_raises_no_warning(store: Path) -> None:
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_LOCAL)
    observations.record("reach_overruled", project="a", cycle_id="c-1", arm=observations.ARM_TOO_MANDATE)

    assert "no readings in" not in observations.render(observations.summarize(observations.read()))


# --- how often work stopped -------------------------------------------------------


def test_the_stop_count_is_pooled_across_arms_and_carries_its_own_claim(store: Path) -> None:
    """One `waited_seconds` reading per wait, so its count is how often work stopped — which
    falsifies a different claim than the durations do. Whether a channel was configured has nothing
    to do with whether selection by reach settles the number of stops, so the count is not split."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=60, arm=observations.ARM_NOTIFIED)
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=120, arm=observations.ARM_NOTIFIED)
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=30, arm=observations.ARM_SILENT)

    out = observations.render(observations.summarize(observations.read()))

    assert observations.STOP_COUNT_CLAIM in out
    # Three waits, pooled — not two and one.
    assert re.search(r"stops \(timed, every arm\)\s+3", out)


def test_no_stop_count_line_without_waits(store: Path) -> None:
    observations.record("acceptance_reopened", project="a", cycle_id="c-1")

    assert "stops (" not in observations.render(observations.summarize(observations.read()))


def test_the_chained_stop_count_is_printed_beside_the_timed_one_never_instead(store: Path) -> None:
    """They are not the same quantity. The timed count is waits `rein ui` saw, across every project
    in this user-global store; the chained count is human interventions in one repository's audit
    chain, needing no dashboard. A reader who takes them for one number reads the gap as drift."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=60, arm=observations.ARM_NOTIFIED)

    out = observations.render(observations.summarize(observations.read()), chain_stops=4)

    assert re.search(r"stops \(timed, every arm\)\s+1", out)
    assert re.search(r"stops \(this repo, chained\)\s+4", out)
    assert "the two count different things" in out


def test_an_empty_store_still_reports_what_the_chain_counted(store: Path) -> None:
    """The case this figure exists for. A cycle driven from the terminal writes no observation and
    still stopped for a human every time it did — so returning "nothing recorded yet" here would
    withhold the count at the one moment it is the only count there is."""
    out = observations.render({}, chain_stops=5)

    assert re.search(r"stops \(this repo, chained\)\s+5", out)
    assert observations.STOP_COUNT_CLAIM in out
    assert "nothing recorded yet" not in out

    # …and with nothing anywhere, it says so about both rather than only the store.
    both_empty = observations.render({}, chain_stops=0)
    assert "nothing recorded yet" in both_empty
    assert "no stop yet either" in both_empty


# --- how long work sat stopped ----------------------------------------------------


def test_the_missing_arm_is_never_filled_by_turning_the_channel_off(store: Path) -> None:
    """The advice for a one-sided record is the decision about how to obtain a control group,
    written where somebody will act on it. `20-open.md` item 8 settled that the control group is
    not worth degrading the measured thing for — and this line went on telling the one reader who
    has a working channel to unset it. Fixing the arm and leaving the advice left the harness
    recommending what it had just ruled out."""
    advice = observations.ARMS["waited_seconds"].one_sided

    for undo in ("unset", "unsetting", "turn off", "turning it off", "remove `command:`", "without a channel"):
        assert undo not in advice, f"the advice asks the reader to {undo} the thing being measured"
    # And it says why waiting will not necessarily close the gap: the arm is one setting for the
    # whole machine, and a cycle with no dashboard contributes no reading to either side.
    assert "rein ui" in advice


def test_the_two_arms_are_not_presented_as_a_comparison_to_complete(store: Path) -> None:
    """Item 8 settled here. The arm is one setting for the whole machine, so the columns are two
    conditions that were spent under, not an assignment anybody made; the only way to make them
    comparable on purpose is to stop notifying somebody, which is measuring by damaging what is
    measured. A figure that keeps calling itself an unfinished comparison leaves a person
    arranging their work around one that cannot become identifiable however they arrange it.

    The phrase is the canary, not the prose: what must not come back is the record presenting the
    effect of a notification as something it settles."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=60, arm=observations.ARM_NOTIFIED)
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=90, arm=observations.ARM_SILENT)

    out = observations.render(observations.summarize(observations.read()))

    # Printed even when both arms are on record, which is exactly when it would be misread.
    assert "never a controlled comparison" in out
    assert "no readings in" not in out


def test_the_chained_stop_time_sits_beside_the_timed_one_and_is_not_pooled(store: Path) -> None:
    """Two different spans. `waited_seconds` starts when the decision became derivable, which only
    a running watcher sees; the chained one starts at the loop's last event, so it also holds
    whatever somebody had to fix before the gate would open. A single mean would answer neither."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=120, arm=observations.ARM_NOTIFIED)

    out = observations.render(observations.summarize(observations.read()), chain_stops=3, chain_stopped=[600.0, 1800.0])

    assert "waited_seconds/notified" in out and "mean 2.0 min" in out
    assert re.search(r"stopped \(this repo, chained\)\s+2 stops, mean 20.0 min", out)
    assert observations.STOP_TIME_CLAIM in out
    assert "the two measure different spans" in out


def test_the_chained_stop_time_carries_its_claim_with_an_empty_store(store: Path) -> None:
    """The case it exists for: a cycle driven from the terminal records no wait at all, and the
    chain still knows how long the work sat."""
    out = observations.render({}, chain_stops=2, chain_stopped=[300.0])

    assert "nothing recorded yet" not in out
    assert observations.STOP_TIME_CLAIM in out
    # Nothing to compare it against, so the note that distinguishes the two spans stays off.
    assert "the two measure different spans" not in out


def test_the_stop_count_has_no_ceiling_anywhere(store: Path) -> None:
    """The report says so. `test_only_these_modules_read_the_store` is what makes it true."""
    observations.record("waited_seconds", project="a", cycle_id="c-1", value=1, arm=observations.ARM_SILENT)

    out = observations.render(observations.summarize(observations.read()))

    assert "never a ceiling" in out
    assert "No thresholds, and none are coming" in out


def test_a_terminal_only_cycle_reports_both_chained_figures(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """End to end, on the case that motivated it: no dashboard was ever started, so the store is
    empty, and the chain still says how often the work stopped and for how long each time."""
    stamps = [
        ("plan_frozen", (), "2026-01-05T09:00:00+09:00"),
        ("gate_approved", ("mandate",), "2026-01-05T09:40:00+09:00"),
        ("task_completed", ("T-1",), "2026-01-05T11:00:00+09:00"),
        ("gate_approved", ("T-001",), "2026-01-05T13:00:00+09:00"),
    ]
    built: list[models.Event] = []
    previous: models.Event | None = None
    for name, subjects, ts in stamps:
        unstamped = event_chain.make(name, "demo-cycle", subject_ids=subjects)
        previous = event_chain.link(previous, replace(unstamped, ts=ts))
        built.append(previous)
    repo_root = tmp_path / "work"
    seed_repo(repo_root, events=built)

    assert observe_mod.main(["--repo", str(repo_root)]) == 0

    out = capsys.readouterr().out
    assert re.search(r"stops \(this repo, chained\)\s+2", out)
    # 40 minutes for the mandate, 120 for the crossing.
    assert re.search(r"stopped \(this repo, chained\)\s+2 stops, mean 80.0 min", out)


def test_another_projects_readings_do_not_borrow_this_repositorys_chain(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`--project` names a project in a user-global store; the chained figures are one
    repository's audit chain. Printing them together under a heading that says "this repo" puts
    two scopes in one table and reads as one."""
    built = [
        event_chain.link(
            None,
            replace(
                event_chain.make("gate_approved", "demo-cycle", subject_ids=("mandate",)),
                ts="2026-01-05T09:00:00+09:00",
            ),
        )
    ]
    repo_root = tmp_path / "work"
    seed_repo(repo_root, events=built)
    observations.record("waited_seconds", project="elsewhere", cycle_id="c", value=60.0, arm=observations.ARM_SILENT)

    assert observe_mod.main(["--repo", str(repo_root), "--project", "elsewhere"]) == 0

    out = capsys.readouterr().out
    assert "project elsewhere" in out
    assert "chained" not in out


def test_the_chain_is_read_when_the_project_named_is_this_repositorys(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Naming the project explicitly must not cost the figures when it is the same scope."""
    built = [
        event_chain.link(
            None,
            replace(
                event_chain.make("gate_approved", "demo-cycle", subject_ids=("mandate",)),
                ts="2026-01-05T09:00:00+09:00",
            ),
        )
    ]
    repo_root = tmp_path / "work"
    seed_repo(repo_root, events=built)

    assert observe_mod.main(["--repo", str(repo_root), "--project", repo_root.name]) == 0

    assert re.search(r"stops \(this repo, chained\)\s+1", capsys.readouterr().out)


def test_a_tampered_chain_yields_no_figures_rather_than_wrong_ones(
    store: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Both figures come off a log whose own check has to pass first. A count assembled from a
    chain that failed verification is the one thing these must not be."""
    built = [event_chain.link(None, event_chain.make("gate_approved", "demo-cycle", subject_ids=("mandate",)))]
    repo_root = tmp_path / "work"
    seed_repo(repo_root, events=[replace(built[0], ts="2026-01-05T09:00:00+09:00")])

    assert observe_mod.main(["--repo", str(repo_root)]) == 0

    assert "chained" not in capsys.readouterr().out


#: The two places allowed to read observations back, and what each reads them for. Both print to a
#: person and neither returns a value to a caller that decides anything: `observe_cmd` is the report
#: itself, `resume` says how many readings arrived since this person last looked.
READERS = {"observe_cmd", "resume"}

#: Reading the store. Recording into it is `record`, which every phase may call.
_READS = frozenset({"read", "summarize", "render"})


def _modules_reading_the_store() -> set[str]:
    """Every module under `src/rein` that calls one of `_READS` on `observations`."""
    import ast

    source_root = Path(__file__).resolve().parent.parent / "src" / "rein"
    found: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        if path.name == "observations.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or node.attr not in _READS:
                continue
            if isinstance(node.value, ast.Name) and node.value.id == "observations":
                found.add(path.stem)
    return found


def test_only_these_modules_read_the_store() -> None:
    """The invariant the whole design of `observations` rests on, fixed against the source.

    A figure becomes a ceiling when something consults it to decide. `00-concept.md` argues the
    count cannot become one *because there is no path by which it could* — a property about every
    future edit, which a test asserting two strings in a report does not hold. So read the source:
    a `read()` that appears in `approve`, `build_loop`, `revise` or `review` fails here, and the
    person who added it decides whether the guarantee or the caller goes.
    """
    assert _modules_reading_the_store() == READERS


def test_the_judging_paths_only_ever_record() -> None:
    """Named separately because these four are the ones the guarantee is about.

    `_modules_reading_the_store` would catch them, but only as a set that stopped matching. This
    says which callers were meant: the gate, the build, the roll back, and the change request.
    """
    judging = {"approve", "build_loop", "revise", "change_request"}

    assert judging & _modules_reading_the_store() == set()


def test_the_chained_count_is_printed_with_its_causes_underneath() -> None:
    """CR-41: the breakdown sits under the one count it breaks down, largest first, with the claim
    that says what splitting it is for."""
    out = observations.render({}, chain_stops=5, chain_causes={"code": 1, "decision": 3, "precondition": 1})
    lines = out.splitlines()
    at = lines.index(next(line for line in lines if line.startswith("stops (this repo, chained)")))
    assert [line.split()[0] for line in lines[at + 1 : at + 4]] == ["decision", "code", "precondition"]
    assert observations.STOP_CAUSE_CLAIM in out
