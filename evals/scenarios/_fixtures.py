"""What every implementer scenario's repository holds: a small product, a ticket, a dossier."""

from __future__ import annotations

from pathlib import Path

from rein import dag, dossier

GREET = """import sys


def greet(name: str, loud: bool = False) -> str:
    text = f"hello, {name}"
    return text.upper() if loud else text


if __name__ == "__main__":
    print(greet(sys.argv[1], loud="--loud" in sys.argv))
"""


def product(root: Path) -> None:
    (root / "src").mkdir(exist_ok=True)
    (root / "src" / "greet.py").write_text(GREET, encoding="utf-8")
    (root / "docs").mkdir(exist_ok=True)
    (root / "docs" / "usage.md").write_text("# Usage\n\n`python src/greet.py NAME`\n", encoding="utf-8")


def ticket(root: Path, task: dag.Task, body: str) -> None:
    (root / "docs" / "tasks").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "tasks" / f"{task.id}.md").write_text(f"# {task.id}: {task.title}\n\n{body}\n", encoding="utf-8")
    (root / "docs" / "20-design.md").write_text("# Design\n\nOne module, `src/greet.py`.\n", encoding="utf-8")
    document = dossier.build(task, plan=None, repo_path=lambda rel: root / rel)
    dossier.write(str(root), document)


def changed(root: Path) -> str:
    """Every tracked path that differs from the fixture's first commit, committed or not."""
    from evals.harness import git

    first = git(root, "rev-list", "--max-parents=0", "HEAD").split()[0]
    return git(root, "diff", "--name-only", first)
