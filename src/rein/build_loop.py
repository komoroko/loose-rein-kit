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
import contextlib
import fnmatch
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from rein import (
    adapters,
    build_git,
    build_prompts,
    common,
    conflict,
    control_plane,
    dag,
    diff_facts,
    digests,
    dossier,
    event_chain,
    evidence,
    executors,
    faults,
    gate_guard,
    human_review,
    models,
    preflight,
    review_cache,
    review_policy,
    review_reading,
    review_transport,
    run_record,
    strict_yaml,
)
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: What the integration reviewer's findings file is named after. Not a task id — the subject is the
#: join of a batch — and `dossier.findings_path` only needs a stable name to write beside.
_INTEGRATION_SUBJECT = "integration"

#: What a failed negative control is reported as. Not a step in `quality_gate` — it is a verdict on
#: what those steps *together* claimed — but it comes back through the same channel a red step
#: does, so the retry loop has to know the name.
NEGATIVE_CONTROL = "negative-control"

#: What a failed acceptance criterion is reported as, followed by the criterion's own id. Written
#: down here because the retry loop has to seed a budget under the same name `_run_acceptance`
#: returns, and a prefix spelled twice is a budget nobody finds.
_ACCEPTANCE_PREFIX = "acceptance:"

#: The send-back allowance for a gate verdict that is **not** a configured command step: the
#: negative control, and each of the task's own acceptance criteria. Both come back through the
#: channel a red step uses and neither has a `retries` of its own to inherit, so `budgets` had no
#: entry for either and `.get(name, 0)` answered zero — the task ended on the first occurrence,
#: having never told the implementer what was missing, while the docstring of each said it
#: "inherits the send-back budget and the retry machinery whole".
#:
#: Not derived from `quality_gate`: no step there is either of these, and any step's number would
#: be a guess wearing a derivation. One, because both failures name exactly what is missing — a
#: test that fails against the old code, or the criterion the ticket asked for — and an implementer
#: that cannot answer that in one more launch is telling you the ticket needs a human, which is
#: what `rein report --outcome needs-revision` is for rather than a budget to keep spending.
SEND_BACK_RETRIES = 1

#: Attempt endings that are a defect in the **plan**, not in the code, and so call for
#: `needs-revision` rather than `blocked`. `blocked` says the implementer could not make
#: the code work; filing a plan defect under it sends the next reader looking in the wrong
#: place, and `needs-revision` is the status `/revise` and `rein dag --impacted` act on.
#: A scope violation is one by construction: its own message says the way forward is a
#: human re-approving a wider scope.
_PLAN_DEFECT_KINDS = frozenset({"agent_needs_revision", "scope_violation"})

StopLoop = common.StopLoop
EnvironmentFault = faults.EnvironmentFault

#: How long the loop waits between retries of a launch the machine failed, by attempt. Seconds,
#: not hours: this covers a blip (a signal, a momentary timeout), never a capacity limit — one
#: that resets at 3am is not something a process should sit on holding the build lock and a set
#: of worktrees. That wait belongs to whatever will re-run `rein build`, which is why capacity
#: exhaustion skips these retries entirely and exits with `EXIT_RETRY_LATER` straight away.
_LAUNCH_BACKOFF_SEC: tuple[float, ...] = (5.0, 15.0, 30.0)


def _wait_out_the_machine(where: str, rc: int, attempt: int) -> None:
    """Say that a launch failed for a machine reason, and wait before running it again.

    A named unit rather than three lines inside `_launch`, because this is the only thing about
    the retry a caller can observe. It was observable only by intercepting `time.sleep` for the
    whole process — and the process sleeps for reasons that have nothing to do with this one:
    `subprocess.Popen.wait(timeout=...)` polls with `time.sleep`, so two tests that counted this
    backoff by patching the global were really counting every subprocess wait in the run, and
    failed intermittently whenever the machine was busy enough for one to poll.
    """
    delay = _LAUNCH_BACKOFF_SEC[min(attempt, len(_LAUNCH_BACKOFF_SEC) - 1)]
    print(f"    [launch] {where}: the launch failed (rc={rc}) for a machine reason; retrying in {delay:g}s")
    time.sleep(delay)


#: How often a launch in flight says it is still in flight. Not a progress bar and not a poll:
#: nothing is asked and nothing is read, the run just keeps talking.
#:
#: It exists because the *host* is what kills a silent command. Gemini CLI's shell tool caps a
#: command by `tools.shell.inactivityTimeout` — 300 seconds **without output**, not 300 seconds of
#: runtime — and a single implementer launch is silent for far longer than that, because
#: `common.run` captures the CLI's output rather than streaming it. So a foreground `rein build`
#: died mid-task on a host that would happily have waited all day, and the only advice left was
#: "detach and end your turn", which is how a build gets abandoned by the session that started it.
#: One line a minute is what makes the host's own wait usable, and it costs no tokens: the CLI
#: prints it, not a model.
_HEARTBEAT_SEC = 60.0

#: How many uncommitted paths the clean-tree refusal names before summarizing the rest.
#: Enough to recognize what is in the way; a full listing of a tree nobody committed is
#: not more informative than its first screen.
_DIRTY_PATHS_SHOWN = 20


class _Heartbeat:
    """Prints one line every :data:`_HEARTBEAT_SEC` while a launch is in flight.

    A thread rather than a wrapper around `common.run`, because what has to keep talking is the
    *waiting*, and the waiting is inside a call that captures its child's output by design (an
    agent's stdout is an answer to be parsed, not console noise).
    """

    def __init__(self, what: str) -> None:
        self._what = what
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def _tick(self) -> None:
        waited = 0.0
        while not self._done.wait(_HEARTBEAT_SEC):
            waited += _HEARTBEAT_SEC
            print(f"    [waiting] {self._what}: {waited / 60:.0f}m so far", flush=True)

    def __enter__(self) -> _Heartbeat:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._done.set()
        self._thread.join(timeout=1.0)


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


#: How a run ended, by the exit code it ended on — the `outcome` its measurement records.
_RUN_OUTCOME: Mapping[int, str] = {
    common.EXIT_DONE: "done",
    common.EXIT_HUMAN_NEEDED: "human-needed",
    common.EXIT_CANNOT_PROCEED: "cannot-proceed",
    common.EXIT_RETRY_LATER: "retry-later",
}


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
    #: Glob patterns (fnmatch-style) restricting this step to a matching diff. Empty: every
    #: task, unconditionally — frozen at gate 3 alongside the rest of config.yaml, never a knob
    #: a task's own ticket sets (`models.GateStep.matches_paths`).
    paths: tuple[str, ...] = ()
    #: Where this step runs: `task`, `integration`, or `both`. Never "whether" — every configured
    #: step still runs; this is how often the same confidence gets bought.
    stage: str = "both"

    @property
    def runnable(self) -> bool:
        return self.kind == "agent" or bool(self.command)

    @property
    def display(self) -> str:
        return " ".join(self.command) if self.command else f"<{self.kind}:{self.name}>"

    def matches_paths(self, changed: Sequence[str]) -> bool:
        if not self.paths or not changed:
            return True
        return any(fnmatch.fnmatch(path, pattern) for path in changed for pattern in self.paths)


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

    @classmethod
    def from_models(cls, config: models.Config) -> Config:
        """The knobs, with every role the run will launch resolved up front.

        Resolving here rather than at the first step that needs it is what makes an unlaunchable
        adapter stop the build before an implementer has been paid for, instead of halfway
        through a task.
        """
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
                agent_argv=(
                    adapters.launch_argv(config, step.agent_role) if step.kind == "agent" and step.agent_role else ()
                ),
                paths=step.paths,
                stage=step.stage,
            )
            for step in config.quality_gate
        )
        argv = adapters.launch_argv(config, "implementer")
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


def set_task_status(
    repo: repo_mod.Repo,
    task_id: str,
    status: str,
    *,
    note: str = "",
    commit: str = "",
    evidence: Mapping[str, Any] | None = None,
    handoff: Mapping[str, Any] | None = None,
) -> None:
    """Write one task's status and the event that explains it, in one transaction.

    `commit` is the commit that landed the task — recorded as `completed_commit` and
    carried in the same event, so "which commit closed T-NNN" is answerable from either the SSOT
    or the log without a second event to count twice.

    `evidence` is what makes a `done` mean anything: the content fingerprint the verdict was
    reached on and the gate steps established green against it. It is written in this same
    transaction rather than a later one, so there is no window in which a task is done and
    nothing says on what.

    `handoff` is a diagnostic patch riding along — what the last agent launch said, or the fault
    that stopped it. It travels with a status write rather than getting a write of its own,
    because a transaction here must record *why*, and "an agent produced some output" is not a
    reason a hash-chained log should carry a line for.

    Retried on a lost race with a leaf writing its own task entry through the control plane.
    """
    if status not in models.TASK_STATUS_VALUES:
        raise ValueError(f"unknown task status {status!r}")
    store_mod.retry_on_stale(
        lambda: _set_task_status_once(
            repo, task_id, status, note=note, commit=commit, evidence=evidence or {}, handoff=handoff or {}
        )
    )


def _failure_detail(handoff: object) -> dict[str, Any]:
    """The why-it-stopped fields of a handoff, shaped for an event detail. {} when it says nothing.

    `failure_summary` is deliberately not among them: it is a multi-kilobyte gate log, and an audit
    record is an index into the evidence rather than a copy of it. Each field is read at its own
    type — a `retries_left` that is not a mapping of counts says nothing about the budget, and
    putting whatever it holds on the event would put junk in an append-only log.
    """
    if not isinstance(handoff, Mapping):
        return {}
    detail: dict[str, Any] = {}
    step = handoff.get("failed_step")
    if isinstance(step, str) and step:
        detail["step"] = step
    budgets = handoff.get("retries_left")
    if isinstance(step, str) and isinstance(budgets, Mapping) and isinstance(budgets.get(step), int):
        detail["retries_left"] = budgets[step]
    escalation = handoff.get("escalation")
    if isinstance(escalation, Mapping) and isinstance(escalation.get("kind"), str) and escalation["kind"]:
        detail["escalation"] = escalation["kind"]
    futile = handoff.get("futile")
    if isinstance(futile, str) and futile:
        detail["futile"] = futile
    return detail


def _set_task_status_once(
    repo: repo_mod.Repo,
    task_id: str,
    status: str,
    *,
    note: str,
    commit: str,
    evidence: Mapping[str, Any],
    handoff: Mapping[str, Any],
) -> None:
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
    if status in {"done", "awaiting-evidence"}:
        # Both mean the same thing about the code — the whole DoD was established against this
        # tree — and differ only in whether an observation nobody here could make is outstanding.
        # So both carry the record, and a promotion from one to the other keeps the one already
        # written rather than needing the run that established it to still be alive.
        merged.pop("handoff", None)
        previous = entry.get("evidence")
        carried: Mapping[str, Any] = evidence or (previous if isinstance(previous, dict) else {})
        merged["evidence"] = {**dict(carried), "updated_at": event_chain.now_iso()}
    else:
        # The record says why this task *is* done. A task that leaves `done` has no such record,
        # the same reason `completed_commit` is dropped rather than merged.
        merged.pop("evidence", None)
        if handoff:
            previous = entry.get("handoff")
            carried = dict(previous) if isinstance(previous, dict) else {}
            merged["handoff"] = {**carried, **dict(handoff), "updated_at": event_chain.now_iso()}
    # `completed_commit` is set unconditionally above rather than merged, so a task leaving `done`
    # loses it: the field says which commit *completed* the task, and a task sent back to todo or
    # needs-revision has none. The event log keeps the earlier one.
    tasks[task_id] = {k: v for k, v in merged.items() if v != ""}
    raw["updated_at"] = event_chain.now_iso()

    event = {"done": "task_completed", "in-progress": "task_started", "blocked": "task_failed"}.get(
        status, "decision_declared"
    )
    detail: dict[str, Any] = {"status": status, "note": note}
    if landed:
        detail["commit"] = landed
    if status == "blocked":
        # What actually went red, on the event that says the task stopped. The gate-step failures
        # carried `step` and `retries_left` on their own `task_failed` records and this one — the
        # terminal one, the only `task_failed` a task that blocked on its first round produces at
        # all — carried a prose note and nothing a reader could sort or count by. The facts are
        # already in the handoff being written in this same transaction, so nothing new is
        # discovered here; it is simply written down where the chain can see it.
        detail.update(_failure_detail(merged.get("handoff")))
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append(event, cycle_id=state.cycle_id, subject_ids=[task_id], detail=detail)


# --- what the next attempt inherits (through the Central Store) ----------------
#
# The implementer's own agent session is process-local and dies with the terminal that ran it, so
# what a build restarted from another terminal inherits has to be written down: which gate step
# failed, what it said, how much of the budget is left, and the salvage branch holding the
# interrupted attempt's commits.

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
    repo: repo_mod.Repo,
    task_id: str,
    *,
    failed_step: str,
    failure_summary: str,
    retries_left: dict[str, int],
    futile: str = "",
) -> None:
    """Record a gate-step failure as both the audit event and the next attempt's inheritance.

    `futile` is the loop's reason for not spending another round (`Orchestrator._futile`) — carried
    because "the budget ran out" and "the budget was abandoned as pointless" are different things
    for whoever reads this afterwards, and the second one names something to go and repair.
    """
    patch: dict[str, Any] = {
        "failed_step": failed_step[:_HANDOFF_STEP_MAX],
        "failure_summary": failure_summary[-_HANDOFF_SUMMARY_MAX:],
        "retries_left": {name[:_HANDOFF_STEP_MAX]: max(0, min(100, n)) for name, n in retries_left.items()},
    }
    if futile:
        patch["futile"] = futile[:_HANDOFF_SUMMARY_MAX]
    detail: dict[str, Any] = {"step": failed_step, "retries_left": retries_left.get(failed_step, 0)}
    if futile:
        detail["futile"] = futile
    update_task_handoff(repo, task_id, patch, event="task_failed", detail=detail)


def record_escalation(
    repo: repo_mod.Repo,
    task_id: str,
    *,
    kind: str,
    message: str,
    tree: str,
    futile: str = "",
) -> None:
    """Record an attempt that ended *before* the quality gate — the event and the inheritance, together.

    `record_attempt_failure`'s sibling for the other way a task stops. A gate step that failed
    leaves its step name and its budget; an attempt `_check_implementer_output` stopped leaves the
    verdict it reached and the fingerprint of the tree it reached it over — which is what the next
    `rein build` needs to know that it is about to buy the same answer twice.

    One transaction, so the `knowledge_gap` a human reads and the record the next attempt inherits
    cannot exist apart; and one event, not two — this replaces `Orchestrator._escalate` on this
    path rather than joining it.
    """
    escalation: dict[str, Any] = {
        "kind": kind[:_HANDOFF_STEP_MAX],
        "message": message[-_HANDOFF_SUMMARY_MAX:],
    }
    if tree:
        escalation["tree"] = tree
    patch: dict[str, Any] = {"escalation": escalation}
    detail: dict[str, Any] = {"kind": kind, "message": message}
    if futile:
        patch["futile"] = futile[:_HANDOFF_SUMMARY_MAX]
        detail["futile"] = futile
    update_task_handoff(repo, task_id, patch, event="knowledge_gap", detail=detail)


def record_salvage(repo: repo_mod.Repo, task_id: str, *, branch: str, salvage_state: str) -> None:
    """Note where an interrupted attempt's work went, and whether the next one picked it up."""
    patch = {"salvage_branch": branch[:_HANDOFF_BRANCH_MAX], "salvage_state": salvage_state}
    update_task_handoff(
        repo, task_id, patch, event="decision_declared", detail={"salvage_branch": branch, "state": salvage_state}
    )


def agent_launch_note(*, role: str, adapter: str, rc: int, output: str, session: str = "") -> dict[str, Any]:
    """What the last agent launch actually said, shaped for `handoff.last_agent`.

    The loop used to throw a successful launch's output away the moment it returned, which is how
    an implementer that ended with *"bwrap: setting up uid map: Permission denied"* reached the
    operator as a task that simply changed nothing. The tail is kept rather than the whole stream:
    an agent's closing words are where it says what stopped it, and `state.yaml` has to stay
    writable at exactly the moment it is carrying a failure.

    A note is *carried* to the next status write rather than written on its own. A launch is not a
    state change, and this repository has no event-less write path on purpose — so an event per
    launch would be the alternative, in a log that deliberately never rotates.
    """
    return {
        "role": role[:_HANDOFF_STEP_MAX],
        "adapter": adapter[:_HANDOFF_STEP_MAX],
        "rc": max(-256, min(256, rc)),
        "session": session[:128],
        "output_tail": output[-_HANDOFF_SUMMARY_MAX:],
        "at": event_chain.now_iso(),
    }


def fault_note(fault: EnvironmentFault) -> dict[str, Any]:
    """Why the machine stopped under a task, shaped for `handoff.last_fault`.

    An environment fault reaches no verdict: no status moves to `blocked` and no retry budget is
    spent (this module's docstring). That is right, and it used to mean the reason existed only in
    a terminal that has since closed. Carrying it separately from `failure_summary` keeps
    *"nobody asked this code anything"* distinguishable from *"this code failed"*, which is the
    distinction the whole fault type exists for.
    """
    return {
        "kind": fault.fault.name,
        "where": fault.where[:200],
        "rc": max(-256, min(256, fault.rc)),
        "output_tail": fault.output[-_HANDOFF_SUMMARY_MAX:],
        "at": event_chain.now_iso(),
    }


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


class GateViolationFault(Exception):
    """An attempt edited a gate-guarded path while its prerequisite gate is still pending.

    Raised as soon as `_run_task_to_done` sees it — right after the implementer runs, inside the
    retry loop — rather than waiting for the finalize/merge-stage check that already existed to
    be the only thing that ever looked. A worktree that never reaches merge (blocked on a later
    content failure, or the run stopped by an environment fault first) used to carry the
    violation undetected until someone ran `rein doctor` by hand.
    """

    def __init__(self, violations: list[tuple[str, str]]) -> None:
        self.violations = violations
        super().__init__(f"{len(violations)} gate-guarded path(s) changed while the gate is pending")


@dataclass(frozen=True)
class LeafOutcome:
    """What one parallel leaf's run came to.

    Four outcomes, not two: `fault` set means the leaf produced **no verdict at all** — the
    machine failed under it — so the caller must neither merge it nor mark it. `violations` set
    means a gate-guarded path changed early, caught before merge rather than at it. `ok=False`
    with neither is a real verdict: the code could not pass the gate.
    """

    ok: bool
    log: str = ""
    fault: EnvironmentFault | None = None
    violations: list[tuple[str, str]] | None = None


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
        # The frozen Expected Model. Read once: it cannot change during a run (gate ③ froze it,
        # and `rein guard` denies a write while it is frozen), and every dossier needs it.
        self._plan = self.store.read_plan()
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
        # The reuse half of the evidence ledger: a content-addressed cache of facts already
        # established, outside the working tree. Off in a dry run (nothing is established) and
        # off when the operator says so. A miss only ever costs a re-run.
        self.ledger = evidence.Ledger.for_repo(self.repo, enabled=not dry_run and evidence.cache_enabled_by_env())
        #: Set once a gate-④ warm-up could not be taken. Retrying it per task would spend a session
        #: limit on an optimization, and the gate takes the reading either way (`_warm_reading`).
        self._warming_off = False
        # What each task's gate steps were established green against, keyed by task id. Written
        # into `state.yaml` beside the `done` it justifies — this is the auditable half.
        self._evidence: dict[str, dict[str, Any]] = {}
        self._evidence_lock = threading.Lock()
        # Diagnostics waiting for a status write to ride along with: what the last agent launch
        # said, and the last fault that stopped one. Neither is a verdict, so neither gets a
        # transaction (and an event) of its own.
        self._pending_diagnostics: dict[str, dict[str, Any]] = {}
        # Which gate steps went green during the attempt this thread is running. Thread-local
        # because leaves run concurrently and each is establishing evidence about its own tree.
        self._local = threading.local()
        # What this run put in front of a model, by role. Measured, not estimated — see
        # `spend_summary`.
        self._spent: dict[str, dict[str, int]] = {}
        #: What the provider billed for each role's launches, when the adapter reports it.
        self._usage: dict[str, usage_mod.Usage] = {}
        self._spend_lock = threading.Lock()
        # Tasks whose attempt ended before the quality gate, and the status that ending calls for.
        # The caller reads this instead of assuming every unsuccessful attempt is `blocked`: an
        # implementer that found the *design* wrong has said `needs-revision`, and overwriting
        # that with `blocked` would file a defect in the plan as a defect in the code.
        self._stops: dict[str, str] = {}
        # DoD steps already red on the work branch before any task ran, and what they said. A task
        # that fails one of these is not sent back to an implementer: `_establish_baseline`.
        self._baseline_red: dict[str, str] = {}
        self._baseline_taken = False

    def _stop_verdict(self, task_id: str) -> tuple[str, bool]:
        """(the status this task's failed attempt calls for, whether an escalation is still owed).

        An attempt stopped by `_check_implementer_output` already said why, in the right words.
        One that ran out of a gate step's retry budget has not, and the caller escalates for it.
        """
        recorded = self._stops.pop(task_id, "")
        return (recorded, False) if recorded else ("blocked", True)

    @property
    def _current_step_evidence(self) -> list[dict[str, Any]]:
        steps = getattr(self._local, "steps", None)
        if steps is None:
            steps = []
            self._local.steps = steps
        return steps

    @property
    def _current_acceptance(self) -> list[dict[str, Any]]:
        established = getattr(self._local, "acceptance", None)
        if established is None:
            established = []
            self._local.acceptance = established
        return established

    @property
    def _current_control(self) -> dict[str, Any]:
        control = getattr(self._local, "negative_control", None)
        if control is None:
            control = {}
            self._local.negative_control = control
        return control

    def _row_for(self, role: str) -> dict[str, int]:
        return self._spent.setdefault(role, {"launches": 0, "prompt_bytes": 0, "handed_bytes": 0, "cold_launches": 0})

    def _spend(self, role: str, prompt_bytes: int, *, resumed: bool = False) -> None:
        """Count one launch's input against the role that made it.

        One call per *attempt*: the loop composes every prompt itself, so this is the one number
        here that can be counted exactly, and a retry really does send it again.

        `resumed` distinguishes a launch that continues the agent's own session from one that
        starts cold. A cold launch re-reads its ticket, its design slice and the code it is working
        on from scratch — the `Adapter` docstring has called that the largest avoidable cost in a
        long build since the capability record was written, and until this counter existed the
        claim was not something the run could confirm or refute about itself.
        """
        with self._spend_lock:
            spent = self._row_for(role)
            spent["launches"] += 1
            spent["prompt_bytes"] += prompt_bytes
            if not resumed:
                spent["cold_launches"] += 1

    def _spend_usage(self, role: str, spent: usage_mod.Usage) -> None:
        """Count what the provider says one launch cost, against the role that made it.

        Beside the byte counters rather than instead of them: bytes are what this process *sent*
        and are always knowable; tokens are what the launch *cost* and only an adapter that reports
        them can say. An adapter with no envelope records `unavailable`, which is a state with a
        name — never a row of zeros that would read as free.
        """
        with self._spend_lock:
            self._usage[role] = self._usage.get(role, usage_mod.Usage()) + spent

    def usage_totals(self) -> dict[str, usage_mod.Usage]:
        """This run's measured cost, by role. A copy — the caller must not hold the lock's data."""
        with self._spend_lock:
            return dict(self._usage)

    def _spend_handover(self, role: str, handed_bytes: int) -> None:
        """Count what a launch was *told to read*, as opposed to what was sent in its argv.

        These are different measurements and only the first one was ever taken. The prompt this
        process composes is a few kilobytes; the dossier plus the ticket, design slice and baseline
        it names are the actual reading list, and they are where a build's input budget goes. A
        measurement that cannot see the larger of the two numbers cannot answer whether handing the
        same documents to every launch is worth caching — which is the question it exists for.

        It is still not a token count and still not the whole truth: what an agent then chooses to
        open on its own is outside this process entirely. Naming the boundary is the point.
        """
        with self._spend_lock:
            self._row_for(role)["handed_bytes"] += handed_bytes

    def spend_totals(self) -> dict[str, dict[str, int]]:
        """This run's measurement, by role. A copy — the caller must not hold the lock's data."""
        with self._spend_lock:
            return {role: dict(row) for role, row in self._spent.items()}

    def spend_summary(self) -> str:
        """Where this run's input went, worst first. Empty when nothing was launched.

        Two lines answering two questions, because they are different measurements. Bytes are what
        *this process sent* — always knowable, and the only number available for an adapter that
        reports nothing. Tokens are what the launch *cost*, which only the provider can say, and
        the gap between them is the point: the system prompt, the CLI's own project instructions
        and the cache are all inside the second number and invisible to the first.
        """
        with self._spend_lock:
            rows = sorted(self._spent.items(), key=lambda item: -(item[1]["prompt_bytes"] + item[1]["handed_bytes"]))
            measured = dict(self._usage)
        if not rows:
            return ""
        sent = sum(row["prompt_bytes"] for _, row in rows)
        handed = sum(row["handed_bytes"] for _, row in rows)
        launches = sum(row["launches"] for _, row in rows)
        cold = sum(row["cold_launches"] for _, row in rows)
        parts = [
            f"{role} {(row['prompt_bytes'] + row['handed_bytes']) / 1024:.0f}KiB/{row['launches']}"
            for role, row in rows
        ]
        lines = [
            f"input: {sent / 1024:.0f}KiB sent + {handed / 1024:.0f}KiB handed to read over "
            f"{launches} launches ({cold} cold) — " + ", ".join(parts)
        ]
        if billed := usage_mod.summarize(measured, what="billed"):
            lines.append(billed)
        return "\n".join(lines)

    def _note_diagnostic(self, task_id: str, patch: dict[str, Any]) -> None:
        """Hold a diagnostic until the next status write for this task carries it into the store."""
        if self.dry_run or not task_id:
            return
        with self._evidence_lock:
            self._pending_diagnostics.setdefault(task_id, {}).update(patch)

    def _add_review_findings(self, task_id: str, findings: Sequence[Mapping[str, Any]]) -> None:
        """Add review findings to what this task will carry, without discarding what it already has.

        A task can be read twice — by its own reviewer in its worktree, and by the integration
        reviewer once it has merged — and the two are different observations of different trees.
        Replacing the key would silently drop whichever arrived first, which for the per-task
        reviewer is the one whose findings `brief.residual_findings` carries to gate ④.
        """
        if self.dry_run or not task_id:
            return
        with self._evidence_lock:
            review = self._pending_diagnostics.setdefault(task_id, {}).setdefault("review", {})
            kept = list(review.get("findings") or [])
            kept += [dict(f) for f in findings]
            review["findings"] = kept

    def _take_diagnostics(self, task_id: str) -> dict[str, Any]:
        with self._evidence_lock:
            return self._pending_diagnostics.pop(task_id, {})

    def _set_status(self, task_id: str, status: str, *, commit: str = "") -> None:
        if self.dry_run:
            self._sim_status[task_id] = status
            print(f"    [dry-run] {task_id} → {status}")
            return
        set_task_status(
            self.repo,
            task_id,
            status,
            commit=commit,
            evidence=self._evidence.get(task_id, {}),
            handoff=self._take_diagnostics(task_id),
        )

    # -- launching an agent (the machine's side of the boundary) --

    def _spend_launch_retry(self) -> bool:
        """Take one from the run's launch allowance. False when it is empty."""
        with self._launch_lock:
            if self._launch_retries_left <= 0:
                return False
            self._launch_retries_left -= 1
            return True

    def _launch(
        self,
        argv: list[str],
        *,
        cwd: str,
        where: str,
        env: dict[str, str] | None = None,
        task_id: str = "",
        role: str = "",
        session: str = "",
        resumed: bool = False,
    ) -> str:
        """One agent-CLI launch, retried while it is the machine that keeps failing.

        Returns the launch's output. Raises :class:`faults.EnvironmentFault` — never `StopLoop`
        — when the launch cannot be made to happen, because "the agent never ran" is not a
        verdict about any task and must not be caught by anything that treats it as one.

        Capacity exhaustion skips the retries: waiting seconds cannot fix a limit that lifts in
        hours, and sitting on the build lock until it does would make the run un-restartable from
        anywhere else. It exits to the caller immediately so a supervisor can do the waiting.

        Whatever the launch said is noted against `task_id` on both paths. The output used to be
        returned and then dropped on the floor by every caller, so an agent's own account of why
        it stopped — the single most useful sentence in the whole run — survived nowhere.
        """
        attempt = 0
        adapter = argv[0] if argv else ""
        record = adapters.adapter_for(argv)
        prompt_bytes = sum(len(part.encode("utf-8")) for part in argv)
        while True:
            # Counted per attempt, inside the loop, because a retry is another launch: the same
            # argv goes to the provider again and is paid for again. Counting once per `_launch`
            # under-reported every retried task, and put two fields called `launches` in the same
            # `run_measured` event disagreeing with each other — the byte counter saying 1 where
            # the billed one said 3.
            self._spend(role or where, prompt_bytes, resumed=resumed)
            with _Heartbeat(where):
                rc, out = _run(argv, cwd=cwd, timeout=self.config.timeout_agent, env=env)
            if rc == 0:
                try:
                    said, spent = record.read_output(out) if record else (out, usage_mod.Usage.unavailable())
                except usage_mod.AdapterEnvelopeError as exc:
                    # The CLI can report a failed run on a process that exited 0. Without this the
                    # failure would travel on as the agent's answer, and whatever went wrong would
                    # be read as something the agent said.
                    rc, out, said, spent = 1, f"{exc}\n{out}", "", usage_mod.Usage.unavailable()
                self._spend_usage(role or where, spent)
                if rc == 0:
                    note = agent_launch_note(role=role or where, adapter=adapter, rc=0, output=said, session=session)
                    self._note_diagnostic(task_id, {"last_agent": note})
                    return said
            else:
                self._spend_usage(role or where, usage_mod.Usage.unavailable())
            note = agent_launch_note(role=role or where, adapter=adapter, rc=rc, output=out, session=session)
            fault = faults.classify_launch(rc, out)
            if fault is faults.Fault.ENV_PERMANENT or faults.is_capacity(out) or not self._spend_launch_retry():
                raised = EnvironmentFault(fault, where=where, rc=rc, output=out)
                self._note_diagnostic(task_id, {"last_agent": note, "last_fault": fault_note(raised)})
                raise raised
            _wait_out_the_machine(where, rc, attempt)
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

    # -- the dossier: what the loop already knows, handed over instead of re-derived --

    def _write_dossier(self, task: dag.Task, cwd: str, base: str, role: str) -> str:
        """Assemble this task's dossier into `cwd` and return its repo-relative path ("" in a dry run).

        Written before every launch rather than once per task: the diff moves between attempts,
        and a dossier describing the tree as it was two retries ago is worse than none.
        """
        if self.dry_run:
            return ""
        changed, diff_cmd = self._review_scope(task, cwd, base)
        document = dossier.build(
            task,
            plan=self._plan,
            repo_path=self.repo.path,
            changed=changed,
            diff_cmd=diff_cmd,
            base=base,
            history=self._history_for(task),
            handoff=self._handoff_for(task),
            env={
                "role": role,
                "task_id": task.id,
                "run_id": self.run_id,
                "sandbox": self._profile_for_role(role),
                "control_plane": self.control is not None,
            },
        )
        written = dossier.write(cwd, document)
        self._spend_handover(role, dossier.handover_bytes(document, written, self.repo.path))
        return f"{dossier.RELATIVE_PATH}/{task.id}.json"

    def _profile_for_role(self, role: str) -> str:
        """The executor profile's kind for `role` — what the agent is actually running inside.

        Handed to the agent so it stops having to infer its own environment from the shape of its
        prompt, and so a `codex` told it is already inside an OCI profile can stop trying to build
        a second sandbox around itself.
        """
        key = {"implementer": "implementer", "code_reviewer": "reviewer"}.get(role, "quality_gate")
        profile = self.config.raw.profile_for(key)
        return profile.kind if profile is not None else "host"

    def _history_for(self, task: dag.Task) -> list[dict[str, Any]]:
        """One line per past attempt: which step went red and why, oldest first.

        The handoff carried the *latest* failure only, so a task on its fourth attempt arrived
        with no memory of the three before it and could — and did — re-try the same fix. The lines
        come out of the audit chain, which already records every one of them.
        """
        if self.dry_run or not self.cycle_id:
            return []
        try:
            events = self.store.read_events()
        except Exception:  # noqa: BLE001 - a damaged chain is doctor's to report, not the loop's
            return []
        seen: list[dict[str, Any]] = []
        for event in events:
            if event.event != "task_failed" or task.id not in event.subject_ids:
                continue
            step = str(event.detail.get("step", "")) or str(event.detail.get("kind", ""))
            if step:
                seen.append({"attempt": len(seen) + 1, "step": step})
        handoff = self._handoff_for(task)
        if seen and handoff.get("failure_summary"):
            seen[-1]["reason"] = str(handoff["failure_summary"])[-600:]
        return seen[-dossier.MAX_HISTORY :]

    # -- implementer launch and quality gate --

    def _implementer_prompt(self, task: dag.Task, failure_log: str, dossier_path: str = "") -> str:
        return build_prompts.implementer_prompt(
            task,
            failure_log,
            gate_cmds=self.config.gate_cmds,
            has_baseline=self.repo.path("docs/05-current-state.md").exists(),
            pathspec=self.ws.pathspec,
            handoff=self._handoff_for(task),
            dossier_path=dossier_path,
        )

    @property
    def _implementer_adapter(self) -> adapters.Adapter | None:
        return adapters.adapter_for(self.config.adapter_argv)

    @property
    def _resume_capable(self) -> bool:
        """Whether a retry can continue the implementer's own session instead of starting cold.

        Read off the adapter's capability record rather than an `== "claude"` test. The
        consequence of a `False` here is the largest avoidable cost in a long build — every retry
        re-reads the ticket, the design slice and the code from scratch — so it belongs somewhere
        `doctor` can see it and say so, not in a branch inside the launcher.
        """
        adapter = self._implementer_adapter
        return bool(adapter and adapter.resumable)

    def _invoke_implementer(
        self,
        task: dag.Task,
        cwd: str,
        failure_log: str,
        session: str = "",
        resume: bool = False,
        base: str = "",
    ) -> None:
        """One headless implementer launch; `session`/`resume` thread retry-session continuity.

        With a session id, the first launch stamps it (--session-id) and a retry resumes it
        (--resume) so the implementer keeps its own context across its retries instead of
        re-reading ticket/design/code cold. A failed resume falls back to one fresh launch
        (session files can expire) rather than stopping the loop on a continuity optimization —
        but only when a fresh launch could plausibly do better. An exhausted session limit or a
        CLI that is not on PATH gets no second attempt: nothing was going to launch.
        """
        if self.dry_run:
            print(f"    [dry-run] launch implementer (cwd={cwd}) task={task.id}")
            return
        prompt = self._implementer_prompt(task, failure_log, self._write_dossier(task, cwd, base, "implementer"))
        where = f"{task.id}: implementer"
        adapter = self._implementer_adapter
        flags: list[str] = []
        if session and adapter is not None:
            flags += [*(adapter.resume_flags if resume else adapter.session_flags), session]
        try:
            self._launch(
                adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE, extra=flags),
                cwd=cwd,
                where=where,
                env=self._leaf_env(task),
                task_id=task.id,
                role="implementer",
                session=session,
                resumed=resume,
            )
            return
        except EnvironmentFault as fault:
            # The case this branch is most worth having is a session that outgrew the model's
            # window: what did not fit is its own accumulated context, and a cold launch is exactly
            # how it stops being carried. It reaches here already — transient, not capacity — which
            # is why `faults.is_context_overflow` stays a predicate for callers that have no
            # session to reset rather than a classification that would take this retry away.
            if not resume or not fault.retryable or faults.is_capacity(fault.output):
                raise
            print(f"    [resume] {task.id}: resuming session failed (rc={fault.rc}); relaunching fresh")
        # A fresh token: the first one was spent on the launch that failed, and the server
        # accepts each nonce once.
        self._launch(
            adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE),
            cwd=cwd,
            where=where,
            env=self._leaf_env(task),
            task_id=task.id,
            role="implementer",
        )

    def _leaf_env(self, task: dag.Task, role: str = "implementer") -> dict[str, str] | None:
        """The environment an implementer runs with: the control socket, a scoped token, and who it is.

        The token is scoped to this run and this task, granting only what a leaf legitimately
        needs (declare a decision, record a knowledge gap, report status, append an event). It can
        never carry `gate.approve` or its siblings — `mint` refuses to sign those, and the
        server refuses to serve them even if a token somehow claimed them.

        The `REIN_*` variables say what the agent *is*. Its role, its task, and the kind of
        executor profile it is running inside were previously things it could only infer from the
        shape of the prompt it was handed — which is guessing, and it guessed wrong in the one
        case that mattered: a `codex` implementer already inside an OCI profile still tried to
        build a second sandbox around itself.

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
            "REIN_ROLE": role,
            "REIN_TASK_ID": task.id,
            "REIN_RUN_ID": self.run_id,
            "REIN_SANDBOX": self._profile_for_role(role),
        }

    @property
    def _steps_effective(self) -> tuple[GateStep, ...]:
        """The gate steps actually run. All of them: the DoD has no opt-out knob."""
        return self.config.steps

    def _steps_at(self, stage: str) -> tuple[GateStep, ...]:
        """The DoD steps belonging to one stage of the run.

        `stage: both` is the default and what every step has always done. Naming `task` or
        `integration` moves *when* a step runs, never whether: a whole suite re-established from
        scratch on each attempt of each task, and again over the join, is the same confidence
        bought several times. An operator decides that at gate ③; no task can.
        """
        return tuple(step for step in self._steps_effective if step.stage in {stage, "both"})

    def _steps_for(self, task: dag.Task, cwd: str = "", base: str = "") -> tuple[GateStep, ...]:
        """The gate steps for one task: this stage's DoD, minus any step whose `paths:` this
        task's diff does not touch.

        Still not a knob an implementer can turn: `paths:` and `stage:` are frozen at gate 3 in
        config.yaml alongside every other DoD step, not read from the task or its ticket. A step
        naming no `paths:` — every packaged step ships this way — runs for every task exactly as
        before. The diff is computed fresh (`_review_scope`, the same source the review prompt's
        scope uses) so an empty/unresolved diff (a fresh worktree, dry-run, no `cwd` given) runs
        every step rather than guessing an empty scope means nothing to check.
        """
        steps = self._steps_at("task")
        if not cwd:
            return steps
        changed, _ = self._review_scope(task, cwd, base)
        return tuple(step for step in steps if step.matches_paths(changed))

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
            paths = self.ws.branch_changed_paths(task.id, cwd=cwd)
            return paths, f"git diff {self.ws.target_branch(task.id)}...HEAD"
        if base:
            return self.ws.changed_since(base), f"git diff {base[:12]}..HEAD"
        return [], ""

    def _review_prompt(
        self,
        task: dag.Task,
        cwd: str,
        base: str,
        dossier_path: str = "",
        findings_path: str = "",
        argv: Sequence[str] = (),
    ) -> str:
        changed, diff_cmd = self._review_scope(task, cwd, base)
        return build_prompts.review_prompt(
            task,
            gate_cmds=self.config.gate_cmds,
            changed_paths=changed,
            diff_cmd=diff_cmd,
            dossier_path=dossier_path,
            findings_path=findings_path,
            # Keyed on the argv this step will actually be launched with, not on the default one: a
            # step may name its own `agent_argv`, and offering a discipline the launched CLI does
            # not have is the dangling reference this replaced.
            disciplines=adapters.disciplines_for(argv or self.config.adapter_argv),
        )

    def _fingerprint(self, cwd: str) -> str:
        """The content digest of the tree at `cwd` ("" when it cannot be computed)."""
        return "" if self.dry_run else self.ws.fingerprint(cwd)

    def _run_agent_step(self, step: GateStep, task: dag.Task, cwd: str, base: str) -> bool:
        """Run the review agent step headless. Returns True if the tree ended up changing.

        **The reviewer reports; it does not repair.** It used to be launched with write access and
        told to apply its fixes, which put judging a change and editing it away in one pair of
        hands — and moved the tree underneath the gate, so every already-passed step had to be
        re-run behind it. Now it writes findings to a file, the implementer resolves the `must_fix`
        ones within this step's own retry budget, and the reviewer looks again. The extra launch
        is the price of the separation; what it buys back is a reviewer that no longer re-runs the
        test suite the caller runs anyway, and a tree that only one participant moves.

        Launched with the adapter of the role the *step* declares — not the implementer's. Those
        were the same process until this was fixed, so a reviewer configured as a second opinion
        was the same model that had just written the code.

        A reviewer whose findings cannot be read is not a reviewer that found nothing: an
        unreadable answer stops the step rather than passing it.
        """
        role = step.agent_role or "code_reviewer"
        before = self._fingerprint(cwd)
        rounds = max(0, step.retries)
        for attempt in range(rounds + 1):
            findings = self._collect_findings(step, task, cwd, base, role)
            self._add_review_findings(task.id, findings)
            outstanding = dossier.must_fix(findings)
            if not outstanding:
                if findings:
                    print(f"    [review] {task.id}: {len(findings)} finding(s), none blocking")
                break
            if attempt == rounds:
                # The budget is spent and the defects stand. Reporting the step as passed here is
                # the one thing this must never do, so it raises rather than returns.
                raise StopLoop(
                    f"{task.id}: the reviewer's findings were not resolved within "
                    f"{rounds} round(s):\n{dossier.render_findings(outstanding)}"
                )
            print(f"    [review] {task.id}: {len(outstanding)} must-fix finding(s) → back to the implementer")
            self._invoke_review_fixer(task, cwd, base, dossier.render_findings(outstanding))
        after = self._fingerprint(cwd)
        # An unknown fingerprint on either side reads as "it changed": re-running the passed steps
        # costs time, skipping them over an unread tree costs the verdict.
        return not before or not after or after != before

    def _collect_findings(self, step: GateStep, task: dag.Task, cwd: str, base: str, role: str) -> list[dict[str, Any]]:
        """One reviewer launch, and its findings — read from the file, never from the chatter."""
        dossier_path = self._write_dossier(task, cwd, base, role)
        target = dossier.findings_path(cwd, task.id)
        target.unlink(missing_ok=True)  # a stale file from the previous round is not this answer
        argv = step.agent_argv or self.config.adapter_argv
        findings_rel = f"{dossier.RELATIVE_PATH}/{target.name}"
        prompt = self._review_prompt(task, cwd, base, dossier_path, findings_rel, argv=argv)
        # `REVIEW`, not `WRITE`: the reviewer's `.rein/work/` file is the only thing it needs to
        # produce, and everything else it might touch belongs to somebody else. Naming the file
        # here is what lets an adapter that can scope a write grant exactly that one — and what
        # made "no flags at all" wrong for every CLI whose tools are deny-by-default, whose
        # reviewer could not write the findings the loop then refused to proceed without.
        self._launch(
            adapters.command(argv, prompt, access=adapters.REVIEW, writable=findings_rel),
            cwd=cwd,
            where=f"{task.id}: the '{step.name}' agent step",
            env=self._leaf_env(task, role),
            task_id=task.id,
            role=role,
        )
        if not target.exists():
            raise StopLoop(
                f"{task.id}: the reviewer wrote no findings file ({dossier.RELATIVE_PATH}/{target.name}). "
                "A review that produced nothing readable is not a review that found nothing."
            )
        try:
            return dossier.parse_findings(target.read_text(encoding="utf-8"))
        except (dossier.FindingsError, OSError) as exc:
            raise StopLoop(f"{task.id}: the reviewer's findings could not be read — {exc}") from None

    def _invoke_review_fixer(self, task: dag.Task, cwd: str, base: str, findings: str) -> None:
        dossier_path = self._write_dossier(task, cwd, base, "implementer")
        self._launch(
            adapters.command(
                self.config.adapter_argv,
                build_prompts.review_fix_prompt(
                    task, findings, gate_cmds=self.config.gate_cmds, dossier_path=dossier_path
                ),
                access=adapters.WRITE,
            ),
            cwd=cwd,
            where=f"{task.id}: the review fixer",
            env=self._leaf_env(task),
            task_id=task.id,
            role="implementer",
        )

    # --- conflict resolution ---------------------------------------------------
    #
    # Two seams the merge paths need and this class already owns: launching an implementer, and
    # running the deterministic half of the DoD. `conflict` holds the decision they feed; keeping
    # the plumbing here is what stops that module importing this one.

    def _graph_task(self, task_id: str) -> dag.Task | None:
        if not task_id or self._plan is None:
            return None
        try:
            return dag.join(self._plan, self.state).get(task_id)
        except (dag.DagError, KeyError):
            return None

    def task_gate(self, cwd: str) -> tuple[int, str]:
        """The **deterministic** half of the task-stage DoD over `cwd`. `(0, "")` when every step passed.

        Command steps only. A merge resolution is judged on whether the tree still holds up, and
        that is what an exit status answers; spending a reviewer agent on merge glue would be a
        different question asked at several times the price. The steps themselves are the frozen
        ones — `stage` and `paths` came from gate ③ like every other part of the DoD.
        """
        for step in self._steps_at("task"):
            if step.kind != "command" or not step.command:
                continue
            failure = self._run_cmd_step(step, cwd)
            if failure:
                return 1, f"{step.name}: {failure}"
        return 0, ""

    def resolve_conflict(self, collision: conflict.Conflict, cwd: str) -> str:
        """Launch an implementer against a conflicted worktree; return the outcome it reported.

        The prompt carries **both sides' purpose**, not just the hunks (`build_prompts.conflict_prompt`).
        What comes back is a claim travelling the one channel a claim may travel — `rein report` —
        and `conflict` decides what it is worth.
        """
        ours = self._graph_task(collision.ours_task)
        theirs = self._graph_task(collision.theirs_task)
        prompt = build_prompts.conflict_prompt(ours, theirs, collision.paths, gate_cmds=self.config.gate_cmds)
        self._launch(
            adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE),
            cwd=cwd,
            where=f"{collision.ours_task or 'merge'}: conflict",
            env=self._leaf_env(ours) if ours is not None else None,
            task_id=collision.ours_task,
            role="implementer",
        )
        return str(self._read_report(ours).get("outcome", "")) if ours is not None else ""

    def _run_cmd_step(self, step: GateStep, cwd: str, *, note: bool = True) -> str:
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
        tool = self._step_tool(step, profile)
        subject = self._fingerprint(cwd)
        if self.ledger.hit(evidence.KIND_GATE_STEP, subject, tool):
            print(f"    [gate] {step.name}: already green on this tree — reusing")
            if note:
                self._note_evidence(step, profile, reused=True)
            return ""
        spec = executors.ExecutionSpec(
            command=tuple(step.command),
            profile=profile,
            mounts=self._mounts_for(profile, cwd),
            workdir=_SANDBOX_WORKDIR if profile.is_sandboxed else cwd,
            timeout_sec=self.config.timeout_cmd,
        )
        where = f"gate step '{step.name}'"
        try:
            # Same reason as a launch: an executor captures its child's output, so a test suite
            # that takes twenty minutes is twenty silent minutes to whatever host is waiting.
            with _Heartbeat(where):
                result = executors.for_profile(profile).run(spec)
        except executors.ExecutorError as exc:
            raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=1, output=str(exc)) from exc
        if result.exit_code == 0:
            # Only a green is recorded. Caching a red would let one broken afternoon stand in for
            # a verdict on code nobody re-ran, which is the direction that costs correctness
            # rather than time.
            self.ledger.record(evidence.KIND_GATE_STEP, subject, tool)
            if note:
                self._note_evidence(step, profile, reused=False)
            return ""
        fault = faults.classify_step(result.exit_code, result.output)
        if fault.is_environment:
            raise EnvironmentFault(fault, where=where, rc=result.exit_code, output=result.output)
        return summarize_failure(step.display, result.exit_code, result.output)

    def _step_tool(self, step: GateStep, profile: models.ExecutorProfile) -> tuple[str, ...]:
        """The tool identity a gate step's evidence is keyed on.

        The step's name and argv, plus what it ran inside. "`make test` was green" is not a fact
        about the code alone: a different image is a different claim, and a `host` profile is a
        claim about a machine nothing pins at all — which is exactly why it is named here rather
        than folded into a blank.
        """
        where = str(profile.raw.get("image", "")) if profile.is_sandboxed else f"host:{profile.name}"
        return (step.name, *step.command, where)

    def _note_evidence(self, step: GateStep, profile: models.ExecutorProfile, *, reused: bool) -> None:
        """Remember that this step was green, for the `evidence` block the task's `done` carries."""
        self._current_step_evidence.append(
            {
                "name": step.name,
                "image": str(profile.raw.get("image", "")) if profile.is_sandboxed else f"host:{profile.name}",
                "reused": reused,
            }
        )

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
        steps = self._steps_for(task, cwd, base)
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
        failed, failure = self._negative_control(task, cwd, base, passed)
        if failed:
            return failed, failure
        return self._run_acceptance(task, cwd)

    def _negative_control(
        self, task: dag.Task, cwd: str, base: str, passed: Sequence[GateStep]
    ) -> tuple[str | None, str]:
        """Ask whether the DoD that just went green would have gone green *without* the change.

        The DoD is the only automated evidence a task's `done` rests on, and until this existed
        nobody ever asked whether it could go red. The tests it runs were written by the
        implementer in the same launch as the code they test; the blind extractor is deliberately
        never shown them (`review_reading.split_tests`), and the security reviewer reads them only
        for what an attacker could do with them. So the Expected/Actual split this whole workflow is
        built on reached the code and — until the per-task reviewer was asked the question named
        below — never once the tests, and a test that asserts nothing produces a green that
        re-running it reproduces exactly. Re-running defends against an agent that *lies*; it does nothing
        against one that *self-confirms*, which is the failure mode this system exists to catch.

        The control is the experiment that closes it, and it is mechanical rather than a reading:
        take the base commit this change is a change to, apply **only the task's test half** onto
        it, and re-establish the steps that just passed. If every step is still green, nothing in
        the change is under test, and the green that would have closed the task is a fact about
        code that was already there.

        **Read the two outcomes for what each is worth — they are not symmetric.** A green control
        is the strong one: it is a fact about every test in the change at once, and no reading of
        the test files could establish it more cheaply or more surely. A red one says only that
        the test half is *not inert* against the old code — the step that went red may have gone
        red because a test asserted something false there, or because the test imports a symbol
        the base does not have and never got as far as asserting anything. This experiment cannot
        separate those without parsing a test runner's output, which is a thing this loop does not
        do. So `discriminating` is the absence of the failure, not the presence of a good test;
        what asks whether the tests are *any good* is the per-task reviewer, which reads them.

        Three answers are not passes, and each says so rather than being folded into one:

        * **no test path changed** — there is no control to take. Not a failure: a task whose work
          is genuinely covered by tests that already existed is a real thing, and blocking it would
          make the loop demand a test per task rather than evidence per claim. It is recorded, so
          "this task's green rests on tests nobody wrote for it" is on the record instead of being
          the silence it has always been.
        * **the control could not be set up** — no base, no command step in the DoD to re-establish,
          a diff git would not give up, an unapplied patch, a worktree that would not create.
          Recorded with the reason. Never a pass and never a block: a broken experiment is not
          evidence in either direction, and inventing a verdict from one is the thing the rest of
          this module refuses to do.
        * **every step green** — the block. It comes back through the same channel a red step does,
          so it spends that attempt's budget and the implementer is told what is missing.
        """
        commands = [step for step in passed if step.kind == "command" and step.command]
        if self.dry_run:
            return None, ""
        if not commands:
            # Recorded rather than returned in silence, for the same reason `no_tests_changed` is:
            # a quality gate made only of agent steps, or one whose command steps are all
            # unconfigured, leaves the task's `done` resting on nothing this experiment can negate.
            # Saying so is the whole point of the record, and `brief._control` reads it.
            return self._control_undetermined("the quality gate passed no command step to re-establish")
        changed, _ = self._review_scope(task, cwd, base)
        tests = [path for path in changed if diff_facts.classify_path(path) == "test"]
        if not tests:
            self._note_control("no_tests_changed", detail="the change touched no test path")
            print(f"    [control] {task.id}: no test path changed — the DoD's green is not controlled")
            return None, ""
        control_base = base if cwd == self.root else self.ws.fork_point(self.ws.target_branch(task.id), cwd)
        if not control_base:
            return self._control_undetermined("the base this change is a change to could not be resolved")
        patch = self.ws.diff_from(control_base, cwd, tests)
        if patch is None:
            return self._control_undetermined(
                f"the test half of the change against {control_base[:12]} could not be read out of git"
            )
        if not patch.strip():
            return self._control_undetermined(f"the test half of the change against {control_base[:12]} was empty")
        try:
            return self._take_control(task, control_base, patch, commands)
        except EnvironmentFault:
            raise
        except StopLoop as exc:
            return self._control_undetermined(str(exc))

    def _take_control(
        self, task: dag.Task, control_base: str, patch: str, commands: Sequence[GateStep]
    ) -> tuple[str | None, str]:
        """Run `commands` over `control_base` + `patch` and report which of them went red."""
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as handle:
            handle.write(patch)
            patch_file = handle.name
        try:
            with build_git.scratch_worktree(
                self.repo, self.config.worktree_dir, f"control-{task.id}", control_base, _late_run
            ) as control_cwd:
                rc, out = _late_run(["git", "apply", patch_file], cwd=control_cwd)
                if rc != 0:
                    return self._control_undetermined(
                        f"the test half did not apply onto {control_base[:12]}: {out[-300:]}"
                    )
                for step in commands:
                    # `note=False`: this green is a fact about the control tree, not about the
                    # task's, and the task's `evidence.steps` is the list of what its own DoD
                    # established. The ledger still records it — it is a true fact about a real
                    # tree, keyed on that tree's fingerprint, so it can never be mistaken for one
                    # about the task's.
                    if self._run_cmd_step(step, control_cwd, note=False):
                        self._note_control("discriminating", base=control_base, step=step.name)
                        print(
                            f"    [control] {task.id}: '{step.name}' goes red without the change "
                            "— the test half is not inert"
                        )
                        return None, ""
        finally:
            Path(patch_file).unlink(missing_ok=True)
        # Deliberately not noted: `evidence.negative_control` justifies a `done`, and this verdict
        # is the one that stops there being one. It travels as a task failure instead — the same
        # channel a red step uses, under the step name `NEGATIVE_CONTROL` — so the event chain
        # carries it and the next attempt inherits the summary.
        names = ", ".join(step.name for step in commands)
        return NEGATIVE_CONTROL, (
            f"The quality gate is green, and it is green without your change. Re-running "
            f"{names} over {control_base[:12]} with only this task's test files applied passed "
            "every step, which means no test in this change exercises it: whatever the code now "
            "does, the suite would say the same if the code were not there.\n"
            "Add or fix a test that fails against the code as it was and passes against the code "
            "as it is. If this task genuinely cannot be tested that way — it changes no behaviour "
            "anything can observe — say so with `rein report --outcome needs-revision` and name "
            "the acceptance criterion that has no observable form, rather than writing a test that "
            "cannot fail."
        )

    def _control_undetermined(self, detail: str) -> tuple[str | None, str]:
        self._note_control("undetermined", detail=detail)
        print(f"    [control] could not be taken: {detail}")
        return None, ""

    def _note_control(self, result: str, *, base: str = "", step: str = "", detail: str = "") -> None:
        record: dict[str, Any] = {"result": result}
        if base:
            record["base"] = base
        if step:
            record["step"] = step
        if detail:
            record["detail"] = detail[:500]
        self._current_control.clear()
        self._current_control.update(record)

    def _run_acceptance(self, task: dag.Task, cwd: str) -> tuple[str | None, str]:
        """Establish the task's own acceptance criteria, after the shared DoD has passed.

        Last, and only after the DoD, because the two answer different questions and the order
        matters when one of them fails: "the code is unsound" is a more useful first sentence than
        "the code did not do what the ticket asked", and the second is usually a consequence of
        the first.

        A criterion that runs and fails returns through the same channel a gate step does, so it
        inherits the send-back budget and the retry machinery whole — the implementer gets told
        which criterion, and why, exactly as it would about a red `check`. Criteria the loop
        cannot establish (`external`) are *not* failures and are not reported here; the caller
        finds them on the evidence record and parks the task at `awaiting-evidence`.
        """
        for entry in task.acceptance:
            evidence_spec = entry.get("evidence")
            if not isinstance(evidence_spec, dict):
                continue  # prose only, and honest about it: gate ④ is where a human reads it
            kind = str(evidence_spec.get("kind", ""))
            if kind not in models.MECHANIZED_EVIDENCE_KINDS:
                continue
            ac_id = str(entry.get("id", "?"))
            failure = self._establish_acceptance(task, ac_id, kind, evidence_spec, cwd)
            if failure:
                statement = str(entry.get("statement", ""))
                return f"{_ACCEPTANCE_PREFIX}{ac_id}", f"{ac_id} ({statement}) is not satisfied.\n{failure}"
        return None, ""

    def _establish_acceptance(self, task: dag.Task, ac_id: str, kind: str, spec: Mapping[str, Any], cwd: str) -> str:
        """One criterion. "" when established, a compact failure otherwise."""
        subject = self._fingerprint(cwd)
        tool = (f"{task.id}:{ac_id}", kind, *(str(p) for p in spec.get("command", spec.get("paths", []))))
        if self.ledger.hit(evidence.KIND_ACCEPTANCE, subject, tool):
            self._note_acceptance(ac_id, kind, reused=True)
            return ""
        if kind == "artifact":
            missing = [str(p) for p in spec.get("paths", []) if not (Path(cwd) / str(p)).exists()]
            if missing:
                return f"these artifacts do not exist: {', '.join(missing)}"
        else:
            step = GateStep(
                name=f"acceptance:{ac_id}",
                kind="command",
                command=tuple(str(part) for part in spec.get("command", [])),
                executor_profile=str(spec.get("executor_profile", "")),
            )
            # Through the same runner as a gate step, so a criterion runs in a sandbox for the
            # same reason a test does — it is repository-derived code either way.
            if failure := self._run_cmd_step(step, cwd):
                return failure
        self.ledger.record(evidence.KIND_ACCEPTANCE, subject, tool)
        self._note_acceptance(ac_id, kind, reused=False)
        return ""

    def _note_acceptance(self, ac_id: str, kind: str, *, reused: bool) -> None:
        self._current_acceptance.append({"id": ac_id, "kind": kind, "reused": reused})

    def _warm_reading(self, task: dag.Task) -> None:
        """Take gate ④'s reading of this task now, while its diff is one task wide.

        The gate reads the change in the readings the plan's task scopes describe
        (`review_reading.plan_readings`), and it asks each one the same question this does — same
        measure, same key. Answering it here means the gate finds it answered: the peak of one
        launch stays the size of one task, and a review regenerated after a fix re-reads only the
        task whose code moved.

        **A warm-up never fails a build.** It is an optimization over a cache the gate does not
        depend on: if it does not happen, `rein review generate` takes the reading itself, at the
        cost this exists to avoid and with nothing else different. So an adapter that will not
        answer stops the warming for the rest of the run — retrying it once per task would spend a
        session limit on it — and says so once, rather than stopping the build.

        Skipped for a task with no declared scope: an undeclared scope means *unbounded*, so its
        reading would be the whole change, which is neither one task wide nor what the gate will
        ask for.
        """
        if self.dry_run or self._warming_off or self.config.raw.composition == review_reading.WHOLE:
            return
        if not task.scope_include:
            return
        try:
            # The same base the gate will resolve, not the plan's field: they differ whenever the
            # plan names a commit this checkout does not have, and a warm-up taken against a
            # different base answers a question nobody asks.
            base = review_reading.resolve_base(self.repo, self._plan, None)
            head = self.repo._git_rc("rev-parse", "HEAD")[1].strip()
            if not base or not head:
                return
            exclude = review_reading.not_the_product(self.repo, self.state)
            limits = {**human_review.DEFAULT_BUDGET, **self.config.raw.budgets}
            review_reading.warm(
                self.repo,
                review_transport.StagedReviewers(self.repo, config=self.config.raw),
                reading=review_reading.Reading(unit=task.id, include=tuple(task.scope_include)),
                base=base,
                head=head,
                exclude=exclude,
                limits=limits,
                config=self.config.raw,
                cache=review_cache.StageCache(self.repo.root),
            )
        except (review_policy.ReviewPolicyError, common.ReinError, OSError) as exc:
            self._warming_off = True
            print(f"    [review] {task.id}: the gate-④ reading was not taken here ({exc}); the gate will take it")

    def _completion_status(self, task: dag.Task) -> str:
        """`done`, or `awaiting-evidence` when a criterion nobody here can establish is still open.

        Asked **after the work has landed on the work branch**, not while it sat in a worktree,
        and that placement is the whole design. An external criterion is something a person has
        to go and look at — a staging deployment, a device, a screen — and none of that is
        observable about code that only exists on an unmerged leaf branch. So the code merges
        (it passed the entire DoD; nothing about it is in question) and only the *task* waits,
        which is enough: gate ④ cannot open while a task is not done.

        It also makes the fingerprints line up. The observation a human records is about the tree
        they can actually see — the canonical checkout — and so is this check.
        """
        outstanding = self._unestablished_acceptance(task)
        if not outstanding:
            return "done"
        self._escalate(
            "awaiting_evidence",
            f"{task.id}: passed the quality gate and landed, but {', '.join(outstanding)} needs evidence this "
            f"loop cannot obtain. Observe it, then record it with "
            f"`rein evidence record --task {task.id} --ac <id> --note …`.",
            task=task.id,
        )
        return "awaiting-evidence"

    def _unestablished_acceptance(self, task: dag.Task) -> list[str]:
        """Criteria this loop cannot establish and nobody has recorded against the current tree.

        `external` says so up front: a staging check, a device, a person. The loop does not fail
        the task for it and does not quietly round it to `done` either — both would be a claim
        nobody made. A human closes it with `rein evidence record`, and the record is bound to a
        tree, so it cannot outlive the code it was about.
        """
        if self.dry_run:
            return []
        external = [
            str(entry.get("id", "?"))
            for entry in task.acceptance
            if isinstance(entry.get("evidence"), dict) and str(entry["evidence"].get("kind", "")) == "external"
        ]
        if not external:
            return []
        tree = self._fingerprint(self.root)
        recorded = {
            str(item.get("id")) for item in self._recorded_acceptance(task.id) if str(item.get("tree", "")) == tree
        }
        return [ac_id for ac_id in external if ac_id not in recorded]

    def _recorded_acceptance(self, task_id: str) -> list[Mapping[str, Any]]:
        """External observations a human has recorded for this task, from the canonical store."""
        state = self.store.read_state()
        entry = state.raw.get("tasks", {}).get(task_id) if state is not None else None
        recorded = entry.get("acceptance") if isinstance(entry, dict) else None
        return [item for item in recorded if isinstance(item, dict)] if isinstance(recorded, list) else []

    def _record_task_evidence(self, task: dag.Task, cwd: str, base: str) -> None:
        """Remember what this task's pass was established on, for the `done` that follows.

        Written into `state.yaml` beside the status, in the same transaction, so `done` carries
        its own justification instead of being a word somebody's process exiting produced. Steps
        are deduplicated by name keeping the last run of each: a step re-run after an agent step
        moved the tree was established twice, and the second one is the one that holds.
        """
        if self.dry_run:
            return
        by_name: dict[str, dict[str, Any]] = {}
        for entry in self._current_step_evidence:
            by_name[str(entry["name"])] = entry
        record: dict[str, Any] = {
            "steps": list(by_name.values()),
            "reported": self._read_report(task).get("outcome", "none"),
        }
        if self._current_acceptance:
            record["acceptance"] = list(self._current_acceptance)
        if self._current_control:
            record["negative_control"] = dict(self._current_control)
        fingerprint = self._fingerprint(cwd)
        if fingerprint:
            record["tree"] = fingerprint
        if base and _COMMIT_RE.match(base):
            record["base"] = base
        with self._evidence_lock:
            self._evidence[task.id] = record

    def _read_report(self, task: dag.Task) -> dict[str, Any]:
        """What the implementer said about this attempt, through `rein report`. {} when it said nothing."""
        if self.dry_run:
            return {}
        report = self._handoff_for(task).get("report")
        return dict(report) if isinstance(report, dict) else {}

    def _check_implementer_output(self, task: dag.Task, cwd: str, base: str) -> tuple[str, str]:
        """What the implementer's attempt actually produced. ("", "") when it may go to the gate.

        Returns `(kind, message)` for the three ways an attempt ends without the quality gate
        having anything to say about it — the ones the loop used to run a full DoD over, and then
        mark `done`:

          `no_implementation`  the diff is empty. Nothing was built, so a green gate is a
                               statement about the code that was already there. This is the case
                               that let a sandbox refusing to let an agent write pass as success.
          `agent_blocked`      the implementer said it could not do this. Asking a reviewer to
                               review nothing, and a test suite to confirm it, is cost with no
                               question attached.
          `report_mismatch`    the implementer named paths it did not change. Its account of its
                               own work is wrong, which is a finding whatever the tests say.

        The empty-diff check needs a resolved scope to mean anything: an unresolved one (dry run,
        a serial task with no base) is read as "not known", never as "nothing" — a fail-open the
        gate itself already takes for its `paths:` filtering.
        """
        report = self._read_report(task)
        outcome = str(report.get("outcome", ""))
        if outcome in {"blocked", "needs-revision"}:
            summary = str(report.get("summary", "")).strip() or "(no summary given)"
            return f"agent_{outcome.replace('-', '_')}", f"{task.id}: the implementer reported {outcome} — {summary}"

        changed, diff_cmd = self._review_scope(task, cwd, base)
        if diff_cmd and not changed:
            said = f" It reported: {report.get('summary', '')!r}." if report.get("summary") else ""
            unheard = "" if report else " It never called `rein report`, so it said nothing about why."
            return (
                "no_implementation",
                f"{task.id}: the implementer produced no change at all ({diff_cmd} is empty).{said}{unheard} "
                "A quality gate green over an unchanged tree is a fact about the code that was already there.",
            )

        outside = dossier.scope_violations(task, changed)
        if outside:
            return (
                "scope_violation",
                f"{task.id}: changed {', '.join(outside)}, which its declared scope does not cover "
                f"(include={list(task.scope_include)}, exclude={list(task.scope_exclude)}). "
                "The plan says where this task's work belongs; landing it elsewhere is a scope change, "
                "and a scope change to an approved plan is a human's decision. Either the change "
                "belongs to another task, or the plan drew this one's scope too small — the second "
                "is answered with `rein revise --to tasks`, widening `scope.include`, and a "
                "re-approval, never by editing the frozen plan in place.",
            )

        claimed = {str(p) for p in report.get("touched", []) if isinstance(p, str)}
        untouched = sorted(claimed - set(changed)) if claimed and changed else []
        if untouched:
            return (
                "report_mismatch",
                f"{task.id}: the implementer reported changing {', '.join(untouched)}, "
                "which the diff does not contain. Its account of its own work does not match what it did.",
            )
        return "", ""

    def _futile(self, task: dag.Task, failed: str, failure_log: str, tree: str, seen: tuple[str, str, str]) -> str:
        """Why spending another round on this failure would buy the same answer. "" = worth retrying.

        Two readings, and neither parses the failure's text. `faults` refuses to interpret build-tool
        output on principle — detecting "the lockfile is out of sync" or "the browser is not
        installed" would mean carrying a pattern for every tool anyone runs — so what is read here
        is the *observation* instead, which is tool-agnostic and exact:

          **It was already red.** The step failed on the work branch before any task ran
          (`_establish_baseline`). Sending an implementer back to fix a break it did not cause, in
          a scope that does not contain it, is three launches spent on a question nobody asked.

          **Nothing moved.** The same step failed with byte-identical output over a tree with the
          same fingerprint. The implementer ran and changed nothing; the next round has the same
          inputs and will reach the same place. This is what actually catches the reported cases —
          a lockfile mismatch, a missing browser binary, an absent CDK context — without knowing
          anything about any of them.

        An unknown fingerprint ("" — a dry run, a git layer that could not answer) never matches:
        fail open towards retrying, because spending a retry is recoverable and refusing one on an
        unread tree is not.
        """
        if failed in self._baseline_red:
            return (
                f"{task.id}: '{failed}' was already red on {self.branch} before this task ran, so a "
                f"send-back would ask the implementer to fix a break outside its scope.\n"
                f"What the baseline said:\n{self._baseline_red[failed]}"
            )
        digest = digests.of_bytes(failure_log.encode("utf-8"))
        if tree and seen == (failed, digest, tree):
            return (
                f"{task.id}: '{failed}' failed identically over an unchanged tree — the implementer "
                "ran and moved nothing, so another round has the same inputs and reaches the same place."
            )
        return ""

    def _stop_before_the_gate(self, task: dag.Task, kind: str, message: str, *, tree: str, futile: str = "") -> None:
        """Record an attempt that ended before the quality gate, and the status that ending calls for.

        `_escalate`'s counterpart for this one path, and it replaces it rather than joining it: the
        `knowledge_gap` a human reads and the record the next attempt inherits are one write, so a
        terminal killed between them cannot leave an escalation in the chain with nothing saying
        what the next run already knows.
        """
        logger.warning(f"[escalation] {message}")
        if not self.dry_run and self.cycle_id:
            record_escalation(self.repo, task.id, kind=kind, message=message, tree=tree, futile=futile)
        self._stops[task.id] = "needs-revision" if kind in _PLAN_DEFECT_KINDS else "blocked"

    def _already_answered(self, task: dag.Task, cwd: str) -> tuple[str, str, str] | None:
        """`(kind, message, tree)` this task already reached over exactly this tree. None = ask again.

        `_futile`'s reading — *nothing moved* — applied to the other way an attempt ends. A gate
        step can fail twice inside one run, so that comparison lives in a local; an attempt
        `_check_implementer_output` stopped returns immediately, so the second asking is always a
        *later `rein build`*, and the only thing that survives one is the handoff.

        What it catches is a task whose work already landed some other way — a salvage merge, a
        hand-applied fix — where every launch reports, correctly, that there is nothing to do. A
        field run paid for three of them on one task. Nothing here parses that report: the reading
        is the fingerprint, the same tool-agnostic observation `_futile` makes.

        An unknown fingerprint ("") never matches, the same fail-open: spending a launch is
        recoverable, refusing one over an unread tree is not.
        """
        if self.dry_run:
            return None
        recorded = self._handoff_for(task).get("escalation")
        if not isinstance(recorded, Mapping):
            return None
        kind, tree = str(recorded.get("kind", "")), str(recorded.get("tree", ""))
        if not kind or not tree or tree != self._fingerprint(cwd):
            return None
        return kind, str(recorded.get("message", "")), tree

    def _run_task_to_done(self, task: dag.Task, cwd: str, base: str = "") -> tuple[bool, str]:
        """Take one task to done via implementer implementation + the quality-gate pipeline.

        Each cmd step carries its own send-back budget (step.retries); a failure consumes only
        that step's budget. Returns (ok, log); ok=False means some step's budget ran out
        (the caller marks the task blocked).

        **The gate is not the first question asked.** What the implementer produced is checked
        first (`_check_implementer_output`), because a DoD that passes over an empty diff is not
        evidence about this task, and because running a reviewer and a full test suite against an
        attempt that already said "I am blocked" spends a model on a question nobody asked.
        """
        budgets = {s.name: s.retries for s in self._steps_for(task, cwd, base) if s.kind == "command"}
        # Every other verdict `_run_pipeline` can return. Neither the negative control nor an
        # acceptance criterion is a configured step, and both come back through this channel, so
        # without an entry here each one got the silent zero `.get(name, 0)` produced.
        budgets[NEGATIVE_CONTROL] = SEND_BACK_RETRIES
        for entry in task.acceptance:
            budgets[f"{_ACCEPTANCE_PREFIX}{entry.get('id', '?')}"] = SEND_BACK_RETRIES
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
        # (step, failure digest, tree fingerprint) of the previous round — what `_futile` compares
        # this round against. "" for the fingerprint means "unknown", which never matches.
        seen: tuple[str, str, str] = ("", "", "")
        answered = self._already_answered(task, cwd)
        if answered is not None:
            kind, prior, tree = answered
            futile = (
                f"{task.id}: not re-launched — the last attempt reached '{kind}' over a tree with this "
                "exact fingerprint, so a fresh implementer has the same inputs and reaches the same "
                f"verdict. If something outside the tree was repaired, `rein task reset {task.id} "
                "--fresh --reason ...` discards this record and buys the launch."
            )
            message = f"{prior}\n{futile}" if prior else futile
            self._stop_before_the_gate(task, kind, message, tree=tree, futile=futile)
            return False, message
        while True:
            self._invoke_implementer(task, cwd, failure_log, session=session, resume=resume, base=base)
            if not self.dry_run:
                changed, _ = self._review_scope(task, cwd, base)
                violations = self._gate_violations(changed)
                if violations:
                    raise GateViolationFault(violations)
                kind, message = self._check_implementer_output(task, cwd, base)
                if kind:
                    # Not a gate failure, so it spends no step's budget: no step ever ran. The
                    # attempt is over, and the reason — which the loop now actually holds — goes
                    # to the human rather than being reconstructed from an unchanged tree. The
                    # tree goes with it, so the next `rein build` can tell "try again" from
                    # "ask the same question a second time".
                    self._stop_before_the_gate(task, kind, message, tree=self._fingerprint(cwd))
                    return False, message
            self._local.steps, self._local.acceptance, self._local.negative_control = [], [], {}
            after_implementer = self._fingerprint(cwd)
            failed, failure_log = self._run_pipeline(task, cwd, base)
            if failed is None:
                self._record_task_evidence(task, cwd, base)
                return True, ""
            futile = self._futile(task, failed, failure_log, after_implementer, seen)
            seen = (failed, digests.of_bytes(failure_log.encode("utf-8")), after_implementer)
            if failed not in budgets:
                # Every name `_run_pipeline` can return is seeded above. One that is not is a
                # verdict this loop has no send-back rule for, and the old `.get(failed, 0)` gave
                # it a silent zero — ending the task on its first occurrence, with the log saying
                # "retries left: 0" for a budget nobody had ever set.
                raise common.ReinError(f"internal: no retry budget is registered for the gate verdict {failed!r}")
            left = 0 if futile else budgets[failed]
            if futile:
                print(f"    quality gate fail at step '{failed}', not retried — {futile}")
            else:
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
                    futile=futile,
                )
            if left <= 0:
                return False, failure_log
            if session and budgets[failed] <= 0:  # final retry for this step → fresh session
                session, resume = str(uuid.uuid4()), False
            else:
                resume = bool(session)

    # -- post-merge integration gate --

    def _integration_fix_prompt(self, ids: str, failure_log: str) -> str:
        return build_prompts.integration_fix_prompt(
            ids, failure_log, gate_cmds=self.config.gate_cmds, pathspec=self.ws.pathspec
        )

    def _invoke_integration_fixer(self, ids: str, prompt: str) -> None:
        """One implementer launch over the merged tree. The caller says what it is being sent.

        The prompt is the caller's because the join has two send-backs and they are not the same
        work: a red command step, and a reviewer's findings about what only the join shows. Both
        used to be framed as "the combined state fails the deterministic gate", which was true of
        one of them (`integration_fix_prompt`, `integration_review_fix_prompt`).
        """
        self._launch(
            adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE),
            cwd=self.root,
            where=f"{ids}: the integration fixer",
            role="implementer",
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
            for step in self._steps_at("integration"):
                if step.kind == "agent":
                    # `stage:` moves *when* a step runs, never whether — and an agent step declared
                    # at the integration stage was being skipped, which made the join the one tree
                    # no reviewer ever read. The command steps have just run over it; this is the
                    # half of the question they cannot answer.
                    self._run_integration_agent_step(step, tasks)
                    continue
                if not step.command:
                    continue
                failure = self._run_cmd_step(step, cwd=self.root)
                if failure:
                    failed, failure_log = step.name, failure
                    break
            if failed is None:
                return True, ""
            # Same rule as a task's send-back: a step that was already red before any of this
            # batch ran is not something a fixer launch can be spent on. Without this the join
            # paid the whole integration budget on the break the per-task loop had just refused
            # to pay it on, one level up.
            futile = self._baseline_red.get(failed, "")
            left = 0 if futile else budgets.get(failed, 0)
            if futile:
                print(f"    integration gate fail at step '{failed}', not retried — already red on {self.branch}")
            else:
                print(f"    integration gate fail at step '{failed}' (retries left: {left}): {ids}")
            detail: dict[str, Any] = {"step": failed, "stage": "integration", "retries_left": left}
            if futile:
                detail["futile"] = futile
            self._event("task_failed", [t.id for t in tasks], detail)
            if left <= 0:
                return False, failure_log
            budgets[failed] = left - 1
            self._invoke_integration_fixer(ids, self._integration_fix_prompt(ids, failure_log))

    def _run_integration_agent_step(self, step: GateStep, tasks: Sequence[dag.Task]) -> None:
        """Read the tree the merge produced, which no per-task reviewer ever saw.

        Its `must_fix` findings go back to the integration fixer within this step's own retries,
        the same shape a red command step takes; unresolved ones stop the batch rather than being
        reported as passed. Its `consider` findings are attributed to the merged task whose
        declared scope owns the anchor — the same derivation gate ④ uses to decide which task
        answers a finding (`findings.owner_of_path`) — so they reach the human through
        `brief.residual_findings` beside that task's own review, stamped with the tree they were
        made against. A finding no task's scope owns is printed rather than filed against a task
        that does not own it: gate ④'s seam reading covers exactly that region, and inventing an
        owner here is the guess `findings` refuses to make.
        """
        ids = ",".join(t.id for t in tasks)
        target = dossier.findings_path(self.root, _INTEGRATION_SUBJECT)
        rounds = max(0, step.retries)
        for attempt in range(rounds + 1):
            target.unlink(missing_ok=True)  # a stale file from the previous round is not this answer
            findings_rel = f"{dossier.RELATIVE_PATH}/{target.name}"
            self._launch(
                adapters.command(
                    step.agent_argv or self.config.adapter_argv,
                    build_prompts.integration_review_prompt(
                        ids,
                        gate_cmds=self.config.gate_cmds,
                        diff_cmd=f"git diff {self._plan.base_commit if self._plan else 'HEAD~1'}..HEAD",
                        findings_path=findings_rel,
                        disciplines=adapters.disciplines_for(step.agent_argv or self.config.adapter_argv),
                    ),
                    access=adapters.REVIEW,
                    writable=findings_rel,
                ),
                cwd=self.root,
                where=f"{ids}: the '{step.name}' agent step over the merged tree",
                role=step.agent_role or "code_reviewer",
            )
            if not target.exists():
                raise StopLoop(
                    f"{ids}: the integration reviewer wrote no findings file "
                    f"({dossier.RELATIVE_PATH}/{target.name}). A review that produced nothing "
                    "readable is not a review that found nothing."
                )
            try:
                findings = dossier.parse_findings(target.read_text(encoding="utf-8"))
            except (dossier.FindingsError, OSError) as exc:
                raise StopLoop(f"{ids}: the integration reviewer's findings could not be read — {exc}") from None
            outstanding = dossier.must_fix(findings)
            if not outstanding:
                self._file_integration_findings(findings, tasks)
                if findings:
                    print(f"    [review] {ids}: {len(findings)} finding(s) about the join, none blocking")
                return
            if attempt == rounds:
                raise StopLoop(
                    f"{ids}: the integration reviewer's findings were not resolved within "
                    f"{rounds} round(s):\n{dossier.render_findings(outstanding)}"
                )
            print(f"    [review] {ids}: {len(outstanding)} must-fix finding(s) about the join → back to an implementer")
            self._invoke_integration_fixer(
                ids,
                build_prompts.integration_review_fix_prompt(
                    ids,
                    dossier.render_findings(outstanding),
                    gate_cmds=self.config.gate_cmds,
                    pathspec=self.ws.pathspec,
                ),
            )

    def _file_integration_findings(self, findings: Sequence[Mapping[str, Any]], tasks: Sequence[dag.Task]) -> None:
        """Attribute each non-blocking finding to the merged task whose scope owns its anchor."""
        owners = {t.id: t.scope_include for t in tasks}
        for finding in findings:
            path = str(finding.get("anchor", "")).split(":", 1)[0]
            owner = ""
            if path:
                best = -1
                for task_id, scope in owners.items():
                    covered = common.longest_cover(path, scope)
                    if covered is not None and len(covered.rstrip("/")) > best:
                        owner, best = task_id, len(covered.rstrip("/"))
            if owner:
                self._add_review_findings(owner, [finding])
            else:
                print(f"    [review] a finding about the join no task's scope owns: {finding.get('statement', '')}")

    # -- worktree / merge --

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
        except GateViolationFault as exc:
            return LeafOutcome(ok=False, log=str(exc), violations=exc.violations)
        except StopLoop as exc:
            return LeafOutcome(ok=False, log=str(exc))

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

    def _escalate_gate_violation(self, task_id: str, where: str, violations: list[tuple[str, str]]) -> None:
        listing = "\n".join(f"  {p} — {reason}" for p, reason in violations)
        self._escalate(
            "gate_violation",
            f"{task_id}: {where} touches gate-guarded paths whose prerequisite gate is pending — "
            f"the task is blocked for human review (gate rule 3: never land next-phase edits silently).\n{listing}",
            task=task_id,
        )

    def _block_for_gate_violation(self, task_id: str, where: str, violations: list[tuple[str, str]]) -> None:
        """Block `task_id`: it touched a gate-guarded path while a prerequisite gate is pending.

        Shared by every place that runs this same check — right after an attempt's implementer
        (`_run_task_to_done`) and the pre-existing finalize/merge-time check — so serial and
        parallel each have one call site for "early" and one for "final" rather than four
        separate copies of set-status-and-escalate.
        """
        self._set_status(task_id, "blocked")
        self._escalate_gate_violation(task_id, where, violations)

    def _cleanup_worktree(self, task: dag.Task) -> None:
        self.ws.cleanup_worktree(task.id)

    def _load_landing(self) -> None:
        """Which tasks already have an open pull request, and on which branch their work belongs.

        Read from the audit log, not configured: `pr-stack` records every pull request it opens,
        and that record is what says a task's next commit belongs on a slice branch rather than on
        the work branch. A slice already *ready* is excluded — past gate ④ a change is a human's
        call, not something a re-run lands on quietly. Empty when no stack has been published,
        which is every first build, and why nothing about that path changes.
        """
        from rein import pr_stack

        events, _ = event_chain.scan(self.repo.events)
        self.ws.landing = {r.task_id: r.branch for r in pr_stack.ledger(events) if r.task_id and not r.ready}

    def merge_leaf(self, task: dag.Task, branch: str) -> bool:
        """Merge one leaf into its target branch, classifying a conflict rather than only failing on it.

        A conflict used to abort and block the task, full stop. It still ends that way when the two
        sides genuinely disagree — but "genuinely" is now established rather than assumed: the
        collision goes through `conflict`, which resolves the mechanical kind and escalates the
        rest with the reason recorded. An implementer that reports nothing at all lands on
        `semantic`, which is the same blocked task as before, now with a `knowledge_gap` beside it.
        """
        cwd = self.ws.merge_cwd(task.id)
        if self.dry_run or not cwd:
            with self._merge_checkout(task) as scratch:
                return self._merge_into(task, branch, scratch)
        return self._merge_into(task, branch, cwd)

    @contextlib.contextmanager
    def _merge_checkout(self, task: dag.Task) -> Iterator[str]:
        """A checkout holding this task's target branch, made only when no worktree already has it."""
        if self.dry_run:
            yield self.root
            return
        with build_git.scratch_worktree(
            self.repo, self.config.worktree_dir, "_merge", self.ws.target_branch(task.id), _late_run
        ) as path:
            yield path

    def _merge_into(self, task: dag.Task, branch: str, cwd: str) -> bool:
        if self.dry_run:
            return self.ws.merge_leaf(task.id, branch, cwd)
        if self.ws.merge_leaf(task.id, branch, cwd):
            return True
        resolution = conflict.merge_with_resolution(
            self._plan or models.Plan({}),
            cwd=cwd,
            source_ref=branch,
            ours_task=self._landing_owner(task.id),
            theirs_task=task.id,
            implement=self.resolve_conflict,
            quality_gate=self.task_gate,
            run=_late_run,
        )
        if resolution.merged:
            print(f"    [merge] {task.id}: conflict resolved ({resolution.kind})")
            self.ws.git(["worktree", "remove", "--force", self.ws.worktree_path(task.id)])
            return True
        conflict.escalate(self.repo, resolution)
        self._escalate("merge_conflict", f"{task.id}: {resolution.escalation}", task=task.id)
        return False

    def _landing_owner(self, task_id: str) -> str:
        """The task whose branch this one lands on — itself when a pull request already holds it."""
        return task_id if self.ws.landing.get(task_id) else ""

    def _landed(self, task_id: str) -> str:
        """The commit the caller just created on this task's target branch, as `completed_commit`.

        Read at the moment the task's commit becomes that branch's tip — right after the serial
        finalize, or right after that one leaf's merge — never once at the end of a batch. A
        batch's leaves merge one after another, so a hash read after all of them names the last
        merge for every member of the batch, and an integration gate that commits a fix moves it
        further still.
        """
        return "" if self.dry_run else self.ws.landed(task_id)

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
        judge the work, it asks for a re-run. A `knowledge_gap` here would leave a permanent
        "unresolved escalation" on gate ⑤'s screen for a machine's bad afternoon, in a log that
        is append-only by design.
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
        if problems := self._source_problems() + self._tree_problems():
            for problem in problems:
                logger.error(problem)
            return common.EXIT_CANNOT_PROCEED
        if not self.dry_run and (blockers := self._preflight()):
            logger.error(
                "refusing to start: this run cannot finish in this environment, and every reason "
                "below was knowable before an implementer was launched.\n"
                + "\n".join(f"  - {b.render()}" for b in blockers)
            )
            return common.EXIT_CANNOT_PROCEED
        self._load_landing()
        if self.dry_run:
            return self._run_loop()  # read-only: no lock either, and no contention to guard against
        #: How this run ended, for the measurement below. It starts at the pessimistic value so
        #: that a raise anywhere is recorded as what it was rather than as nothing.
        outcome = "failed"
        try:
            # Lock order is build.lock -> store.lock, always; the control plane takes the store
            # lock per request inside it. The socket lives for exactly this run: a leaf that
            # outlives the orchestrator has nothing to talk to, which is the correct answer.
            with build_lock(self.repo), control_plane.serving(self.repo) as server:
                self.control = server
                try:
                    rc = self._run_loop()
                    outcome = _RUN_OUTCOME.get(rc, "failed")
                    return rc
                finally:
                    # In the `finally` because a run that stopped for capacity established real
                    # facts before it stopped, and making the next attempt re-establish them is
                    # exactly the waste the ledger exists to end.
                    self.ledger.flush()
                    self._record_spend(outcome)
                    for summary in (self.ledger.summary(), self.spend_summary()):
                        if summary:
                            print(f"[{summary}]")
        except store_mod.LockUnavailableError as exc:
            # Retry-later, not cannot-proceed: nothing here is broken, another run simply has the
            # repository. A supervisor restarting `rein build` races the previous process's
            # shutdown often enough that reading this as fatal would stop the loop for good.
            logger.error(f"another build run holds the lock: {exc}")
            return common.EXIT_RETRY_LATER

    def _preflight(self) -> list[preflight.Problem]:
        """Why this run cannot finish, found before the first launch (`rein.preflight`).

        Every launch this run makes goes through one of the role adapters below, and every gate
        step through one of the profiles; those are what get checked, and nothing else. Skipped in
        a dry run, which launches nothing and enters no sandbox — its job is to print the control
        flow, and refusing to do that because an image is unbuilt would withhold the one answer a
        dry run exists to give.
        """
        roles = {"implementer": self.config.adapter_argv}
        for step in self.config.steps:
            if step.kind == "agent" and step.agent_argv:
                roles[step.agent_role or "code_reviewer"] = step.agent_argv
        return preflight.check(
            self.config.raw, self.config.raw.quality_gate, roles, runtime=executors.container_runtime()
        )

    def _record_spend(self, outcome: str) -> None:
        """Append this run's measurement to the audit chain (`run_record`). Never raises.

        `outcome` is how the run ended, in exit-code terms. It was missing here and present in the
        review pipeline's copy of this event, which is one of the ways the two shapes had drifted
        apart; the words differ because the two runs end differently, and `kind` is what tells a
        reader which vocabulary it is reading.
        """
        run_record.record(
            self.store,
            kind="build",
            cycle=self.cycle_id,
            run_id=self.run_id,
            outcome=outcome,
            by_role=self.spend_totals(),
            billed=self.usage_totals(),
        )

    def _source_problems(self) -> list[str]:
        """Why the prose this build would read is not the prose gate ③ approved.

        A ticket edited after the freeze changes what gets built, and `plan.yaml` being
        digest-frozen said nothing about it. Refusing here is the same posture `rein guard`
        already takes towards a frozen document — the way forward is `rein revise --to tasks`
        and a re-approval, not an edit nobody recorded.

        A source that is merely *uncommitted* is caught earlier and more widely by
        :meth:`_tree_problems`, which refuses any uncommitted change at all.

        Empty when the plan was frozen before this release recorded sources, so an in-flight
        repository upgrading mid-cycle is not stopped by a check it has no data for.
        """
        if self.state is None:
            return []
        pinned = self.state.frozen_sources
        if not pinned:
            return []
        moved = [path for path, frozen in sorted(pinned.items()) if self._live_digest(path) != frozen]
        if moved:
            return [
                f"{len(moved)} document(s) the build reads changed since gate 3 froze them: "
                f"{', '.join(moved)}. Building now would implement text nobody approved — "
                "roll back with `rein revise --to tasks`, re-approve, and run again."
            ]
        return []

    def _tree_problems(self) -> list[str]:
        """Why this run cannot tell its own work from what was already in the tree.

        **A serial task is only isolated from the working tree if the working tree is a commit.**
        A parallel leaf gets that by construction — `git worktree add` hands it a clean checkout of
        the branch, so everything it finds there afterwards is its own. A serial task runs in the
        repository root, where its change is derived as "the commits since the pre-task HEAD, plus
        the dirty tree" (:meth:`build_git.Workspace.changed_since`). That derivation is exact when
        the tree starts clean and silently wrong when it does not: an edit that was already sitting
        there is indistinguishable from one the implementer just made, and every reading built on
        top of it inherits the confusion —

          * it counts against the task's declared `scope`, so unrelated work in the tree blocks a
            task that never touched it;
          * it fills the empty-diff check, so an implementer that wrote *nothing* looks productive
            — the exact failure `no_implementation` exists to catch, defeated from the other side;
          * it reaches the reviewer as part of the change under review;
          * and `finalize_commit`'s `git add -A` lands it inside `T-NNN: <title>`, so the commit the
            gate ④ record names contains work no task claimed. That one does not wash out on the
            next run — it is in the history.

        So the tree is a precondition, not something to compensate for afterwards. Subtracting a
        recorded baseline was the alternative and it cannot fix the last item without scoping the
        finalize commit to a path list, which merely hands the same unowned diff to the next task.

        `.rein/` is excluded, as it is everywhere else: orchestration state is not any task's work.
        """
        if self.dry_run:
            return []
        dirty = self.ws.dirty_paths(self.root)
        if not dirty:
            return []
        shown = ", ".join(dirty[:_DIRTY_PATHS_SHOWN])
        if len(dirty) > _DIRTY_PATHS_SHOWN:
            shown += f", and {len(dirty) - _DIRTY_PATHS_SHOWN} more"
        lines = [
            f"{len(dirty)} uncommitted change(s) in the working tree: {shown}. "
            "A serial task's change is measured against the commit it started from, so anything "
            f"already uncommitted is attributed to the first task this run touches — it counts "
            f"against that task's scope and lands inside its commit. Commit them on `{self.branch}` "
            "or stash them, then run again."
        ]
        interrupted = sorted(
            task_id
            for task_id, status in (self.state.task_status if self.state else {}).items()
            if status == "in-progress"
        )
        if interrupted:
            lines.append(
                f"{', '.join(interrupted)} is still 'in-progress' from a run that did not finish. "
                "If these paths are its work, commit them as `T-NNN: <its title>` so the task keeps "
                "the commit that completed it."
            )
        return lines

    def _live_digest(self, path: str) -> str:
        candidate = self.repo.path(path)
        return digests.of_file(candidate) if candidate.is_file() else ""

    def _promote_observed(self, graph: dag.Graph) -> bool:
        """Finish any task whose outstanding observation a human has since recorded.

        Without this the only way forward from `awaiting-evidence` would be `rein task reset`,
        which sends the task back to the frontier and re-runs an implementer over code that is
        already merged and already passed — paying a model to redo work whose only missing piece
        was a person looking at a screen. Here it is a status flip: the DoD record is already in
        `state.yaml`, and the observation names the tree it was made against, so all that is left
        is to check the tree has not moved since.
        """
        promoted = False
        for task in graph.tasks:
            if task.status != "awaiting-evidence" or self._unestablished_acceptance(task):
                continue
            print(f"  [evidence] {task.id}: the outstanding observation has been recorded — finishing")
            self._set_status(task.id, "done", commit=self._landed(task.id))
            promoted = True
        return promoted

    def _establish_baseline(self) -> None:
        """Which DoD steps are already red on the tree no task has touched yet.

        The reported case: a run's tasks stopped, one after another, on a `check` that a
        dependency drift had broken weeks earlier. Each one spent its whole send-back budget —
        three implementer launches — on a failure it had not caused and could not have fixed
        within its own scope, and the audit chain recorded three `task_failed` verdicts about the
        code each of them wrote.

        The loop could not tell those apart from a real regression because it had never asked the
        one question that separates them: *was this step red before the task touched anything?*
        Asked once here, against the work branch's tip, and answered by the same runner and the
        same evidence ledger the gate itself uses — so on an unmoved tree it is a cache hit and
        costs nothing.

        Taken once, and only when a batch is actually about to run: a `rein build` that finds every
        task done goes straight to gate ④, and paying a full gate run there answers nothing.

        Recording, not refusing. A cycle whose first task is "fix the failing tests" is a
        legitimate thing to start, and the implementer runs *before* the gate: if it fixed the
        step, the step goes green and none of this applies. What changes is only what happens when
        it is still red — `_run_task_to_done` stops rather than buying the same failure three more
        times.
        """
        if self.dry_run or self._baseline_taken:
            return
        self._baseline_taken = True
        steps = [s for s in self._steps_at("task") if s.kind == "command" and s.command]
        for step in steps:
            try:
                failure = self._run_cmd_step(step, self.root)
            except EnvironmentFault:
                # Not a fact about the code either way, and the run is about to hit it again for
                # real. Leave it to the batch, which knows how to abort without marking anything.
                return
            if failure:
                self._baseline_red[step.name] = failure
        if self._baseline_red:
            print(
                f"    [baseline] already red on the work branch before any task ran: "
                f"{', '.join(sorted(self._baseline_red))}"
            )

    def _run_loop(self) -> int:
        self._recover_in_progress()
        while True:
            graph = self._load_graph()
            if self._promote_observed(graph):
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
            # Here rather than at the top of the run: a `rein build` that finds every task done goes
            # straight to gate ④, and a full gate run at the root to answer a question no task is
            # going to ask is exactly the waste this exists to end.
            self._establish_baseline()
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
            except GateViolationFault as exc:
                # Caught right after this attempt's implementer ran, rather than only at the
                # finalize check below — a task that never gets that far (blocked on a later
                # content failure) must not carry an undetected violation until `doctor` is run
                # by hand.
                self._block_for_gate_violation(
                    task.id, "its work-branch changes (caught before finalize)", exc.violations
                )
                raise StopLoop(
                    f"{task.id}: changed gate-guarded paths while their gate is pending. Human intervention needed.",
                    code=1,
                ) from exc
            if not ok:
                status, owed = self._stop_verdict(task.id)
                self._set_status(task.id, status)
                if owed:
                    self._escalate(
                        "blocked",
                        f"{task.id}: could not pass the quality gate within the limit; blocked.\n{log}",
                        task=task.id,
                    )
                raise StopLoop(f"{task.id} is {status}. Human intervention needed.", code=1)
            # A serial task lands directly on the work branch (its own commits plus the finalize
            # below), where --no-verify and already-in-HEAD commits both escape the commit-stage
            # guard — so re-check everything the task changed before accepting it as done.
            if not self.dry_run and pre_head:
                violations = self._gate_violations(self.ws.changed_since(pre_head))
                if violations:
                    self._block_for_gate_violation(task.id, "its work-branch changes", violations)
                    raise StopLoop(
                        f"{task.id}: changed gate-guarded paths while their gate is pending "
                        f"(commits since {pre_head[:12]} stay on the branch for review). "
                        "Human intervention needed.",
                        code=1,
                    )
            # Finalize the task diff only. The .rein/ orchestration state (tasks.yaml status, etc.)
            # is not included in the per-task commit (keeping one commit = one task). If the
            # implementer has not committed, this finalizes the diff (no-op otherwise).
            if not self.ws.finalize_commit(self.root, f"{task.id}: {task.title}"):
                # The tree on the work branch keeps the diff, but the task must not be marked done
                # without its commit (one commit = one task is the record gate ④ reviews).
                self._set_status(task.id, "blocked")
                raise StopLoop(f"{task.id}: finalize commit failed on the work branch. Human intervention needed.")
            self._set_status(task.id, self._completion_status(task), commit=self._landed(task.id))
            self._warm_reading(task)

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
        branches = {
            task.id: self.ws.add_worktree(task.id, str(self._handoff_for(task).get("salvage_branch", "")))
            for task in tasks
        }
        results: dict[str, LeafOutcome] = {}
        with ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel)) as pool:
            futures = {pool.submit(self._safe_run_task, t, self.ws.worktree_path(t.id)): t for t in tasks}
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
            if outcome.violations:
                self._block_for_gate_violation(
                    task.id, "its worktree changes (caught before merge)", outcome.violations
                )
                self._cleanup_worktree(task)  # not merged; the branch keeps the diff for review
                blocked_any = True
                continue
            if not ok:
                status, owed = self._stop_verdict(task.id)
                self._set_status(task.id, status)
                if owed:
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
            if not self.ws.finalize_commit(self.ws.worktree_path(task.id), f"{task.id}: {task.title}"):
                # Keep the worktree (it may hold the only copy) and let the rest of the batch merge.
                self._set_status(task.id, "blocked")
                blocked_any = True
                continue
            # The leaf's commits were made in its worktree, where --no-verify (finalize) or a
            # bypassed hook can carry a gate violation; merging would bury it in the work branch's
            # HEAD where --check-diff never looks again. Check the branch's full diff first.
            if not self.dry_run:
                violations = self._gate_violations(self.ws.branch_changed_paths(task.id))
                if violations:
                    self._block_for_gate_violation(task.id, f"leaf branch {branches[task.id]}", violations)
                    self._cleanup_worktree(task)  # not merged; the branch keeps the diff for review
                    blocked_any = True
                    continue
            if self.merge_leaf(task, branches[task.id]):
                merged.append(task)  # done is decided after the integration gate below
                landed[task.id] = self._landed(task.id)  # this leaf's merge commit, before the next one
            else:
                self._set_status(task.id, "blocked")
                self._cleanup_worktree(task)  # conflict: aborted merge, worktree no longer needed
                blocked_any = True
        # Integration gate: a join of 2+ leaves creates a combined tree nobody has verified (a
        # single-leaf join is byte-identical to that leaf's already-gated worktree state). Not a
        # knob: each leaf was green only in isolation, so a batch that merged two or more of them
        # has never been verified as one tree until now. A `stage: integration` step is the other
        # reason to run it — those never ran per task, so even a single leaf has to face them.
        #
        # Only the leaves that landed on the *work branch* are in that join. A task whose pull
        # request is open landed on its own slice branch, so the combined tree does not exist here
        # yet — `rein pr-stack --restack` is what brings them together, and it runs this same gate
        # once it has. Gating on a tree that is not the one under test would be the worse error.
        elsewhere = [t for t in merged if self.ws.landing.get(t.id)]
        if elsewhere:
            print(
                f"    [merge] {', '.join(t.id for t in elsewhere)} landed on their pull-request branches; "
                "`rein pr-stack --restack` joins them into the work branch"
            )
        merged = [t for t in merged if not self.ws.landing.get(t.id)]
        if merged and (len(merged) >= 2 or self._steps_at("integration") != self._steps_at("task")):
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
                self._set_status(task.id, self._completion_status(task), commit=landed.get(task.id, ""))
                self._warm_reading(task)
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
        if merged:
            self._warn_on_review_outlook()
        if blocked_any:
            # A real verdict outranks a machine fault when both happened: re-running clears the
            # fault but never the blocked task, so the human has to look either way. The fault is
            # still recorded — it explains a leaf that came back `todo` with nothing said about it.
            if fault is not None:
                self._record_abort(fault)
            raise StopLoop("A blocked task occurred. Human intervention needed.", code=common.EXIT_HUMAN_NEEDED)
        if fault is not None:
            raise fault

    def _warn_on_review_outlook(self) -> None:
        """Say it at task 9 of 17, not at gate ④, when the change outgrows what a review can read.

        The loop already knows the diff after each task lands, and it said nothing: a cycle that
        crossed its own `max_diff_bytes` and a cycle carrying a committed binary both went the
        whole way to gate ④ before anyone heard, and at gate ④ the budget's instruction ("split the
        scope") is not a move that exists — every task is merged and `done`.

        A warning, never a stop. A task that passed its gate has earned its merge; what this
        changes is who knows what, and when.
        """
        if self.dry_run:
            return
        from rein import review as review_mod

        view = review_mod.outlook(self.repo)
        if view is None:
            return
        if view.over_budget:
            print(
                f"    [outlook] {view.line()}\n"
                "              gate ④ refuses a change over the budget, and its answer is to split "
                "the scope — which stops being possible once every task is merged. Now is when."
                + (f"\n              {view.made_of()}" if view.made_of() else "")
            )
        if view.unreadable:
            print(
                f"    [outlook] {len(view.unreadable)} binary/unsupported file(s) in this change make "
                f"coverage `insufficient`"
                + (f", which blocks gate ④ at {view.effective_risk} risk" if view.coverage_blocks_gate else "")
                + f": {', '.join(view.unreadable[:5])}"
            )

    # -- handing over to the review pipeline -----------------------------------

    def _present_gate4(self, graph: dag.Graph) -> int:
        """All tasks done. Say what still has to happen — and what has NOT been established.

        (There is no "you left a step empty" nudge here any more: the config schema requires a
        `command` for every command step, so an empty one cannot reach this code. The scaffold
        ships a placeholder `["true"]` instead, which `doctor.check_quality_gate` reports and a
        silent skip cannot.)

        This deliberately does not invite an approval. Green tests plus an AI's summary is not
        evidence that the code does what the plan says: gate ④ approves a grounded review — a
        blind extraction of actual behaviour, compared against the frozen plan, with a coverage
        manifest saying what could not be analysed — and this loop has produced none of that.
        """
        print("\n========== all tasks done ==========")
        print(dag.render(graph))
        for measured in (self.ledger.summary(), self.spend_summary()):
            if measured:
                print(measured)

        print(
            "\nWhat this run established: every task's code passed the configured quality gate.\n"
            "What it did NOT establish: that the code does what the plan claims.\n"
            "\nNext:\n"
            "  1. rein review generate   — coverage manifest, blind actual extraction,\n"
            "                                   conformance comparison, security and maintainability review\n"
            "  2. rein ui                — read the scope and the orient brief, then answer the\n"
            "                                   Decision Cards and freeze the review\n"
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
    parser.add_argument(
        "--supervise",
        action="store_true",
        help=(
            "on EXIT_RETRY_LATER (3), sleep and re-run in this same process instead of exiting — "
            "the documented while-loop recipe, built in. Returns as soon as a run returns "
            "anything other than 3."
        ),
    )
    parser.add_argument(
        "--supervise-interval-sec",
        type=int,
        default=900,
        help="seconds to sleep between retries under --supervise (default: 900, the documented recipe's interval)",
    )
    args = parser.parse_args(argv)
    common.configure_logging()
    if args.supervise and args.dry_run:
        logger.error("--supervise and --dry-run are mutually exclusive — a supervised run has to call the real loop")
        return 2
    if args.supervise and args.supervise_interval_sec < 1:
        logger.error("--supervise-interval-sec must be at least 1")
        return 2
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
    except (OSError, ValueError, adapters.LaunchRefused, models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(f"cannot load .rein/config.yaml: {exc} — `rein doctor` validates it")
        return 1
    if not args.supervise:
        return Orchestrator(config, dry_run=args.dry_run, repo=repo).run()
    return _supervise(config, repo, args.supervise_interval_sec)


def _supervise(config: Config, repo: repo_mod.Repo, interval_sec: int) -> int:
    """Re-run the build loop on `EXIT_RETRY_LATER` until it returns anything else.

    Formalizes the while-loop recipe `build.md` has documented since 0.2.2 — same semantics
    (only `EXIT_RETRY_LATER` is retried; 0/1/2 return immediately), carried inside one
    long-lived process instead of a hand-written shell wrapper someone has to remember to start
    again every time it or its parent session dies. Each iteration is a fresh `Orchestrator`,
    so it sees `state.yaml` as it stands and takes/releases the build lock exactly as a
    standalone `rein build` would — nothing here changes what one run does, only whether
    something is still watching after it returns 3.
    """
    attempt = 0
    while True:
        attempt += 1
        rc = Orchestrator(config, dry_run=False, repo=repo).run()
        if rc != common.EXIT_RETRY_LATER:
            return rc
        logger.info(f"[supervise] attempt {attempt}: capacity/lock retry — sleeping {interval_sec}s")
        time.sleep(interval_sec)


if __name__ == "__main__":
    raise SystemExit(main())


@contextlib.contextmanager
def resolving(repo: repo_mod.Repo) -> Iterator[Orchestrator]:
    """An orchestrator with a live control plane, for a caller that only needs conflict resolution.

    The build lock then the control socket, the same order and for the same reasons `run()` takes
    them: nothing else may be driving the repository while an implementer is writing in it, and a
    leaf that cannot reach the control plane cannot report an outcome at all — which would make
    every conflict look semantic.
    """
    orchestrator = Orchestrator(Config.load(repo), dry_run=False, repo=repo)
    with build_lock(repo), control_plane.serving(repo) as server:
        orchestrator.control = server
        yield orchestrator
