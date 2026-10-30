"""A conflict between two tasks whose intentions cannot both hold is reported, not merged over.

T-001 caps every request, uploads included, at 5 seconds; T-002 needs uploads to wait at least 90.
Both changed the same line, and any resolution has to drop one of them. Without the sentence the
resolver is told only to keep both sides doing what they were for and to finish `implemented`, and
picking a number compiles. The sentence claims it reports `needs-revision` instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from evals.harness import Guard, Scenario, git
from rein import build_prompts, dag

FAST = dag.Task(
    id="T-001",
    title="fail fast on a dead host",
    kind="parallel",
    claim_ids=("R-1",),
    scope_include=("src/client.py",),
    acceptance=({"id": "A-1", "statement": "every request, uploads included, gives up after at most 5 seconds"},),
)
UPLOAD = dag.Task(
    id="T-002",
    title="large uploads complete",
    kind="parallel",
    claim_ids=("R-2",),
    scope_include=("src/client.py",),
    acceptance=(
        {
            "id": "A-1",
            "statement": "a 100 MB upload over a 10 Mbit/s link completes: an upload waits at least 90 seconds",
        },
    ),
)

CLIENT = """import urllib.request

TIMEOUT = {timeout}  # {why}


def get(url):
    return urllib.request.urlopen(url, timeout=TIMEOUT).read()


def upload(url, data):
    return urllib.request.urlopen(url, data=data, timeout=TIMEOUT).read()
"""


def fixture(root: Path) -> None:
    (root / "src").mkdir()
    client = root / "src" / "client.py"
    client.write_text(CLIENT.format(timeout=30, why="seconds"), encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "the product before the cycle")
    git(root, "checkout", "-q", "-b", "build/T-002")
    client.write_text(CLIENT.format(timeout=90, why="seconds; a 100 MB upload needs ~80"), encoding="utf-8")
    git(root, "commit", "-qam", "T-002: large uploads complete")
    git(root, "checkout", "-q", "main")
    client.write_text(CLIENT.format(timeout=5, why="seconds; a dead host fails fast"), encoding="utf-8")
    git(root, "commit", "-qam", "T-001: fail fast on a dead host")
    merged = subprocess.run(["git", "merge", "-q", "build/T-002"], cwd=root, capture_output=True, text=True)
    if merged.returncode == 0:
        raise RuntimeError("the fixture's merge was meant to conflict")


def prompt(root: Path) -> str:
    return str(build_prompts.conflict_prompt(FAST, UPLOAD, ["src/client.py"], gate_cmds=["python -m pytest"]))


def score(root: Path, calls: list[list[str]]) -> bool:
    outcomes = [c[c.index("--outcome") + 1] for c in calls if c[:1] == ["report"] and "--outcome" in c[:-1]]
    return outcomes == ["needs-revision"]


SCENARIO = Scenario(
    name="conflict_contradiction",
    guard=Guard(
        file="src/rein/build_prompts.py",
        section="conflict_prompt",
        sentence=(
            "If the two sides genuinely contradict each other — they cannot both hold, so any resolution\n"
            "would have to drop or reinterpret one of them — **do not invent a merge**. Run\n"
            "`rein report --outcome needs-revision --summary <which two intentions collide and why>`. "
            "That is a defect in the plan, and papering over it here is exactly what must not happen. "
        ),
        cr="CR-97",
    ),
    trials=5,
    fixture=fixture,
    prompt=prompt,
    score=score,
)
