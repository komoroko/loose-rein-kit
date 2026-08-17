"""`rein evidence` — what a human observed that this loop could not.

Most acceptance criteria the build can establish itself: run an argv and read the exit status,
or check that a file exists. Some it never can. A behaviour that only appears against a staging
environment, a device that has to be held, a screen somebody has to look at — the plan can *say*
those, and calling them `external` is more honest than either pretending the loop checked them
or leaving them as prose in a ticket nothing reads.

A task carrying an unestablished `external` criterion reaches `awaiting-evidence`: not failed,
not done, and off the frontier until somebody records what they saw. That is what this command
records.

Three properties make the record worth something:

  **It is bound to a tree.** An observation is about a particular state of the code, so the
  content fingerprint goes into the record. Change the code and the observation stops matching —
  the same rule the evidence ledger and the security findings follow, for the same reason.

  **A leaf cannot write one.** This runs from the canonical checkout only. An implementer that
  could record its own acceptance would be signing off on its own work, which is the one thing
  the whole gate ladder exists to prevent.

  **It needs a terminal.** Not because a pty proves a human — nothing here can — but for the
  narrower property `rein approve` also rests on: a recorded observation cannot happen by
  accident, by a piped stdin, or by a CI job that was configured once and forgotten.

It opens no gate and binds no receipt. It records that somebody looked.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from rein import build_git, build_loop, common, dag, event_chain, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

_NOTE_MAX = 2000


class EvidenceError(RuntimeError):
    """The observation cannot be recorded."""


def outstanding(repo: repo_mod.Repo) -> list[dict[str, Any]]:
    """Every acceptance criterion waiting on a human, with what it asks for.

    Derived rather than stored: the plan says which criteria are `external`, the state says what
    has been recorded, and the working tree says what the code is now. Storing the difference
    would be a fourth place for the same fact to go stale.
    """
    store = store_mod.Store(repo)
    plan, state = store.read_plan(), store.read_state()
    if plan is None or state is None:
        return []
    tree = _tree_fingerprint(repo)
    rows: list[dict[str, Any]] = []
    for task in dag.join(plan, state).tasks:
        recorded = {str(item.get("id")) for item in _recorded(state, task.id) if str(item.get("tree", "")) == tree}
        for entry in task.acceptance:
            spec = entry.get("evidence")
            if not isinstance(spec, dict) or str(spec.get("kind", "")) != "external":
                continue
            ac_id = str(entry.get("id", "?"))
            rows.append(
                {
                    "task": task.id,
                    "id": ac_id,
                    "statement": str(entry.get("statement", "")),
                    "status": task.status,
                    "recorded": ac_id in recorded,
                }
            )
    return rows


def record(repo: repo_mod.Repo, task_id: str, ac_id: str, note: str) -> dict[str, Any]:
    """Write one observation into `state.yaml`, bound to the tree it was made against."""
    if not note.strip():
        raise EvidenceError("an observation with no note records that somebody clicked, not what they saw")
    if not repo.is_canonical_checkout:
        raise EvidenceError(
            "acceptance evidence is recorded from the canonical checkout, never from a leaf worktree — "
            "an agent that can record its own acceptance has signed off on its own work"
        )
    tree = _tree_fingerprint(repo)
    if not tree:
        raise EvidenceError(
            "cannot fingerprint the working tree, so the observation could not be bound to the code it is "
            "about — and an observation that outlives its code is not evidence"
        )
    _assert_declared(repo, task_id, ac_id)
    entry = {"id": ac_id, "tree": tree, "note": note.strip()[:_NOTE_MAX], "recorded_at": event_chain.now_iso()}
    store_mod.retry_on_stale(lambda: _record_once(repo, task_id, entry))
    return entry


def _assert_declared(repo: repo_mod.Repo, task_id: str, ac_id: str) -> None:
    """The criterion has to exist in the frozen plan, and has to be one the loop cannot establish.

    Recording against an invented id would be evidence for nothing; recording against a `command`
    criterion would be a person overriding a check the machine is perfectly able to run.
    """
    plan = store_mod.Store(repo).read_plan()
    task = next((t for t in plan.tasks if t.id == task_id), None) if plan is not None else None
    if task is None:
        raise EvidenceError(f"{task_id} is not a task in the frozen plan")
    entry = next((a for a in task.acceptance if str(a.get("id")) == ac_id), None)
    if entry is None:
        raise EvidenceError(f"{task_id} declares no acceptance criterion {ac_id!r}")
    spec = entry.get("evidence")
    kind = str(spec.get("kind", "")) if isinstance(spec, dict) else ""
    if kind != "external":
        stated = f"kind {kind!r}" if kind else "no evidence at all"
        raise EvidenceError(
            f"{task_id}/{ac_id} declares {stated}, not 'external' — this verb records what the loop cannot "
            "establish, and overriding what it can is not that"
        )


def _record_once(repo: repo_mod.Repo, task_id: str, entry: dict[str, Any]) -> None:
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise EvidenceError("no .rein/state.yaml to record an observation in")
    seen = store_mod.read_digest(state)
    raw = json.loads(json.dumps(state.raw))
    tasks = raw.setdefault("tasks", {})
    task_entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {"status": "todo"}
    kept = [
        item
        for item in task_entry.get("acceptance", [])
        if isinstance(item, dict) and (item.get("id"), item.get("tree")) != (entry["id"], entry["tree"])
    ]
    tasks[task_id] = {**task_entry, "acceptance": [*kept, entry]}
    raw["updated_at"] = event_chain.now_iso()

    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append(
            "decision_declared",
            cycle_id=state.cycle_id,
            actor="local-confirmation",
            subject_ids=[task_id],
            detail={"acceptance": entry["id"], "tree": entry["tree"], "statement": entry["note"][:200]},
        )


def _recorded(state: models.State, task_id: str) -> list[dict[str, Any]]:
    entry = state.raw.get("tasks", {}).get(task_id)
    value = entry.get("acceptance") if isinstance(entry, dict) else None
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _tree_fingerprint(repo: repo_mod.Repo) -> str:
    """The canonical checkout's content fingerprint — the same one the build binds its facts to."""
    workspace = build_git.GitWorkspace(
        repo,
        branch="",
        dry_run=False,
        worktree_dir="",
        branch_pattern="",
        run=build_loop._late_run,
    )
    return workspace.fingerprint(str(repo.root))


# --- CLI ------------------------------------------------------------------------


def _render(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "No acceptance criterion is waiting on an observation."
    lines = ["Acceptance criteria this loop cannot establish:", ""]
    for row in rows:
        mark = "recorded" if row["recorded"] else "WAITING"
        lines.append(f"  [{mark}] {row['task']}/{row['id']}  {row['statement']}")
    lines += [
        "",
        "Record what you observed:",
        "  rein evidence record --task T-NNN --ac A-N --note '<what you saw, and where>'",
        "",
        "It binds the observation to the current content of the tree, so changing the code",
        "retires it — which is the point. Nothing here opens a gate.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein evidence",
        description="acceptance evidence this loop cannot obtain: what is waiting, and recording what you saw",
    )
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("show", help="every external acceptance criterion and whether it has been observed")
    recorder = sub.add_parser("record", help="record an observation you made yourself")
    recorder.add_argument("--task", required=True, help="the task (e.g. T-004)")
    recorder.add_argument("--ac", required=True, help="the acceptance criterion (e.g. A-2)")
    recorder.add_argument("--note", required=True, help="what you observed, and where — a human reads this at gate 4")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        if args.action == "show":
            print(_render(outstanding(repo)))
            return 0
        if not sys.stdin.isatty():
            logger.error(
                "recording an observation needs an interactive terminal. A piped stdin, a CI job or an "
                "agent's captured subprocess would be recording that nobody looked."
            )
            return 1
        entry = record(repo, args.task, args.ac, args.note)
    except (EvidenceError, models.DocumentError, store_mod.StoreError, dag.DagError) as exc:
        logger.error(str(exc))
        return 1
    print(f"recorded {args.task}/{entry['id']} against tree {entry['tree'][:19]}…")
    print("Run `rein build` to let the task finish — it re-checks the tree the observation names.")
    return 0
