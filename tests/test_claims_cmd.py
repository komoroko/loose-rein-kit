"""`rein claims` — the promises and their reach, read back across cycles.

The property these pin is the one `rein decisions` pinned one axis over, and the reason this
command exists at all: **the claims and the scope are archived with the cycle too**, so a
repository that has closed a cycle shows neither in its working tree. A reader that only looked
there would answer "this repository has promised nothing".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rein import claims_cmd, event_chain, models
from rein import repo as repo_mod
from tests._support import make_plan, seed_repo

_PLAN = """cycle:
  id: 2026-08-01-first
scope:
  include: [src/rein]
  exclude: [src/rein/data]
claims:
  - id: C-001
    statement: the observation store is never an input to a gate
    risk: high
  - id: C-002
    statement: a stop is counted once
    risk: low
"""

_REVIEW = """machine:
  claims:
    - claim_id: C-001
      verdict: aligned
      integrity:
        status: verified
      semantic_support:
        status: supported
      conformance:
        status: pass
"""


def _chain(cycle_id: str) -> list[models.Event]:
    return [event_chain.link(None, event_chain.make("gate_approved", cycle_id, subject_ids=("mandate",)))]


def _write_chain(path: Path, chain: list[models.Event]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e.to_mapping(), ensure_ascii=False) + "\n" for e in chain), encoding="utf-8")


def _archive(root: Path, name: str, *, plan: str = _PLAN, review: str | None = _REVIEW) -> Path:
    """One closed cycle, laid out the way `cycle-close` lays it out."""
    base = root / "docs" / "archive" / name
    _write_chain(base / "rein" / "events.ndjson", _chain(name))
    (base / "rein" / "plan.yaml").write_text(plan, encoding="utf-8")
    if review is not None:
        (base / "rein" / "review.yaml").write_text(review, encoding="utf-8")
    return base


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(tmp_path, events=_chain("live"))
    return repo_mod.get(str(tmp_path))


def test_a_closed_cycle_is_the_only_place_its_promises_still_are(repo: repo_mod.Repo) -> None:
    """`cycle.CYCLE_STATE` archives `plan.yaml`, which carries both the frozen claims and the
    frozen scope. Neither is in the working tree once the cycle is closed."""
    _archive(repo.root, "2026-08-01-first")

    cycles, unverified = claims_cmd.history(repo)

    assert unverified == []
    assert [c.label for c in cycles] == ["docs/archive/2026-08-01-first", ""]
    closed = cycles[0]
    assert [c.ident for c in closed.claims] == ["C-001", "C-002"]
    assert closed.include == ("src/rein",) and closed.exclude == ("src/rein/data",)
    # The open cycle's plan is the only one the working tree holds, and it is not this one.
    assert [c.statement for c in cycles[-1].claims] != [c.statement for c in closed.claims]


def test_the_scope_is_read_beside_the_claims_not_apart_from_them(repo: repo_mod.Repo) -> None:
    """What was promised and what the promise could reach are two faces of one mandate. Reading
    one without the other answers half of "what did I delegate"."""
    _archive(repo.root, "2026-08-01-first")

    out = claims_cmd.render(*claims_cmd.history(repo))

    assert "include: src/rein" in out and "exclude: src/rein/data" in out


def test_an_unbounded_scope_says_so_rather_than_printing_nothing(repo: repo_mod.Repo) -> None:
    """An empty `include` is every guarded path, not none of them (`plan.schema.json`). A blank
    here would read as the opposite of what it means."""
    _archive(repo.root, "2026-08-01-first", plan="claims: []\nscope:\n  include: []\n")

    out = claims_cmd.render(*claims_cmd.history(repo))

    assert "unbounded" in out


def test_the_three_axes_are_not_collapsed_into_one_word(repo: repo_mod.Repo) -> None:
    """`review.schema.json`: integrity is a fact, semantic support is a judgement, conformance is
    an observation, and there is deliberately no single `verified`. Printing one would be how an
    AI's opinion comes to be read as a check."""
    _archive(repo.root, "2026-08-01-first")

    reviewed = next(c for c in claims_cmd.history(repo)[0][0].claims if c.ident == "C-001")

    assert reviewed.verdict == "aligned"
    assert reviewed.axes == (("integrity", "verified"), ("semantics", "supported"), ("conformance", "pass"))


def test_a_claim_no_review_covered_is_unreviewed_not_unverified(repo: repo_mod.Repo) -> None:
    """"nobody looked" and "we looked and could not tell" must never render the same (plan §2.4).
    `unverified` is a verdict the review reached; this is the absence of one."""
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = claims_cmd.history(repo)

    uncovered = next(c for c in cycles[0].claims if c.ident == "C-002")
    assert uncovered.verdict == claims_cmd.UNREVIEWED and uncovered.axes == ()


def test_a_cycle_whose_review_was_never_generated_says_so_for_every_claim(repo: repo_mod.Repo) -> None:
    """The same rule at the cycle's scale: a cycle closed without a generated review has claims
    with no result, and none of them may borrow a verdict from somewhere else."""
    _archive(repo.root, "2026-08-01-first", review=None)

    cycles, _ = claims_cmd.history(repo)

    assert {c.verdict for c in cycles[0].claims} == {claims_cmd.UNREVIEWED}
    assert cycles[0].unreadable == []


def test_cycles_come_back_oldest_first_with_the_open_one_last(repo: repo_mod.Repo) -> None:
    _archive(repo.root, "2026-09-01-second")
    _archive(repo.root, "2026-08-01-first")

    cycles, _ = claims_cmd.history(repo)

    assert [c.label for c in cycles] == [
        "docs/archive/2026-08-01-first",
        "docs/archive/2026-09-01-second",
        "",
    ]


def test_an_archive_written_by_another_release_is_still_read(repo: repo_mod.Repo) -> None:
    """Validating an archive against today's schema would make the history go blank on the next
    schema change — the failure reading across cycles exists to prevent."""
    _archive(
        repo.root,
        "2026-08-01-first",
        plan="schema_version: 99\nclaims:\n  - id: C-001\n    statement: from the future\n"
        "    a_field_this_release_never_heard_of: true\n",
        review=None,
    )

    cycles, _ = claims_cmd.history(repo)

    assert [c.ident for c in cycles[0].claims] == ["C-001"]


def test_a_plan_that_is_not_a_document_is_named_not_skipped(repo: repo_mod.Repo) -> None:
    """Silence would read as "that cycle promised nothing", which is the one thing this must
    never say about a cycle it could not read."""
    _archive(repo.root, "2026-08-01-first", plan="- this is a list, not a mapping\n", review=None)

    cycles, _ = claims_cmd.history(repo)

    assert cycles[0].unreadable and "plan.yaml" in cycles[0].unreadable[0]
    assert "! plan.yaml" in claims_cmd.render(cycles)


def test_a_review_that_is_not_a_document_is_named_and_the_claims_still_print(repo: repo_mod.Repo) -> None:
    """The two documents fail apart. A broken `review.yaml` costs the verdicts, not the promises."""
    _archive(repo.root, "2026-08-01-first", review="- not a mapping\n")

    cycles, _ = claims_cmd.history(repo)

    assert [c.ident for c in cycles[0].claims] == ["C-001", "C-002"]
    assert cycles[0].unreadable and "review.yaml" in cycles[0].unreadable[0]
    assert {c.verdict for c in cycles[0].claims} == {claims_cmd.UNREVIEWED}


def test_an_archive_whose_chain_is_damaged_is_named_and_left_out(repo: repo_mod.Repo) -> None:
    """The same rule every cross-cycle report follows: not folded in as though it were sound, and
    not dropped in silence either."""
    base = _archive(repo.root, "2026-08-01-first")
    (base / "rein" / "events.ndjson").write_text('{"seq": 1, "event": "nonsense"}\n', encoding="utf-8")

    cycles, unverified = claims_cmd.history(repo)

    assert unverified == ["docs/archive/2026-08-01-first/rein/events.ndjson"]
    assert [c.label for c in cycles] == [""]
    assert "did not verify" in claims_cmd.render(cycles, unverified)


def test_an_empty_history_says_where_claims_will_come_from(tmp_path: Path) -> None:
    seed_repo(tmp_path, plan=make_plan(claims=[], tasks=[]), events=_chain("live"))

    cycles, _ = claims_cmd.history(repo_mod.get(str(tmp_path)))

    out = claims_cmd.render(cycles)
    assert "No claim is on record yet" in out
    assert "no claim was frozen" in out


def test_the_command_prints_the_history(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    _archive(repo.root, "2026-08-01-first")

    assert claims_cmd.main(["--repo", str(repo.root)]) == 0

    out = capsys.readouterr().out
    assert "docs/archive/2026-08-01-first" in out
    assert "the observation store is never an input to a gate" in out
    assert "3 claim(s) across 2 cycle(s)" in out  # two archived, one in the open cycle


def test_no_repository_is_an_error_not_an_empty_history(tmp_path: Path) -> None:
    assert claims_cmd.main(["--repo", str(tmp_path / "nowhere")]) == 1
