"""The lens library: what applies, what is asked about, and what is off (plan §I).

The rule these pin is that a lens is a record with a condition, not a paragraph in a prompt. A
reviewer sent to attack a failure that cannot occur in this change costs a pass over the deliverable
and brings back "attacked, nothing" — while the findings that *are* possible compete with it for
the reader's attention. Over-reviewing is not thorough, so the condition is what decides.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from rein import approve as approve_mod
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


def test_recording_against_a_task_no_plan_holds_is_refused(tmp_path: Path, config_home: Path) -> None:
    """The half `--select` already refused, and the one that was missing. A place the plan does not
    hold is worse than no place: `--grid` matches a row to a column by task id, so a record against
    a name no column carries is invisible on the screen built to show where each lens went — and
    `unplaced` cannot name it either, because that list is the ones with no place at all."""
    from rein import event_chain

    repo = _repo(tmp_path)
    library = lenses.library()
    lens_id = next(lens.id for lens in library if lens.stage == "code")

    argv = ["--record", lens_id, "--found", "yes", "--stage", "code", "--repo", str(tmp_path)]
    assert lens_cmd.main([*argv, "--task", "T-404"]) == 2

    live, _ = event_chain.scan(repo.events)
    assert [event for event in live if event.event == "lens_applied"] == []
    assert lens_cmd.main([*argv, "--task", "T-001"]) == 0


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


# --- the conditions a machine cannot read off the plan -----------------------------
#
# Six packaged lenses are `conditional`, and each of them says in its own words that the plan is
# not where its answer is: "the plan cannot see that; the design document can", "whether it does
# takes reading the diff". Their `when:` blocks are `min_claims: 1` — true of any cycle that
# reaches a gate — so what actually decided them was a human at the mandate, which is *before* the
# document, the ticket and the diff exist.


def _decider(tmp_path: Path, body: str) -> list[str]:
    """A decider on disk. Argv, stdin, stdout — no network in a test, and none in the product."""
    import sys

    script = tmp_path / "decider.py"
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


_ECHO = """
import json, sys
body = json.load(sys.stdin)
print(json.dumps({"answers": {lens: {"probability": 0.9} for lens in body["questions"]}}))
"""

_DENY = """
import json, sys
body = json.load(sys.stdin)
print(json.dumps({"answers": {lens: {"probability": 0.1} for lens in body["questions"]}}))
"""


def test_a_condition_nobody_can_be_asked_about_is_unavailable_never_false() -> None:
    """With no decider configured, nothing was asked. Saying "the condition does not hold" would
    turn the absence of a feature into a finding about the change."""
    from rein import lens_judge

    verdicts = lens_judge.judge(lens_judge.Settings(), state="a design", questions={"L-A": "does it?"})

    assert [v.outcome for v in verdicts] == [lens_judge.UNAVAILABLE]
    assert verdicts[0].probability is None


def test_a_decider_that_does_not_answer_leaves_every_lens_a_candidate() -> None:
    """The degradation runs toward *more* review. That costs tokens and finds nothing; the
    opposite degradation would cost findings."""
    from rein import lens_judge

    def broken(command: object, payload: object, *, timeout: int) -> str:
        raise RuntimeError("connection refused")

    settings = lens_judge.Settings(command=("decide",))
    verdicts = lens_judge.judge(settings, state="a design", questions={"L-A": "?"}, transport=broken)

    assert [v.outcome for v in verdicts] == [lens_judge.UNAVAILABLE]
    assert lens_judge.dropped(verdicts, lens_judge.Settings(command=("decide",), may_drop=True)) == set()


@pytest.mark.parametrize("reply", ["not json at all", "{}", '{"answers": []}', '{"answers": {"L-A": {}}}'])
def test_a_reply_this_cannot_read_is_unavailable_rather_than_guessed_at(reply: str) -> None:
    """A verdict nobody can trace back to what was asked is worth less than no verdict."""
    from rein import lens_judge

    settings = lens_judge.Settings(command=("decide",))
    verdicts = lens_judge.judge(
        settings, state="a design", questions={"L-A": "?"}, transport=lambda c, p, *, timeout: reply
    )

    assert [v.outcome for v in verdicts] == [lens_judge.UNAVAILABLE]


@pytest.mark.parametrize(
    "value",
    [
        "NaN",  # `json.loads` accepts it by default, and it compares false against every threshold
        "Infinity",
        "-Infinity",
        "1.5",
        "-3.0",
    ],
)
def test_a_number_that_is_not_a_probability_is_unavailable_rather_than_a_verdict(value: str) -> None:
    """A probability is a finite number in [0, 1], and being one is what makes the comparison
    against a threshold mean anything. NaN in particular reads as `does_not_hold` — it compares
    false against everything — so a decider answering with one would *drop* a lens while carrying a
    value the audit chain has no canonical form for."""
    from rein import lens_judge

    settings = lens_judge.Settings(command=("decide",), may_drop=True)
    verdicts = lens_judge.judge(
        settings,
        state="a design",
        questions={"L-A": "?"},
        transport=lambda c, p, *, timeout: f'{{"answers": {{"L-A": {{"probability": {value}}}}}}}',
    )

    assert [v.outcome for v in verdicts] == [lens_judge.UNAVAILABLE]
    assert verdicts[0].probability is None
    assert lens_judge.dropped(verdicts, settings) == set()


def test_a_reply_that_is_not_a_probability_leaves_the_cycle_running(tmp_path: Path, config_home: Path) -> None:
    """The end of the same path, and the one that mattered: an unreadable answer has to cost a
    review nobody needed, never the command. `digests.canonical` refuses NaN, so a verdict carrying
    one reaches the chain write and takes `rein lens --select` down with it."""
    nan = "import json, sys\nb = json.load(sys.stdin)\n" + (
        'sys.stdout.write(\'{"answers": {\' + ", ".join(\'"%s": {"probability": NaN}\' % k '
        "for k in b[\"questions\"]) + '}}')\n"
    )
    repo = _judged_repo(tmp_path, _decider(tmp_path, nan), may_drop=True)

    assert lens_cmd.main(["--select", "design", "--repo", str(tmp_path)]) == 0

    assert {row["outcome"] for row in _judgements(repo)[0].detail["verdicts"]} == {"unavailable"}


def test_a_deliverable_is_measured_in_the_bytes_a_transport_carries() -> None:
    """`MAX_STATE` is a size on the wire. Counting characters would let a document outside ASCII
    through at three times the limit it names, and the message says bytes."""
    from rein import lens_judge

    settings = lens_judge.Settings(command=("decide",))
    over = "あ" * (lens_judge.MAX_STATE // 3 + 1)

    assert len(over) < lens_judge.MAX_STATE < len(over.encode("utf-8"))
    verdicts = lens_judge.judge(
        settings, state=over, questions={"L-A": "?"}, transport=lambda c, p, *, timeout: "unreachable"
    )
    assert [v.outcome for v in verdicts] == [lens_judge.UNAVAILABLE]
    assert "bytes" in verdicts[0].reason


def test_the_probability_is_kept_even_when_it_changed_nothing() -> None:
    """A threshold is a knob somebody has to be able to move, and the only thing that makes moving
    it informed is the distribution it would have been applied to. Which side of the line a lens
    fell on says nothing about how far."""
    from rein import lens_judge

    settings = lens_judge.Settings(command=("decide",), threshold=0.5)
    verdicts = lens_judge.judge(
        settings,
        state="a design",
        questions={"L-A": "?", "L-B": "?"},
        transport=lambda c, p, *, timeout: '{"answers": {"L-A": {"probability": 0.51}, "L-B": {"probability": 0.02}}}',
    )

    assert [(v.lens_id, v.outcome) for v in verdicts] == [
        ("L-A", lens_judge.HOLDS),
        ("L-B", lens_judge.DOES_NOT_HOLD),
    ]
    assert [v.probability for v in verdicts] == [0.51, 0.02]


def test_a_verdict_removes_nothing_unless_the_settings_say_it_may() -> None:
    """Judging and acting on the judgement are separate decisions, and `may_drop` off is what
    ships. The period in which a decider's verdicts and a human's own calls at the gate both exist
    is the only one in which they can be read side by side."""
    from rein import lens_judge

    verdicts = [lens_judge.Verdict("L-A", lens_judge.DOES_NOT_HOLD, probability=0.1)]

    assert lens_judge.dropped(verdicts, lens_judge.Settings(command=("d",))) == set()
    assert lens_judge.dropped(verdicts, lens_judge.Settings(command=("d",), may_drop=True)) == {"L-A"}


def test_the_request_carries_the_lens_s_own_words_unchanged() -> None:
    """`applies_when` *is* the question. A second field holding a differently-worded one for the
    machine would be the same claim in two places, and one of them would go stale."""
    from rein import lens_judge

    body = json.loads(lens_judge.request("a design", {"L-A": "the design names a process boundary"}))

    assert body["state"] == "a design"
    assert body["questions"]["L-A"] == {"type": "noul", "instructions": "the design names a process boundary"}


def _judged_repo(tmp_path: Path, command: list[str], *, may_drop: bool = False) -> Any:
    from rein import repo as repo_mod
    from tests._support import SANDBOXED_PROFILES, make_claim, make_config, make_plan, make_state, make_task

    config = make_config(profiles=SANDBOXED_PROFILES)
    config.setdefault("review_policy", {})["lens_judgement"] = {"command": command, "may_drop": may_drop}
    seed_repo(
        tmp_path,
        state=make_state(gates=dict.fromkeys(models.GATE_ENDS, "pending"), plan_status="draft"),
        plan=make_plan(
            claims=[make_claim("C-001", requirement_ids=["R-1"])],
            tasks=[make_task("T-001", claim_ids=["C-001"], scope_include=["src/**"])],
        ),
        config=config,
    )
    design = tmp_path / "docs" / "20-design.md"
    design.parent.mkdir(parents=True, exist_ok=True)
    design.write_text("# Design\n\nOne component, no process boundary.\n", encoding="utf-8")
    return repo_mod.Repo(tmp_path)


def _judgements(repo: Any) -> list[models.Event]:
    from rein import event_chain

    live, _ = event_chain.scan(repo.events)
    return [event for event in live if event.event == "lens_judged"]


def test_nothing_is_removed_before_the_freeze(tmp_path: Path, config_home: Path) -> None:
    """Asked, because `docs/20-design.md` is written at step 4 and the selection is step 6 — the
    deliverable these conditions name exists well before the mandate. Removing nothing, because
    the list on the approval screen has to be the one the plan holds: a human cannot keep or drop
    a lens a decider cut on the way to the gate."""
    repo = _judged_repo(tmp_path, _decider(tmp_path, _DENY), may_drop=True)

    assert lens_cmd.main(["--select", "design", "--repo", str(tmp_path)]) == 0

    judged = _judgements(repo)
    assert len(judged) == 1
    assert {row["outcome"] for row in judged[0].detail["verdicts"]} == {"does_not_hold"}
    named = approve_mod.naming(repo, "mandate")
    assert {row["id"] for row in named["lenses"]} >= {row["lens"] for row in judged[0].detail["verdicts"]}


def test_a_stage_whose_deliverable_is_not_written_yet_asks_nothing_and_records_nothing(
    tmp_path: Path, config_home: Path
) -> None:
    """Nothing was asked, so there is no judgement to record — the same reason a repository with
    no decider records none. An event per call would fill the chain with the absence of an input,
    and reading one back later would let a document that did not exist yet decide a cycle."""
    repo = _judged_repo(tmp_path, _decider(tmp_path, _ECHO))
    (tmp_path / "docs" / "20-design.md").unlink()

    assert lens_cmd.main(["--select", "design", "--repo", str(tmp_path)]) == 0

    assert _judgements(repo) == []


def test_an_outage_does_not_close_the_question(tmp_path: Path, config_home: Path) -> None:
    """An `unavailable` is not an answer. The decider was down, or the reply was unreadable;
    nothing was settled, and reading that back as though it had been would let one outage decide a
    whole cycle. The record of the outage stays — it is a fact about what happened — and the
    question stays open for the call that can answer it."""
    repo = _judged_repo(tmp_path, _decider(tmp_path, "import sys\nsys.exit(3)\n"))
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    assert {row["outcome"] for row in _judgements(repo)[0].detail["verdicts"]} == {"unavailable"}

    (tmp_path / "decider.py").write_text(_ECHO, encoding="utf-8")
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    judged = _judgements(repo)
    assert len(judged) == 2, "the outage is recorded, and it did not answer the question"
    assert {row["outcome"] for row in judged[-1].detail["verdicts"]} == {"holds"}


def test_a_lens_the_last_recording_never_covered_reopens_the_question(tmp_path: Path, config_home: Path) -> None:
    """The selection can grow between two hand-offs, and a lens nobody was asked about has no
    answer to read back — however many answers sit beside it."""
    from rein import lens_judge

    answered = [lens_judge.Verdict("L-A", lens_judge.HOLDS, probability=0.9)]
    event = _judged(("L-A", 0.9))

    assert lens_cmd._recorded([event], "design", "", {"L-A": "?"}) == answered
    assert lens_cmd._recorded([event], "design", "", {"L-A": "?", "L-B": "?"}) == []


def test_the_conditional_half_is_decided_against_the_deliverable_after_the_freeze(
    tmp_path: Path, config_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _judged_repo(tmp_path, _decider(tmp_path, _ECHO))
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    _approve_mandate(repo)
    capsys.readouterr()

    assert lens_cmd.main(["--select", "design", "--repo", str(tmp_path)]) == 0

    judged = _judgements(repo)
    assert len(judged) == 1
    detail = judged[0].detail
    assert detail["stage"] == "design" and detail["threshold"] == 0.5
    outcomes = {row["lens"]: row["outcome"] for row in detail["verdicts"]}
    assert outcomes and set(outcomes.values()) == {"holds"}
    # The probability is on the record even though it decided nothing here.
    assert all(row["probability"] == 0.9 for row in detail["verdicts"])
    assert "decided against this stage's deliverable" in capsys.readouterr().out


def test_the_hand_off_asks_once_and_reads_the_answer_back(tmp_path: Path, config_home: Path) -> None:
    """A judgement re-run on every call would let a reviewer and the person who approved the gate
    hold two different answers to one question — the property the freeze exists to remove."""
    repo = _judged_repo(tmp_path, _decider(tmp_path, _ECHO))
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    _approve_mandate(repo)

    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    # The decider is replaced between the two calls. If it were asked again the answer would move.
    (tmp_path / "decider.py").write_text(_DENY, encoding="utf-8")
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    judged = _judgements(repo)
    assert len(judged) == 1
    assert {row["outcome"] for row in judged[0].detail["verdicts"]} == {"holds"}


def test_a_verdict_that_may_drop_narrows_the_hand_off_and_not_the_plan(
    tmp_path: Path, config_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """It can only ever remove, and only from what this reviewer is sent to look for. What the
    mandate approved is the ceiling, and the frozen record of it does not move."""
    from rein import store as store_mod

    repo = _judged_repo(tmp_path, _decider(tmp_path, _DENY), may_drop=True)
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    frozen = [dict(entry.raw) for entry in _plan(store_mod, repo).lenses]
    _approve_mandate(repo)
    capsys.readouterr()

    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    out = capsys.readouterr().out
    assert "0 proposed" in out
    assert "does_not_hold" in out
    # The audit guarantee is untouched: the plan still records every lens the human approved.
    assert [dict(entry.raw) for entry in _plan(store_mod, repo).lenses] == frozen


def test_a_verdict_the_settings_do_not_act_on_says_so(
    tmp_path: Path, config_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _judged_repo(tmp_path, _decider(tmp_path, _DENY))
    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])
    _approve_mandate(repo)
    capsys.readouterr()

    lens_cmd.main(["--select", "design", "--repo", str(tmp_path)])

    out = capsys.readouterr().out
    assert "does_not_hold" in out
    assert "recorded only" in out
    assert "0 proposed" not in out


# --- the threshold is a knob, and the record is what makes moving it informed -------


def _judged(*rows: tuple[str, float | None], threshold: float = 0.5, cycle: str = "demo-cycle") -> models.Event:
    from rein import event_chain

    verdicts: list[dict[str, Any]] = []
    for lens_id, probability in rows:
        if probability is None:
            verdicts.append({"lens": lens_id, "outcome": "unavailable"})
        elif probability >= threshold:
            verdicts.append({"lens": lens_id, "outcome": "holds", "probability": probability})
        else:
            verdicts.append({"lens": lens_id, "outcome": "does_not_hold", "probability": probability})
    return event_chain.make(
        "lens_judged",
        cycle,
        subject_ids=sorted(lens_id for lens_id, _p in rows),
        detail={"stage": "design", "task": "", "threshold": threshold, "may_drop": False, "verdicts": verdicts},
    )


def test_a_verdict_that_could_not_be_taken_is_counted_and_never_folded() -> None:
    """An outage and a decision that nothing applies are different facts, and a tally that cannot
    tell them apart reads the first as a finding about the library."""
    rows = lens_cmd.verdicts([_judged(("L-1", 0.9), ("L-2", 0.1), ("L-3", None))])

    assert lens_cmd.verdict_counts(rows) == {
        "L-1": {"judged": 1, "holds": 1, "does_not_hold": 0, "unavailable": 0},
        "L-2": {"judged": 1, "holds": 0, "does_not_hold": 1, "unavailable": 0},
        "L-3": {"judged": 1, "holds": 0, "does_not_hold": 0, "unavailable": 1},
    }


def test_the_counterfactual_is_over_the_answers_not_the_outcomes() -> None:
    """Which side of the line a verdict fell on says nothing about how far, which is the whole
    reason the probability is kept when it changed nothing. An `unavailable` one is not a number on
    the wrong side of a line — no setting of the knob would have changed it."""
    rows = lens_cmd.verdicts([_judged(("L-1", 0.95), ("L-2", 0.55), ("L-3", 0.2), ("L-4", None))])

    assert lens_cmd.at_threshold(rows, 0.5) == (2, 1)
    assert lens_cmd.at_threshold(rows, 0.7) == (1, 2)
    assert lens_cmd.at_threshold(rows, 0.1) == (3, 0)


def test_the_report_shows_what_another_threshold_would_have_given() -> None:
    events = [_judged(("L-1", 0.95), ("L-2", 0.2))]
    out = lens_cmd.render_stats(lens_cmd.stats(events), lenses.library(), events=events)

    assert "2 verdict(s) on 2 conditional lens(es), at threshold 0.50" in out
    assert "had the threshold been set elsewhere" in out
    assert "0.70      1 would hold,    1 would not" in out
    assert "<- in force" in out
    # A knob a report turns by itself is not a knob anybody has to be able to move.
    assert "Nothing here changes it" in out


def test_the_other_arm_is_counted_and_says_it_is_a_lower_bound() -> None:
    """Reading only "applied and never found" makes a narrowing selection look better the more it
    removes. This is what the chain can say about the opposite error, and what it cannot."""
    events = [_judged(("L-1", 0.2)), _applied("L-1", True), _judged(("L-2", 0.2)), _applied("L-2", False)]

    out = lens_cmd.render_stats(lens_cmd.stats(events), lenses.library(), events=events)

    assert lens_cmd.found_anyway(lens_cmd.verdicts(events), events) == ["L-1"]
    assert "found something in the same cycle anyway: L-1" in out
    assert "lower bound, not a rate" in out


def test_a_verdict_in_another_cycle_is_not_that_cycle_s_miss() -> None:
    """The pairing is per cycle. A lens judged not to apply last month and applied today is two
    facts about two changes, and joining them would invent a miss nobody had."""
    events = [_judged(("L-1", 0.2), cycle="cycle-1"), _applied("L-1", True)]

    assert lens_cmd.found_anyway(lens_cmd.verdicts(events), events) == []


def test_the_verdict_half_is_printed_before_the_notes_that_bound_it() -> None:
    """Both notes are about what the whole report may be read to justify. A section arriving after
    the scope note would be numbers with nothing saying how far they reach."""
    events = [_judged(("L-1", 0.95))]
    out = lens_cmd.render_stats(lens_cmd.stats(events), lenses.library(), events=events)

    assert out.index("conditional lens(es), at threshold") < out.index("shared across every repository")


#: Every module under `src/rein` allowed to reach a recorded verdict, and what for. `models` holds
#: the event vocabulary; `lens_cmd` writes one, reads it back for the same hand-off, and reports.
#: `lens_judge` is not here because it never reads a stored one — it makes them.
VERDICT_READERS = {"models", "lens_cmd"}

#: The event kind, which is what a chain reader has to match on to find a verdict at all.
VERDICT_EVENT = "lens_judged"

#: The module that produces verdicts. Importing it, or reaching anything through it, is a path to
#: a probability, so the import itself counts.
JUDGE_MODULE = "lens_judge"

#: The `lens_cmd` functions that hand back a *stored* probability. Qualified rather than bare:
#: `verdicts` and `_recorded` are ordinary words this package already uses for other things
#: (`review`, `build_loop`, `evidence_cmd`), and a check that flagged those would be noise nobody
#: could keep passing.
VERDICT_READING_CALLS = frozenset(
    {
        "verdicts",
        "verdict_counts",
        "at_threshold",
        "found_anyway",
        "render_verdicts_stats",
        "_verdict_state",
        "_recorded",
    }
)


def _verdict_references(tree: ast.AST) -> set[str]:
    """Every way this module could reach a recorded verdict, named.

    Read as references rather than as text. `lens_cmd.verdicts(events)` hands back the recorded
    probabilities and the word `lens_judged` lives in *its* body, not in its caller's — so a check
    that matched the file's characters would have let any module in the package read them by
    importing one function.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and node.value == VERDICT_EVENT:
            found.add(VERDICT_EVENT)
        elif isinstance(node, ast.alias) and node.name.rsplit(".", 1)[-1] == JUDGE_MODULE:
            found.add(f"import {JUDGE_MODULE}")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").rsplit(".", 1)[-1] == JUDGE_MODULE:
            found.add(f"import {JUDGE_MODULE}")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == JUDGE_MODULE:
                found.add(f"{JUDGE_MODULE}.{node.attr}")
            elif node.value.id == "lens_cmd" and node.attr in VERDICT_READING_CALLS:
                found.add(f"lens_cmd.{node.attr}")
    return found


def _modules_naming_a_verdict() -> set[str]:
    source_root = Path(__file__).resolve().parent.parent / "src" / "rein"
    out: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        if _verdict_references(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            out.add(path.stem)
    return out


def test_only_these_modules_read_a_recorded_verdict() -> None:
    """The invariant CR-29 rests on, fixed against the source rather than remembered.

    A probability becomes an input the moment something consults it to decide. The guarantee is
    that there is **no path** by which cycle N's verdicts could change cycle N+1's selection — a
    property about every future edit, which no assertion about one report's text can hold. So read
    the source: a verdict reached from `approve`, `build_loop`, `review` or `lenses` fails here,
    and whoever added it decides whether the guarantee or the caller goes.
    """
    assert _modules_naming_a_verdict() == VERDICT_READERS


@pytest.mark.parametrize(
    "borrowed",
    [
        # Neither of these contains the event kind, and both hold every recorded probability.
        "def pick(events):\n    return [p for _c, _l, p, _t in lens_cmd.verdicts(events)]\n",
        "from rein import lens_judge\n\ndef pick(rows):\n    return [lens_judge.Verdict.from_dict(r) for r in rows]\n",
    ],
)
def test_the_check_catches_a_reader_that_never_spells_the_event_kind(borrowed: str) -> None:
    """The failure mode the text match had: a module can reach the probabilities through one
    import and never write the characters a grep for the event kind would look for."""
    assert VERDICT_EVENT not in borrowed
    assert _verdict_references(ast.parse(borrowed))


def test_the_selection_is_resolved_without_ever_seeing_a_verdict() -> None:
    """The other half of the same guarantee, from the behaviour rather than the source. `resolve`
    is a function of the library and the plan's facts, and there is no argument through which a
    past answer could reach it."""
    import inspect

    assert "event" not in inspect.signature(lenses.resolve).parameters
    assert "event" not in inspect.signature(lenses.select).parameters
    assert "event" not in inspect.signature(lenses.Facts.of).parameters


# --- where each lens went, and where it did not ------------------------------------


def _grid_plan(**kwargs: Any) -> Any:
    from tests._support import make_plan

    return models.Plan(make_plan(**kwargs))


def _entry(lens_id: str, stage: str = "design", status: str = "applied") -> dict[str, str]:
    return {"id": lens_id, "stage": stage, "status": status}


def _cells(built: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {row["lens"]: {cell["column"]: cell["state"] for cell in row["cells"]} for row in built["rows"]}


def test_three_absences_with_different_provenance_stay_three_things() -> None:
    """`dropped` is named in `lens_selected` and gone from the frozen plan, `absent` is in neither,
    `pending` is in both with nothing recorded. The tally already refuses to guess between the last
    one's three causes, and a grid that collapsed them would be guessing for it."""
    library = [_lens("L-KEPT"), _lens("L-DROPPED"), _lens("L-NEVER")]
    plan = _grid_plan(lenses=[_entry("L-KEPT")], tasks=[])
    events = [_selected("L-KEPT", "L-DROPPED")]

    built = lens_cmd.grid(plan, library, events)

    assert _cells(built) == {
        "L-KEPT": {"(plan)": "pending"},
        "L-DROPPED": {"(plan)": "dropped"},
        "L-NEVER": {"(plan)": "absent"},
    }
    assert set(built["meaning"]) >= {"dropped", "absent", "pending"}


def test_applied_and_found_is_not_the_same_colour_as_applied_and_found_nothing() -> None:
    """ "Found nothing" is a fact about this change. A lens that keeps finding nothing is a fact
    about the lens, and that is the tally's question, over cycles, not this screen's."""
    library = [_lens("L-1"), _lens("L-2")]
    plan = _grid_plan(lenses=[_entry("L-1"), _entry("L-2")], tasks=[])
    events = [_selected("L-1", "L-2"), _applied("L-1", True), _applied("L-2", False)]

    assert _cells(lens_cmd.grid(plan, library, events)) == {
        "L-1": {"(plan)": "found"},
        "L-2": {"(plan)": "applied"},
    }


def test_a_code_lens_gets_a_column_per_task_and_the_others_get_the_plan() -> None:
    """`requirements`, `design` and `tasks` are judged against the plan as a whole. A column per
    task there would copy one answer across the row and invite it to be read as several."""
    from tests._support import make_claim, make_task

    library = [
        _lens("L-CODE", stage="code", when=lenses.Condition(paths=("**/*.sql",))),
        _lens("L-DESIGN"),
    ]
    plan = _grid_plan(
        claims=[make_claim("C-001", requirement_ids=["R-1"])],
        tasks=[
            make_task("T-001", claim_ids=["C-001"], scope_include=["db/schema.sql"]),
            make_task("T-002", claim_ids=["C-001"], scope_include=["src/app.py"]),
        ],
        lenses=[_entry("L-CODE", stage="code"), _entry("L-DESIGN")],
    )
    events = [_selected("L-CODE", "L-DESIGN")]

    built = lens_cmd.grid(plan, library, events, tracked=("db/schema.sql", "src/app.py"))

    cells = _cells(built)
    assert built["columns"] == ["(plan)", "T-001", "T-002"]
    # Every row carries every column, and the ones it was never going to answer for say so rather
    # than stopping short — a gap is a state on the screen that the key does not explain.
    assert cells["L-CODE"] == {"(plan)": "n/a", "T-001": "pending", "T-002": "narrowed"}
    assert cells["L-DESIGN"] == {"(plan)": "pending", "T-001": "n/a", "T-002": "n/a"}
    assert built["meaning"]["n/a"]


def test_a_verdict_shows_where_it_removed_a_lens_and_where_it_could_not_be_taken() -> None:
    """A `does_not_hold` the settings did not act on is not a cell state: the lens went to the
    reviewer, and the grid has to show where it went."""
    from rein import event_chain

    library = [_lens("L-DECLINED"), _lens("L-UNJUDGED"), _lens("L-KEPT")]
    plan = _grid_plan(lenses=[_entry("L-DECLINED"), _entry("L-UNJUDGED"), _entry("L-KEPT")], tasks=[])
    judged = event_chain.make(
        "lens_judged",
        "demo-cycle",
        subject_ids=["L-DECLINED", "L-KEPT", "L-UNJUDGED"],
        detail={
            "stage": "design",
            "task": "",
            "threshold": 0.5,
            "may_drop": True,
            "verdicts": [
                {"lens": "L-DECLINED", "outcome": "does_not_hold", "probability": 0.12},
                {"lens": "L-UNJUDGED", "outcome": "unavailable"},
                {"lens": "L-KEPT", "outcome": "holds", "probability": 0.8},
            ],
        },
    )

    built = lens_cmd.grid(plan, library, [_selected("L-DECLINED", "L-UNJUDGED", "L-KEPT"), judged])

    cells = _cells(built)
    assert cells["L-DECLINED"]["(plan)"] == "declined"
    assert cells["L-UNJUDGED"]["(plan)"] == "unjudged"
    assert cells["L-KEPT"]["(plan)"] == "pending"
    declined = next(row for row in built["rows"] if row["lens"] == "L-DECLINED")
    # How far below the line, not merely which side — the same reason the probability is recorded.
    assert declined["cells"][0]["probability"] == 0.12


def test_a_verdict_the_settings_did_not_act_on_leaves_the_cell_alone() -> None:
    from rein import event_chain

    library = [_lens("L-1")]
    plan = _grid_plan(lenses=[_entry("L-1")], tasks=[])
    judged = event_chain.make(
        "lens_judged",
        "demo-cycle",
        subject_ids=["L-1"],
        detail={
            "stage": "design",
            "task": "",
            "threshold": 0.5,
            "may_drop": False,
            "verdicts": [{"lens": "L-1", "outcome": "does_not_hold", "probability": 0.1}],
        },
    )

    assert _cells(lens_cmd.grid(plan, library, [_selected("L-1"), judged]))["L-1"]["(plan)"] == "pending"


def test_an_application_with_no_place_is_named_rather_than_put_somewhere() -> None:
    """Recorded before `--stage`/`--task` existed, or by a reviewer that did not pass them. Placing
    it in one column would be the grid inventing a fact the record does not hold."""
    library = [_lens("L-1", stage="code")]
    plan = _grid_plan(lenses=[_entry("L-1", stage="code")], tasks=[])
    built = lens_cmd.grid(plan, library, [_selected("L-1"), _applied("L-1", True)])

    assert built["unplaced"] == ["L-1"]
    assert "recorded without a stage or task" in lens_cmd.render_grid(built)


def test_the_terminal_grid_prints_no_state_the_key_does_not_explain() -> None:
    """Every mark in the table is a state with a line in the key. A row that stopped short of the
    columns it does not answer for left the renderer filling the gap with a dash — a fourth absence
    beside three whose difference is the whole of what this screen is for, and the one nothing
    explained."""
    from tests._support import make_claim, make_task

    library = [_lens("L-CODE", stage="code"), _lens("L-DESIGN")]
    plan = _grid_plan(
        claims=[make_claim("C-001", requirement_ids=["R-1"])],
        tasks=[make_task("T-001", claim_ids=["C-001"])],
        lenses=[_entry("L-CODE", stage="code"), _entry("L-DESIGN")],
    )
    built = lens_cmd.grid(plan, library, [_selected("L-CODE", "L-DESIGN")])

    table = lens_cmd.render_grid(built).split("\n\n")[0]
    marks = {word for line in table.split("\n")[1:] for word in line.split()[1:]}
    assert marks and marks <= set(built["meaning"])


def test_the_grid_says_how_far_its_numbers_reach() -> None:
    """The cells are one repository's cycle; the library they are about is user-global (CR-8)."""
    built = lens_cmd.grid(_grid_plan(tasks=[]), [_lens("L-1")], [])

    assert "shared across every repository" in built["scope_note"]
    assert built["scope_note"] in lens_cmd.render_grid(built)
