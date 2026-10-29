"""The task DAG: structure from the frozen plan, status from the mutable state.

Consumption order, parallelism, merge order, and stopping are decided **in code from this
graph**, not by agent discretion (AGENTS.md "Task dependency graph"). That is the whole point
of deriving layers and the critical path here: two runs of the same plan schedule identically,
and a reviewer can predict what `/build` will do before it does it.

The two halves come from different files, so that progress edits and plan edits never touch the
same one:

  ``plan.yaml.tasks``   id, title, kind, blocked_by, claim_ids, risk, scope — frozen when the mandate is approved
  ``state.yaml.tasks``  status, attempts, completed_commit — mutated every iteration

A status entry naming a task the plan does not declare is an error, not a stray key: it means
the plan was rewound without the state following, and scheduling against it would run work
nobody approved.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from rein import common, models
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

KIND_VALUES = models.TASK_KIND_VALUES
STATUS_ORDER = models.TASK_STATUS_ORDER
STATUS_VALUES = models.TASK_STATUS_VALUES


class Linked(Protocol):
    """Anything with an id and the ids it waits for: a plan's task and a graph's task both are."""

    @property
    def id(self) -> str: ...

    @property
    def blocked_by(self) -> tuple[str, ...]: ...


def chains(tasks: Sequence[Linked]) -> list[tuple[str, ...]]:
    """The tasks cut into maximal dependency chains, each in order, heads in the order given.

    Two tasks are one link when the downstream one has exactly that one upstream and the upstream
    has exactly that one downstream. Nothing branches off a chain and nothing joins it, so the
    change a chain makes is one line of work: each task after the first was built on the one before
    and on nothing else. A task that is no link at either end is a chain of one. Acceptance reads
    the change one chain at a time (`review_reading.plan_readings`).
    """
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for upstream in task.blocked_by:
            dependents.setdefault(upstream, []).append(task.id)
    following: dict[str, str] = {}
    for task in tasks:
        if len(task.blocked_by) == 1 and dependents.get(task.blocked_by[0]) == [task.id]:
            following[task.blocked_by[0]] = task.id
    continues = set(following.values())
    found: list[tuple[str, ...]] = []
    for task in tasks:
        if task.id in continues:
            continue
        chain = [task.id]
        while chain[-1] in following:
            chain.append(following[chain[-1]])
        found.append(tuple(chain))
    return found


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
    #: `agent` or `person`: whether the loop launches an implementer at this task at all.
    produced_by: str = "agent"
    #: What must hold outside the repository before a launch, as the frozen plan declares it.
    requires: tuple[Mapping[str, Any], ...] = ()
    #: The plan premises this task's (effective) criteria rest on. It does not run until each one
    #: has been observed.
    assumes: tuple[str, ...] = ()
    #: The variables this task's work runs with (unexpanded) and the command prefixes its
    #: implementer may run without asking — the frozen `environment` block.
    env: tuple[tuple[str, str], ...] = ()
    allow: tuple[str, ...] = ()
    #: The long commands the loop runs on the host before this task's gate (`operate`).
    operate: tuple[Mapping[str, Any], ...] = ()
    #: How many launches an irreversible task's approval covers (0: undeclared), and what one costs.
    #: A budget raised after the mandate (`rein task attempts`) replaces the plan's.
    attempt_max: int = 0
    attempt_cost: str = ""
    #: The issue this sub-issue belongs to. A launch never carries work from two issues.
    issue_id: str = ""
    #: Whether this sub-issue declares an `operator_surface` it cannot undo — a crossing of its own.
    irreversible: bool = False
    #: Why a bundle holding this sub-issue was split (`state.yaml` `unbundled`): it now runs alone.
    unbundled: str = ""
    #: The sub-issues one launch carries, lead first, when this task is a bundle (:func:`bundle`).
    #: Empty for a launch that carries this sub-issue alone.
    members: tuple[str, ...] = ()

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    def budget(self) -> str:
        """What this task's attempt budget covers and how much of it is used, in one sentence. "" = none.

        Says what is counted, because it is not always the run the budget was priced for (#117). With
        `operate` the loop starts the run and counts every start. Without it the run happens outside
        the loop, and what is counted is implementation attempts: a run launched by hand is in no
        count but the hand that launched it.
        """
        if not self.attempt_max:
            return ""
        if self.operate:
            return f"covers {self.attempt_max} run(s), {self.attempts} used — each: {self.attempt_cost}"
        return (
            f"covers {self.attempt_max} implementation attempt(s), {self.attempts} used — each: {self.attempt_cost}. "
            "No `operate` step, so the loop does not start the run itself and counts none of its launches"
        )


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
        """Claims no task is answerable for — a mandate readiness failure (plan §16.4).

        A claim with no owning task is a promise the build cannot keep, and it would surface at
        acceptance as an unexplained `missing` verdict rather than as the planning gap it is.
        """
        covered = {cid for t in self.tasks for cid in t.claim_ids}
        return sorted(c.id for c in plan.claims if c.id not in covered)


def join(plan: models.Plan, state: models.State | None) -> Graph:
    """Build the graph from the plan's structure and the state's status.

    A status entry for a task the plan does not declare is an error: the plan was rewound and
    the state did not follow, and scheduling against that mismatch would run unapproved work.
    """
    status_map = state.task_status if state is not None else {}
    # Order added after the mandate joins the frozen edges here, so every reader of the graph —
    # the frontier, the layers, the critical path — sees one DAG.
    after = state.task_after if state is not None else {}
    # Scope added after the mandate (`rein task scope-add`) joins the frozen scope here for the same
    # reason: the guard, the dossier and the reviewers all read the one the graph carries.
    scope_added = state.task_scope_added if state is not None else {}
    # A budget raised at the crossing (`rein task attempts`) replaces the frozen one the same way.
    attempts_max = state.task_attempts_max if state is not None else {}
    unbundled = state.task_unbundled if state is not None else {}
    # A premise observed false with a fallback swaps the criteria resting on it for the ones a
    # human approved with the mandate. The plan document is untouched — it is frozen, and its
    # fallback is part of what was frozen — so this is where the swap becomes the task's bar.
    falsified = {
        pid for pid, seen in (state.premises if state is not None else {}).items() if seen.get("status") == "falsified"
    }
    replacements = {
        (str(c.get("task", "")), str(c.get("id", ""))): {k: v for k, v in c.items() if k != "task"}
        for premise in plan.premises
        if premise.id in falsified
        for c in premise.fallback_criteria
    }

    def effective(task_id: str, acceptance: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]:
        return tuple(replacements.get((task_id, str(e.get("id", ""))), e) for e in acceptance)

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
            "the plan was rewound without the state following. A task taken out of this cycle on "
            "purpose is recorded with `rein task defer <id> --reason ...`; one removed by mistake goes "
            "back into plan.yaml."
        )

    return Graph.from_tasks(
        [
            Task(
                id=t.id,
                title=t.title,
                kind=t.kind,
                blocked_by=tuple(dict.fromkeys((*t.blocked_by, *after.get(t.id, ())))),
                status=status_map.get(t.id, "todo"),
                risk=t.risk,
                claim_ids=t.claim_ids,
                domains=t.domains,
                attempts=attempts_map.get(t.id, 0),
                scope_include=tuple(dict.fromkeys((*t.scope_include, *scope_added.get(t.id, ())))),
                scope_exclude=t.scope_exclude,
                acceptance=effective(t.id, t.acceptance),
                produced_by=t.produced_by,
                requires=t.requires,
                assumes=_assumed(effective(t.id, t.acceptance)),
                env=t.env,
                allow=t.allow,
                operate=t.operate,
                attempt_max=attempts_max.get(t.id, t.attempt_max),
                attempt_cost=t.attempt_cost,
                issue_id=t.issue_id,
                irreversible=bool(t.irreversible_surfaces),
                unbundled=unbundled.get(t.id, ""),
            )
            for t in plan.tasks
        ]
    )


def rollup_status(statuses: Sequence[str]) -> str:
    """One status for an issue, read off its sub-issues'.

    `done` when every one is, `todo` while none has started, and otherwise the one a person most
    needs to see: something stuck outranks something running.
    """
    found = set(statuses)
    if found == {"done"}:
        return "done"
    if found <= {"todo"}:
        return "todo"
    for worst in ("blocked", "needs-revision", "awaiting-evidence"):
        if worst in found:
            return worst
    return "in-progress"


def issue_graph(plan: models.Plan, graph: Graph) -> Graph:
    """The issues as a graph of their own: their edges, their DAG role, and a status rolled up.

    What the dashboard draws. The loop schedules sub-issues, never this.
    """
    statuses: dict[str, list[str]] = {}
    for task in graph.tasks:
        statuses.setdefault(task.issue_id, []).append(task.status)
    return Graph.from_tasks(
        [
            Task(
                id=issue.id,
                title=issue.title,
                kind=issue.kind,
                blocked_by=issue.blocked_by,
                status=rollup_status(statuses.get(issue.id, [])),
            )
            for issue in plan.issues
        ]
    )


#: Separates a member's id from its criterion's in a bundle's acceptance: `T-002/A-1`. Criterion
#: ids are scoped to their sub-issue, so two members can both have an `A-1`.
MEMBER_SEP = "/"


def bundleable(task: Task) -> str:
    """Why `task` has to be launched alone, or "" when it may share a launch with its siblings.

    Each reason is a thing a shared launch would get wrong. A crossing's budget prices the runs a
    human approved for that crossing, so a sibling's failure must not spend it. `operate` and
    `requires` are steps and preconditions of one sub-issue's launch. A person's deliverable is
    never launched at all. And a sub-issue a bundle already failed with runs alone, so that what
    decides it is a launch of its own.
    """
    if task.produced_by != "agent":
        return "a person produces it"
    if task.irreversible or task.attempt_max:
        return "it crosses an irreversible point"
    if task.operate:
        return "the loop operates it"
    if task.requires:
        return "it has preconditions of its own"
    if task.unbundled:
        return "a bundle holding it was split"
    return ""


def bundle(graph: Graph, lead: Task) -> Task:
    """`lead` and the sub-issues of its issue that follow it, as one launch — or `lead` alone.

    Deterministic from the graph: the plan's order, the statuses, and the split marks. Members are
    taken in plan order after `lead` while each is `todo`, may share a launch, runs in `lead`'s
    environment, and waits for nothing outside the bundle that is not done. The first sub-issue
    that fails any of those ends the bundle, so a bundle is always a contiguous run of its issue.
    Done siblings are passed over: they are not work, and they do not break the run.

    The bundle is a :class:`Task` under `lead`'s id whose `members` names everything it carries.
    Its scope covers what any member's does, its criteria are every member's, named
    ``<member>/<criterion>`` so two members' `A-1` stay apart, and its claims are all of theirs.
    """
    if bundleable(lead):
        return lead
    siblings = [t for t in graph.tasks if t.issue_id == lead.issue_id]
    members = [lead]
    ids = {lead.id}
    for task in siblings[siblings.index(lead) + 1 :]:
        if task.is_done:
            continue
        if (
            task.status != "todo"
            or bundleable(task)
            or (task.env, task.allow) != (lead.env, lead.allow)
            or not all(dep in ids or graph.get(dep).is_done for dep in task.blocked_by)
        ):
            break
        members.append(task)
        ids.add(task.id)
    if len(members) == 1:
        return lead
    return merged(members)


def merged(members: Sequence[Task]) -> Task:
    """One launch carrying `members` (lead first), under the lead's id."""
    from dataclasses import replace

    lead = members[0]
    ids = [m.id for m in members]

    def union(values: Sequence[tuple[str, ...]]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(v for value in values for v in value))

    # A member with no `include` is unbounded, and so is anything carrying it. An `exclude` stays
    # only where no member's own scope reaches: one member's "not here" is not a sibling's.
    include = () if any(not m.scope_include for m in members) else union([m.scope_include for m in members])
    exclude = tuple(
        path
        for path in union([m.scope_exclude for m in members])
        if all(common.outside_scope([path], m.scope_include, m.scope_exclude) for m in members)
    )
    return replace(
        lead,
        title=f"{lead.issue_id}: {', '.join(ids)}",
        blocked_by=tuple(dep for dep in union([m.blocked_by for m in members]) if dep not in ids),
        risk=models.max_risk([m.risk for m in members]),
        claim_ids=union([m.claim_ids for m in members]),
        domains=union([m.domains for m in members]),
        scope_include=include,
        scope_exclude=exclude,
        acceptance=tuple(
            {**entry, "id": f"{m.id}{MEMBER_SEP}{entry.get('id', '?')}"} for m in members for entry in m.acceptance
        ),
        assumes=union([m.assumes for m in members]),
        members=tuple(ids),
    )


def of_member(entries: Sequence[Mapping[str, Any]], member: str) -> list[dict[str, Any]]:
    """`member`'s entries out of a bundle's criteria (or their records), under their own ids again."""
    prefix = f"{member}{MEMBER_SEP}"
    return [
        {**entry, "id": str(entry["id"])[len(prefix) :]}
        for entry in entries
        if str(entry.get("id", "")).startswith(prefix)
    ]


def _assumed(acceptance: tuple[Mapping[str, Any], ...]) -> tuple[str, ...]:
    """The premise ids these criteria rest on, in first-seen order."""
    found: list[str] = []
    for entry in acceptance:
        value = entry.get("assumes")
        if isinstance(value, list):
            found += [str(p) for p in value]
    return tuple(dict.fromkeys(found))


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
