"""The task DAG: structure from the frozen plan, status from the mutable state.

Consumption order, parallelism, merge order, and stopping are decided **in code from this
graph**, not by agent discretion (AGENTS.md "Task dependency graph"). That is the whole point
of deriving layers and the critical path here: two runs of the same plan schedule identically,
and a reviewer can predict what `/build` will do before it does it.

The two halves come from different files, so that progress edits and plan edits never touch the
same one:

  ``plan.yaml.tasks``   id, title, kind, blocked_by, claim_ids, risk, scope — frozen at gate ③
  ``state.yaml.tasks``  status, attempts, completed_commit — mutated every iteration

A status entry naming a task the plan does not declare is an error, not a stray key: it means
the plan was rewound without the state following, and scheduling against it would run work
nobody approved.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rein import common, models
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

KIND_VALUES = models.TASK_KIND_VALUES
STATUS_ORDER = models.TASK_STATUS_ORDER
STATUS_VALUES = models.TASK_STATUS_VALUES


class DagError(ValueError):
    """An inconsistency in the graph (cycle, unknown dependency, duplicate id, invalid value)."""


@dataclass(frozen=True)
class Task:
    """One schedulable task: its plan-side structure joined with its state-side status."""

    id: str
    title: str
    kind: str
    blocked_by: tuple[str, ...] = ()
    status: str = "todo"
    risk: str = "low"
    claim_ids: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    attempts: int = 0
    #: The paths this task is allowed to change, as the frozen plan declares them. Empty means
    #: unbounded — which is what an undeclared scope has always meant, and not "nothing". The
    #: plan schema has carried these since it was written; carrying them through the graph is
    #: what finally lets something *check* them, instead of asking an agent nicely in a prompt.
    scope_include: tuple[str, ...] = ()
    scope_exclude: tuple[str, ...] = ()
    #: This task's own acceptance criteria as the frozen plan states them — what it was *for*,
    #: as against the shared DoD's "is this code sound". Prose in a ticket nothing parsed, until
    #: the plan grew a place to put them.
    acceptance: tuple[Mapping[str, Any], ...] = ()

    @property
    def is_done(self) -> bool:
        return self.status == "done"


@dataclass(frozen=True)
class Graph:
    """A validated task DAG. Created only via :meth:`from_tasks` or :func:`load`."""

    tasks: tuple[Task, ...]
    _by_id: dict[str, Task] = field(default_factory=dict)

    @classmethod
    def from_tasks(cls, tasks: list[Task]) -> Graph:
        by_id: dict[str, Task] = {}
        for t in tasks:
            if t.id in by_id:
                raise DagError(f"duplicate task ID: {t.id}")
            if t.kind not in KIND_VALUES:
                raise DagError(f"{t.id}: invalid kind '{t.kind}' (one of {sorted(KIND_VALUES)})")
            if t.status not in STATUS_VALUES:
                raise DagError(f"{t.id}: invalid status '{t.status}' (one of {sorted(STATUS_VALUES)})")
            if t.risk not in models.RISK_VALUES:
                raise DagError(f"{t.id}: invalid risk '{t.risk}' (one of {sorted(models.RISK_VALUES)})")
            by_id[t.id] = t
        for t in tasks:
            for dep in t.blocked_by:
                if dep not in by_id:
                    raise DagError(f"{t.id}: references unknown dependency '{dep}'")
                if dep == t.id:
                    raise DagError(f"{t.id}: depends on itself")
        graph = cls(tasks=tuple(tasks), _by_id=by_id)
        graph._ensure_acyclic()
        return graph

    def get(self, task_id: str) -> Task:
        return self._by_id[task_id]

    def _ensure_acyclic(self) -> None:
        # If Kahn's algorithm cannot extract every node, there is a cycle.
        if len(self._topo_order()) != len(self.tasks):
            raise DagError("the dependency graph has a cycle (it is not a DAG)")

    def _topo_order(self) -> list[str]:
        """A deterministic topological order (ties by ascending id). On a cycle, only what was extracted."""
        indegree = {t.id: len(t.blocked_by) for t in self.tasks}
        dependents = self._dependents_map()
        ready = sorted(tid for tid, d in indegree.items() if d == 0)
        order: list[str] = []
        while ready:
            tid = ready.pop(0)
            order.append(tid)
            newly: list[str] = []
            for child in dependents[tid]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    newly.append(child)
            ready = sorted(ready + newly)  # re-sort each time for determinism
        return order

    def _dependents_map(self) -> dict[str, list[str]]:
        """Each task -> the ids that directly depend on it."""
        dependents: dict[str, list[str]] = {t.id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.blocked_by:
                dependents[dep].append(t.id)
        return dependents

    # ---- derivation -------------------------------------------------------

    def fan_out(self) -> dict[str, int]:
        """How many tasks are directly waiting on each task."""
        return {tid: len(children) for tid, children in self._dependents_map().items()}

    def dependents_closure(self, seed_ids: list[str]) -> set[str]:
        """The **transitive dependents** of the seeds — the ripple a roll back has to reclassify.

        The seeds themselves are excluded (seed = direct impact, result = ripple beyond, a
        disjoint pair). Unknown seed ids are ignored; the caller validates.
        """
        dependents = self._dependents_map()
        result: set[str] = set()
        stack = [tid for tid in seed_ids if tid in self._by_id]
        while stack:
            current = stack.pop()
            for child in dependents.get(current, []):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result - set(seed_ids)

    def frontier(self) -> list[Task]:
        """todo tasks startable right now (every blocker done). Ascending id."""
        result = [t for t in self.tasks if t.status == "todo" and all(self.get(dep).is_done for dep in t.blocked_by)]
        return sorted(result, key=lambda t: t.id)

    def layers(self) -> list[list[str]]:
        """Structural execution layers; depth = longest dependency chain. Within a layer, ascending id."""
        depth: dict[str, int] = {}
        for tid in self._topo_order():
            deps = self.get(tid).blocked_by
            depth[tid] = 1 + max((depth[d] for d in deps), default=-1)
        max_depth = max(depth.values(), default=-1)
        return [sorted(tid for tid, d in depth.items() if d == level) for level in range(max_depth + 1)]

    def critical_path(self) -> list[str]:
        """The longest chain. Ties resolved deterministically by ascending id."""
        length: dict[str, int] = {}
        pred: dict[str, str | None] = {}
        for tid in self._topo_order():
            best_len = 0
            best_pred: str | None = None
            for dep in sorted(self.get(tid).blocked_by):
                if length[dep] > best_len:
                    best_len, best_pred = length[dep], dep
            length[tid] = best_len + 1
            pred[tid] = best_pred
        if not length:
            return []
        end = min(length, key=lambda t: (-length[t], t))
        path: list[str] = []
        node: str | None = end
        while node is not None:
            path.append(node)
            node = pred[node]
        return list(reversed(path))

    def order_frontier(self) -> list[Task]:
        """The frontier in optimal consumption order.

        Priority: ① foundation / high fan-out → ② on the critical path → ③ the rest, ties by
        ascending id. In code rather than by agent choice, so two runs schedule identically.
        """
        fan = self.fan_out()
        on_critical = set(self.critical_path())
        return sorted(
            self.frontier(),
            key=lambda t: (
                0 if t.kind == "foundation" else 1,
                -fan[t.id],
                0 if t.id in on_critical else 1,
                t.id,
            ),
        )

    def counts(self) -> dict[str, int]:
        """Counts by status, in vocabulary order."""
        result = {s: 0 for s in STATUS_ORDER}
        for t in self.tasks:
            result[t.status] += 1
        return result

    def claims_without_a_task(self, plan: models.Plan) -> list[str]:
        """Claims no task is answerable for — a gate ③ readiness failure (plan §16.4).

        A claim with no owning task is a promise the build cannot keep, and it would surface at
        gate ④ as an unexplained `missing` verdict rather than as the planning gap it is.
        """
        covered = {cid for t in self.tasks for cid in t.claim_ids}
        return sorted(c.id for c in plan.claims if c.id not in covered)


def join(plan: models.Plan, state: models.State | None) -> Graph:
    """Build the graph from the plan's structure and the state's status.

    A status entry for a task the plan does not declare is an error: the plan was rewound and
    the state did not follow, and scheduling against that mismatch would run unapproved work.
    """
    status_map = state.task_status if state is not None else {}
    attempts_map: dict[str, int] = {}
    if state is not None:
        raw_tasks = state.raw.get("tasks")
        if isinstance(raw_tasks, dict):
            for tid, body in raw_tasks.items():
                if isinstance(body, dict) and isinstance(body.get("attempts"), int):
                    attempts_map[tid] = body["attempts"]

    known = {t.id for t in plan.tasks}
    orphans = sorted(tid for tid in status_map if tid not in known)
    if orphans:
        raise DagError(
            f"state.yaml holds status for task(s) the plan does not declare: {', '.join(orphans)} — "
            "the plan was rewound without the state following. Run `rein revise` to reconcile."
        )

    return Graph.from_tasks(
        [
            Task(
                id=t.id,
                title=t.title,
                kind=t.kind,
                blocked_by=t.blocked_by,
                status=status_map.get(t.id, "todo"),
                risk=t.risk,
                claim_ids=t.claim_ids,
                domains=t.domains,
                attempts=attempts_map.get(t.id, 0),
                scope_include=t.scope_include,
                scope_exclude=t.scope_exclude,
                acceptance=t.acceptance,
            )
            for t in plan.tasks
        ]
    )


def load(repo: repo_mod.Repo) -> Graph:
    """The graph for `repo`. Raises :class:`DagError` when the plan is missing or inconsistent."""
    from rein import store as store_mod

    store = store_mod.Store(repo)
    plan = store.read_plan()
    if plan is None:
        raise DagError(f"no plan at {repo.plan} — run `/req` and `/design` first, or `rein init`")
    return join(plan, store.read_state())


# The render (dag_render.py) and trace (dag_trace.py) halves split out; consumers keep
# addressing everything through `dag.` — re-exported lazily below (PEP 562 __getattr__),
# because a module-level import back from the split halves would be circular.
if TYPE_CHECKING:
    from rein.dag_render import mermaid as mermaid
    from rein.dag_render import render as render
    from rein.dag_trace import TraceReport as TraceReport
    from rein.dag_trace import render_trace as render_trace
    from rein.dag_trace import trace as trace

_SPLIT_HOMES = {
    "render": "dag_render",
    "mermaid": "dag_render",
    "TraceReport": "dag_trace",
    "trace": "dag_trace",
    "render_trace": "dag_trace",
}


def __getattr__(name: str) -> object:
    home = _SPLIT_HOMES.get(name)
    if home is None:
        raise AttributeError(f"module 'rein.dag' has no attribute {name!r}")
    import importlib

    return getattr(importlib.import_module(f"rein.{home}"), name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="derive and inspect the task DAG (read-only)")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--render", action="store_true", help="the human-facing view (default)")
    group.add_argument("--mermaid", action="store_true", help="the dependency graph as Mermaid")
    group.add_argument("--frontier", action="store_true", help="the startable tasks, in consumption order")
    group.add_argument("--validate", action="store_true", help="structural consistency only; print nothing on success")
    group.add_argument("--trace", action="store_true", help="the requirement → claim → task thread")
    group.add_argument("--impacted", metavar="T-NNN", help="the transitive dependents of a task (roll-back ripple)")
    parser.add_argument(
        "--test-plan",
        metavar="PATH",
        default=None,
        help="with --trace: also require every requirement to appear in this test plan",
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    if args.test_plan and not args.trace:
        logger.error("--test-plan is a dimension of --trace; pass both")
        return 2

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    if args.trace:
        from rein import dag_trace

        return dag_trace.run(repo, args.test_plan)

    try:
        graph = load(repo)
    except (DagError, models.DocumentError) as exc:
        logger.error(str(exc))
        return 1

    if args.validate:
        return 0
    if args.impacted:
        if args.impacted not in {t.id for t in graph.tasks}:
            logger.error(f"unknown task {args.impacted}")
            return 2
        impacted = sorted(graph.dependents_closure([args.impacted]))
        print("\n".join(impacted) if impacted else "(no downstream tasks)")
        return 0
    if args.frontier:
        ordered = graph.order_frontier()
        print("\n".join(f"{t.id} [{t.kind}] {t.title}" for t in ordered) if ordered else "(no startable todo)")
        return 0

    from rein import dag_render

    print(dag_render.mermaid(graph) if args.mermaid else dag_render.render(graph))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
