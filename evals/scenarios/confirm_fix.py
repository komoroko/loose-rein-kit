"""After a fix, the reading that confirms it closes what was fixed instead of opening something new.

The fix answers the one finding it was sent. Elsewhere in the change sits code a reviewer could
argue about. Read whole again (the `without` arm, the reading before CR-93), a reviewer may raise
it, and the task goes back for something it was never asked about. Confirming (the `with` arm),
the finding is closed and whatever else it notices is set apart, not sent back.
"""

from __future__ import annotations

import json
from pathlib import Path

from evals.harness import GATE, Guard, Scenario, git
from evals.scenarios import _fixtures
from rein import build_prompts, dag

TASK = dag.Task(
    id="T-001",
    title="parse a port",
    kind="foundation",
    scope_include=("src/port.py",),
    acceptance=(
        {"id": "A-1", "statement": "`parse_port` returns the port for 1..65535 and raises ValueError otherwise"},
    ),
)

BEFORE_FIX = """def parse_port(text):
    port = int(text)
    return port


def describe(port):
    # a little loose, and not what this task is about
    return "port " + str(port) + (" (privileged)" if port < 1024 else "")
"""

AFTER_FIX = """def parse_port(text):
    port = int(text)
    if not 1 <= port <= 65535:
        raise ValueError(f"port out of range: {port}")
    return port


def describe(port):
    # a little loose, and not what this task is about
    return "port " + str(port) + (" (privileged)" if port < 1024 else "")
"""

FINDING = {"severity": "must_fix", "statement": "parse_port accepts 0 and 70000", "anchor": "src/port.py:2"}
ANSWER = ".rein/work/review.T-001.findings.json"


def fixture(root: Path) -> None:
    _fixtures.product(root)
    _fixtures.ticket(root, TASK, "`parse_port(text)`: the port for 1..65535, ValueError otherwise.")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the product before T-001")
    git(root, "checkout", "-q", "-b", "build/T-001")
    (root / "src" / "port.py").write_text(BEFORE_FIX, encoding="utf-8")
    git(root, "add", "src")
    git(root, "commit", "-q", "-m", "T-001: parse a port")
    git(root, "tag", "reviewed")
    (root / "src" / "port.py").write_text(AFTER_FIX, encoding="utf-8")
    git(root, "add", "src")
    git(root, "commit", "-q", "-m", "T-001: review fix")
    git(root, "checkout", "-q", "main")


def prompt(root: Path) -> str:
    return build_prompts.confirm_prompt(
        [build_prompts.ConfirmSubject(key="T-001", findings=[FINDING], fix_diff_cmd="git diff reviewed..build/T-001")],
        findings_path=str(root / ANSWER),
    )


def control(root: Path) -> str:
    return str(
        build_prompts.batch_review_prompt(
            [build_prompts.ReviewSubject(TASK, "build/T-001", "git diff main...build/T-001", ".rein/work/T-001.json")],
            gate_cmds=[GATE],
            findings_path=str(root / ANSWER),
            reviews=("adversarial",),
            questions={},
        )
    )


def score(root: Path, calls: list[list[str]]) -> bool:
    """Would the task land: nothing goes back to the implementer."""
    path = root / ANSWER
    if not path.exists():
        return False
    entry = json.loads(path.read_text(encoding="utf-8")).get("tasks", {}).get("T-001")
    if not isinstance(entry, dict):
        return False
    sent_back = [f for f in entry.get("findings", []) if isinstance(f, dict) and f.get("severity") == "must_fix"]
    if "confirm" in entry:
        sent_back += [v for v in entry["confirm"] if isinstance(v, dict) and v.get("closed") is not True]
    return not sent_back


SCENARIO = Scenario(
    name="confirm_fix",
    guard=Guard(
        file="src/rein/build_prompts.py",
        section="confirm_prompt",
        sentence=(
            "What you notice anywhere else goes under `outside`: it is not this fix's question, and it is carried to "
            "acceptance rather than dropped."
        ),
        cr="CR-93",
    ),
    trials=5,
    fixture=fixture,
    prompt=prompt,
    score=score,
    control=control,
)
