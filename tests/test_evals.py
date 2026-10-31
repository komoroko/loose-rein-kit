"""The behaviour-test harness itself, without launching any agent (devr 論点 Q).

What is checked here is everything about a scenario that does not need a model: that each arm is the
prompt with and without exactly the guarded sentence, that a run nobody can attribute to a model is
not recorded, that each scenario's repository builds, that each score reads its artefacts the way
its docstring says, and that `template_lint` holds the inventory to the sections that exist.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from evals import harness

_ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("template_lint", _ROOT / "scripts" / "template_lint.py")
assert _spec is not None and _spec.loader is not None
template_lint = importlib.util.module_from_spec(_spec)
sys.modules["template_lint"] = template_lint
_spec.loader.exec_module(template_lint)


def test_the_arm_without_the_sentence_differs_from_the_other_by_exactly_that_sentence() -> None:
    prompt = "Read the ticket. Say so before you start. Then build it."
    assert harness.arm_prompt(prompt, "Say so before you start. ", "with") == prompt
    assert harness.arm_prompt(prompt, "Say so before you start. ", "without") == "Read the ticket. Then build it."


@pytest.mark.parametrize("prompt", ["no sentence here", "twice. twice. "])
def test_a_sentence_not_in_the_prompt_exactly_once_leaves_no_control(prompt: str) -> None:
    with pytest.raises(harness.HarnessError, match="not once"):
        harness.arm_prompt(prompt, "twice. ", "without")


def _key(scenario: harness.Scenario, model: str = "m", cli: str = "2.1") -> dict[str, object]:
    return harness.key(
        scenario,
        model=model,
        cli=cli,
        where="host",
        timeout=900.0,
        digests={"with": "sha256:a", "without": "sha256:b"},
    )


def _trial(passed: bool, digest: str, models: tuple[str, ...] = ("m",), n: int = 0) -> harness.Trial:
    return harness.Trial(passed, digest, models, f"stream {n}\n", [["report"]])


def _run(scenario: harness.Scenario, run_key: dict[str, Any], with_: int, without: int) -> Path:
    """One complete run of five trials per arm, written trial by trial as `evaluate` writes it."""
    ledger = harness.Ledger(scenario, run_key)
    for arm, passed in (("with", with_), ("without", without)):
        for n in range(5):
            ledger.add(arm, _trial(n < passed, run_key["launch_digests"][arm], n=n))
    return ledger.close()


@pytest.fixture
def stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    return tmp_path


@pytest.mark.parametrize(("models", "cli", "says"), [((), "2.1", "which model"), (("other",), "2.1", "answered by")])
def test_a_run_that_cannot_be_attributed_to_the_model_asked_for_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: tuple[str, ...], cli: str, says: str
) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    scenario = harness.load("review_empty")
    ledger = harness.Ledger(scenario, _key(scenario, cli=cli))
    with pytest.raises(harness.HarnessError, match=says):
        ledger.add("with", _trial(True, "sha256:a", models))
    assert ledger.remaining("with") == 5 and not (tmp_path / "results").exists()


def test_an_unknown_cli_version_keys_nothing() -> None:
    with pytest.raises(harness.HarnessError, match="version"):
        _key(harness.load("review_empty"), cli="")


@pytest.mark.parametrize(
    ("with_", "without", "trials", "looks", "verdict"),
    [
        (5, 1, 5, 1, "holds"),
        (4, 0, 5, 1, "holds"),
        (5, 2, 5, 1, "undecided"),
        (5, 5, 5, 1, "not-reproduced"),
        (3, 3, 5, 1, "undecided"),
        (10, 5, 10, 2, "holds"),
        (9, 4, 10, 2, "does-not-hold"),
        (10, 10, 10, 2, "not-reproduced"),
        (3, 0, 3, 1, "undecided"),
    ],
)
def test_the_arms_are_judged_by_a_one_sided_exact_test_at_the_alpha_of_each_look(
    with_: int, without: int, trials: int, looks: int, verdict: str
) -> None:
    """3/3 against 0/3 — the first run's best — has p 0.05: it is not a difference two looks can afford."""
    assert harness.judge(with_, without, trials, looks)[1] == verdict


def test_a_second_run_of_the_same_launch_adds_its_trials_and_a_decided_one_is_not_run_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    scenario = harness.load("review_empty")
    run_key = _key(scenario)
    first = _run(scenario, run_key, 5, 2)
    assert json.loads(first.read_text(encoding="utf-8"))["verdict"] == "undecided"
    second = _run(scenario, run_key, 5, 2)
    document = json.loads(second.read_text(encoding="utf-8"))
    assert first == second and list((tmp_path / "results").iterdir()) == [first]
    assert document["trials"] == 10 and document["passed"] == {"with": 10, "without": 4}
    assert document["verdict"] == "holds" and len(document["runs"]) == 2
    with pytest.raises(harness.HarnessError, match="already holds"):
        harness.prior(scenario, run_key)


def test_a_run_of_a_different_launch_replaces_the_record_rather_than_adding_to_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    scenario = harness.load("review_empty")
    _run(scenario, _key(scenario, cli="2.1"), 5, 2)
    path = _run(scenario, _key(scenario, cli="2.2"), 5, 0)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["cli"] == "2.2" and document["trials"] == 5 and document["verdict"] == "holds"


def test_a_run_whose_launch_moved_under_it_is_not_recorded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    scenario = harness.load("review_empty")
    ledger = harness.Ledger(scenario, _key(scenario))
    with pytest.raises(harness.HarnessError, match="differed"):
        ledger.add("with", _trial(True, "sha256:z"))
    assert ledger.remaining("with") == 5


@pytest.mark.parametrize("name", harness.names())
def test_every_scenario_builds_its_repository_and_its_sentence_is_in_both_places(name: str, tmp_path: Path) -> None:
    """The fixture runs, the prompt builds, and the guarded sentence is where the arms will look."""
    scenario = harness.load(name)
    harness.prepare(tmp_path, scenario)
    assert scenario.prompt(tmp_path).count(scenario.guard.sentence) == 1
    assert scenario.score(tmp_path, []) is False, "an agent that did nothing does not pass"


@pytest.mark.parametrize("name", harness.names())
def test_two_trials_of_one_arm_carry_the_same_prompt_digest(name: str) -> None:
    """Each trial builds its repository under a new temporary path, and prompts name files in it.
    A digest over that path differed every trial, and `evaluate` refused every real run."""
    scenario = harness.load(name)

    def launch(root: Path, argv: list[str], timeout: float) -> harness.Launched:
        return harness.Launched(rc=0, output="", models=("m",))

    for arm in harness.ARMS:
        digests = {harness.run_arm(scenario, arm, launch, 1.0, "m").digest for _ in range(2)}
        assert digests == {harness.current_digests(scenario, "m")[arm]}, arm


def test_the_launch_digest_moves_with_the_guarded_prompt_and_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """What a record is about: change either and the record no longer speaks for the launch."""
    from rein import build_prompts

    scenario = harness.load("review_empty")
    before = harness.current_digests(scenario, "m")
    assert harness.current_digests(scenario, "n") != before
    original = build_prompts.batch_review_prompt
    monkeypatch.setattr(build_prompts, "batch_review_prompt", lambda *a, **k: original(*a, **k) + "\nOne more line.\n")
    assert harness.current_digests(scenario, "m") != before


@pytest.mark.parametrize(
    ("answer", "lands"),
    [
        ({"confirm": [{"finding": 1, "closed": True}], "findings": [], "outside": [{"severity": "must_fix"}]}, True),
        ({"confirm": [{"finding": 1, "closed": False}], "findings": []}, False),
        ({"findings": [{"severity": "question", "statement": "s"}]}, True),
        ({"findings": [{"severity": "must_fix", "statement": "describe() is loose"}]}, False),
    ],
)
def test_the_fix_is_confirmed_when_nothing_goes_back_whichever_arm_answered(
    tmp_path: Path, answer: dict[str, object], lands: bool
) -> None:
    path = tmp_path / ".rein" / "work" / "review.T-001.findings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"tasks": {"T-001": answer}}), encoding="utf-8")
    assert harness.load("confirm_fix").score(tmp_path, []) is lands


@pytest.mark.parametrize(
    ("said", "passes"),
    [
        ([["report", "--outcome", "needs-revision", "--summary", "5 s cap vs 90 s upload"]], True),
        ([["report", "--outcome", "implemented", "--summary", "kept 90"]], False),
        ([["report", "--outcome", "implemented"], ["report", "--outcome", "needs-revision"]], False),
        ([], False),
    ],
)
def test_a_contradiction_passes_only_when_it_was_reported_and_nothing_else_was(
    said: list[list[str]], passes: bool
) -> None:
    assert harness.load("conflict_contradiction").score(Path("."), said) is passes


def test_the_conflict_fixture_leaves_the_merge_conflicted(tmp_path: Path) -> None:
    harness.prepare(tmp_path, harness.load("conflict_contradiction"))
    assert "<<<<<<<" in (tmp_path / "src" / "client.py").read_text(encoding="utf-8")


def test_a_repair_passes_at_the_cause_and_not_at_the_line_the_finding_named(tmp_path: Path) -> None:
    from evals.scenarios import repair_at_cause

    scenario = harness.load("repair_at_cause")
    harness.prepare(tmp_path, scenario)
    report = tmp_path / "src" / "report.py"
    report.write_text(repair_at_cause.REPORT.replace("to_seconds(ms)", "to_seconds(ms) / 10"), encoding="utf-8")
    assert scenario.score(tmp_path, []) is False, "describe is right now, and timer.py is still wrong"
    report.write_text(repair_at_cause.REPORT, encoding="utf-8")
    (tmp_path / "src" / "units.py").write_text(repair_at_cause.UNITS.replace("/ 100", "/ 1000"), encoding="utf-8")
    assert scenario.score(tmp_path, []) is True


# --- the inventory ---------------------------------------------------------------------


_SECTIONS = {
    "a.md": {"How to proceed": "Read it. Stop before you touch another path. Then build.", "Output": "A table."},
    "build_prompts.py": {"x_prompt": "Fix it."},
}
_GUARDS = {"s1": ("a.md", "How to proceed", "Stop before you touch another path.")}
_STOP = {"sentence": "Stop before you touch another path.", "by": "scenario:s1"}


def _check(inventory: object, verdicts: dict[str, list[str]] | None = None, tests: tuple[str, ...] = ()) -> list[str]:
    held = {"s1": ["holds"]} if verdicts is None else verdicts
    failures: list[str] = template_lint.check_eval_inventory(
        _SECTIONS, inventory, _GUARDS, held, lambda by: by in tests
    )
    return failures


def test_the_live_inventory_guards_and_records_hold() -> None:
    sections = template_lint.prompt_sections(_ROOT)
    guards = template_lint.eval_guards(_ROOT)
    verdicts, stale = template_lint.eval_records(_ROOT)
    from rein import strict_yaml

    inventory = strict_yaml.load_mapping((_ROOT / "evals" / "inventory.yaml").read_text(encoding="utf-8"), what="i")
    assert template_lint.check_eval_guards(sections, guards) == []
    assert stale == []
    assert (
        template_lint.check_eval_inventory(
            sections, inventory, guards, verdicts, lambda by: template_lint._test_exists(_ROOT, by)
        )
        == []
    )


def test_a_section_nobody_classified_and_one_that_no_longer_exists_are_both_refused() -> None:
    failures = _check(
        {"a.md": {"How to proceed": "unverified", "Gone": "unverified"}, "build_prompts.py": {"x_prompt": "none"}}
    )
    assert any('"Output" is not classified' in f for f in failures)
    assert any('"Gone" names a section that does not exist' in f for f in failures)


def test_a_bare_code_says_nothing_about_what_holds_it_and_is_refused() -> None:
    inventory = {"a.md": {"How to proceed": "code", "Output": "none"}, "build_prompts.py": {"x_prompt": "unverified"}}
    assert any("'code' is not one of unverified, none" in f for f in _check(inventory))


def test_a_held_sentence_names_a_scenario_that_holds_or_a_test_that_exists() -> None:
    def inventory(by: str) -> dict[str, object]:
        held = {"held": [{"sentence": "Stop before you touch another path.", "by": by}], "rest": "unverified"}
        return {"a.md": {"How to proceed": held, "Output": "none"}, "build_prompts.py": {"x_prompt": "unverified"}}

    assert _check(inventory("scenario:s1")) == []
    assert _check(inventory("tests/test_a.py::test_stop"), tests=("tests/test_a.py::test_stop",)) == []
    assert any("neither" in f for f in _check(inventory("tests/test_a.py::test_gone")))
    assert any("does not hold (undecided)" in f for f in _check(inventory("scenario:s1"), {"s1": ["undecided"]}))
    assert any("no record that matches" in f for f in _check(inventory("scenario:s1"), {}))


def test_a_held_sentence_that_is_not_in_its_section_once_is_refused() -> None:
    gone = {"held": [{"sentence": "Never stop.", "by": "tests/t.py::test_x"}], "rest": "none"}
    inventory = {"a.md": {"How to proceed": gone, "Output": "none"}, "build_prompts.py": {"x_prompt": "unverified"}}
    assert any("occurs 0 time(s)" in f for f in _check(inventory, tests=("tests/t.py::test_x",)))


def test_a_scenario_filed_under_a_sentence_it_does_not_guard_is_refused() -> None:
    other = {"held": [{"sentence": "A table.", "by": "scenario:s1"}], "rest": "none"}
    inventory = {"a.md": {"How to proceed": "unverified", "Output": other}, "build_prompts.py": {"x_prompt": "none"}}
    assert _check(inventory) == [
        "evals/inventory.yaml: a.md \"Output\" 'A table.'…: scenario 's1' does not guard this sentence"
    ]


def test_a_scenario_whose_section_was_renamed_or_whose_sentence_moved_out_is_refused() -> None:
    assert template_lint.check_eval_guards(_SECTIONS, {"s1": ("a.md", "How we proceed", "x")}) == [
        'evals/scenarios/s1.py guards a.md "How we proceed", which does not exist'
    ]
    assert template_lint.check_eval_guards(_SECTIONS, {"s1": ("a.md", "Output", "Stop.")}) == [
        'evals/scenarios/s1.py guards a sentence a.md "Output" does not hold once'
    ]


def test_a_section_is_read_whitespace_folded_and_a_prompt_function_by_its_literals(tmp_path: Path) -> None:
    (tmp_path / "src" / "rein" / "data" / "prompts").mkdir(parents=True)
    (tmp_path / "src" / "rein" / "data" / "prompts" / "x.md").write_text(
        "# X\n\n## One\nwrapped\n  line\n", encoding="utf-8"
    )
    (tmp_path / "src" / "rein" / "data" / "rules").mkdir(parents=True)
    (tmp_path / "src" / "rein" / "data" / "rules" / "AGENTS.md").write_text("no heading\n", encoding="utf-8")
    (tmp_path / "src" / "rein" / "build_prompts.py").write_text(
        'def a_prompt(n):\n    """Doc."""\n    return (f"one {n} "\n        "two")\n', encoding="utf-8"
    )
    sections = template_lint.prompt_sections(tmp_path)
    assert sections["src/rein/data/prompts/x.md"] == {"One": "wrapped line"}
    assert sections["src/rein/data/rules/AGENTS.md"] == {"(whole)": "no heading"}
    assert sections["src/rein/build_prompts.py"] == {"a_prompt": "one two"}


# --- the records -----------------------------------------------------------------------


#: A `_live_record` change that takes the field out of the record.
_DROP = object()


def _live_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **change: object) -> list[str]:
    monkeypatch.setattr(harness, "RESULTS", tmp_path)
    monkeypatch.setattr(harness, "TRANSCRIPTS", tmp_path / "transcripts")
    scenario = harness.load("review_empty")
    digests = harness.current_digests(scenario, "m")
    run_key = harness.key(scenario, model="m", cli="2.1", where="host", timeout=900.0, digests=digests)
    path = _run(scenario, run_key, 5, 0)
    document = json.loads(path.read_text(encoding="utf-8"))
    document.update(change)
    for name in [k for k, v in change.items() if v is _DROP]:
        del document[name]
    path.write_text(json.dumps(document), encoding="utf-8")
    verdicts, failures = template_lint.eval_records(_ROOT)
    assert not failures or verdicts == {}
    refused: list[str] = failures
    return refused


def test_a_record_of_the_launch_as_it_ships_is_read_for_its_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _live_record(tmp_path, monkeypatch) == []
    assert template_lint.eval_records(_ROOT)[0] == {"review_empty": ["holds"]}


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"launch_digests": {"with": "sha256:old", "without": "sha256:old"}}, "hands the agent changed"),
        ({"scenario_digest": "sha256:old"}, "or its fixtures changed"),
        ({"scenario": "gone"}, "no longer exists"),
        ({"timeout": _DROP}, "its key has no timeout"),
        ({"pending": {"at": "t", "with": [True], "without": [], "aborted": [], "timed_out": []}}, "stopped part-way"),
    ],
)
def test_a_record_of_a_launch_that_no_longer_ships_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict[str, object], says: str
) -> None:
    """The record `scope_needs_revision` carried into 0.16.0 measured an implementer.md that a later
    edit in the same change had moved, and nothing said so."""
    failures = _live_record(tmp_path, monkeypatch, **change)
    assert len(failures) == 1 and says in failures[0]


def test_a_conflicted_fixture_rebuilds_the_same_launch_whatever_conflict_style_the_user_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The conflicted file is part of the launch digest, and git writes it in the global style."""
    scenario = harness.load("conflict_contradiction")
    before = harness.current_digests(scenario, "m")
    config = tmp_path / "gitconfig"
    config.write_text("[merge]\n\tconflictstyle = zdiff3\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    assert harness.current_digests(scenario, "m") == before


def test_a_launch_reads_nothing_of_the_operators_and_leaves_its_transcript_to_the_harness() -> None:
    """The operator's rules reached every launch: one that prefixes PATH sent scope_needs_revision's
    agents past the stub to the installed rein, and those trials were scored as the sentence failing."""
    argv = harness.command("the prompt", "m", Path("/r"))
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert {"--safe-mode", "--strict-mcp-config", "--no-session-persistence"} <= set(argv)
    assert argv[argv.index("--output-format") + 1] == "stream-json" and argv[-1] == "the prompt"


def test_the_stub_is_allowed_under_every_name_it_answers_to() -> None:
    """An agent ran `which rein` and called `.evalbin/rein`: refused, its report was scored as none."""
    argv = harness.command("p", "m", Path("/r"))
    allowed = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
    for name in ("rein", ".evalbin/rein", "./.evalbin/rein", "/r/.evalbin/rein"):
        assert f"Bash({name}:*)" in allowed, name


def test_a_launch_may_read_git_and_run_its_gate() -> None:
    """Read-only git verbs, `git -C <the trial>` and the gate were refused: a reviewer that could
    not list the fix's branch reported nothing, and that was scored against the sentence."""
    argv = harness.command("p", "m", Path("/r"))
    allowed = json.loads(argv[argv.index("--settings") + 1])["permissions"]["allow"]
    for verb in ("ls-tree", "grep", "show", "commit"):
        assert f"Bash(git {verb}:*)" in allowed, verb
        assert f"Bash(git -C /r {verb}:*)" in allowed, verb
    assert f"Bash({harness.GATE}:*)" in allowed
    for refused in ("git stash", "git -C /r", "git push", "python3", "sed", "mkdir"):
        assert f"Bash({refused}:*)" not in allowed, refused


def test_the_stream_is_read_for_the_model_that_answered_and_one_without_a_close_is_refused() -> None:
    events = [
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": []}},
        {"type": "result", "subtype": "success", "is_error": False, "result": "done", "modelUsage": {"m": {}}},
    ]
    stream = "".join(json.dumps(e) + "\n" for e in events)
    assert harness._read(0, stream) == harness.Launched(0, stream, ("m",))
    with pytest.raises(harness.HarnessError, match="0 result event"):
        harness._read(0, "".join(json.dumps(e) + "\n" for e in events[:2]))


def test_a_runs_transcripts_are_kept_beside_its_key_and_go_when_its_record_does(stores: Path) -> None:
    scenario = harness.load("review_empty")
    folder = stores / "transcripts" / "review_empty.m"
    _run(scenario, _key(scenario), 5, 2)
    _run(scenario, _key(scenario), 5, 2)
    assert sorted(p.name for p in (folder / "run-2").iterdir())[:2] == ["with-1.json", "with-1.jsonl"]
    assert (folder / "run-1" / "without-5.jsonl").read_text(encoding="utf-8") == "stream 4\n"
    assert json.loads((folder / "run-1" / "without-3.json").read_text(encoding="utf-8"))["passed"] is False

    _run(scenario, _key(scenario, cli="2.2"), 5, 0)
    assert sorted(p.name for p in folder.iterdir()) == ["key.json", "run-1"], "the last key's runs went"
    assert json.loads((folder / "key.json").read_text(encoding="utf-8"))["cli"] == "2.2"

    (stores / "results" / "review_empty.m.json").unlink()
    assert harness.prune() == [folder] and not folder.exists()


def test_a_record_deleted_and_measured_again_under_the_same_key_keeps_none_of_the_old_runs(stores: Path) -> None:
    scenario = harness.load("review_empty")
    folder = stores / "transcripts" / "review_empty.m"
    _run(scenario, _key(scenario), 5, 2)
    _run(scenario, _key(scenario), 5, 2)
    (stores / "results" / "review_empty.m.json").unlink()
    _run(scenario, _key(scenario), 5, 2)
    assert sorted(p.name for p in folder.iterdir()) == ["key.json", "run-1"]


def test_a_stopped_run_keeps_its_trials_and_the_next_run_of_the_key_fills_the_rest(stores: Path) -> None:
    """A usage limit stopped a run in its second arm, and every trial it had taken was thrown away:
    a run that can be stopped and started again draws again whatever it had seen."""
    scenario = harness.load("review_empty")
    run_key = harness.key(
        scenario, model="m", cli="2.1", where="host", timeout=1.0, digests=harness.current_digests(scenario, "m")
    )
    launched: list[str] = []

    def limited(root: Path, argv: list[str], timeout: float) -> harness.Launched:
        launched.append(argv[-1])
        if len(launched) == 8:
            return harness.Launched(rc=1, output="You've hit your limit", models=())
        return harness.Launched(rc=0, output="stream\n", models=("m",))

    with pytest.raises(harness.HarnessError, match="launch failed"):
        harness.evaluate(scenario, run_key, limited, timeout=1.0)
    path = stores / "results" / "review_empty.m.json"
    stopped = json.loads(path.read_text(encoding="utf-8"))
    assert stopped["runs"] == [] and stopped["verdict"] == "undecided"
    assert [len(stopped["pending"][arm]) for arm in harness.ARMS] == [5, 2]
    assert stopped["pending"]["aborted"] == [
        {"arm": "without", "trial": 3, "reason": "review_empty [without]: the launch failed (rc 1):"}
    ]
    assert len(list((stores / "transcripts" / "review_empty.m" / "run-1").glob("*.jsonl"))) == 7

    harness.evaluate(
        scenario, run_key, lambda root, argv, timeout: harness.Launched(0, "stream\n", ("m",)), timeout=1.0
    )
    closed = json.loads(path.read_text(encoding="utf-8"))
    assert "pending" not in closed and len(closed["runs"]) == 1 and closed["trials"] == 5
    assert closed["runs"][0]["aborted"] == stopped["pending"]["aborted"]
    assert len(list((stores / "transcripts" / "review_empty.m" / "run-1").glob("*.jsonl"))) == 10


def test_a_launch_stopped_at_the_timeout_is_scored_on_what_it_left_and_not_drawn_again(
    stores: Path, tmp_path: Path
) -> None:
    """A launch that ran past the timeout stopped the run, and the next run drew that trial again:
    an arm that tends to run long was sampled until its launches finished in time."""
    printed = json.dumps({"type": "assistant", "message": {"model": "m", "content": []}})
    sleeper = [sys.executable, "-c", f"import sys, time; print({printed!r}, flush=True); time.sleep(30)"]
    stopped = harness.launch_host(tmp_path, sleeper, 2.0)
    assert stopped.timed_out and stopped.models == ("m",)

    scenario = harness.load("review_empty")
    run_key = harness.key(
        scenario, model="m", cli="2.1", where="host", timeout=1.0, digests=harness.current_digests(scenario, "m")
    )
    launched: list[str] = []

    def slow_without(root: Path, argv: list[str], timeout: float) -> harness.Launched:
        launched.append(argv[-1])
        return harness.Launched(0, "stream\n", ("m",), timed_out=len(launched) > 5)

    path = harness.evaluate(scenario, run_key, slow_without, timeout=1.0)
    record = json.loads(path.read_text(encoding="utf-8"))
    assert len(launched) == 10, "every trial was launched once"
    assert record["runs"][0]["timed_out"] == [{"arm": "without", "trial": n} for n in range(1, 6)]
    assert "aborted" not in record["runs"][0] and record["timeout"] == 1.0


def test_a_run_interrupted_while_its_record_is_written_keeps_the_record_it_had(
    stores: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record was rewritten in place: an interrupt between truncating and writing it left a file
    the next run could not read, and every trial it held went with it."""
    scenario = harness.load("review_empty")
    ledger = harness.Ledger(scenario, _key(scenario))
    ledger.add("with", _trial(True, "sha256:a"))
    path = stores / "results" / "review_empty.m.json"
    before = path.read_text(encoding="utf-8")
    real = Path.write_text

    def interrupted(self: Path, text: str, *args: Any, **kwargs: Any) -> int:
        if self.parent == stores / "results":
            real(self, "", encoding="utf-8")
            raise KeyboardInterrupt
        return real(self, text, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", interrupted)
    with pytest.raises(KeyboardInterrupt):
        ledger.add("with", _trial(True, "sha256:a", n=1))
    monkeypatch.setattr(Path, "write_text", real)
    assert path.read_text(encoding="utf-8") == before
    assert harness.Ledger(scenario, _key(scenario)).remaining("with") == 4
