"""A repair is made where the defect comes from, not on the line the finding names.

The grounded review found `describe(1500)` printing `15.0s` in T-002's code. The cause is T-001's
`to_seconds`, which divides by 100; `timer.py`, also T-001's, is wrong for the same reason and no
finding names it. Patching `describe` passes a test that fails without the patch, so the loop's
proof (`build_loop._verify_repair`) lands it. The sentence claims the repair goes to the cause.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from evals.harness import GATE, Guard, Scenario, git
from rein import build_prompts, dag

UNITS = '''def to_seconds(ms):
    """Milliseconds to seconds."""
    return ms / 100
'''

TIMER = """from units import to_seconds


def elapsed(start_ms, end_ms):
    return to_seconds(end_ms - start_ms)
"""

REPORT = """from units import to_seconds


def describe(ms):
    return f"{to_seconds(ms):.1f}s"
"""

TASK = dag.Task(
    id="T-002",
    title="describe a duration",
    kind="parallel",
    blocked_by=("T-001",),
    claim_ids=("R-2",),
    scope_include=("src/report.py", "tests/test_report.py"),
    acceptance=({"id": "A-1", "statement": "`describe(ms)` renders a duration in seconds, one decimal"},),
)

FINDINGS = "- F-1 (diverged) at src/report.py:5: describe(1500) returns '15.0s'; 1500 ms is 1.5 s"

CHECK = (
    "import sys; sys.path.insert(0, 'src'); from units import to_seconds; from timer import elapsed; "
    "from report import describe; "
    "sys.exit(0 if (to_seconds(1500), elapsed(1000, 3500), describe(1500)) == (1.5, 2.5, '1.5s') else 1)"
)


def fixture(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "units.py").write_text(UNITS, encoding="utf-8")
    (root / "src" / "timer.py").write_text(TIMER, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "T-001: durations")
    (root / "src" / "report.py").write_text(REPORT, encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "T-002: describe a duration")


def prompt(root: Path) -> str:
    return str(build_prompts.gate_four_fix_prompt(TASK, FINDINGS, gate_cmds=[GATE]))


def score(root: Path, calls: list[list[str]]) -> bool:
    """The cause is gone: every caller of `to_seconds`, named by the finding or not, is right."""
    done = subprocess.run([sys.executable, "-c", CHECK], cwd=root, capture_output=True, timeout=60)
    return done.returncode == 0


SCENARIO = Scenario(
    name="repair_at_cause",
    guard=Guard(
        file="src/rein/build_prompts.py",
        section="gate_four_fix_prompt",
        sentence=(
            "Repair each at its cause. The line a finding names is where the defect showed; find where it "
            "comes from and fix it there, even when that is in code another task of this plan wrote — the "
            "approved mandate covers it. "
        ),
        cr="CR-97",
    ),
    trials=5,
    fixture=fixture,
    prompt=prompt,
    score=score,
)
