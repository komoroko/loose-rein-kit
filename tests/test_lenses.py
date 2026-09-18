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


def _selected(*lens_ids: str) -> models.Event:
    from rein import event_chain

    return event_chain.make("lens_selected", "demo-cycle", subject_ids=sorted(lens_ids))


def test_stats_count_applications_and_finds_separately() -> None:
    counts = lens_cmd.stats([_applied("L-1", False), _applied("L-1", True), _applied("L-2", False)])
    assert counts == {
        "L-1": {"selected": 0, "applied": 2, "found": 1},
        "L-2": {"selected": 0, "applied": 1, "found": 0},
    }


def test_a_lens_dropped_at_the_gate_is_not_the_same_as_one_that_never_came_up() -> None:
    """Dropping a `proposed` lens deletes it from the plan before the freeze, so it leaves no
    `lens_applied` and reads like a lens whose condition never held. The difference was on record
    the whole time — `lens_selected` names every lens the resolution wrote into the plan — and the
    tally was reading the other event. A lens wide enough to be proposed every cycle and dropped
    every cycle costs a judgement each time and used to be invisible to the retirement rule."""
    counts = lens_cmd.stats([_selected("L-1", "L-2"), _applied("L-2", True)])
    assert counts["L-1"] == {"selected": 1, "applied": 0, "found": 0}
    assert counts["L-2"] == {"selected": 1, "applied": 1, "found": 1}

    library = [_lens("L-1", lens_class=lenses.CLASS_CONDITIONAL, applies_when="the cycle touches a schema")]
    out = lens_cmd.render_stats(counts, library)
    assert "L-1" in out.split("never recorded as applied")[1]
    assert "L-2" not in out.split("never recorded as applied")[1]
    # …and it names the count without claiming a cause it cannot see.
    assert "cannot tell which" in out


def test_the_stats_say_whose_numbers_they_are_before_pointing_at_a_shared_library() -> None:
    """The counts are one repository's chain and archives; the library they invite an edit to is
    user-global. The output used to end "Narrow it in <user-global path>, or drop it" with nothing
    saying the reading behind that instruction was narrower than the thing it would change."""
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="the cycle states a claim")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", False)]), library)
    assert "this repository's chain and archives only" in out
    assert out.index("this repository's chain and archives only") > out.index("never found anything")


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
        state=make_state(gates=dict.fromkeys(models.GATE_ENDS, "pending"), plan_status="draft"),
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


# --- one selection, two readers with different reach ------------------------------


def capture_last_select(root: Path, task_id: str) -> str:
    """`rein lens --select code --task <id>`'s output, as a reviewer would be handed it."""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        assert lens_cmd.main(["--select", "code", "--task", task_id, "--repo", str(root)]) == 0
    return buffer.getvalue()


def _task(task_id: str, scope_include: list[str]) -> Any:
    from tests._support import make_task

    return models.Task(make_task(task_id, claim_ids=["C-001"], scope_include=scope_include))


def test_a_path_lens_is_dropped_for_a_task_whose_scope_it_misses() -> None:
    """`Facts.changed` is the union of every task's scope, so one task touching a schema file put
    the schema lens on every other task's review. That is the cost the class system exists to
    avoid, reappearing one level down."""
    schema = _lens(
        "L-SCHEMA", stage="code", when=lenses.Condition(paths=("**/*.schema.json",)), lens_class=lenses.CLASS_STANDARD
    )

    assert lenses.for_task([schema], _task("T-001", ["src/x.schema.json"]), tracked=()) == [schema]
    assert lenses.for_task([schema], _task("T-002", ["src/ui/colors.css"]), tracked=()) == []


def test_a_lens_with_no_path_condition_reaches_every_task() -> None:
    """Its condition is a fact about the plan — how many claims it states, how many tasks it has,
    what relations hold between them — and that is equally true of any task in that plan."""
    counted = _lens("L-COUNT", stage="code", when=lenses.Condition(min_claims=1), lens_class=lenses.CLASS_STANDARD)

    assert lenses.for_task([counted], _task("T-001", ["src/x.schema.json"]), tracked=()) == [counted]
    assert lenses.for_task([counted], _task("T-002", ["docs/notes.md"]), tracked=()) == [counted]


def test_narrowing_can_only_ever_remove() -> None:
    """The invariant the mandate rests on. A path matching one task's scope matches the union of
    every task's scope too, so a lens that survives the narrowing was in the frozen selection
    already — a reviewer cannot widen what was authorized, only spend it more precisely."""
    library = [
        _lens(
            "L-SCHEMA",
            stage="code",
            when=lenses.Condition(paths=("**/*.schema.json",)),
            lens_class=lenses.CLASS_STANDARD,
        ),
        _lens(
            "L-CI",
            stage="code",
            when=lenses.Condition(paths=(".github/workflows/**",)),
            lens_class=lenses.CLASS_STANDARD,
        ),
        _lens("L-COUNT", stage="code", when=lenses.Condition(min_claims=1), lens_class=lenses.CLASS_STANDARD),
    ]
    tasks = [
        _task("T-001", ["src/x.schema.json"]),
        _task("T-002", [".github/workflows/ci.yml"]),
        _task("T-003", ["README.md"]),
    ]
    frozen = lenses.select(
        library,
        stage="code",
        facts=lenses.Facts(claims=1, tasks=3, changed=tuple(p for t in tasks for p in t.scope_include)),
    )[0]

    for task in tasks:
        assert set(lenses.for_task(frozen, task, tracked=())) <= set(frozen)


def test_a_scope_that_names_a_directory_carries_the_lenses_for_what_is_under_it() -> None:
    """The defect the narrowing shipped with. A scope entry is a subtree — `common.path_covered` is
    this repository's one definition of that — and a lens pattern matches a file, so handing the
    entry straight to fnmatch read the subtree as a filename and took the schema lens off the one
    task that touches schemas. The union hid it: another task naming a file outright kept the lens
    alive plan-wide."""
    schema = _lens(
        "L-SCHEMA", stage="code", when=lenses.Condition(paths=("**/*.schema.json",)), lens_class=lenses.CLASS_STANDARD
    )
    tracked = ("src/rein/data/schema/event.schema.json", "src/ui/colors.css")

    assert lenses.for_task([schema], _task("T-001", ["src/rein/data/schema"]), tracked=tracked) == [schema]
    assert lenses.for_task([schema], _task("T-002", ["src/rein/data/schema/"]), tracked=tracked) == [schema]
    assert lenses.for_task([schema], _task("T-003", ["src/ui"]), tracked=tracked) == []


def test_a_task_that_declares_no_scope_keeps_every_lens() -> None:
    """An empty `include` is unbounded, says the schema. Deciding a path condition against an empty
    list instead makes it hold nowhere, so the widest scope would get the narrowest review."""
    schema = _lens(
        "L-SCHEMA", stage="code", when=lenses.Condition(paths=("**/*.schema.json",)), lens_class=lenses.CLASS_STANDARD
    )

    assert lenses.for_task([schema], _task("T-001", []), tracked=("src/x.schema.json",)) == [schema]


def test_a_scope_naming_what_does_not_exist_yet_is_still_a_name() -> None:
    """A cycle writes files that are not there when it is planned, and the name the plan declares is
    the only thing anybody knows about them."""
    assert lenses.scope_paths(["src/new.schema.json"], ()) == ("src/new.schema.json",)
    assert lenses.scope_paths([], ("src/x.py",)) == ()


def test_the_union_is_unbounded_when_any_task_is() -> None:
    """One task with no scope makes the plan's union unbounded, which is what the integration
    reviewer reads against anyway."""
    from tests._support import make_claim, make_plan, make_task

    plan = models.Plan(
        make_plan(
            claims=[make_claim("C-001")],
            tasks=[
                make_task("T-001", claim_ids=["C-001"]),
                make_task("T-002", claim_ids=["C-001"], scope_include=["src"]),
            ],
        )
    )

    assert lenses.Facts.of(plan, tracked=("src/x.schema.json", "docs/notes.md")).changed == (
        "docs/notes.md",
        "src/x.schema.json",
    )


def test_the_integration_reader_takes_the_selection_unnarrowed() -> None:
    """It reads the tree the merge produced, which is exactly the union the selection was resolved
    against. The unit was never wrong; handing one list to two readers was."""
    library = [
        _lens(
            "L-SCHEMA",
            stage="code",
            when=lenses.Condition(paths=("**/*.schema.json",)),
            lens_class=lenses.CLASS_STANDARD,
        ),
        _lens(
            "L-CI",
            stage="code",
            when=lenses.Condition(paths=(".github/workflows/**",)),
            lens_class=lenses.CLASS_STANDARD,
        ),
    ]
    union = lenses.Facts(claims=1, tasks=2, changed=("src/x.schema.json", ".github/workflows/ci.yml"))

    assert {lens.id for lens in lenses.select(library, stage="code", facts=union)[0]} == {"L-SCHEMA", "L-CI"}


def test_selecting_for_one_task_drops_what_that_task_cannot_carry(tmp_path: Path, config_home: Path) -> None:
    from tests._support import make_task

    repo = _repo(
        tmp_path,
        tasks=[
            make_task("T-001", claim_ids=["C-001"], scope_include=["src/x.schema.json"]),
            make_task("T-002", claim_ids=["C-001"], scope_include=["src/ui/colors.css"]),
        ],
    )
    assert lens_cmd.main(["--select", "code", "--repo", str(tmp_path)]) == 0
    from rein import store as store_mod

    applied = {e.id for e in _plan(store_mod, repo).lenses if e.stage == "code" and e.status == "applied"}
    assert "L-CODE-SCHEMA-DRIFT" in applied  # the union carries it

    library = lenses.library()
    plan = _plan(store_mod, repo)
    unrelated = next(t for t in plan.tasks if t.id == "T-002")
    frozen_ids, _ = lenses.frozen(plan, stage="code")
    kept = {lens.id for lens in lenses.for_task(lenses.by_id(library, frozen_ids)[0], unrelated, tracked=())}
    assert "L-CODE-SCHEMA-DRIFT" not in kept


def test_a_directory_scoped_task_keeps_the_lens_for_what_is_under_it(tmp_path: Path, config_home: Path) -> None:
    """End to end through the command, against a real file listing. The unit tests above pass the
    listing in; this is the path that has to fetch it, and it is where a scope entry stopped being
    a string and became a subtree."""
    import subprocess

    from tests._support import make_task

    _repo(
        tmp_path,
        tasks=[
            make_task("T-001", claim_ids=["C-001"], scope_include=["src/rein/data/schema"]),
            make_task("T-002", claim_ids=["C-001"], scope_include=["src/ui"]),
        ],
    )
    schema = tmp_path / "src" / "rein" / "data" / "schema" / "event.schema.json"
    schema.parent.mkdir(parents=True)
    schema.write_text("{}", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    with_schema = capture_last_select(tmp_path, "T-001")
    without = capture_last_select(tmp_path, "T-002")

    assert "L-CODE-SCHEMA-DRIFT" in with_schema
    assert "L-CODE-SCHEMA-DRIFT" not in without


def test_narrowing_to_a_task_no_plan_holds_is_refused(tmp_path: Path, config_home: Path) -> None:
    """Falling back to the whole list would review one task against a wider selection than the
    reviewer thinks it asked for, with nothing on screen saying so."""
    _repo(tmp_path)

    assert lens_cmd.main(["--select", "code", "--task", "T-404", "--repo", str(tmp_path)]) == 2


def test_task_without_select_is_refused(tmp_path: Path, config_home: Path) -> None:
    _repo(tmp_path)

    assert lens_cmd.main(["--task", "T-001", "--repo", str(tmp_path)]) == 2


# --- the pre-freeze snapshot of what the loop decided and how far it reaches -------


def _events(repo: Any) -> list[Any]:
    from rein import event_chain

    return event_chain.scan(repo.events)[0]


def test_the_pre_freeze_pass_records_the_reaches_it_saw(tmp_path: Path, config_home: Path) -> None:
    """The only "before" a freeze can be compared against. A human moving a decision from `mandate`
    to `local` edits the file, and nothing else in the harness is watching when they do."""
    from tests._support import make_decision

    repo = _repo(tmp_path, decisions=[make_decision("D-001", reach="mandate", settled_by="human")])
    assert lens_cmd.main(["--select", "code", "--repo", str(tmp_path)]) == 0

    from rein import event_chain

    assert event_chain.derived_reaches(_events(repo)) == {"D-001": "mandate"}


def test_the_snapshot_is_not_retaken_when_nothing_moved(tmp_path: Path, config_home: Path) -> None:
    """A record about the draft, not a change to it. Re-recording an unchanged reading would put a
    row in the chain for every drafting command that happened to run."""
    from tests._support import make_decision

    repo = _repo(tmp_path, decisions=[make_decision("D-001")])
    lens_cmd.main(["--select", "code", "--repo", str(tmp_path)])
    before = sum(1 for e in _events(repo) if e.event == "decisions_derived")
    lens_cmd.main(["--select", "code", "--repo", str(tmp_path)])

    assert sum(1 for e in _events(repo) if e.event == "decisions_derived") == before == 1


def test_a_moved_reach_is_snapshotted_without_rewriting_the_plan(tmp_path: Path, config_home: Path) -> None:
    """The snapshot changes nothing in the document, so pairing it with a plan write would re-write
    the plan every time a decision was added."""
    from rein import store as store_mod
    from tests._support import make_decision

    repo = _repo(tmp_path, decisions=[make_decision("D-001", reach="mandate", settled_by="human")])
    lens_cmd.main(["--select", "code", "--repo", str(tmp_path)])
    digest_before = store_mod.read_digest(_plan(store_mod, repo))

    plan = _plan(store_mod, repo)
    raw = json.loads(json.dumps(dict(plan.raw)))
    raw["decisions"] = [make_decision("D-001", reach="local")]
    _write_plan(store_mod, repo, raw)
    digest_after_edit = store_mod.read_digest(_plan(store_mod, repo))
    lens_cmd.main(["--select", "code", "--repo", str(tmp_path)])

    from rein import event_chain

    assert event_chain.derived_reaches(_events(repo)) == {"D-001": "local"}
    assert store_mod.read_digest(_plan(store_mod, repo)) == digest_after_edit != digest_before


def test_the_stats_point_at_where_a_lens_gets_in_not_only_at_what_to_remove() -> None:
    """Every other line of this report argues for removal, and it counts only lenses that exist —
    so a library read through it alone shrinks and never grows, with no threshold anywhere to make
    that visible. Entry is a human's judgement at the retrospective, and the output has to say so
    where the removal advice is read."""
    library = [_lens("L-1", lens_class=lenses.CLASS_STANDARD, applies_when="the cycle states a claim")]
    out = lens_cmd.render_stats(lens_cmd.stats([_applied("L-1", False), _applied("L-1", False)]), library)

    assert "can only ever argue for removal" in out
    # The section that holds the table a lens is written into, not the one holding the root causes
    # it is held against — a pointer at the wrong section is the same defect as a pointer at a
    # document that does not exist, one heading further in.
    assert "section 2 of docs/retrospective.md" in out
    # After the advice it qualifies, like the scope note: a reader holds both by the time they act.
    assert out.index("docs/retrospective.md") > out.index("never found anything")
