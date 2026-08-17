"""The per-task dossier: the loop handing over what it knows, instead of sending agents to find out.

Everything here was already computed somewhere in the run and then dropped — the claims are in
the frozen plan, the scope has been in `plan.yaml`'s schema since it was written with nothing
reading it, the source/test/lockfile split is what `diff_facts` does for the coverage manifest,
and the attempt history is in the audit chain. These tests are about the handover, and about the
one thing the handover makes possible: checking a scope that used to be an instruction in a
prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

from rein import dag, diff_facts, dossier, models
from tests._support import make_claim, make_plan


def task(**kwargs: object) -> dag.Task:
    fields: dict[str, object] = {"id": "T-001", "title": "the task", "kind": "parallel"}
    fields.update(kwargs)
    return dag.Task(**fields)  # type: ignore[arg-type]


# --- what a reader needs to read, versus what they only need to know ------------


def test_lockfiles_and_generated_files_are_summarized_not_spelled_out() -> None:
    """800 lines of lockfile say one thing, and saying it in the middle of a code review buries
    the code."""
    split = dossier.classify_paths(
        ["src/api/handler.py", "tests/test_handler.py", "uv.lock", "src/generated/pb2.py", "migrations/001_x.sql"]
    )
    assert split["source"] == ["src/api/handler.py"]
    assert split["tests"] == ["tests/test_handler.py"]
    assert split["migrations"] == ["migrations/001_x.sql"]
    assert {entry["path"] for entry in split["mechanical"]} == {"uv.lock", "src/generated/pb2.py"}


def test_a_migration_is_not_mechanical() -> None:
    """Mechanical to write, consequential to run — the one kind that has to stay in front of a reader."""
    assert "migration" not in diff_facts.MECHANICAL_KINDS
    assert diff_facts.classify_path("migrations/002_add_column.sql") == "migration"


def test_an_oversized_path_list_says_how_much_it_dropped() -> None:
    """A cap that hides what it cut is a cap that makes the next reader wrong without knowing."""
    split = dossier.classify_paths([f"src/f{i}.py" for i in range(dossier.MAX_PATHS + 7)])
    assert split["omitted"] == 7


# --- the scope nothing was reading ----------------------------------------------


def test_a_task_with_no_declared_scope_is_unbounded() -> None:
    """An empty `include` has always meant "not stated", and it must not start meaning "nothing"."""
    assert dossier.scope_violations(task(), ["anywhere/at/all.py"]) == []


def test_a_change_outside_the_declared_scope_is_reported() -> None:
    """ "Do not reach into other tasks' territory" was an instruction in a prompt, checked by nobody
    — while the plan had said exactly where this task's work belongs the whole time."""
    scoped = task(scope_include=("src/api/",))
    assert dossier.scope_violations(scoped, ["src/api/handler.py", "src/billing/charge.py"]) == [
        "src/billing/charge.py"
    ]


def test_an_exclusion_wins_over_an_inclusion() -> None:
    scoped = task(scope_include=("src/",), scope_exclude=("src/vendor/",))
    assert dossier.scope_violations(scoped, ["src/api/x.py", "src/vendor/lib.py"]) == ["src/vendor/lib.py"]


def test_a_prefix_needs_its_slash() -> None:
    """The same rule `guard.paths` uses, so an operator learns it once."""
    scoped = task(scope_include=("src/api/handler.py",))
    assert dossier.scope_violations(scoped, ["src/api/handler.py"]) == []
    assert dossier.scope_violations(scoped, ["src/api/handler.py.bak"]) == ["src/api/handler.py.bak"]


def test_the_plan_carries_scope_all_the_way_into_the_graph() -> None:
    """It was in the schema and in `models.Task`, and stopped there — which is why nothing checked it."""
    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001")],
            tasks=[
                {
                    "id": "T-001",
                    "title": "t",
                    "kind": "parallel",
                    "risk": "low",
                    "claim_ids": ["C-001"],
                    "scope": {"include": ["src/api/"], "exclude": ["src/api/vendor/"]},
                }
            ],
        )
    )
    joined = dag.join(plan, None).tasks[0]
    assert joined.scope_include == ("src/api/",)
    assert joined.scope_exclude == ("src/api/vendor/",)


# --- assembly and handover -------------------------------------------------------


def test_the_dossier_carries_what_each_claim_actually_says(tmp_path: Path) -> None:
    """An agent handed only claim ids works from the ticket's prose and never learns what the
    claim it is answering asserts."""
    plan = models.Plan(make_plan(claims=[make_claim("C-001", requirement_ids=["R-1"])], tasks=[]))
    document = dossier.build(task(claim_ids=("C-001",)), plan=plan, repo_path=lambda rel: tmp_path / rel)

    assert [c["id"] for c in document["claims"]] == ["C-001"]
    assert document["claims"][0]["statement"]
    assert document["claims"][0]["requirement_ids"] == ["R-1"]


def test_the_dossier_digests_the_documents_it_sends_the_agent_to_read(tmp_path: Path) -> None:
    """ "Which text was this built from" had no answer when a ticket was edited between the
    approval and the build."""
    ticket = tmp_path / "docs/tasks/T-001.md"
    ticket.parent.mkdir(parents=True)
    ticket.write_text("# T-001\n", encoding="utf-8")

    document = dossier.build(task(), plan=None, repo_path=lambda rel: tmp_path / rel)
    assert document["sources"]["ticket"]["path"] == "docs/tasks/T-001.md"
    assert document["sources"]["ticket"]["digest"].startswith("sha256:")
    assert "design" not in document["sources"], "a document that does not exist is not a source"


def test_the_dossier_lands_where_a_task_commit_already_excludes_it(tmp_path: Path) -> None:
    """Under `.rein/`, which `finalize_commit` excludes and which dies with the worktree — the
    canonical record of anything decided here goes through the control plane instead."""
    document = dossier.build(task(), plan=None, repo_path=lambda rel: tmp_path / rel)
    written = dossier.write(str(tmp_path), document)

    assert written == tmp_path / ".rein/work/T-001.json"
    assert json.loads(written.read_text(encoding="utf-8"))["task"]["id"] == "T-001"


def test_the_dossier_keeps_more_than_the_last_failure(tmp_path: Path) -> None:
    """The handoff carried one attempt, so a task on its fourth arrived with no memory of the
    three before it — and re-tried the same fix."""
    history = [{"attempt": n, "step": "check"} for n in range(1, dossier.MAX_HISTORY + 4)]
    document = dossier.build(task(), plan=None, repo_path=lambda rel: tmp_path / rel, history=history)

    assert len(document["history"]) == dossier.MAX_HISTORY
    assert document["history"][-1]["attempt"] == history[-1]["attempt"]
