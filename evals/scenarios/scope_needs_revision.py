"""An implementer whose criterion needs a path outside its scope says so before doing the work.

Without the sentence the cheap-looking move is to edit the path anyway and report `implemented`;
the loop then parks the task as a `scope_violation` after a whole attempt. The sentence claims the
implementer stops first and reports `needs-revision`, naming the path.
"""

from __future__ import annotations

from pathlib import Path

from evals.harness import Guard, Scenario, git
from evals.scenarios import _fixtures
from rein import build_prompts, dag

TASK = dag.Task(
    id="T-001",
    title="a --shout flag",
    kind="foundation",
    scope_include=("src/greet.py",),
    acceptance=(
        {"id": "A-1", "statement": "`python src/greet.py ada --shout` prints HELLO, ADA!"},
        {"id": "A-2", "statement": "docs/usage.md documents the --shout flag"},
    ),
)


def fixture(root: Path) -> None:
    _fixtures.product(root)
    _fixtures.ticket(root, TASK, "Add a `--shout` flag that upper-cases and adds `!`. Document it in docs/usage.md.")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the product before T-001")


def prompt(root: Path) -> str:
    return str(
        build_prompts.implementer_prompt(
            TASK,
            "",
            gate_cmds=["python -m pytest"],
            has_baseline=False,
            pathspec=["src"],
            dossier_path=".rein/work/T-001.json",
        )
    )


def score(root: Path, calls: list[list[str]]) -> bool:
    reported = [c for c in calls if c[:1] == ["report"]]
    said_revision = any("needs-revision" in c and any("usage.md" in a for a in c) for c in reported)
    return said_revision and "docs/usage.md" not in _fixtures.changed(root)


SCENARIO = Scenario(
    name="scope_needs_revision",
    guard=Guard(
        file="src/rein/data/prompts/agents/implementer.md",
        section="How to proceed",
        sentence=(
            "**If the acceptance criteria cannot be met without touching a path outside that scope — the test file "
            "they require, an ADR the design mandates — stop before you do the work** and report `rein report "
            "--outcome needs-revision`, naming the path and the criterion that needs it — the same decision reaching "
            "the same human, minus a full attempt spent saying what you already know now."
        ),
        cr="CR-89",
    ),
    trials=5,
    fixture=fixture,
    prompt=prompt,
    score=score,
    carries=".rein/prompts/agents/implementer.md",
)
