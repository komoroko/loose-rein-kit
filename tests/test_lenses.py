"""The lens library: what applies, what is asked about, and what is off (plan §I).

The rule these pin is that a lens is a record with a condition, not a paragraph in a prompt. A
reviewer sent to attack a failure that cannot occur in this change costs a pass over the deliverable
and brings back "attacked, nothing" — while the findings that *are* possible compete with it for
the reader's attention. Over-reviewing is not thorough, so the condition is what decides.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from rein import lens_cmd, lenses, models
from tests._support import seed_repo


def _lens(lens_id: str, **kwargs: Any) -> lenses.Lens:
    base: dict[str, Any] = {"id": lens_id, "stage": "design", "attack": "try it"}
    base.setdefault("when", lenses.Condition(min_claims=1))
    base.update(kwargs)
    return lenses.Lens(**base)


def _facts(**kwargs: Any) -> lenses.Facts:
    base: dict[str, Any] = {"claims": 1, "tasks": 1}
    base.update(kwargs)
    return lenses.Facts(**base)


@pytest.fixture
def config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    return tmp_path / "rein"


# --- the condition decides ------------------------------------------------------


def test_a_standard_lens_applies_without_asking() -> None:
    applied, proposed = lenses.select([_lens("L-1", lens_class=lenses.CLASS_STANDARD)], stage="design", facts=_facts())
    assert [lens.id for lens in applied] == ["L-1"]
    assert proposed == []


def test_a_conditional_lens_is_proposed_rather_than_applied() -> None:
    """Deciding its condition takes judgement, so it goes to the human who is already deciding."""
    applied, proposed = lenses.select(
        [_lens("L-1", lens_class=lenses.CLASS_CONDITIONAL)], stage="design", facts=_facts()
    )
    assert applied == []
    assert [lens.id for lens in proposed] == ["L-1"]


def test_an_unclassified_lens_is_off() -> None:
    """Off is what "nobody has written down when this applies" means. It stays in the library so
    the next time its cause comes back there is something to attach a condition to."""
    applied, proposed = lenses.select(
        [_lens("L-1", lens_class=lenses.CLASS_UNCLASSIFIED)], stage="design", facts=_facts()
    )
    assert applied == [] and proposed == []


def test_a_lens_whose_paths_the_change_never_touches_does_not_apply() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, when=lenses.Condition(paths=("**/*.sql",)))]
    assert lenses.select(library, stage="design", facts=_facts(changed=("src/ui.py",)))[0] == []
    found = lenses.select(library, stage="design", facts=_facts(changed=("db/x.sql",)))[0]
    assert [lens.id for lens in found] == ["L-1"]


def test_an_axis_a_condition_does_not_name_is_not_a_condition_that_matches_nothing() -> None:
    """A condition states the axes it turns on. `min_claims: 1` says nothing about paths, and a
    lens that silently never ran because it named no glob would be the worst of both: carried in
    the library, costed at the gate, and never applied."""
    condition = lenses.Condition(min_claims=1)
    assert condition.holds(_facts(changed=("anything.py",))) is True
    assert condition.holds(_facts(changed=())) is True


def test_a_risk_floor_keeps_a_lens_off_low_risk_work() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, when=lenses.Condition(claim_risk="high"))]
    assert lenses.select(library, stage="design", facts=_facts(risks=("low", "medium")))[0] == []
    assert len(lenses.select(library, stage="design", facts=_facts(risks=("low", "critical")))[0]) == 1


def test_a_named_fact_the_cycle_does_not_have_keeps_a_lens_off() -> None:
    """The axis the early stages have instead of paths. An NFR lens over a cycle that declares no
    NFR has nothing to measure against and degrades into generic advice."""
    when = lenses.Condition(requires=(lenses.FACT_NFR_CLAIMS,))
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, when=when)]
    assert lenses.select(library, stage="design", facts=_facts())[0] == []
    with_nfr = _facts(present=frozenset({lenses.FACT_NFR_CLAIMS}))
    assert len(lenses.select(library, stage="design", facts=with_nfr)[0]) == 1


def test_a_count_condition_keeps_a_lens_off_a_cycle_too_small_for_it() -> None:
    """One claim contradicts nothing, so the contradiction lens has nothing to attack."""
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, when=lenses.Condition(min_claims=2))]
    assert lenses.select(library, stage="design", facts=_facts(claims=1))[0] == []
    assert len(lenses.select(library, stage="design", facts=_facts(claims=2))[0]) == 1


def test_a_standard_lens_with_no_condition_is_loaded_as_unclassified() -> None:
    """The check the class system rests on. `standard` means applied without asking anybody, and
    the licence for that is a condition a machine can settle; with no `when:` there is nothing to
    settle, so what would ship is a lens applied to everything — the always-on list this replaced,
    re-entering through an empty field."""
    raw = {"id": "L-X", "stage": "design", "attack": "try it", "class": "standard"}
    loaded = lenses._lens(raw)

    assert loaded is not None
    assert loaded.lens_class == lenses.CLASS_UNCLASSIFIED


def test_a_lens_for_another_stage_is_never_selected() -> None:
    """The earliest stage that could carry the failure is where the lens belongs. Running the same
    one everywhere is how a list becomes something to work through rather than to use."""
    library = [_lens("L-1", stage="requirements", lens_class=lenses.CLASS_STANDARD)]
    assert lenses.select(library, stage="design", facts=_facts()) == ([], [])


# --- the packaged library ---------------------------------------------------------


def test_every_packaged_lens_states_when_it_applies(config_home: Path) -> None:
    """A lens with no condition cannot be told apart from one whose condition is "always". The
    first is unfinished; the second is rare and has to say so."""
    for lens in lenses.library():
        assert lens.applies_when, f"{lens.id} carries no condition in prose"
        assert lens.stage in lenses.STAGE_VALUES
        assert lens.lens_class in lenses.LENS_CLASS_VALUES


def test_every_packaged_standard_lens_carries_a_machine_decidable_condition(config_home: Path) -> None:
    """Otherwise the class means nothing: `standard` is "applied without asking", and what makes
    that safe to ship across repositories is a condition the machine settles rather than prose
    nobody reads at review time."""
    for lens in lenses.library():
        if lens.lens_class == lenses.CLASS_STANDARD:
            assert lens.when.stated, f"{lens.id} is standard with no `when:` condition"


def test_every_packaged_stage_has_at_least_one_lens(config_home: Path) -> None:
    """A stage in `STAGES` that no lens belongs to and no command selects for is the same
    unfinished record the class system refuses — declared, and doing nothing."""
    covered = {lens.stage for lens in lenses.library()}
    assert covered == set(lenses.STAGES)


def test_a_code_lens_is_off_for_a_change_that_cannot_carry_its_failure(config_home: Path) -> None:
    """The sharpest conditions in the library: at the code stage the plan already says which files
    may be touched, so a schema lens over a change that touches no schema can be decided by the
    machine rather than costed on the reviewer."""
    library = lenses.library()
    no_schema = lenses.Facts(claims=1, tasks=1, changed=("src/ui/colors.css",))
    applied, _ = lenses.select(library, stage="code", facts=no_schema)
    assert "L-CODE-SCHEMA-DRIFT" not in {lens.id for lens in applied}

    with_schema = lenses.Facts(claims=1, tasks=1, changed=("src/rein/data/schema/plan.schema.json",))
    applied, _ = lenses.select(library, stage="code", facts=with_schema)
    assert "L-CODE-SCHEMA-DRIFT" in {lens.id for lens in applied}


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
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="the cycle states a claim")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", False)]), library)

    assert "L-1" in out
    assert "never found anything" in out


def test_a_lens_that_finds_something_is_not_named_as_silent() -> None:
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="the cycle states a claim")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", True)]), library)
    assert "never found anything" not in out


# --- the selection is frozen with the mandate -------------------------------------


def _repo(tmp_path: Path, **plan_kwargs: Any) -> Any:
    from rein import repo as repo_mod
    from tests._support import SANDBOXED_PROFILES, make_claim, make_config, make_plan, make_state, make_task

    plan_kwargs.setdefault("claims", [make_claim("C-001", requirement_ids=["R-1"])])
    plan_kwargs.setdefault("tasks", [make_task("T-001", claim_ids=["C-001"], scope_include=["src/**"])])
    seed_repo(
        tmp_path,
        state=make_state(gates=dict.fromkeys(models.GATE_ORDER, "pending"), plan_status="draft"),
        plan=make_plan(**plan_kwargs),
        config=make_config(profiles=SANDBOXED_PROFILES),
    )
    return repo_mod.Repo(tmp_path)


def test_selecting_writes_the_selection_into_the_plan(tmp_path: Path, config_home: Path) -> None:
    """The library is user-global and a person edits it between cycles. Resolved once, against the
    plan, and written into the plan — where the mandate freezes it with everything else."""
    from rein import store as store_mod

    repo = _repo(tmp_path)
    assert lens_cmd.main(["--select", "design", "--repo", str(tmp_path)]) == 0

    plan = store_mod.Store(repo).read_plan()
    assert plan is not None
    assert {entry.stage for entry in plan.lenses} == set(lenses.STAGES)
    assert "L-DES-COVERAGE" in {entry.id for entry in plan.lenses if entry.status == "applied"}


def test_a_frozen_mandate_reads_the_selection_back_rather_than_re_deriving_it(
    tmp_path: Path, config_home: Path
) -> None:
    """The whole reason the selection is written down. One line changed in a user-global overlay
    must not change what this cycle was reviewed for, with nothing in the chain to show it."""
    from rein import store as store_mod

    repo = _repo(tmp_path)
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    before = [dict(entry.raw) for entry in _plan(store_mod, repo).lenses]

    # The overlay turns a packaged lens off, exactly as somebody editing their own library would.
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / lenses.LIBRARY_NAME).write_text(
        "lenses:\n  - id: L-DES-COVERAGE\n    stage: design\n    class: unclassified\n"
        "    attack: switched off locally\n    applies_when: never, here\n",
        encoding="utf-8",
    )
    _approve_mandate(repo)
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    assert [dict(entry.raw) for entry in _plan(store_mod, repo).lenses] == before


def test_re_selecting_before_the_freeze_resolves_against_the_plan_as_it_now_is(
    tmp_path: Path, config_home: Path
) -> None:
    """`/req` runs with no tasks in the plan yet and `/tasks` runs with all of them. The last write
    before the mandate is the one that gets frozen, so both see the same facts."""
    from tests._support import make_task

    repo = _repo(tmp_path, tasks=[])
    lens_cmd.main(["--select", "requirements", "--repo", str(tmp_path)])
    from rein import store as store_mod

    assert not [e for e in _plan(store_mod, repo).lenses if e.stage == "code"]

    plan = _plan(store_mod, repo)
    raw = json.loads(json.dumps(dict(plan.raw)))
    raw["tasks"] = [make_task("T-001", claim_ids=["C-001"], scope_include=["src/x.schema.json"])]
    _write_plan(store_mod, repo, raw)
    lens_cmd.main(["--select", "code", "--repo", str(tmp_path)])

    applied = {e.id for e in _plan(store_mod, repo).lenses if e.stage == "code" and e.status == "applied"}
    assert "L-CODE-SCHEMA-DRIFT" in applied


def _plan(store_mod: Any, repo: Any) -> Any:
    plan = store_mod.Store(repo).read_plan()
    assert plan is not None
    return plan


def _write_plan(store_mod: Any, repo: Any, raw: dict[str, Any]) -> None:
    store = store_mod.Store(repo)
    with store.transaction() as tx:
        tx.write("plan", raw, expect_digest=store_mod.read_digest(store.read_plan()))
        tx.append("decision_declared", cycle_id="demo-cycle", subject_ids=["T-001"])


def _approve_mandate(repo: Any) -> None:
    from rein import store as store_mod
    from tests._support import make_state

    store = store_mod.Store(repo)
    state = store.read_state()
    assert state is not None
    raw = make_state(gates={"mandate": "approved", "acceptance": "pending"}, plan_status="frozen")
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=store_mod.read_digest(state))
        tx.append("gate_approved", cycle_id="demo-cycle", subject_ids=["mandate"])
