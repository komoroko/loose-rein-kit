"""A premise nobody measured is data, observed before anything rests on it (CR-39, #92).

T-029's cycle froze criteria written against `--max-turns 1` returning `error_max_turns`. The CLI
said `success`, the criteria could not be met, and the correction — "one of three fixtures is not
obtainable; verify that branch with a fake" — cost a mandate roll back, four human interactions
and two paid adversarial reviews. Here the plan states the premise, how to observe it, and what to
do if it is false; the loop observes it and applies what was approved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from rein import build_loop, common, events, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import agent_envelope, make_config, make_plan, make_state, make_task, seed_repo

WORK_BRANCH = "build/demo"
GATE = [{"name": "test", "kind": "command", "command": ["true"], "executor_profile": "quality", "retries": 0}]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _plan(probe: list[str], *, fallback: bool, extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    observer = make_task("T-029", claim_ids=["C-001"], title="collect fixtures from the real CLI")
    resting = make_task(
        "T-026",
        kind="parallel",
        blocked_by=["T-029"],
        claim_ids=["C-001"],
        acceptance=[
            {
                "id": "A-2",
                "statement": "the TRUNCATED branch is verified against the real error_max_turns fixture",
                "evidence": {"kind": "command", "command": ["false"]},
                "assumes": ["P-3"],
            }
        ],
    )
    plan = make_plan(tasks=[observer, resting, *(extra or [])])
    premise: dict[str, Any] = {
        "id": "P-3",
        "says": "--max-turns 1 yields error_max_turns",
        "probe": probe,
        "observed_by": "T-029",
    }
    if fallback:
        premise["fallback"] = {
            "says": "if false, the TRUNCATED branch is verified with a factory fake; fixtures 3 -> 2",
            "criteria": [
                {
                    "task": "T-026",
                    "id": "A-2",
                    "statement": "the TRUNCATED branch is verified with a factory fake",
                    "evidence": {"kind": "command", "command": ["true"]},
                }
            ],
        }
    plan["premises"] = [premise]
    return plan


def seeded(tmp_path: Path, plan: dict[str, Any]) -> repo_mod.Repo:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    seed_repo(
        root,
        config=make_config(branch=WORK_BRANCH, quality_gate=GATE, launch_retries=0),
        plan=plan,
        state=make_state(plan_status="frozen"),
    )
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    git(root, "checkout", "-q", "-b", WORK_BRANCH)
    return repo_mod.Repo(root)


def implementer(launched: list[str]) -> object:
    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: object) -> tuple[int, str]:
        if not cmd or cmd[0] != "claude":
            return common.run(cmd, cwd, timeout)
        where = Path(cwd or ".")
        launched.append(where.name)
        (where / f"{where.name}.py").write_text("# done\n", encoding="utf-8")
        return 0, agent_envelope("")

    return _run


def build(repo: repo_mod.Repo) -> int:
    return build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=False, repo=repo).run()


def state_of(repo: repo_mod.Repo) -> dict[str, Any]:
    raw = store_mod.Store(repo).read_raw("state")
    assert raw is not None
    return dict(raw)


def test_a_falsified_premise_with_an_approved_fallback_is_applied_without_a_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, _plan(["false"], fallback=True))
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", implementer(launched))

    assert build(repo) == common.EXIT_DONE
    assert launched == ["T-029", "T-026"], "the observer first, then what rests on the observation"
    state = state_of(repo)
    assert state["tasks"]["T-026"]["status"] == "done"
    assert state["premises"]["P-3"]["status"] == "falsified"
    assert state["gates"]["mandate"]["status"] == "approved", "no roll back"
    chain = store_mod.Store(repo).read_events()
    [observed] = [e for e in chain if e.detail.get("kind") == "premise_falsified"]
    assert observed.detail["fallback"] is True
    assert events.stop_causes(chain).get("premise", 0) == 0
    assert events.premise_outcomes(chain) == {"with_fallback": 1, "without_fallback": 0}


def test_a_held_premise_leaves_the_criterion_as_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Here the criterion as written (`false`) is what runs, and fails: nothing was swapped."""
    repo = seeded(tmp_path, _plan(["true"], fallback=True))
    monkeypatch.setattr(build_loop, "_run", implementer([]))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    state = state_of(repo)
    assert state["premises"]["P-3"]["status"] == "held"
    assert state["tasks"]["T-026"]["status"] == "blocked"


def test_a_falsified_premise_without_a_fallback_parks_only_what_rests_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    beside = make_task("T-030", kind="parallel", claim_ids=["C-001"])
    repo = seeded(tmp_path, _plan(["false"], fallback=False, extra=[beside]))
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert "T-026" not in launched
    state = state_of(repo)
    assert state["tasks"]["T-026"]["status"] == "needs-revision"
    assert state["tasks"]["T-030"]["status"] == "done", "everything else continues"
    assert state["gates"]["mandate"]["status"] == "approved"
    chain = store_mod.Store(repo).read_events()
    assert events.stop_causes(chain)["premise"] == 1
    assert events.premise_outcomes(chain) == {"with_fallback": 0, "without_fallback": 1}


def test_nothing_resting_on_a_premise_runs_before_it_is_observed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = seeded(tmp_path, _plan(["true"], fallback=True))
    build_loop.set_task_status(repo, "T-029", "blocked")
    launched: list[str] = []
    monkeypatch.setattr(build_loop, "_run", implementer(launched))

    assert build(repo) == common.EXIT_HUMAN_NEEDED
    assert launched == []
    assert "premises" not in state_of(repo)


def _errors(plan: dict[str, Any]) -> list[str]:
    return models.cross_reference_errors(models.Plan(plan))


def test_a_fallback_may_only_replace_what_rests_on_its_premise() -> None:
    plan = _plan(["true"], fallback=True)
    plan["tasks"][1]["acceptance"][0]["assumes"] = []
    assert any("does not assume P-3" in e for e in _errors(plan))


def test_an_observer_that_rests_on_its_own_premise_is_refused() -> None:
    plan = _plan(["true"], fallback=False)
    plan["premises"][0]["observed_by"] = "T-026"
    assert any("the observation would wait for itself" in e for e in _errors(plan))


def test_a_falsified_premise_nothing_unfinished_rests_on_is_recorded_and_not_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An escalation about a premise rather than a task would never be closed by anything: it has no
    task to finish. With nothing left resting on it there is nothing to decide, so nobody is asked."""
    after_observer = make_task("T-030", kind="parallel", blocked_by=["T-029"], claim_ids=["C-001"])
    repo = seeded(tmp_path, _plan(["false"], fallback=False, extra=[after_observer]))
    build_loop.set_task_status(repo, "T-026", "done")
    monkeypatch.setattr(build_loop, "_run", implementer([]))

    assert build(repo) == common.EXIT_DONE
    chain = store_mod.Store(repo).read_events()
    assert state_of(repo)["premises"]["P-3"]["status"] == "falsified"
    assert not [e for e in chain if e.event == "knowledge_gap" and e.detail.get("kind") == "premise_falsified"]
    assert events.stops(chain) == events.stops([e for e in chain if e.detail.get("kind") != "premise_falsified"])


def test_a_dry_run_is_not_held_back_by_a_premise_it_cannot_observe(tmp_path: Path) -> None:
    repo = seeded(tmp_path, _plan(["false"], fallback=False))
    loop = build_loop.Orchestrator(build_loop.Config.load(repo), dry_run=True, repo=repo)
    assert loop.run() == common.EXIT_DONE
