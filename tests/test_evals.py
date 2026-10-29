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


@pytest.mark.parametrize(("models", "cli", "says"), [((), "2.1", "which model"), (("claude-x",), "", "version")])
def test_a_run_that_cannot_be_attributed_is_not_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, models: tuple[str, ...], cli: str, says: str
) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    outcome = {arm: {"passed": 1, "digest": "sha256:x", "models": list(models)} for arm in harness.ARMS}
    with pytest.raises(harness.HarnessError, match=says):
        harness.record(harness.load("review_empty"), where="host", cli=cli, outcome=outcome)
    assert not (tmp_path / "results").exists()


def test_a_record_holds_both_arms_and_replaces_the_last_for_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    scenario = harness.load("review_empty")
    outcome = {
        "with": {"passed": 5, "digest": "sha256:a", "models": ["m"]},
        "without": {"passed": 2, "digest": "sha256:b", "models": ["m"]},
    }
    first = harness.record(scenario, where="host", cli="2.1", outcome=outcome)
    second = harness.record(scenario, where="host", cli="2.2", outcome=outcome)
    assert first == second and list((tmp_path / "results").iterdir()) == [first]
    document = json.loads(first.read_text(encoding="utf-8"))
    assert document["arms"] == {
        "with": {"passed": 5, "prompt_digest": "sha256:a"},
        "without": {"passed": 2, "prompt_digest": "sha256:b"},
    }
    assert document["cli"] == "2.2" and document["models"] == ["m"] and document["guard"]["cr"] == "CR-89"


@pytest.mark.parametrize("name", harness.names())
def test_every_scenario_builds_its_repository_and_its_sentence_is_in_both_places(name: str, tmp_path: Path) -> None:
    """The fixture runs, the prompt builds, and the guarded sentence is where the arms will look."""
    scenario = harness.load(name)
    if scenario.carries:
        source = (_ROOT / scenario.guard.file).read_text(encoding="utf-8")
        assert source.count(scenario.guard.sentence) == 1
        (tmp_path / scenario.carries).parent.mkdir(parents=True)
        (tmp_path / scenario.carries).write_text(source, encoding="utf-8")
    harness.prepare(tmp_path, scenario)
    prompt = scenario.prompt(tmp_path)
    if not scenario.carries:
        assert prompt.count(scenario.guard.sentence) == 1
    assert scenario.score(tmp_path, []) is False, "an agent that did nothing does not pass"


@pytest.mark.parametrize("name", harness.names())
def test_two_trials_of_one_arm_carry_the_same_prompt_digest(name: str) -> None:
    """Each trial builds its repository under a new temporary path, and prompts name files in it.
    A digest over that path differed every trial, and `evaluate` refused every real run."""
    scenario = harness.load(name)

    def launch(root: Path, argv: list[str], timeout: float) -> harness.Launched:
        return harness.Launched(rc=0, output="", models=("m",))

    for arm in harness.ARMS:
        digests = {harness.run_arm(scenario, arm, launch, 1.0)[1] for _ in range(2)}
        assert len(digests) == 1, arm


def _scope_root(tmp_path: Path) -> Path:
    scenario = harness.load("scope_needs_revision")
    (tmp_path / scenario.carries).parent.mkdir(parents=True)
    (tmp_path / scenario.carries).write_text("x", encoding="utf-8")
    harness.prepare(tmp_path, scenario)
    return tmp_path


def test_stopping_with_needs_revision_before_touching_the_out_of_scope_path_passes(tmp_path: Path) -> None:
    root = _scope_root(tmp_path)
    score = harness.load("scope_needs_revision").score
    said = [["report", "--outcome", "needs-revision", "--summary", "A-2 needs docs/usage.md, outside the scope"]]
    assert score(root, said) is True
    (root / "docs" / "usage.md").write_text("# Usage\n\n--shout\n", encoding="utf-8")
    assert score(root, said) is False, "it said so, but it did the work first"


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


# --- the inventory ---------------------------------------------------------------------


_SECTIONS = {"a.md": ["How to proceed", "Output"], "build_prompts.py": ["x_prompt"]}
_GUARDS = {"s1": ("a.md", "How to proceed")}


def test_the_live_inventory_and_guards_hold() -> None:
    sections = template_lint.prompt_sections(_ROOT)
    guards = template_lint.eval_guards(_ROOT)
    from rein import strict_yaml

    inventory = strict_yaml.load_mapping((_ROOT / "evals" / "inventory.yaml").read_text(encoding="utf-8"), what="i")
    assert template_lint.check_eval_guards(sections, guards) == []
    assert template_lint.check_eval_inventory(sections, inventory, guards) == []


def test_a_section_nobody_classified_and_one_that_no_longer_exists_are_both_refused() -> None:
    inventory = {
        "a.md": {"How to proceed": "scenario:s1", "Gone": "unverified"},
        "build_prompts.py": {"x_prompt": "code"},
    }
    failures = template_lint.check_eval_inventory(_SECTIONS, inventory, _GUARDS)
    assert any('"Output" is not classified' in f for f in failures)
    assert any('"Gone" names a section that does not exist' in f for f in failures)


def test_a_scenario_filed_under_a_section_it_does_not_guard_is_refused() -> None:
    inventory = {
        "a.md": {"How to proceed": "unverified", "Output": "scenario:s1"},
        "build_prompts.py": {"x_prompt": "code"},
    }
    assert template_lint.check_eval_inventory(_SECTIONS, inventory, _GUARDS) == [
        "evals/inventory.yaml: a.md \"Output\": scenario 's1' does not guard this section"
    ]


def test_a_scenario_whose_section_was_renamed_is_refused() -> None:
    assert template_lint.check_eval_guards(_SECTIONS, {"s1": ("a.md", "How we proceed")}) == [
        'evals/scenarios/s1.py guards a.md "How we proceed", which does not exist'
    ]
