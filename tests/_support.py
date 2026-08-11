"""Importable test helpers: the `.rein/` skeleton, document builders, and a git fake.

Every command test needs the same handful of things — a tmp repo carrying the four SSOT
documents, and a stand-in for the real `git` calls. Here they live once; the *fixtures* that
wrap them (``make_repo``, ``chdir_tmp``) are in ``conftest.py``.

Two rules keep these fixtures honest.

**Every document a builder emits validates against its shipped schema.** `seed_repo` is not a
place to hand-write approximate YAML: a test that passes against a document the real tool
would reject is a test that proves nothing. :func:`assert_seeds_are_valid` enforces it, and
``test_support.py`` runs it over every builder default.

**Runtime state goes to tmp_path.** `seed_repo` points ``XDG_RUNTIME_DIR`` at the repo when
asked, so a test never touches the developer's real store lock or journal.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rein import digests, event_chain, models, store

GATE_ORDER = models.GATE_ORDER

DEMO_PROJECT = "demo"
DEMO_CYCLE = "demo-cycle"
DEMO_BRANCH = "build/demo"

#: A 40-hex commit that looks real enough for the schema's `^[0-9a-f]{7,64}$`.
DEMO_COMMIT = "61f3d58c4e0b1122334455667788990011223344"


def _digest(seed: str) -> str:
    """A well-formed, deterministic digest for a fixture (never a real hash of anything)."""
    return digests.of({"fixture": seed})


# --- state.yaml ---------------------------------------------------------------


def make_state(
    *,
    phase: str = "build",
    gates: dict[str, str] | None = None,
    project: str = DEMO_PROJECT,
    cycle_id: str = DEMO_CYCLE,
    plan_status: str = "frozen",
    tasks: dict[str, str] | None = None,
    updated_at: str = "2026-07-23T10:00:00+09:00",
) -> dict[str, Any]:
    """A state document; `gates` overrides the mid-build baseline (approved through tasks).

    An approved gate automatically gets a receipt, because the schema refuses one without —
    which is the point: there is no such thing as an approval with nothing behind it.
    """
    resolved = {
        "requirements": "approved",
        "design": "approved",
        "tasks": "approved",
        "build": "pending",
        "release": "pending",
    }
    resolved.update(gates or {})

    gate_block: dict[str, Any] = {}
    for name in GATE_ORDER:
        status = resolved[name]
        gate_block[name] = {
            "status": status,
            "receipt": make_receipt(name) if status == "approved" else None,
        }

    document: dict[str, Any] = {
        "project": project,
        "cycle_id": cycle_id,
        "current_phase": phase,
        "updated_at": updated_at,
        "gates": gate_block,
        "plan": {"status": plan_status, "digest": _digest("plan")},
        "execution": {"status": "idle"},
        "review": {"status": "none"},
        "tasks": {tid: {"status": status} for tid, status in (tasks or {}).items()},
    }
    return document


def make_receipt(gate: str) -> dict[str, Any]:
    """A schema-valid gate receipt naming an approval id derived from the gate."""
    return {
        "approval_id": f"GA-{gate.upper()}-0001",
        "validation_digest": _digest(f"validation:{gate}"),
        "attested_chain_root": _digest(f"chain:{gate}"),
        "result_chain_root": _digest(f"chain:{gate}"),
    }


# --- plan.yaml ----------------------------------------------------------------


def make_claim(
    claim_id: str = "C-001",
    *,
    risk: str = "low",
    requirement_ids: list[str] | None = None,
) -> dict[str, Any]:
    """A schema-valid claim. `requirement_ids` defaults to R-1 — the traceability thread's near end.

    Defaulting it is not decoration: `dag_trace` reads it to answer "which requirement does this
    claim state", and a claim carrying none makes the trace report `unknown` rather than pass.
    """
    return {
        "id": claim_id,
        "statement": f"{claim_id} holds",
        "risk": risk,
        "requirement_ids": requirement_ids if requirement_ids is not None else ["R-1"],
    }


def make_task(
    task_id: str = "T-001",
    *,
    kind: str = "foundation",
    blocked_by: list[str] | None = None,
    claim_ids: list[str] | None = None,
    risk: str = "low",
    title: str = "",
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "id": task_id,
        "title": title or f"task {task_id}",
        "kind": kind,
        "risk": risk,
    }
    if blocked_by:
        task["blocked_by"] = blocked_by
    if claim_ids is not None:
        task["claim_ids"] = claim_ids
    return task


def make_plan(
    *,
    cycle_id: str = DEMO_CYCLE,
    branch: str = DEMO_BRANCH,
    claims: list[dict[str, Any]] | None = None,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A plan document. The default is one claim covering R-1 and one task answering it."""
    return {
        "cycle": {"id": cycle_id, "base_commit": DEMO_COMMIT, "branch": branch},
        "claims": claims if claims is not None else [make_claim()],
        "tasks": tasks if tasks is not None else [make_task(claim_ids=["C-001"])],
    }


# --- review.yaml --------------------------------------------------------------


def make_review(
    *,
    generated: bool = False,
    coverage_status: str = "sufficient",
    human_status: str = "not_started",
    extra_behaviors: list[dict[str, Any]] | None = None,
    security_findings: list[dict[str, Any]] | None = None,
    effective_risk: str = "",
    gaps: list[dict[str, Any]] | None = None,
    analyzed_bytes: int = 0,
    unsupported_files: list[dict[str, Any]] | None = None,
    review_budget: list[dict[str, Any]] | None = None,
    base_sha: str = "",
    head_sha: str = "",
) -> dict[str, Any]:
    """A review document. `generated=False` is the honest empty state, not an empty result.

    `effective_risk` left empty is itself a case worth testing: a review that does not record
    what it weighed is read as `high`, so the strict path applies.
    """
    if not generated:
        return {"machine": {"status": "not_generated"}, "human": {"status": human_status}}
    machine: dict[str, Any] = {
        "status": "generated",
        "binding": {
            "change_digest": _digest("change"),
            "plan_digest": _digest("plan"),
            "toolchain_digest": _digest("toolchain"),
        },
        "coverage": [
            {
                "diff_digest": _digest("diff"),
                "analyzed_files": 3,
                "truncated": False,
                "coverage_status": coverage_status,
            }
        ],
        "actual_extraction": [],
        "claims": [],
        "extra_behaviors": extra_behaviors or [],
        "security": {"findings": security_findings or []},
    }
    if analyzed_bytes:
        machine["coverage"][0]["analyzed_bytes"] = analyzed_bytes
    if unsupported_files:
        machine["coverage"][0]["unsupported_files"] = unsupported_files
    if review_budget:
        machine["review_budget"] = review_budget
    if base_sha:
        machine["binding"]["trusted_base_sha"] = base_sha
    if head_sha:
        machine["binding"]["subject_head_sha"] = head_sha
    if effective_risk:
        machine["effective_risk"] = effective_risk
    if gaps:
        machine["gaps"] = gaps
    return {"machine": machine, "human": {"status": human_status}}


# --- config.yaml --------------------------------------------------------------


#: A digest-pinned OCI profile set, for tests that need to get past doctor's sandbox check.
SANDBOXED_PROFILES: dict[str, dict[str, Any]] = {
    name: {
        "kind": "oci",
        "image": f"localhost/rein-{name}@sha256:" + "0" * 64,
        "network_profile": "none",
        "read_only_root": True,
    }
    for name in ("implementer", "reviewer", "quality")
}


def make_config(
    *,
    project: str = DEMO_PROJECT,
    branch: str = DEMO_BRANCH,
    template_mode: bool = False,
    quality_gate: list[dict[str, Any]] | None = None,
    guard_paths: list[dict[str, str]] | None = None,
    profiles: dict[str, dict[str, Any]] | None = None,
    max_parallel: int = 3,
    launch_retries: int | None = None,
) -> dict[str, Any]:
    execution: dict[str, Any] = {"max_parallel": max_parallel, "worktree_dir": ".worktrees"}
    if launch_retries is not None:
        execution["launch_retries"] = launch_retries
    body: dict[str, Any] = {
        "project": {"name": project, "work_branch": branch},
        "execution": execution,
        "executors": {
            "implementer_profile": "implementer",
            "reviewer_profile": "reviewer",
        },
        "executor_profiles": profiles
        or {
            "implementer": {"kind": "host"},
            "reviewer": {"kind": "host"},
            "quality": {"kind": "host"},
        },
        "agents": {
            "implementer": {"adapter": "claude"},
            "code_reviewer": {"adapter": "claude"},
            "actual_extractor": {"adapter": "claude", "independence_group": "claude/opus"},
            "comparator": {"adapter": "claude", "independence_group": "claude/sonnet"},
        },
        "quality_gate": quality_gate
        or [
            {
                "name": "test",
                "kind": "command",
                "command": ["make", "test"],
                "executor_profile": "quality",
                "retries": 2,
                "required": True,
            },
            {
                "name": "check",
                "kind": "command",
                "command": ["make", "check"],
                "executor_profile": "quality",
                "retries": 2,
                "required": True,
            },
        ],
        "guard": {
            "template_mode": template_mode,
            # `is None` rather than falsy: a test that asks for *no* guarded paths must get
            # none, not silently fall back to the defaults it was trying to remove.
            "paths": guard_paths
            if guard_paths is not None
            else [
                {"path": "docs/20-design.md", "requires_gate": "requirements"},
                {"path": "docs/decisions/", "requires_gate": "requirements"},
                {"path": "docs/tasks/", "requires_gate": "design"},
                {"path": "docs/test/", "requires_gate": "build"},
                {"path": "src/", "requires_gate": "tasks"},
                {"path": "lib/", "requires_gate": "tasks"},
                {"path": "app/", "requires_gate": "tasks"},
                {"path": "backend/", "requires_gate": "tasks"},
                {"path": "frontend/", "requires_gate": "tasks"},
                {"path": "scripts/", "requires_gate": "tasks"},
            ],
        },
        "github": {"enabled": False, "label": "rein"},
    }
    return body


# --- seeding ------------------------------------------------------------------

#: The docs scaffold cycle.py archives/restores (name -> body); mirrors the real layout.
_DOCS_SCAFFOLD = {
    "00-product-brief.md": "scaffold: 00-product-brief.md\n",
    "05-current-state.md": "scaffold: 05-current-state.md\n",
    "10-requirements.md": "scaffold: 10-requirements.md\n",
    "20-design.md": "scaffold: 20-design.md\n",
    "retrospective.md": "scaffold: retrospective.md\n",
    "decisions/ADR-template.md": "scaffold: adr\n",
    "tasks/T-template.md": "scaffold: task\n",
    "test/test-plan.md": "scaffold: test-plan\n",
}

_UNSET = object()


def seed_repo(
    root: Path,
    *,
    state: dict[str, Any] | None | object = _UNSET,
    plan: dict[str, Any] | None | object = _UNSET,
    review: dict[str, Any] | None | object = _UNSET,
    config: dict[str, Any] | None | object = _UNSET,
    events: list[models.Event] | None = None,
    settings: str | None = None,
    lock: bool = True,
    docs: bool = False,
    git: bool = False,
) -> Path:
    """Write an `.rein/` skeleton under `root`. Passing `None` for a document skips it.

    Every document written here is validated against its schema first (:func:`_validate`), so
    a fixture cannot drift into a shape the real tool would refuse.
    """
    from rein import lock as lock_mod

    loop = root / ".rein"
    loop.mkdir(parents=True, exist_ok=True)

    documents = {
        "state": make_state() if state is _UNSET else state,
        "plan": make_plan() if plan is _UNSET else plan,
        "review": make_review() if review is _UNSET else review,
        "config": make_config() if config is _UNSET else config,
    }
    # A frozen state must name the digest of the plan sitting beside it. Letting the two drift
    # would make every fixture trip the commit-stage frozen-artifact check for a reason that has
    # nothing to do with the test.
    state_doc, plan_doc = documents["state"], documents["plan"]
    if isinstance(state_doc, dict) and isinstance(plan_doc, dict):
        plan_block = state_doc.get("plan")
        if isinstance(plan_block, dict) and plan_block.get("digest"):
            plan_block["digest"] = models.Plan(plan_doc).digest()

    for name, document in documents.items():
        if document is None:
            continue
        assert isinstance(document, dict)
        _validate(name, document)
        (loop / f"{name}.yaml").write_bytes(store.dump_yaml(document))

    if events:
        event_chain.append_lines(loop / "events.ndjson", events)
    if lock:
        lock_mod.write(loop / "rein.lock", lock_mod.new("0.1.0", ""))
    if settings is not None:
        claude = root / ".claude"
        claude.mkdir(exist_ok=True)
        (claude / "settings.json").write_text(settings, encoding="utf-8")
    if docs:
        for rel, body in _DOCS_SCAFFOLD.items():
            dest = root / "docs" / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(body, encoding="utf-8")
    if git:
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _validate(name: str, document: dict[str, Any]) -> None:
    """Fail loudly here rather than letting an invalid fixture produce a misleading pass."""
    errors = models.schema_errors(document, name)
    if name == "plan" and not errors:
        errors = models.cross_reference_errors(models.Plan(document))
    if errors:
        raise AssertionError(f"test fixture {name}.yaml is not schema-valid:\n" + "\n".join(f"  - {e}" for e in errors))


def chain(*names: str, cycle_id: str = DEMO_CYCLE) -> list[models.Event]:
    """A ready-made chained event list, for tests that need a populated log."""
    built: list[models.Event] = []
    previous: models.Event | None = None
    for name in names:
        linked = event_chain.link(previous, event_chain.make(name, cycle_id))
        built.append(linked)
        previous = linked
    return built


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# --- git fake -----------------------------------------------------------------

RunFake = Callable[..., tuple[int, str]]


def fake_git(
    responses: dict[tuple[str, ...], tuple[int, str]] | None = None,
    *,
    record: list[list[str]] | None = None,
) -> RunFake:
    """A `build_loop._run` stand-in: first command-prefix match wins, default `(0, "")`.

    `record`, if given, receives each `cmd` list as it is called (order preserved).
    """
    rules = responses or {}

    def _run(cmd: list[str], cwd: str | None = None, timeout: float | None = None, **_: Any) -> tuple[int, str]:
        if record is not None:
            record.append(cmd)
        for prefix, result in rules.items():
            if tuple(cmd[: len(prefix)]) == tuple(prefix):
                return result
        return 0, ""

    return _run
