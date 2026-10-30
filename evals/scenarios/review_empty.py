"""A reviewer handed a sound change answers with an empty list rather than inventing a finding.

A reviewer reads its job as finding something, and a nit dressed as a `must_fix` costs an
implementer round for nothing. The sentence claims that, told an empty list is a real answer, it
writes one when the change is sound.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.harness import GATE, Guard, Scenario, git
from evals.scenarios import _fixtures
from rein import build_prompts, dag

TASK = dag.Task(
    id="T-001",
    title="a --shout flag",
    kind="foundation",
    scope_include=("src/greet.py", "tests/test_greet.py"),
    acceptance=({"id": "A-1", "statement": "`greet('ada', shout=True)` returns `HELLO, ADA!`"},),
)

SOUND = """import sys


def greet(name: str, loud: bool = False, shout: bool = False) -> str:
    text = f"hello, {name}"
    if shout:
        return text.upper() + "!"
    return text.upper() if loud else text


if __name__ == "__main__":
    print(greet(sys.argv[1], loud="--loud" in sys.argv, shout="--shout" in sys.argv))
"""

TEST = """from src.greet import greet


def test_shout_upper_cases_and_exclaims() -> None:
    assert greet("ada", shout=True) == "HELLO, ADA!"


def test_without_shout_nothing_changes() -> None:
    assert greet("ada") == "hello, ada"
    assert greet("ada", loud=True) == "HELLO, ADA"
"""

FINDINGS = ".rein/work/review.T-001.findings.json"


def fixture(root: Path) -> None:
    _fixtures.product(root)
    _fixtures.ticket(root, TASK, "Add `shout`: upper-case and append `!`. Nothing else changes.")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the product before T-001")
    git(root, "checkout", "-q", "-b", "build/T-001")
    (root / "src" / "greet.py").write_text(SOUND, encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_greet.py").write_text(TEST, encoding="utf-8")
    git(root, "add", "src", "tests")
    git(root, "commit", "-q", "-m", "T-001: a shout flag")
    git(root, "checkout", "-q", "main")


def prompt(root: Path) -> str:
    return str(
        build_prompts.batch_review_prompt(
            [build_prompts.ReviewSubject(TASK, "build/T-001", "git diff main...build/T-001", ".rein/work/T-001.json")],
            gate_cmds=[GATE],
            findings_path=str(root / FINDINGS),
            reviews=("adversarial",),
            questions={},
        )
    )


def score(root: Path, calls: list[list[str]]) -> bool:
    path = root / FINDINGS
    if not path.exists():
        return False
    entry = json.loads(path.read_text(encoding="utf-8")).get("tasks", {}).get("T-001")
    return isinstance(entry, dict) and not [f for f in entry.get("findings", []) if f.get("severity") == "must_fix"]


SCENARIO = Scenario(
    name="review_empty",
    guard=Guard(
        file="src/rein/build_prompts.py",
        section="batch_review_prompt",
        sentence=(
            "An empty list is a real answer, and the right one when the change is sound: inventing a finding to "
            "look thorough costs an implementer round for nothing."
        ),
        cr="CR-89",
    ),
    trials=5,
    fixture=fixture,
    prompt=prompt,
    score=score,
)
