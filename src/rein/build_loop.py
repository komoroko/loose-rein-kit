"""The deterministic orchestrator for the implementation phase (the engine behind `/build`).

Scheduling runs **in code, not in a prompt**. An LLM writes the implementation, but which tasks
run, at what parallelism, in what merge order, and when to stop are decided here — so two runs
of the same plan schedule identically and a reviewer can predict the loop instead of
interviewing it:

  - frontier computation / consumption order / max parallelism / worktree isolation / merge order
  - each quality-gate step's pass/fail, by exit code
  - the per-step retry budget, the blocked decision, the stop condition, the gate check

The determinism boundary:
  - Deterministic (here): control flow, parallelism, merge, cmd-step decisions, stopping.
  - Non-deterministic (LLM): the code, and the review step's fixes → absorbed by "re-run the
    preceding cmd steps after an agent step; retry until green, else blocked".

Four properties are load-bearing:

**The loop records only verdicts it earned.** A task status is evidence *about the task*; an
agent CLI missing from PATH, a session limit that resets at 3am, a supervisor's SIGTERM are
facts about the machine. They never share a code path, a status, or an event here (:mod:`rein.
faults` draws the line). An environment fault leaves every task exactly as it found it —
status, attempts, retry budget, handoff — and stops the run, because the next task would fail
the same way. What that buys is not tidiness: `blocked` takes a task off the frontier, so a task
blocked for a machine's reason never reaches the salvage/restore path in :mod:`rein.build_git`
that exists to continue it, and the run's `task_failed` + `knowledge_gap` sit in an append-only
chain that gate ⑤ counts as unresolved escalations forever.

**The loop produces no gate-④ evidence.** Gate ④ approves a *grounded review* — a blind
actual-behaviour extraction compared against the frozen plan, with a coverage manifest — and a
green test run is not a substitute. When the tasks finish, this prints what remains and stops.

**A step's command is an argv list, not a shell string.** No `shlex.split` of user text, and a
pipe has to live in a script a reviewer can read.

**Task status is written through the Central Store**, in the same transaction as the event that
explains it — so a status change with no audit record cannot happen, including when a leaf
worktree is the thing reporting it.

Usage:
  rein build            # run
  rein build --dry-run  # exercise the control flow without calling the agent CLI or git

--dry-run is strictly read-only: statuses advance in an in-memory overlay only, and no document,
event, or lock is written — running it never changes what a later real run sees.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import threading
import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rein import (
    build_git,
    build_prompts,
    common,
    control_plane,
    dag,
    event_chain,
    executors,
    faults,
    gate_guard,
    models,
    strict_yaml,
)
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

StopLoop = common.StopLoop
EnvironmentFault = faults.EnvironmentFault

#: How long the loop waits between retries of a launch the machine failed, by attempt. Seconds,
#: not hours: this covers a blip (a signal, a momentary timeout), never a capacity limit — one
#: that resets at 3am is not something a process should sit on holding the build lock and a set
#: of worktrees. That wait belongs to whatever will re-run `rein build`, which is why capacity
#: exhaustion skips these retries entirely and exits with `EXIT_RETRY_LATER` straight away.
_LAUNCH_BACKOFF_SEC: tuple[float, ...] = (5.0, 15.0, 30.0)

#: Where a sandboxed gate step sees the tree it is testing. One constant, so the mount and the
#: working directory cannot disagree about where the repository is.
_SANDBOX_WORKDIR = "/work"


def _worktree_common_git_dir(checkout: Path) -> Path | None:
    """The main repository's `.git` for a linked worktree, or None for an ordinary checkout.

    A linked worktree's `.git` is a file (`gitdir: <abs>/.git/worktrees/<id>`); the shared object
    store and refs live at that directory's `commondir`. Only the *shared* directory is returned:
    it is what has to exist inside a sandbox at its host path for the redirect to resolve.
    Anything unreadable or unexpected reads as "not a worktree" — the caller then mounts what it
    always did, so a malformed repository degrades to today's behaviour rather than to a crash.
    """
    marker = checkout / ".git"
    if not marker.is_file():
        return None  # an ordinary checkout: `.git` is the real directory, already inside the mount
    try:
        line = marker.read_text(encoding="utf-8").strip()
        if not line.startswith("gitdir:"):
            return None
        git_dir = Path(line[len("gitdir:") :].strip())
        if not git_dir.is_absolute() or not git_dir.is_dir():
            return None
        common = (git_dir / "commondir").read_text(encoding="utf-8").strip()
        joined = common if os.path.isabs(common) else os.path.join(str(git_dir), common)
        # Normalized textually, never `resolve()`d: the container has to carry this directory at
        # the very path the worktree's `.git` file names, and resolving symlinks would rename it.
        shared = Path(os.path.normpath(joined))
        return shared if shared.is_dir() else None
    except OSError:
        return None


#: Adapter name → the argv that launches it headless, with the prompt appended last. An interim
#: registry: PR-D replaces it with the executor profiles, which run the same adapters inside a
#: sandbox rather than on the host.
ADAPTERS: dict[str, tuple[str, ...]] = {
    "claude": ("claude", "-p"),
    "codex": ("codex", "exec"),
    "gemini": ("gemini", "-p"),
}

#: Flags added only to the launches that are *expected* to change the tree. `codex exec` runs in a
#: read-only sandbox unless told otherwise, so the implementer and the agent gate step could not
#: write a byte — the loop would start, every task would produce an empty diff, and nothing would
#: say why. Stating the level explicitly is right under either default.
#: The review transport in `review.py` deliberately does not get these: a reviewer that cannot
#: write is the point, not an oversight.
WRITE_FLAGS: dict[str, tuple[str, ...]] = {"codex": ("--sandbox", "workspace-write")}


def write_flags(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The write-enabling flags for the CLI `argv` launches, keyed on the CLI's own name."""
    return WRITE_FLAGS.get(argv[0], ()) if argv else ()


@dataclass(frozen=True)
class GateStep:
    """One quality-gate step, normalized from config.

    kind="command" — run `command` (argv) and decide by exit code. `retries` is that step's own
                     budget for handing the failure back to the implementer.
    kind="agent"   — a headless review+simplify pass that fixes findings in place. Its content is
                     non-deterministic, so the pipeline re-runs the cmd steps that already passed
                     whenever it changed the tree.

    `required` (command only): an empty command is normally a silent skip — fine for a library,
    but for a runnable deliverable a forgotten smoke command lets the whole build finish without
    ever launching the thing. Marking it required makes the loop refuse to start, before any
    implementer has been paid for.
    """

    name: str
    kind: str
    command: tuple[str, ...] = ()
    retries: int = 2
    required: bool = False
    #: The executor profile this step runs in. Dropping it here is how "repository code runs in
    #: the sandbox, never on the host" quietly stops being true of the quality gate.
    executor_profile: str = ""
    #: The role an agent step runs as, and the argv that launches that role's adapter. Dropping
    #: it makes the step launch `agents.implementer`'s adapter while calling itself
    #: `code_reviewer` — two roles the operator configured separately become one process.
    agent_role: str = ""
    agent_argv: tuple[str, ...] = ()

    @property
    def runnable(self) -> bool:
        return self.kind == "agent" or bool(self.command)

    @property
    def display(self) -> str:
        return " ".join(self.command) if self.command else f"<{self.kind}:{self.name}>"


@dataclass(frozen=True)
class Config:
    """The orchestrator's view of config.yaml — the single source of knobs."""

    raw: models.Config
    max_parallel: int
    worktree_enabled: bool
    worktree_dir: str
    branch_pattern: str
    steps: tuple[GateStep, ...]
    branch: str
    timeout_cmd: float | None
    timeout_agent: float | None
    adapter_argv: tuple[str, ...]
    launch_retries: int

    @property
    def gate_cmds(self) -> list[str]:
        """The deterministic commands of the gate, for prompts and display."""
        return [s.display for s in self.steps if s.kind == "command" and s.command]

    @staticmethod
    def _argv_for(config: models.Config, role: str) -> tuple[str, ...]:
        """The argv that launches `role`'s configured adapter. Refuses an adapter it cannot launch.

        Resolved up front for every role the gate will use, so an unlaunchable adapter stops the
        build before an implementer has been paid for — rather than at the first step that needed
        it, halfway through a task.
        """
        adapter = config.adapter(role) or "claude"
        argv = ADAPTERS.get(adapter)
        if argv is None:
            raise ValueError(
                f"agents.{role}.adapter is {adapter!r}, which this release does not know how to launch "
                f"(one of: {', '.join(sorted(ADAPTERS))})"
            )
        return argv

    @classmethod
    def from_models(cls, config: models.Config) -> Config:
        steps = tuple(
            GateStep(
                name=step.name,
                kind=step.kind,
                command=step.command,
                retries=max(0, step.retries),
                required=step.required,
                executor_profile=step.executor_profile,
                agent_role=step.agent_role,
                # An agent step launches the role it declares. The schema already requires
                # `agent_role` here; resolving it is what makes the declaration true.
                agent_argv=cls._argv_for(config, step.agent_role) if step.kind == "agent" and step.agent_role else (),
            )
            for step in config.quality_gate
        )
        argv = cls._argv_for(config, "implementer")
        return cls(
            raw=config,
            max_parallel=max(1, config.max_parallel),
            # Not optional: parallel leaves writing one tree is how two tasks' changes end up
            # attributed to one review.
            worktree_enabled=True,
            worktree_dir=config.worktree_dir,
            # `-` (not `/`) between branch and task: git forbids a branch that is a path-prefix of
            # another ref ("work" + "work/T-001" cannot coexist), so a slash pattern always fails.
            branch_pattern="{branch}-{task_id}",
            steps=steps,
            branch=config.work_branch,
            timeout_cmd=float(config.command_timeout_sec) or None,
            timeout_agent=float(config.agent_timeout_sec) or None,
            adapter_argv=argv,
            launch_retries=max(0, config.launch_retries),
        )

    @classmethod
    def load(cls, repo: repo_mod.Repo) -> Config:
        store = store_mod.Store(repo)
        config = store.read_config()
        if config is None:
            raise ValueError(f"no {repo.config} — run `rein init` first")
        return cls.from_models(config)


# --- the build lock -----------------------------------------------------------
#
# One lock per repository, in the shared runtime directory rather than inside the working tree:
# a per-worktree lock file meant two leaves could each hold "the" lock (plan §11.1). Lock order
# is build.lock → store.lock, always.


def build_lock(repo: repo_mod.Repo) -> store_mod.FileLock:
    """The exclusive whole-run lock. Held for the duration of a build."""
    return store_mod.FileLock(store_mod.Store(repo).build_lock)


# --- task status (through the Central Store) ----------------------------------


#: What `state.schema.json`'s `$defs/commit` accepts. A hash that fails it is dropped rather than
#: written: `ws.head()` returns "" when git is unavailable, and a dry run has no commit at all.
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,64}$")


def set_task_status(repo: repo_mod.Repo, task_id: str, status: str, *, note: str = "", commit: str = "") -> None:
    """Write one task's status and the event that explains it, in one transaction.

    `commit` is the work-branch commit that landed the task — recorded as `completed_commit` and
    carried in the same event, so "which commit closed T-NNN" is answerable from either the SSOT
    or the log without a second event to count twice.

    Retried on a lost race with a leaf writing its own task entry through the control plane.
    """
    if status not in models.TASK_STATUS_VALUES:
        raise ValueError(f"unknown task status {status!r}")
    store_mod.retry_on_stale(lambda: _set_task_status_once(repo, task_id, status, note=note, commit=commit))


def _set_task_status_once(repo: repo_mod.Repo, task_id: str, status: str, *, note: str, commit: str) -> None:
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise StopLoop("no .rein/state.yaml to record task status in")
    seen = store_mod.read_digest(state)

    landed = commit if status == "done" and _COMMIT_RE.match(commit) else ""
    raw = json.loads(json.dumps(state.raw))
    tasks = raw.setdefault("tasks", {})
    entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {}
    attempts = entry.get("attempts", 0) if isinstance(entry.get("attempts"), int) else 0
    if status == "in-progress":
        attempts += 1
    merged = {**entry, "status": status, "attempts": attempts, "note": note, "completed_commit": landed}
    if status == "done":
        # A finished task inherits nothing: keeping the handoff would hand the next cycle a
        # failure summary and a salvage branch for work that has already landed.
        merged.pop("handoff", None)
    # `completed_commit` is set unconditionally above rather than merged, so a task leaving `done`
    # loses it: the field says which commit *completed* the task, and a task sent back to todo or
    # needs-revision has none. The event log keeps the earlier one.
    tasks[task_id] = {k: v for k, v in merged.items() if v != ""}
    raw["updated_at"] = event_chain.now_iso()

    event = {"done": "task_completed", "in-progress": "task_started", "blocked": "task_failed"}.get(
        status, "decision_declared"
    )
    detail = {"status": status, "note": note}
    if landed:
        detail["commit"] = landed
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append(event, cycle_id=state.cycle_id, subject_ids=[task_id], detail=detail)


# --- what the next attempt inherits (through the Central Store) ----------------
#
# The implementer's own agent session is process-local and dies with the terminal that ran it, so
# a build restarted from another terminal used to begin the task cold: full retry budget, empty
# failure log, and the previous attempt's committed work stranded on a salvage branch nothing
# read back. What survives a crash has to be written down, which is what this is.

#: Caps mirroring state.schema.json's `handoff`, so a long gate log cannot make state.yaml
#: unwritable at exactly the moment it is carrying a failure.
_HANDOFF_STEP_MAX = 64
_HANDOFF_SUMMARY_MAX = 4000
_HANDOFF_BRANCH_MAX = 200


def read_task_handoff(state: models.State | None, task_id: str) -> dict[str, Any]:
    """The handoff recorded for a task, or an empty mapping when there is none."""
    if state is None:
        return {}
    entry = state.raw.get("tasks", {}).get(task_id) if isinstance(state.raw.get("tasks"), dict) else None
    handoff = entry.get("handoff") if isinstance(entry, dict) else None
    return dict(handoff) if isinstance(handoff, dict) else {}


def update_task_handoff(
    repo: repo_mod.Repo, task_id: str, patch: dict[str, Any], *, event: str, detail: dict[str, Any]
) -> None:
    """Merge `patch` into the task's handoff and append `event`, in one transaction.

    Same write path and same stale-retry as `set_task_status`: the record of what the next
    attempt inherits must not be able to exist outside the audit chain.
    """
    store_mod.retry_on_stale(lambda: _update_task_handoff_once(repo, task_id, patch, event, detail))


def _update_task_handoff_once(
    repo: repo_mod.Repo, task_id: str, patch: dict[str, Any], event: str, detail: dict[str, Any]
) -> None:
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise StopLoop("no .rein/state.yaml to record the task handoff in")
    seen = store_mod.read_digest(state)

    raw = json.loads(json.dumps(state.raw))
    tasks = raw.setdefault("tasks", {})
    entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {}
    previous = entry.get("handoff")
    handoff: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    handoff.update(patch)
    handoff["updated_at"] = event_chain.now_iso()
    tasks[task_id] = {**entry, "status": entry.get("status", "todo"), "handoff": handoff}
    raw["updated_at"] = event_chain.now_iso()

    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append(event, cycle_id=state.cycle_id, subject_ids=[task_id], detail=detail)


def record_attempt_failure(
    repo: repo_mod.Repo, task_id: str, *, failed_step: str, failure_summary: str, retries_left: dict[str, int]
) -> None:
    """Record a gate-step failure as both the audit event and the next attempt's inheritance."""
    patch = {
        "failed_step": failed_step[:_HANDOFF_STEP_MAX],
        "failure_summary": failure_summary[-_HANDOFF_SUMMARY_MAX:],
        "retries_left": {name[:_HANDOFF_STEP_MAX]: max(0, min(100, n)) for name, n in retries_left.items()},
    }
    detail = {"step": failed_step, "retries_left": retries_left.get(failed_step, 0)}
    update_task_handoff(repo, task_id, patch, event="task_failed", detail=detail)


def record_salvage(repo: repo_mod.Repo, task_id: str, *, branch: str, salvage_state: str) -> None:
    """Note where an interrupted attempt's work went, and whether the next one picked it up."""
    patch = {"salvage_branch": branch[:_HANDOFF_BRANCH_MAX], "salvage_state": salvage_state}
    update_task_handoff(
        repo, task_id, patch, event="decision_declared", detail={"salvage_branch": branch, "state": salvage_state}
    )


# Single definitions live elsewhere; the old names stay importable from here.
summarize_failure = common.summarize_failure
_FAILURE_MAX_LINES = common._FAILURE_MAX_LINES


# --- subprocess -------------------------------------------------------------


# The implementation lives in common.run; the `_run` name stays because the tests monkeypatch it
# here to fake git and agent-CLI results.
_run = common.run


def _late_run(cmd: list[str], cwd: str | None = None, timeout: float | None = None) -> tuple[int, str]:
    """Late-binding indirection to `_run`: resolved from this module's globals at call time, so a
    test patching build_loop._run reaches the injected GitWorkspace runner too — regardless of
    whether the patch lands before or after the Orchestrator is constructed."""
    return _run(cmd, cwd, timeout)


# --- scheduling (pure, under test) ------------------------------------------


def plan_batch(graph: dag.Graph, max_parallel: int) -> tuple[str, list[dag.Task]] | None:
    """Deterministically decide the next batch to start.

    Returns:
      ("serial", [one foundation task])       — foundation / high fan-out is finalized serially
      ("parallel", [leaf tasks, ≤max_parallel]) — independent leaves are launched in parallel in isolation
      None                                    — the frontier is empty

    A batch is a barrier: the caller waits for all of it before recomputing the frontier, so a
    slot freed by a quick leaf idles until the slowest one in the batch finishes. That cost is
    **deliberate, not an oversight**. Refilling slots as leaves complete would make batch
    membership — and with it the merge order and what the integration gate verifies as one tree —
    depend on which leaf happened to finish first. Determinism here is a reviewability property,
    not a performance one: it is what lets someone predict this loop instead of interviewing it.
    Utilization is the cheaper thing to give up.
    """
    ordered = graph.order_frontier()
    if not ordered:
        return None
    foundations = [t for t in ordered if t.kind == "foundation"]
    if foundations:
        return ("serial", [foundations[0]])
    return ("parallel", ordered[:max_parallel])


@dataclass(frozen=True)
class LeafOutcome:
    """What one parallel leaf's run came to.

    Three outcomes, not two, and the third is the point: `fault` set means the leaf produced
    **no verdict at all** — the machine failed under it — so the caller must neither merge it nor
    mark it. `ok=False` with no fault is a real verdict: the code could not pass the gate.
    """

    ok: bool
    log: str = ""
    fault: EnvironmentFault | None = None


# --- orchestrator body ------------------------------------------------------


class Orchestrator:
    def __init__(self, config: Config, dry_run: bool, repo: repo_mod.Repo | None = None) -> None:
        self.config = config
        self.dry_run = dry_run
        # The discovered repository anchors every path and git call below — the orchestrator
        # behaves identically no matter which directory it was launched from.
        self.repo = repo or repo_mod.get()
        self.root = str(self.repo.root)
        self.store = store_mod.Store(self.repo)
        self.state = self.store.read_state()
        self.cycle_id = self.state.cycle_id if self.state else ""
        self.branch = config.branch
        # The git/worktree layer (build_git.py); the runner is late-bound through _run above.
        self.ws = build_git.GitWorkspace(
            self.repo,
            self.branch,
            dry_run=dry_run,
            worktree_dir=config.worktree_dir,
            branch_pattern=config.branch_pattern,
            run=_late_run,
            on_event=lambda event, subject, detail: self._event(event, subject, detail),
            on_salvage=lambda task_id, branch, state: self._record_salvage(task_id, branch, state),
        )
        # The control plane, once `run()` starts serving. A leaf reaches the Store only through
        # it, so there is nothing to hand out before the socket exists.
        self.control: control_plane.ControlServer | None = None
        # Names this run in every token and every event a leaf records, so a decision can be
        # traced back to the build that produced it.
        self.run_id = f"RUN-{event_chain.now_iso().replace(':', '').replace('-', '')[:15]}"
        # Dry-run status overlay: the simulated statuses live here instead of tasks.yaml, so the
        # loop can progress to completion while the run stays strictly read-only.
        self._sim_status: dict[str, str] = {}
        # The run's allowance for retrying a launch the *machine* failed. One counter for the
        # whole run, guarded because parallel leaves draw on it at the same time: an environment
        # fault is a property of the machine, so a per-task budget would let one broken
        # environment be re-discovered max_parallel times over.
        self._launch_retries_left = config.launch_retries
        self._launch_lock = threading.Lock()

    def _set_status(self, task_id: str, status: str, *, commit: str = "") -> None:
        if self.dry_run:
            self._sim_status[task_id] = status
            print(f"    [dry-run] {task_id} → {status}")
            return
        set_task_status(self.repo, task_id, status, commit=commit)

    # -- launching an agent (the machine's side of the boundary) --

    def _spend_launch_retry(self) -> bool:
        """Take one from the run's launch allowance. False when it is empty."""
        with self._launch_lock:
            if self._launch_retries_left <= 0:
                return False
            self._launch_retries_left -= 1
            return True

    def _launch(self, argv: list[str], *, cwd: str, where: str, env: dict[str, str] | None = None) -> str:
        """One agent-CLI launch, retried while it is the machine that keeps failing.

        Returns the launch's output. Raises :class:`faults.EnvironmentFault` — never `StopLoop`
        — when the launch cannot be made to happen, because "the agent never ran" is not a
        verdict about any task and must not be caught by anything that treats it as one.

        Capacity exhaustion skips the retries: waiting seconds cannot fix a limit that lifts in
        hours, and sitting on the build lock until it does would make the run un-restartable from
        anywhere else. It exits to the caller immediately so a supervisor can do the waiting.
        """
        attempt = 0
        while True:
            rc, out = _run(argv, cwd=cwd, timeout=self.config.timeout_agent, env=env)
            if rc == 0:
                return out
            fault = faults.classify_launch(rc, out)
            if fault is faults.Fault.ENV_PERMANENT or faults.is_capacity(out) or not self._spend_launch_retry():
                raise EnvironmentFault(fault, where=where, rc=rc, output=out)
            delay = _LAUNCH_BACKOFF_SEC[min(attempt, len(_LAUNCH_BACKOFF_SEC) - 1)]
            print(f"    [launch] {where}: the launch failed (rc={rc}) for a machine reason; retrying in {delay:g}s")
            time.sleep(delay)
            attempt += 1

    def _escalate(self, kind: str, message: str, *, task: str | Sequence[str] = "") -> None:
        """Record something a human has to decide about, and say so on the console.

        `kind` is the *escalation's* vocabulary (`blocked`, `no_runnable`, …), not the audit
        chain's. Passing it straight through as the event type — which this did — made every
        escalation path raise `ValueError` out of `event_chain.make`, so the loop died with a
        traceback exactly when it had something to tell a human. The chain records these as
        `knowledge_gap` (what `rein events --summary` lists as still open) and keeps the kind in
        the detail, the same shape `set_task_status` uses to map statuses onto event names.

        There is no "resolve" verb any more: an escalation is closed by a signed disposition in
        the review, not by a flag somebody flips in a log (`rein events --summary` lists
        what is still open).
        """
        logger.warning(f"[escalation] {message}")
        self._event("knowledge_gap", task or self.cycle_id, {"kind": kind, "message": message})

    def _escalate_batch(self, kind: str, message: str, tasks: Sequence[dag.Task]) -> None:
        self._escalate(kind, message, task=[t.id for t in tasks])

    def _event(self, event: str, subject: str | Sequence[str], detail: dict[str, Any] | None = None) -> None:
        """Append one audit event through the Central Store. A no-op in a dry run.

        Every status change the loop makes goes through here or through `set_task_status`, so
        there is no path by which the build mutates state without saying why.

        A batch is several subjects, not one string holding several ids: the schema caps a
        subject at 64 characters, which a comma-joined batch of eleven leaves already exceeds,
        and one id per entry is what makes `rein events` able to find the batch by task.
        """
        if self.dry_run or not self.cycle_id:
            return
        subjects = [subject] if isinstance(subject, str) else list(subject)
        with self.store.transaction() as tx:
            tx.append(event, cycle_id=self.cycle_id, subject_ids=subjects, detail=detail or {})

    def _record_salvage(self, task_id: str, branch: str, state: str) -> None:
        if self.dry_run or not self.cycle_id:
            return
        record_salvage(self.repo, task_id, branch=branch, salvage_state=state)

    def _handoff_for(self, task: dag.Task) -> dict[str, Any]:
        """What an interrupted attempt at this task left for the next one. Empty in a dry run."""
        if self.dry_run:
            return {}
        return read_task_handoff(self.store.read_state(), task.id)

    def _load_graph(self) -> dag.Graph:
        graph = dag.load(self.repo)
        if self.dry_run and self._sim_status:
            graph = dag.Graph.from_tasks([replace(t, status=self._sim_status.get(t.id, t.status)) for t in graph.tasks])
        return graph

    # -- implementer launch and quality gate --

    def _implementer_prompt(self, task: dag.Task, failure_log: str) -> str:
        return build_prompts.implementer_prompt(
            task,
            failure_log,
            gate_cmds=self.config.gate_cmds,
            has_baseline=self.repo.path("docs/05-current-state.md").exists(),
            handoff=self._handoff_for(task),
        )

    @property
    def _resume_capable(self) -> bool:
        """Retry-session continuity is claude-preset-gated — deliberately no adapter layer.

        Resume flags are per-CLI (codex is a different subcommand shape; gemini/custom are
        unverifiable), so only the known `claude -p` contract gets them; every other CLI keeps
        today's fresh launch per retry.
        """
        return bool(self.config.adapter_argv) and self.config.adapter_argv[0] == "claude"

    def _invoke_implementer(
        self, task: dag.Task, cwd: str, failure_log: str, session: str = "", resume: bool = False
    ) -> None:
        """One headless implementer launch; `session`/`resume` thread retry-session continuity.

        With a session id, the first launch stamps it (--session-id) and a retry resumes it
        (--resume) so the implementer keeps its own context across its retries instead of
        re-reading ticket/design/code cold. A failed resume falls back to one fresh launch
        (session files can expire) rather than stopping the loop on a continuity optimization —
        but only when a fresh launch could plausibly do better. Under an exhausted session limit
        or a CLI that is not on PATH the fallback used to fire unconditionally, spending a second
        doomed launch on the one failure mode where nothing was going to launch at all.
        """
        if self.dry_run:
            print(f"    [dry-run] launch implementer (cwd={cwd}) task={task.id}")
            return
        prompt = self._implementer_prompt(task, failure_log)
        where = f"{task.id}: implementer"
        flags = list(write_flags(self.config.adapter_argv))
        flags += (["--resume", session] if resume else ["--session-id", session]) if session else []
        try:
            self._launch([*self.config.adapter_argv, *flags, prompt], cwd=cwd, where=where, env=self._leaf_env(task))
            return
        except EnvironmentFault as fault:
            if not resume or not fault.retryable or faults.is_capacity(fault.output):
                raise
            print(f"    [resume] {task.id}: resuming session failed (rc={fault.rc}); relaunching fresh")
        # A fresh token: the first one was spent on the launch that failed, and the server
        # accepts each nonce once.
        self._launch(
            [*self.config.adapter_argv, *write_flags(self.config.adapter_argv), prompt],
            cwd=cwd,
            where=where,
            env=self._leaf_env(task),
        )

    def _leaf_env(self, task: dag.Task) -> dict[str, str] | None:
        """The environment an implementer runs with: the control socket and a scoped token.

        Scoped to this run and this task, granting only what a leaf legitimately needs
        (declare a decision, record a knowledge gap, report status, append an event). It can
        never carry `gate.approve` or its siblings — `mint` refuses to sign those, and the
        server refuses to serve them even if a token somehow claimed them.

        None when there is no control plane (a dry run), which means the leaf inherits this
        process's environment and its `rein decision add` will refuse rather than write
        into a worktree that is about to be deleted.
        """
        if self.control is None:
            return None
        token = control_plane.mint(
            self.control.secret,
            run_id=self.run_id,
            task_id=task.id,
            capabilities=sorted(control_plane.LEAF_CAPABILITIES),
            ttl_sec=int(self.config.timeout_agent or control_plane.DEFAULT_TTL_SEC),
        )
        return {
            **os.environ,
            control_plane.SOCKET_ENV: str(self.control.socket_path),
            control_plane.TOKEN_ENV: token,
        }

    @property
    def _steps_effective(self) -> tuple[GateStep, ...]:
        """The gate steps actually run. All of them: the DoD has no opt-out knob."""
        return self.config.steps

    def _steps_for(self, task: dag.Task) -> tuple[GateStep, ...]:
        """The gate steps for one task — the shared DoD, identically for every task.

        Per-task steps would be a knob an implementer could turn on its own work; the whole
        point of the DoD is that it is not one.
        """
        return self._steps_effective

    def _review_scope(self, task: dag.Task, cwd: str, base: str) -> tuple[list[str], str]:
        """The changed-path list + exact diff command that scope the review step's read.

        Computed fresh at review time (the tree moves between retries). A leaf worktree's scope
        is everything since it forked off the work branch; a serial task's is the commits since
        `base` (the pre-task HEAD) plus the dirty tree. No base on the work branch (dry-run,
        or a caller without one) degrades to the unscoped prompt.
        """
        if self.dry_run:
            return [], ""
        if cwd != self.root:
            return self.ws.branch_changed_paths(self.ws.branch_for(task.id)), f"git diff {self.branch}...HEAD"
        if base:
            return self.ws.changed_since(base), f"git diff {base[:12]}..HEAD"
        return [], ""

    def _review_prompt(self, task: dag.Task, cwd: str, base: str) -> str:
        changed, diff_cmd = self._review_scope(task, cwd, base)
        return build_prompts.review_prompt(
            task, gate_cmds=self.config.gate_cmds, changed_paths=changed, diff_cmd=diff_cmd
        )

    def _tree_state(self, cwd: str) -> tuple[str, str]:
        return self.ws.tree_state(cwd)

    def _run_agent_step(self, step: GateStep, task: dag.Task, cwd: str, base: str) -> bool:
        """Run the review+simplify agent step headless. Returns True if it changed the tree.

        Launched with the adapter of the role the *step* declares — not the implementer's. Those
        were the same process until this was fixed, so a reviewer configured as a second opinion
        was the same model that had just written the code.
        """
        before = self._tree_state(cwd)
        argv = step.agent_argv or self.config.adapter_argv
        self._launch(
            [*argv, *write_flags(argv), self._review_prompt(task, cwd, base)],
            cwd=cwd,
            where=f"{task.id}: the '{step.name}' agent step",
        )
        return self._tree_state(cwd) != before

    def _run_cmd_step(self, step: GateStep, cwd: str) -> str:
        """Run one cmd step in its executor profile. "" on pass, a compact failure otherwise.

        Raises :class:`faults.EnvironmentFault` when the step could not be *run* — no container
        runtime, an unpinned or missing sandbox image, a command that is not on PATH. Those used
        to be summarized as if the code had failed the gate, which charged the step's retry
        budget and eventually blocked the task for a verdict nothing had reached: the same
        category error as a failed agent launch, on the other side of the pipeline.

        An argv list, never a shell string: a pipe or a redirect has to live in a script a
        reviewer can read, not in a config value nobody parses the same way twice.

        The step names an `executor_profile` — the config schema requires it — and it goes
        through the `executors` dispatch. A `host` profile still runs on the host, which is what
        a `host` profile means and what `doctor` warns about. `make test` runs agent-authored
        test files, and running those with the operator's credentials is exactly what a
        sandboxed profile exists to prevent.
        """
        profile = self._profile_for(step)
        spec = executors.ExecutionSpec(
            command=tuple(step.command),
            profile=profile,
            mounts=self._mounts_for(profile, cwd),
            workdir=_SANDBOX_WORKDIR if profile.is_sandboxed else cwd,
            timeout_sec=self.config.timeout_cmd,
        )
        where = f"gate step '{step.name}'"
        try:
            result = executors.for_profile(profile).run(spec)
        except executors.ExecutorError as exc:
            raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=1, output=str(exc)) from exc
        if result.exit_code == 0:
            return ""
        fault = faults.classify_step(result.exit_code, result.output)
        if fault.is_environment:
            raise EnvironmentFault(fault, where=where, rc=result.exit_code, output=result.output)
        return summarize_failure(step.display, result.exit_code, result.output)

    def _profile_for(self, step: GateStep) -> models.ExecutorProfile:
        """The profile a step runs in: its own, else `executors.quality_gate_profile`, else host.

        Falling back to a host profile rather than refusing keeps a repository that has not built
        its images working — `doctor` is what says the code is running unsandboxed, in one place,
        instead of every step failing with the same message.
        """
        config = self.config.raw
        named = config.profiles.get(step.executor_profile) if step.executor_profile else None
        return named or config.profile_for("quality_gate") or models.ExecutorProfile("host", {"kind": "host"})

    def _mounts_for(self, profile: models.ExecutorProfile, cwd: str) -> tuple[tuple[Path, str, str], ...]:
        """The repository mount a sandboxed gate step needs to have something to test.

        `mount_repo` is the profile's own say in it (the schema has carried the key with nothing
        reading it): `read_only` for a gate that only inspects, otherwise read-write, because a
        test run writes caches and artifacts and a read-only tree fails for the wrong reason.

        A leaf runs in a `git worktree`, whose `.git` is a *file* naming the main repository's
        `.git/worktrees/<id>` by absolute host path. Mounting the checkout alone left that
        redirect pointing at nothing inside the container, so every gate step that shells out to
        git (`pre-commit`, and so `gitleaks`) failed identically on every retry, for every leaf —
        never for a foundation task, which runs on the main checkout where `.git` is a real
        directory. Binding the main `.git` at *the same absolute path* makes the existing
        redirect resolve as-is; `commondir` is relative to it and follows. Rewriting the
        worktree's `.git` file would break the same repository for the host.

        This is the sandbox's boundary widening by exactly one directory: with `--network none`
        still in force, a step can now write the repository it is already building (a leaf
        commits to its own branch anyway) and nothing else.
        """
        if not profile.is_sandboxed:
            return ()
        mode = str(profile.raw.get("mount_repo", "read_write"))
        if mode == "none":
            return ()
        access = "ro" if mode == "read_only" else "rw"
        mounts = [(Path(cwd), _SANDBOX_WORKDIR, access)]
        git_dir = _worktree_common_git_dir(Path(cwd))
        if git_dir is not None:
            mounts.append((git_dir, str(git_dir), access))
        return tuple(mounts)

    def _run_pipeline(self, task: dag.Task, cwd: str, base: str = "") -> tuple[str | None, str]:
        """Run the quality-gate steps (config quality_gate.steps = the DoD) in order.

        Returns (failed_step_name, failure_summary), or (None, "") when every step passed.
        An agent step's fixes invalidate the evidence of the cmd steps that already passed,
        so those are re-run whenever it changed the tree (deterministic re-verification).
        """
        steps = self._steps_for(task)
        if self.dry_run:
            shown = " → ".join(f"{s.name}({s.kind})" for s in steps)
            print(f"    [dry-run] quality gate: {shown} (cwd={cwd})")
            return None, ""
        passed: list[GateStep] = []
        for step in steps:
            if step.kind == "agent":
                if self._run_agent_step(step, task, cwd, base):
                    for prev in passed:
                        failure = self._run_cmd_step(prev, cwd)
                        if failure:
                            return prev.name, failure
                continue
            if not step.command:
                print(f"    [gate] skip {step.name}: no command configured")
                continue
            failure = self._run_cmd_step(step, cwd)
            if failure:
                return step.name, failure
            passed.append(step)
        return None, ""

    def _run_task_to_done(self, task: dag.Task, cwd: str, base: str = "") -> tuple[bool, str]:
        """Take one task to done via implementer implementation + the quality-gate pipeline.

        Each cmd step carries its own send-back budget (step.retries); a failure consumes only
        that step's budget. Returns (ok, log); ok=False means some step's budget ran out
        (the caller marks the task blocked).
        """
        budgets = {s.name: s.retries for s in self._steps_for(task) if s.kind == "command"}
        # What an earlier, interrupted attempt left behind. Restoring the budgets is the load-
        # bearing half: a run killed mid-task and restarted otherwise came back with a full
        # allowance every time, so a task that can never pass could burn retries forever.
        handoff = self._handoff_for(task)
        failure_log = str(handoff.get("failure_summary", ""))
        inherited = handoff.get("retries_left")
        if isinstance(inherited, dict):
            budgets = {name: min(left, inherited.get(name, left)) for name, left in budgets.items()}
        if failure_log:
            print(f"    [handoff] {task.id}: resuming after a failed '{handoff.get('failed_step', '?')}' step")
        # Retry-session continuity (claude preset only): the implementer resumes its own session
        # across its retries. A step's final retry is forced fresh — a resumed session re-reads
        # its own failed reasoning, and the last attempt deserves an unanchored mind working from
        # the compact failure summary alone. The review agent step is never resumed (independence).
        session = str(uuid.uuid4()) if self._resume_capable and not self.dry_run else ""
        resume = False
        while True:
            self._invoke_implementer(task, cwd, failure_log, session=session, resume=resume)
            failed, failure_log = self._run_pipeline(task, cwd, base)
            if failed is None:
                return True, ""
            left = budgets.get(failed, 0)
            print(f"    quality gate fail at step '{failed}' (retries left: {left}): {task.id}")
            budgets[failed] = max(0, left - 1)
            if not self.dry_run:  # unreachable in dry-run today (the dry pipeline always passes); keep read-only anyway
                # The event and the next attempt's inheritance are the same fact, so they are the
                # same write: a terminal killed between them would otherwise leave a task_failed
                # in the chain with nothing saying what the next run has left to spend.
                record_attempt_failure(
                    self.repo,
                    task.id,
                    failed_step=failed,
                    failure_summary=failure_log,
                    retries_left=budgets,
                )
            if left <= 0:
                return False, failure_log
            if session and budgets[failed] <= 0:  # final retry for this step → fresh session
                session, resume = str(uuid.uuid4()), False
            else:
                resume = bool(session)

    # -- post-merge integration gate --

    def _integration_fix_prompt(self, ids: str, failure_log: str) -> str:
        return build_prompts.integration_fix_prompt(ids, failure_log, gate_cmds=self.config.gate_cmds)

    def _invoke_integration_fixer(self, ids: str, failure_log: str) -> None:
        self._launch(
            [
                *self.config.adapter_argv,
                *write_flags(self.config.adapter_argv),
                self._integration_fix_prompt(ids, failure_log),
            ],
            cwd=self.root,
            where=f"{ids}: the integration fixer",
        )

    def _integration_gate(self, tasks: list[dag.Task]) -> tuple[bool, str]:
        """Re-verify the merged/integrated state of the work branch after a multi-leaf join.

        Each leaf passed the gate only in its own isolated worktree; the *combined* file set can
        still be red (a lint/type error only the whole tree surfaces, a format reflow another
        task's change triggers). One batch-level re-run of the deterministic cmd steps catches
        that before the merged tasks are marked done. Cost control: the caller runs this only
        when 2+ leaves merged — a single-leaf join leaves the work tree identical to the one
        already verified in that leaf's worktree (leaves branch from the batch's common base and
        work advances only by this batch's merges), so re-running would prove nothing new.

        On red, a headless fixer runs on the work branch within each step's own retries budget
        (the same deterministic pattern as _run_task_to_done). Returns (ok, last_failure).
        """
        ids = ",".join(t.id for t in tasks)
        if self.dry_run:
            print(f"    [dry-run] integration gate on work after merging {ids}")
            return True, ""
        budgets = {s.name: s.retries for s in self.config.steps if s.kind == "command"}
        while True:
            failed, failure_log = None, ""
            for step in self._steps_effective:
                if step.kind != "command" or not step.command:
                    continue
                failure = self._run_cmd_step(step, cwd=self.root)
                if failure:
                    failed, failure_log = step.name, failure
                    break
            if failed is None:
                return True, ""
            left = budgets.get(failed, 0)
            print(f"    integration gate fail at step '{failed}' (retries left: {left}): {ids}")
            self._event(
                "task_failed", [t.id for t in tasks], {"step": failed, "stage": "integration", "retries_left": left}
            )
            if left <= 0:
                return False, failure_log
            budgets[failed] = left - 1
            self._invoke_integration_fixer(ids, failure_log)

    # -- worktree / merge --

    def _git(self, args: list[str], cwd: str | None = None) -> None:
        self.ws.git(args, cwd)

    def _branch_for(self, task: dag.Task) -> str:
        return self.ws.branch_for(task.id)

    def _worktree_path(self, task: dag.Task) -> str:
        return self.ws.worktree_path(task.id)

    def _add_worktree(self, task: dag.Task) -> str:
        return self.ws.add_worktree(task.id, str(self._handoff_for(task).get("salvage_branch", "")))

    def _safe_run_task(self, task: dag.Task, cwd: str) -> LeafOutcome:
        """Call _run_task_to_done safely from a thread, so one leaf cannot strand the batch.

        A `StopLoop` becomes a failed verdict; an `EnvironmentFault` is carried out **as itself**
        so the caller can tell "this leaf's code did not pass" from "no one ever asked this
        leaf's code anything". Flattening the second into the first is what marked tasks blocked
        for a rate limit.
        """
        try:
            ok, log = self._run_task_to_done(task, cwd=cwd)
            return LeafOutcome(ok=ok, log=log)
        except EnvironmentFault as fault:
            return LeafOutcome(ok=False, log=fault.summary(), fault=fault)
        except StopLoop as exc:
            return LeafOutcome(ok=False, log=str(exc))

    def _finalize_commit(self, cwd: str, message: str) -> bool:
        return self.ws.finalize_commit(cwd, message)

    def _gate_violations(self, paths: list[str]) -> list[tuple[str, str]]:
        """Gate-guard verdict for each path; [(path, deny reason)] for the denied ones.

        The merge/finalize-stage twin of gate_guard's edit-time and commit-stage checkpoints.
        Preservation commits run --no-verify and an implementer may commit with hooks absent or
        bypassed, and once a commit reaches the work branch the commit-stage `--check-diff`
        (a diff vs HEAD) can never see it again — so what a task actually changed is re-checked
        in code here, before it lands. template_mode / enforce_hook short-circuit inside
        evaluate() exactly as they do for the other checkpoints.
        """
        verdicts = ((p, gate_guard.evaluate(str(self.repo.path(p)), self.repo)) for p in paths)
        return [(p, reason) for p, (ok, reason) in verdicts if not ok]

    def _branch_changed_paths(self, branch: str) -> list[str]:
        return self.ws.branch_changed_paths(branch)

    def _changed_since(self, base: str) -> list[str]:
        return self.ws.changed_since(base)

    def _escalate_gate_violation(self, task_id: str, where: str, violations: list[tuple[str, str]]) -> None:
        listing = "\n".join(f"  {p} — {reason}" for p, reason in violations)
        self._escalate(
            "gate_violation",
            f"{task_id}: {where} touches gate-guarded paths whose prerequisite gate is pending — "
            f"the task is blocked for human review (gate rule 3: never land next-phase edits silently).\n{listing}",
            task=task_id,
        )

    def _cleanup_worktree(self, task: dag.Task) -> None:
        self.ws.cleanup_worktree(task.id)

    def merge_leaf(self, task: dag.Task, branch: str) -> bool:
        return self.ws.merge_leaf(task.id, branch)

    def _landed(self) -> str:
        """The work-branch commit the caller just created, as `completed_commit`.

        Read at the moment the task's commit becomes HEAD — right after the serial finalize, or
        right after that one leaf's merge — never once at the end of a batch. A batch's leaves
        merge one after another, so a hash read after all of them names the last merge for every
        member of the batch, and an integration gate that commits a fix moves it further still.
        """
        return "" if self.dry_run else self.ws.head()

    # -- main loop --

    def _recover_in_progress(self) -> None:
        """Reset tasks left in in-progress from a previous interruption back to todo (crash recovery).

        Since the frontier only picks status==todo, re-running with in-progress left over would mean
        that task is never started and the loop deadlocks. Roll back once at startup.
        """
        try:
            graph = dag.load(self.repo)
        except (OSError, dag.DagError, models.DocumentError, strict_yaml.StrictParseError):
            return
        for task in graph.tasks:
            if task.status == "in-progress":
                self._set_status(task.id, "todo")
                print(f"  [recover] {task.id}: reset in-progress -> todo (resuming from an interruption)")

    def _record_abort(self, fault: EnvironmentFault) -> None:
        """Say on the console, and in the chain, that the machine is what stopped this run.

        `run_aborted` is deliberately not one of `events.ATTENTION_EVENTS`: it asks nobody to
        judge the work, it asks for a re-run. Recording it as a `knowledge_gap` — which is what
        the old escalation path did — put a permanent "unresolved escalation" on gate ⑤'s
        screen for a machine's bad afternoon, in a log that is append-only by design.
        """
        logger.error(fault.summary())
        self._event(
            "run_aborted",
            self.cycle_id,
            {
                "fault": fault.fault.value,
                "where": fault.where[:64],
                "rc": fault.rc,
                "capacity": faults.is_capacity(fault.output),
                "reported": faults.reset_hint(fault.output),
            },
        )

    def _abort_run(self, fault: EnvironmentFault) -> int:
        """End the run because the machine failed. Record it; mark no task."""
        self._record_abort(fault)
        return common.EXIT_RETRY_LATER if fault.retryable else common.EXIT_CANNOT_PROCEED

    def run(self) -> int:
        if self.state is None:
            logger.error("no .rein/state.yaml — run `rein init` first")
            return common.EXIT_CANNOT_PROCEED
        if self.state.gate_status("tasks") != "approved":
            logger.error(
                "gate 3 (tasks) is not approved, so there is no frozen plan to build against. "
                "Finish /tasks and get the plan approved first."
            )
            return common.EXIT_CANNOT_PROCEED
        if self.state.plan_status != "frozen":
            logger.error(
                f"the plan is '{self.state.plan_status}', not 'frozen'. Gate 3's approval freezes it; "
                "building against a draft would implement a plan nobody signed for."
            )
            return common.EXIT_CANNOT_PROCEED
        if not self.dry_run and self.branch in ("", "HEAD"):
            # work_branch falls back to "HEAD" when git is unavailable/detached; creating worktrees
            # or committing against that would land the work on an arbitrary base.
            logger.error(
                "cannot determine the work branch (git unavailable or detached HEAD) — "
                "fill `branch:` in state.md or check out the work branch first."
            )
            return common.EXIT_CANNOT_PROCEED
        if self.dry_run:
            return self._run_loop()  # read-only: no lock either, and no contention to guard against
        try:
            # Lock order is build.lock -> store.lock, always; the control plane takes the store
            # lock per request inside it. The socket lives for exactly this run: a leaf that
            # outlives the orchestrator has nothing to talk to, which is the correct answer.
            with build_lock(self.repo), control_plane.serving(self.repo) as server:
                self.control = server
                return self._run_loop()
        except store_mod.LockUnavailableError as exc:
            # Retry-later, not cannot-proceed: nothing here is broken, another run simply has the
            # repository. A supervisor restarting `rein build` races the previous process's
            # shutdown often enough that reading this as fatal would stop the loop for good.
            logger.error(f"another build run holds the lock: {exc}")
            return common.EXIT_RETRY_LATER

    def _run_loop(self) -> int:
        self._recover_in_progress()
        while True:
            graph = self._load_graph()
            counts = graph.counts()
            unfinished = len(graph.tasks) - counts["done"]
            if unfinished == 0:
                return self._present_gate4(graph)

            batch = plan_batch(graph, self.config.max_parallel)
            if batch is None:
                # frontier empty & there are unfinished ones = all blocked/needs-revision. To the human.
                blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
                self._escalate(
                    "no_runnable",
                    f"No runnable tasks and {unfinished} unfinished ({', '.join(blocked)}). Help needed.",
                )
                return common.EXIT_HUMAN_NEEDED

            mode, tasks = batch
            print(f"[batch] mode={mode} tasks={[t.id for t in tasks]}")
            try:
                if mode == "serial" or not self.config.worktree_enabled:
                    self._consume_serial(tasks)
                else:
                    self._consume_parallel(tasks)
            except StopLoop as exc:
                logger.error(str(exc))
                return exc.code
            except EnvironmentFault as fault:
                # Never start the next batch into the same broken environment: whatever stopped
                # this launch would stop the next one, one wasted task at a time.
                return self._abort_run(fault)
            # Recompute at the top of the loop after each batch (reassemble the chain).

    def _consume_serial(self, tasks: list[dag.Task]) -> None:
        """Finalize foundation tasks etc. serially on the work branch."""
        for task in tasks:
            self._set_status(task.id, "in-progress")
            print(f"  [serial] {task.id} {task.title}")
            pre_head = "" if self.dry_run else self.ws.head()
            try:
                ok, log = self._run_task_to_done(task, cwd=self.root, base=pre_head)
            except EnvironmentFault:
                # No verdict was reached, so none is recorded: back to `todo` with its attempts,
                # retry budgets and handoff intact, and the tree left as it stands for the next
                # run's finalize/salvage to pick up. `blocked` here would take the task off the
                # frontier, which is precisely what stops a re-run from ever continuing it.
                self._set_status(task.id, "todo")
                raise
            if not ok:
                self._set_status(task.id, "blocked")
                self._escalate(
                    "blocked",
                    f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}",
                    task=task.id,
                )
                raise StopLoop(f"{task.id} is blocked. Human intervention needed.", code=1)
            # A serial task lands directly on the work branch (its own commits plus the finalize
            # below), where --no-verify and already-in-HEAD commits both escape the commit-stage
            # guard — so re-check everything the task changed before accepting it as done.
            if not self.dry_run and pre_head:
                violations = self._gate_violations(self._changed_since(pre_head))
                if violations:
                    self._set_status(task.id, "blocked")
                    self._escalate_gate_violation(task.id, "its work-branch changes", violations)
                    raise StopLoop(
                        f"{task.id}: changed gate-guarded paths while their gate is pending "
                        f"(commits since {pre_head[:12]} stay on the branch for review). "
                        "Human intervention needed.",
                        code=1,
                    )
            # Finalize the task diff only. The .rein/ orchestration state (tasks.yaml status, etc.)
            # is not included in the per-task commit (keeping one commit = one task). If the
            # implementer has not committed, this finalizes the diff (no-op otherwise).
            if not self._finalize_commit(self.root, f"{task.id}: {task.title}"):
                # The tree on the work branch keeps the diff, but the task must not be marked done
                # without its commit (one commit = one task is the record gate ④ reviews).
                self._set_status(task.id, "blocked")
                raise StopLoop(f"{task.id}: finalize commit failed on the work branch. Human intervention needed.")
            self._set_status(task.id, "done", commit=self._landed())

    def _consume_parallel(self, tasks: list[dag.Task]) -> None:
        """Implement independent leaves worktree-isolated up to max_parallel, then merge in ascending id order.

        Worktree creation is done serially on the main thread (avoiding .git index.lock contention);
        only the implementation is parallelized.

        A leaf the machine stopped is not a leaf that failed: it goes back to `todo` and keeps
        its worktree, so the next run's `add_worktree` finalizes and salvages it and the
        implementer continues rather than restarts. The batch is still played out to the end
        first — leaves that did pass their gate earned their merge, and throwing that away
        because a *different* leaf hit a session limit would be its own kind of dishonesty.
        """
        for task in tasks:
            self._set_status(task.id, "in-progress")
        # Worktree creation is serial (avoid git lock contention). The implementation is run in parallel after.
        branches = {task.id: self._add_worktree(task) for task in tasks}
        results: dict[str, LeafOutcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel)) as pool:
            futures = {pool.submit(self._safe_run_task, t, self._worktree_path(t)): t for t in tasks}
            for future, task in futures.items():
                results[task.id] = future.result()

        blocked_any = False
        merged: list[dag.Task] = []
        landed: dict[str, str] = {}
        # The first fault in id order, so which one is reported does not depend on thread timing.
        fault = next((results[t.id].fault for t in sorted(tasks, key=lambda t: t.id) if results[t.id].fault), None)
        # Merge deterministically in ascending id order (sequential join).
        for task in sorted(tasks, key=lambda t: t.id):
            outcome = results[task.id]
            ok, log = outcome.ok, outcome.log
            if outcome.fault is not None:
                # No verdict: no status, no escalation, and the worktree stays where it is.
                self._set_status(task.id, "todo")
                print(f"  [aborted] {task.id}: the machine stopped this leaf — left todo, work preserved")
                continue
            if not ok:
                self._set_status(task.id, "blocked")
                self._escalate(
                    "blocked",
                    f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}",
                    task=task.id,
                )
                self._cleanup_worktree(task)  # the branch keeps the diff for inspection
                blocked_any = True
                continue
            # The leaf's full diff must be on its branch before the merge — an implementer that
            # forgot to commit would otherwise lose that work when the worktree is removed.
            if not self._finalize_commit(self._worktree_path(task), f"{task.id}: {task.title}"):
                # Keep the worktree (it may hold the only copy) and let the rest of the batch merge.
                self._set_status(task.id, "blocked")
                blocked_any = True
                continue
            # The leaf's commits were made in its worktree, where --no-verify (finalize) or a
            # bypassed hook can carry a gate violation; merging would bury it in the work branch's
            # HEAD where --check-diff never looks again. Check the branch's full diff first.
            if not self.dry_run:
                violations = self._gate_violations(self._branch_changed_paths(branches[task.id]))
                if violations:
                    self._set_status(task.id, "blocked")
                    self._escalate_gate_violation(task.id, f"leaf branch {branches[task.id]}", violations)
                    self._cleanup_worktree(task)  # not merged; the branch keeps the diff for review
                    blocked_any = True
                    continue
            if self.merge_leaf(task, branches[task.id]):
                merged.append(task)  # done is decided after the integration gate below
                landed[task.id] = self._landed()  # this leaf's merge commit, before the next one
            else:
                self._set_status(task.id, "blocked")
                self._cleanup_worktree(task)  # conflict: aborted merge, worktree no longer needed
                blocked_any = True
        # Integration gate: only a join of 2+ leaves creates a combined tree nobody has verified
        # (a single-leaf join is byte-identical to that leaf's already-gated worktree state).
        # Not a knob any more: each leaf was green only in isolation, so a batch that merged
        # two or more of them has never been verified as one tree until now.
        if len(merged) >= 2:
            # An EnvironmentFault here propagates with the merged tasks still `in-progress`, and
            # that is the honest state: they merged, but nothing has verified the combined tree.
            # The next run resets them to `todo` and re-plays them, which re-runs the integration
            # gate. Duplicated work, never a skipped verification — marking them `done` would be
            # the other way round, and nothing would ever come back to check.
            ok, log = self._integration_gate(merged)
        else:
            ok, log = True, ""
        ids = ",".join(t.id for t in merged)
        if ok:
            for task in merged:
                self._set_status(task.id, "done", commit=landed.get(task.id, ""))
        else:
            for task in merged:
                self._set_status(task.id, "blocked")
            self._escalate_batch(
                "integration_red",
                f"{ids}: merged into work, but the integrated state fails the quality gate within the "
                f"limit. Fix the work branch, then set these tasks back to done.\n{log}",
                merged,
            )
            blocked_any = True
        if blocked_any:
            # A real verdict outranks a machine fault when both happened: re-running clears the
            # fault but never the blocked task, so the human has to look either way. The fault is
            # still recorded — it explains a leaf that came back `todo` with nothing said about it.
            if fault is not None:
                self._record_abort(fault)
            raise StopLoop("A blocked task occurred. Human intervention needed.", code=common.EXIT_HUMAN_NEEDED)
        if fault is not None:
            raise fault

    # -- handing over to the review pipeline -----------------------------------

    def _present_gate4(self, graph: dag.Graph) -> int:
        """All tasks done. Say what still has to happen — and what has NOT been established.

        (There is no "you left a step empty" nudge here any more: the config schema requires a
        `command` for every command step, so an empty one cannot reach this code. The scaffold
        ships a placeholder `["true"]` instead, which `doctor` can see and a silent skip cannot.)

        This deliberately does not invite an approval. Green tests plus an AI's summary is not
        evidence that the code does what the plan says: gate ④ approves a grounded review — a
        blind extraction of actual behaviour, compared against the frozen plan, with a coverage
        manifest saying what could not be analysed — and this loop has produced none of that.
        """
        print("\n========== all tasks done ==========")
        print(dag.render(graph))

        print(
            "\nWhat this run established: every task's code passed the configured quality gate.\n"
            "What it did NOT establish: that the code does what the plan claims.\n"
            "\nNext:\n"
            "  1. rein review generate   — coverage manifest, blind actual extraction,\n"
            "                                   conformance comparison, security and maintainability review\n"
            "  2. rein ui                — answer the unprimed challenges, then read the comparison\n"
            "  3. rein approve build     — readiness check, then your confirmation at the terminal\n"
            "\nThis loop cannot open gate 4, and neither can anything but a human: a gate opens only on\n"
            "the gate name typed at an interactive terminal, recorded by `rein approve` itself."
        )
        return common.EXIT_DONE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the deterministic orchestrator for the implementation phase")
    parser.add_argument(
        "--dry-run", action="store_true", help="run only the control flow without calling the agent CLI or git"
    )
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    if not repo.is_canonical_checkout:
        # A leaf worktree cannot own a build: its store mutations have to go through the control
        # plane, and a build that recorded nothing centrally would lose its own decisions.
        logger.error(
            "this is a linked worktree — run the build from the canonical checkout. "
            "Leaf worktrees participate through the control plane, they do not drive it."
        )
        return 2
    try:
        config = Config.load(repo)
    except (OSError, ValueError, models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(f"cannot load .rein/config.yaml: {exc} — `rein doctor` validates it")
        return 1
    return Orchestrator(config, dry_run=args.dry_run, repo=repo).run()


if __name__ == "__main__":
    raise SystemExit(main())
