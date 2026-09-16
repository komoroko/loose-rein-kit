"""The lens library: what applies, what is asked about, and what is off (plan §I).

The rule these pin is that a lens is a record with a condition, not a paragraph in a prompt. A
reviewer sent to attack a failure that cannot occur in this change costs a pass over the deliverable
and brings back "attacked, nothing" — while the findings that *are* possible compete with it for
the reader's attention. Over-reviewing is not thorough, so the condition is what decides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from rein import lens_cmd, lenses, models


def _lens(lens_id: str, **kwargs: Any) -> lenses.Lens:
    base: dict[str, Any] = {"id": lens_id, "stage": "design", "attack": "try it"}
    base.update(kwargs)
    return lenses.Lens(**base)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "rein"


# --- the condition decides ------------------------------------------------------


def test_a_standard_lens_applies_without_asking() -> None:
    applied, proposed = lenses.select([_lens("L-1", lens_class=lenses.CLASS_STANDARD)], stage="design")
    assert [lens.id for lens in applied] == ["L-1"]
    assert proposed == []


def test_a_conditional_lens_is_proposed_rather_than_applied() -> None:
    """Deciding its condition takes judgement, so it goes to the human who is already deciding."""
    applied, proposed = lenses.select([_lens("L-1", lens_class=lenses.CLASS_CONDITIONAL)], stage="design")
    assert applied == []
    assert [lens.id for lens in proposed] == ["L-1"]


def test_an_unclassified_lens_is_off() -> None:
    """Off is what "nobody has written down when this applies" means. It stays in the library so
    the next time its cause comes back there is something to attach a condition to."""
    applied, proposed = lenses.select([_lens("L-1", lens_class=lenses.CLASS_UNCLASSIFIED)], stage="design")
    assert applied == [] and proposed == []


def test_a_lens_whose_paths_the_change_never_touches_does_not_apply() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, paths=("**/*.sql",))]
    assert lenses.select(library, stage="design", changed=["src/ui.py"])[0] == []
    assert [lens.id for lens in lenses.select(library, stage="design", changed=["db/x.sql"])[0]] == ["L-1"]


def test_no_paths_means_the_condition_does_not_turn_on_paths() -> None:
    """Not "matches nothing" — the commonest condition (ambiguity, hidden assumptions) is about
    the prose, and a lens that silently never ran because it named no glob would be the worst of
    both: carried in the library, costed at the gate, and never applied."""
    assert _lens("L-1").matches_paths(["anything.py"]) is True
    assert _lens("L-1").matches_paths([]) is True


def test_a_risk_floor_keeps_a_lens_off_low_risk_work() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, claim_risk="high")]
    assert lenses.select(library, stage="design", risks=["low", "medium"])[0] == []
    assert len(lenses.select(library, stage="design", risks=["low", "critical"])[0]) == 1


def test_a_lens_for_another_stage_is_never_selected() -> None:
    """The earliest stage that could carry the failure is where the lens belongs. Running the same
    one everywhere is how a list becomes something to work through rather than to use."""
    library = [_lens("L-1", stage="requirements", lens_class=lenses.CLASS_STANDARD)]
    assert lenses.select(library, stage="design") == ([], [])


# --- the packaged library ---------------------------------------------------------


def test_every_packaged_lens_states_when_it_applies(config_home: Path) -> None:
    """A lens with no condition cannot be told apart from one whose condition is "always". The
    first is unfinished; the second is rare and has to say so."""
    for lens in lenses.library():
        assert lens.applies_when, f"{lens.id} carries no condition"
        assert lens.stage in lenses.STAGE_VALUES
        assert lens.lens_class in lenses.LENS_CLASS_VALUES


def test_the_users_library_overlays_the_packaged_one_by_id(config_home: Path) -> None:
    """Overlaid rather than replaced: narrowing one packaged lens must not mean copying the set and
    inheriting responsibility for keeping it current."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / lenses.LIBRARY_NAME).write_text(
        "lenses:\n"
        "  - id: L-DES-YAGNI\n"
        "    stage: design\n"
        "    class: unclassified\n"
        "    attack: narrowed locally\n"
        "    applies_when: never, here\n",
        encoding="utf-8",
    )
    found = {lens.id: lens for lens in lenses.library()}

    assert found["L-DES-YAGNI"].lens_class == lenses.CLASS_UNCLASSIFIED
    assert "L-REQ-AMBIGUITY" in found  # the rest of the packaged set is still there


def test_an_unreadable_library_is_no_lenses_rather_than_a_crash(config_home: Path) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / lenses.LIBRARY_NAME).write_text("lenses: [ unterminated\n", encoding="utf-8")
    assert [lens.id for lens in lenses.library() if lens.origin == "packaged"]


# --- what earns its place ---------------------------------------------------------


def _applied(lens_id: str, found: bool) -> models.Event:
    from rein import event_chain

    return event_chain.make("lens_applied", "demo-cycle", detail={"lens": lens_id, "found": found})


def test_stats_count_applications_and_finds_separately() -> None:
    counts = lens_cmd.stats([_applied("L-1", False), _applied("L-1", True), _applied("L-2", False)])
    assert counts == {"L-1": {"applied": 2, "found": 1}, "L-2": {"applied": 1, "found": 0}}


def test_a_lens_that_keeps_applying_and_never_finds_is_named() -> None:
    """The counts carry no threshold and never will: a ceiling on how many lenses may exist gets
    answered by deleting whichever is cheapest, not whichever stopped earning its place."""
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="always")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", False)]), library)

    assert "L-1" in out
    assert "never found anything" in out


def test_a_lens_that_finds_something_is_not_named_as_silent() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="always")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", True)]), library)
    assert "never found anything" not in out
