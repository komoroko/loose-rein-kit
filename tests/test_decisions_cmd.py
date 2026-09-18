"""`rein decisions` — the judgement history, read back across cycles (plan §F).

The property these pin is the one the command exists for: **every write site is per-cycle**, so a
decision settled last cycle is not in `docs/` at all once `cycle-close` has run. A reader that
only looked at the working tree would answer "nothing was decided" to a repository with a year of
judgements in it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rein import decisions_cmd, event_chain, models
from rein import repo as repo_mod
from tests._support import seed_repo

_PLAN = """decisions:
  - id: D-001
    subject: which store the observations live in
    reach: mandate
    status: settled
    settled_by: human
    answer: user-global, beside the project registry
  - id: D-002
    subject: whether to retry a flaky launch
    reach: local
    status: settled
    settled_by: loop
"""

#: The first bullet under each heading is what the scaffold itself ships.
_REQUIREMENTS = """# Requirements

## Clarifications
<!-- Audit trail of how ambiguities were closed: one bullet per resolved [NEEDS CLARIFICATION] marker / question. -->
- Q: <question> → A: <the human's answer> (YYYY-MM-DD)
- Q: where does a cycle end → A: at the acceptance approval (2026-08-01)

## Open questions
<!-- Points to confirm with the human. Resolve before the mandate. -->
-
- how a second machine's observations get pooled (going ahead on: they do not)

## Adversarial review
- a finding, not a decision, and under a heading nobody asked for
"""

_ADR = """# ADR-001: observations live outside the repository

- **Status**: accepted
- **Date**: 2026-08-01
- **Decider**: human (finalized at the mandate gate)
"""


def _chain(cycle_id: str) -> list[models.Event]:
    return [event_chain.link(None, event_chain.make("gate_approved", cycle_id, subject_ids=("mandate",)))]


def _write_chain(path: Path, chain: list[models.Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e.to_mapping(), ensure_ascii=False) + "\n" for e in chain), encoding="utf-8")


def _archive(root: Path, name: str, *, plan: str = _PLAN, requirements: str = _REQUIREMENTS, adr: str = _ADR) -> Path:
    """One closed cycle, laid out the way `cycle-close` lays it out."""
    base = root / "docs" / "archive" / name
    _write_chain(base / "rein" / "events.ndjson", _chain(name))
    (base / "rein" / "plan.yaml").write_text(plan, encoding="utf-8")
    (base / "10-requirements.md").write_text(requirements, encoding="utf-8")
    (base / "decisions").mkdir(parents=True, exist_ok=True)
    (base / "decisions" / "ADR-001.md").write_text(adr, encoding="utf-8")
    return base


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, events=_chain("live"))
    return repo_mod.get(str(tmp_path))


def test_all_four_write_sites_are_read_as_one_history(repo: repo_mod.Repo) -> None:
    """The decision was settled in `plan.yaml`, weighed in an ADR, clarified in one section of
    `10-requirements.md` and passed through in another. Four authors, four moments, one question."""
    _archive(repo.root, "2026-08-01-first")

    cycles, unverified = decisions_cmd.history(repo)

    assert unverified == []
    assert [c.label for c in cycles] == ["docs/archive/2026-08-01-first", ""]
    assert [(d.where, d.ident) for d in cycles[0].decisions] == [
        ("plan", "D-001"),
        ("plan", "D-002"),
        ("adr", "ADR-001"),
        ("clarified", "C1"),
        ("open", "O1"),
    ]


def test_a_closed_cycle_is_the_only_place_its_decisions_still_are(repo: repo_mod.Repo) -> None:
    """The reason the command exists. `cycle-close` archives `plan.yaml`, `10-requirements.md`
    *and* `docs/decisions/`, then restores the last two pristine — so the working tree of a
    repository that has closed a cycle shows none of what that cycle decided."""
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = decisions_cmd.history(repo)

    assert len(cycles[0].decisions) == 5
    assert cycles[-1].label == "" and cycles[-1].decisions == []


def test_cycles_come_back_oldest_first_with_the_open_one_last(repo: repo_mod.Repo) -> None:
    """A history out of order is not a history. The archive name is `<YYYY-MM-DD>-<slug>`, so
    sorting the paths sorts the cycles, and the one still open belongs at the end."""
    _archive(repo.root, "2026-09-01-second")
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = decisions_cmd.history(repo)

    assert [c.label for c in cycles] == [
        "docs/archive/2026-08-01-first",
        "docs/archive/2026-09-01-second",
        "",
    ]


def test_the_templates_own_bullets_are_not_somebody_s_judgement(repo: repo_mod.Repo) -> None:
    """A document nobody has clarified anything in still carries the section's shape. Printing it
    back as a decision is worse than printing nothing."""
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = decisions_cmd.history(repo)

    summaries = [d.summary for d in cycles[0].decisions]
    assert "Q: where does a cycle end → A: at the acceptance approval (2026-08-01)" in summaries
    assert not any("<question>" in s for s in summaries)


def test_a_placeholder_from_a_release_whose_wording_has_moved_is_still_one(repo: repo_mod.Repo) -> None:
    """The reading is per-cycle and the payload is not. A set difference against the *running*
    release's scaffold left an older cycle's placeholders unrecognised and printed them back as
    judgements — the same failure as validating an archive against today's schema."""
    doc = repo.path("docs") / "10-requirements.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "## Clarifications\n"
        "- Q: <the question, as an older release worded its slot> → A: <the answer> (YYYY-MM-DD)\n"
        "- Q: does the reading survive an upgrade → A: yes (2026-09-02)\n",
        encoding="utf-8",
    )

    cycles, _ = decisions_cmd.history(repo)

    summaries = [d.summary for d in cycles[-1].decisions]
    assert "Q: does the reading survive an upgrade → A: yes (2026-09-02)" in summaries
    assert not any("older release worded its slot" in s for s in summaries)


def test_a_comparison_in_a_clarification_is_not_a_placeholder(repo: repo_mod.Repo) -> None:
    """The other direction: a slot test wide enough to swallow real prose drops records."""
    doc = repo.path("docs") / "10-requirements.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text(
        "## Clarifications\n"
        "- Q: how many retries → A: must handle <= 3 retries (2026-09-02)\n"
        "- Q: which way round → A: a < b and c > d (2026-09-02)\n",
        encoding="utf-8",
    )

    cycles, _ = decisions_cmd.history(repo)

    assert len([d for d in cycles[-1].decisions if d.where == "clarified"]) == 2


def test_bullets_under_another_heading_are_not_decisions(repo: repo_mod.Repo) -> None:
    """`## Adversarial review` is a table of findings in the same document. Reading every bullet
    of the file would turn a reviewer's objection into something the human decided."""
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = decisions_cmd.history(repo)

    assert not any("a finding, not a decision" in d.summary for d in cycles[0].decisions)


def test_an_archive_written_by_another_release_is_still_read(repo: repo_mod.Repo) -> None:
    """A closed cycle's `plan.yaml` was written by whatever release closed it. Validating it
    against today's schema would make the history go blank on the next schema change, which is
    the failure reading across cycles exists to prevent."""
    _archive(
        repo.root,
        "2026-08-01-first",
        plan="schema_version: 99\ndecisions:\n  - id: D-001\n    subject: from the future\n    reach: mandate\n"
        "    status: settled\n    settled_by: human\n    a_field_this_release_never_heard_of: true\n",
    )

    cycles, _ = decisions_cmd.history(repo)

    assert [(d.where, d.ident) for d in cycles[0].decisions][0] == ("plan", "D-001")


def test_a_plan_that_is_not_a_document_is_named_not_skipped(repo: repo_mod.Repo) -> None:
    """Silence here would read as "that cycle decided nothing", which is the one thing a history
    must never say about a cycle it could not read."""
    _archive(repo.root, "2026-08-01-first", plan="- this is a list, not a mapping\n")

    cycles, _ = decisions_cmd.history(repo)

    assert cycles[0].unreadable and "plan.yaml" in cycles[0].unreadable[0]
    assert "! plan.yaml" in decisions_cmd.render(cycles)


def test_an_archive_whose_chain_is_damaged_is_named_and_left_out(repo: repo_mod.Repo) -> None:
    """The same rule every cross-cycle report follows. An archive that fails its own check is not
    folded in as though it were sound, and not dropped in silence either."""
    base = _archive(repo.root, "2026-08-01-first")
    (base / "rein" / "events.ndjson").write_text('{"seq": 1, "event": "nonsense"}\n', encoding="utf-8")

    cycles, unverified = decisions_cmd.history(repo)

    assert unverified == ["docs/archive/2026-08-01-first/rein/events.ndjson"]
    assert [c.label for c in cycles] == [""]
    assert "did not verify" in decisions_cmd.render(cycles, unverified)


def test_an_empty_history_says_where_decisions_will_come_from(repo: repo_mod.Repo) -> None:
    cycles, _ = decisions_cmd.history(repo)

    out = decisions_cmd.render(cycles)
    assert "No decision is on record yet" in out
    assert "nothing recorded" in out


def test_the_command_prints_the_history(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    _archive(repo.root, "2026-08-01-first")

    assert decisions_cmd.main(["--repo", str(repo.root)]) == 0

    out = capsys.readouterr().out
    assert "docs/archive/2026-08-01-first" in out
    assert "which store the observations live in → user-global, beside the project registry [human]" in out
    assert "5 record(s) across 2 cycle(s)" in out


def test_no_repository_is_an_error_not_an_empty_history(tmp_path: Path) -> None:
    assert decisions_cmd.main(["--repo", str(tmp_path / "nowhere")]) == 1
