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
chain that acceptance counts as unresolved escalations forever.

**The loop produces no acceptance-gate evidence.** The acceptance gate approves a *grounded review* — a blind
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
import copy
import fnmatch
import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
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
    lenses,
    models,
    observations,
    pr_stack,
    preflight,
    review_cache,
    review_policy,
    review_reading,
    review_transport,
    reviews_cmd,
    run_record,
    sessions,
    status_api,
    strict_yaml,
)
from rein import (
    findings as findings_mod,
)
from rein import junit as junit_mod
from rein import (
    repair as repair_mod,
)
from rein import repo as repo_mod
from rein import store as store_mod
from rein import usage as usage_mod

logger = logging.getLogger(__name__)

#: Where an acceptance repair stands when this cycle ships as a stack: a throwaway worktree on the slice
#: branch that introduced the code. Its own name rather than `pr_stack.RESTACK_WORKTREE`, because
#: the propagation that follows creates that one and git refuses the same path twice.
_GATE4_WORKTREE = "_gate4"

#: What `_verify_repair` refuses to land: a repair with no test, and one whose tests pass without it.
_REPAIR_REFUSED = frozenset({"untested", "inert"})

#: What the integration reviewer's findings file is named after. Not a task id — the subject is the
#: join of a batch — and `dossier.findings_path` only needs a stable name to write beside.
_INTEGRATION_SUBJECT = "integration"
#: The statuses a task's work is on the work branch in: what a reading waits for all of its tasks to reach.
_LANDED_STATUSES = frozenset({"done", "awaiting-evidence"})
#: The findings file a reviewer writes, at the repository root, is named after what it reads
#: (`_read_batch`): tasks that operate are read from their own threads, several at once.
_BATCH_REVIEW_SUBJECT = "review.{ids}"
#: The verdict an agent step reaches about a task that `operate`s, in the task's own send-back
#: channel: `review:<step name>`.
_REVIEW_PREFIX = "review:"

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

#: How many failing test ids a record carries. The report can hold thousands; the record is an
#: index into it, not a copy.
_NODES_SHOWN = 50

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


#: How many uncommitted paths the clean-tree refusal names before summarizing the rest.
#: Enough to recognize what is in the way; a full listing of a tree nobody committed is
#: not more informative than its first screen.
_DIRTY_PATHS_SHOWN = 20


#: Where a sandboxed gate step sees the tree it is testing. One constant, so the mount and the
#: working directory cannot disagree about where the repository is.
_SANDBOX_WORKDIR = "/work"

#: Where the control socket is bound inside an agent sandbox. A fixed path rather than the host's,
#: because the host's lives under `/run/user/<uid>` and a container that mounted *that* would be
#: handed every other socket in it.
_SANDBOX_CONTROL_SOCKET = "/run/rein/control.sock"

#: The verdict name a failed `operate` step comes back under, with the step's name after it.
_OPERATE_PREFIX = "operate:"

#: Names the leaf environment carries from the plan's `environment.env`, so a contained launch
#: passes exactly those through — they were approved with the mandate, the operator's shell was not.
DECLARED_ENV = "REIN_DECLARED_ENV"

_ENV_REF = re.compile(r"\$(?:\{(\w+)\}|(\w+))")


def task_environment(task: dag.Task) -> tuple[dict[str, str], list[str]]:
    """`task.env` with `~`, `$NAME` and `${NAME}` expanded from this process's environment.

    Returns the expanded variables and the names whose value referred to a variable nothing sets.
    Those are reported as unmet preconditions, never expanded to an empty string: a `PYTHONPATH`
    that silently became "" is the failure this block exists to remove, one layer further down.
    """
    expanded: dict[str, str] = {}
    unresolved: list[str] = []
    for name, value in task.env:
        if any((a or b) not in os.environ for a, b in _ENV_REF.findall(value)):
            unresolved.append(name)
            continue
        expanded[name] = os.path.expandvars(os.path.expanduser(value))
    return expanded, unresolved


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
    #: task, unconditionally — frozen by the mandate alongside the rest of config.yaml, never a knob
    #: a task's own ticket sets (`models.GateStep.matches_paths`).
    paths: tuple[str, ...] = ()
    #: Where this step runs: `task`, `integration`, or `both`. Never "whether" — every configured
    #: step still runs; this is how often the same confidence gets bought.
    stage: str = "both"
    #: What an agent step reads for (`reviews.yaml`): packaged review names, and custom ones whose
    #: question is in `Config.questions`.
    reviews: tuple[str, ...] = ()
    #: This step runs the tests — the only kind of step the negative control re-establishes.
    runs_tests: bool = False
    #: Where the step writes a JUnit XML report, relative to its checkout ("": it does not). What
    #: lets a red be read per failing test instead of per step (`Orchestrator._attribute_red`).
    junit: str = ""

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
    worktree_dir: str
    branch_pattern: str
    steps: tuple[GateStep, ...]
    branch: str
    timeout_cmd: float | None
    timeout_agent: float | None
    adapter_argv: tuple[str, ...]
    launch_retries: int
    #: What the grounded review reads before acceptance (`reviews.yaml`).
    readings: models.Readings
    #: Dollars this cycle may spend before the loop stops and hands back. 0.0 = no ceiling.
    max_cost_usd: float = 0.0
    #: The question each custom review asks (`reviews.yaml`).
    questions: Mapping[str, str] = field(default_factory=dict)

    @property
    def gate_cmds(self) -> list[str]:
        """The deterministic commands of the gate, for prompts and display."""
        return [s.display for s in self.steps if s.kind == "command" and s.command]

    @classmethod
    def from_models(cls, config: models.Config, reviews: models.Reviews) -> Config:
        """The knobs, with every role the run will launch resolved up front.

        Resolving here rather than at the first step that needs it is what makes an unlaunchable
        adapter stop the build before an implementer has been paid for, instead of halfway
        through a task.

        The command steps come from `config.yaml` and are frozen with the mandate; the reviewer
        steps come from `reviews.yaml`, which is not (`models.Reviews`). Both run as one DoD.
        """
        commands = tuple(
            GateStep(
                name=step.name,
                kind="command",
                command=step.command,
                retries=max(0, step.retries),
                required=step.required,
                executor_profile=step.executor_profile,
                paths=step.paths,
                stage=step.stage,
                runs_tests=step.runs_tests,
                junit=step.junit,
            )
            for step in config.quality_gate
        )
        readers = tuple(
            GateStep(
                name=step.name,
                kind="agent",
                retries=max(0, step.retries),
                required=True,
                agent_role="code_reviewer",
                agent_argv=adapters.launch_argv(config, "code_reviewer"),
                paths=step.paths,
                stage=step.stage,
                reviews=step.reviews,
            )
            for step in reviews.steps
        )
        steps = commands + readers
        argv = adapters.launch_argv(config, "implementer")
        return cls(
            raw=config,
            max_parallel=max(1, config.max_parallel),
            worktree_dir=config.worktree_dir,
            # `-` (not `/`) between branch and task: git forbids a branch that is a path-prefix of
            # another ref ("work" + "work/T-001" cannot coexist), so a slash pattern always fails.
            branch_pattern="{branch}-{task_id}",
            steps=steps,
            branch=config.work_branch,
            timeout_cmd=float(config.command_timeout_sec) or None,
            timeout_agent=float(config.agent_timeout_sec) or None,
            adapter_argv=argv,
            max_cost_usd=config.max_cost_usd,
            launch_retries=max(0, config.launch_retries),
            readings=reviews.readings,
            questions=reviews.questions,
        )

    @classmethod
    def load(cls, repo: repo_mod.Repo) -> Config:
        store = store_mod.Store(repo)
        config = store.read_config()
        if config is None:
            raise ValueError(f"no {repo.config} — run `rein init` first")
        # The file as it sits on disk only when it is what the chain records: a reviewer step
        # removed by a shell write would otherwise be a build that reads nobody's work.
        try:
            reviews = reviews_cmd.require_bound(repo)
        except reviews_cmd.ReviewsError as exc:
            raise ValueError(str(exc)) from None
        return cls.from_models(config, reviews)


def render_owed(graph: dag.Graph, owed: Mapping[str, Sequence[str]]) -> str:
    """One block per task: its id and title, then each thing a person owes it."""
    return "\n".join(
        f"  {task_id}  {graph.get(task_id).title}\n" + "\n".join(f"      - {line}" for line in lines)
        for task_id, lines in owed.items()
    )


def owed_by_people(repo: repo_mod.Repo) -> str:
    """Everything a person owes the plan as it stands, rendered; "" when nothing is owed.

    For `rein approve mandate`, the other moment the whole list is worth asking for: the plan has
    just frozen, and a person can prepare everything in one sitting before the first launch.
    """
    loop = Orchestrator(Config.load(repo), dry_run=False, repo=repo)
    graph = dag.load(repo)
    owed = loop.owed_everywhere(graph)
    return render_owed(graph, owed) if owed else ""


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
            repo,
            task_id,
            status,
            note=note,
            commit=commit,
            evidence=evidence or {},
            handoff=handoff or {},
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
            merged["handoff"] = {**_merge_handoff(entry.get("handoff"), handoff), "updated_at": event_chain.now_iso()}
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

#: The handoff fields that describe *one* failure: which step, what it said, why no further
#: round was spent, and the verdict an attempt stopped on before the gate. They are written as a
#: unit — a new failure replaces all of them, and a green gate clears all of them. Merged field by
#: field, a join that went red after its tasks had needed a retry kept the retry's `failed_step`
#: beside the join's log, and the next attempt was told its own `test` step had failed with the
#: integration gate's output. `retries_left` is not among them: it is the budget, which spans
#: failures by design.
_FAILURE_FIELDS = ("failed_step", "failure_summary", "futile", "escalation")

#: What a green gate carries to the next status write: the failure the handoff described is over.
_FAILURE_RESOLVED: dict[str, Any] = dict.fromkeys(_FAILURE_FIELDS)


def _merge_handoff(previous: object, patch: Mapping[str, Any]) -> dict[str, Any]:
    """`previous` with `patch` applied; a patch that touches a failure field replaces them all.

    A `None` in the patch removes that field — how `_FAILURE_RESOLVED` clears a failure.
    """
    handoff = dict(previous) if isinstance(previous, Mapping) else {}
    if any(key in patch for key in _FAILURE_FIELDS):
        for key in _FAILURE_FIELDS:
            handoff.pop(key, None)
    handoff.update({key: value for key, value in patch.items() if value is not None})
    return handoff


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
    handoff = _merge_handoff(entry.get("handoff"), patch)
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
    paths: Sequence[str] = (),
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
    if paths:
        # A scope violation's paths decide what moves the task next (`status_api.scope_additions_for`).
        escalation["paths"] = list(paths)
    patch: dict[str, Any] = {"escalation": escalation}
    detail: dict[str, Any] = {"kind": kind, "message": message}
    if futile:
        patch["futile"] = futile[:_HANDOFF_SUMMARY_MAX]
        detail["futile"] = futile
    update_task_handoff(repo, task_id, patch, event="knowledge_gap", detail=detail)


def record_premise(
    repo: repo_mod.Repo, premise_id: str, *, held: bool, output: str, fallback: bool, park: Sequence[str]
) -> None:
    """Record what a premise's probe observed, and park the tasks a falsified one leaves unfinishable.

    One transaction: the observation, the tasks it sends back (only when there is no approved
    fallback, and only the ones whose criteria rest on it), and the event. A falsified premise
    with a fallback parks nothing — `dag.join` swaps the criteria and the work goes on.
    """
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise StopLoop("no .rein/state.yaml to record a premise in")
    seen = store_mod.read_digest(state)
    raw = json.loads(json.dumps(state.raw))
    raw.setdefault("premises", {})[premise_id] = {
        "status": "held" if held else "falsified",
        "observed_at": event_chain.now_iso(),
        "output": output[-500:],
    }
    tasks = raw.setdefault("tasks", {})
    for task_id in park:
        entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {}
        tasks[task_id] = {**entry, "status": "needs-revision"}
    raw["updated_at"] = event_chain.now_iso()
    detail: dict[str, Any] = {"kind": "premise_held" if held else "premise_falsified", "premise": premise_id}
    if not held:
        detail["fallback"] = fallback
    with store.transaction() as tx:
        tx.write("state", raw, expect_digest=seen)
        tx.append("decision_declared", cycle_id=state.cycle_id, subject_ids=[premise_id, *park], detail=detail)


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
    """An attempt changed a path the gate guard refuses to let a task land.

    Raised as soon as `_run_task_to_done` sees it — right after the implementer runs, inside the
    retry loop — rather than waiting for the finalize/merge-stage check that already existed to
    be the only thing that ever looked. A worktree that never reaches merge (blocked on a later
    content failure, or the run stopped by an environment fault first) used to carry the
    violation undetected until someone ran `rein doctor` by hand.
    """

    def __init__(self, violations: list[tuple[str, str]]) -> None:
        self.violations = violations
        super().__init__(f"{len(violations)} path(s) the gate guard refuses were changed")


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


@dataclass(frozen=True)
class ReviewAnswer:
    """What one reading said about one task: at most one of the three is set."""

    #: `must_fix` findings (or a tree that moved under the reader) for its implementer.
    send_back: str = ""
    #: Why the task cannot land on this reading: no entry for it, or an answer nobody can read.
    stop: str = ""
    #: The reader never ran: no verdict about the task at all.
    fault: EnvironmentFault | None = None


def _landable(outcome: LeafOutcome) -> bool:
    """A leaf whose gate passed and that nothing else has stopped: the only kind a reviewer reads."""
    return outcome.ok and outcome.fault is None and not outcome.violations


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
        # The frozen Expected Model. Read once: it cannot change during a run (the mandate froze it,
        # and `rein guard` denies a write while it is frozen), and every dossier needs it.
        self._plan = self.store.read_plan()
        # Resolved once per run from what the mandate froze, never from the library on this
        # machine. `rein lens --select` wrote it into the plan while the plan was still a draft;
        # re-deriving it here would make a reviewer's inputs a function of a user-global file that
        # anybody may have edited since the gate, with nothing in the chain to show it.
        self._code_lenses = self._frozen_code_lenses()
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
        # Which implementer session finished which task, so the one task downstream of it can
        # start from there (`_inherited_session`). A cache like the ledger: a miss is a cold start.
        self.sessions = sessions.Sessions.for_repo(self.repo, enabled=not dry_run)
        self._sessions_said = False
        #: Set once an acceptance warm-up could not be taken. Retrying it per task would spend a session
        #: limit on an optimization, and the gate takes the reading either way (`_warm_reading`).
        self._warming_off = False
        #: Why each task's last repair was refused (`_accept_repair`), handed to its next one.
        self._repair_refusals: dict[str, str] = {}
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
        #: What earlier runs of this cycle already spent, read from the chain once on first ask.
        #: `None` is "not read yet"; a cycle with no recorded run reads as a zero `Spend`.
        self._spend_before: usage_mod.Spend | None = None
        # Tasks whose attempt ended before the quality gate, and the status that ending calls for.
        # The caller reads this instead of assuming every unsuccessful attempt is `blocked`: an
        # implementer that found the *design* wrong has said `needs-revision`, and overwriting
        # that with `blocked` would file a defect in the plan as a defect in the code.
        self._stops: dict[str, str] = {}
        # DoD steps already red on the work branch before any task ran, and what they said. A task
        # that fails one of these is not sent back to an implementer: `_load_baseline` reads the
        # record the mandate froze.
        self._baseline_red: dict[str, str] = {}
        self._baseline_taken = False
        # Leaves whose merge needed a conflict resolved: their join is read again (`_integration_gate`).
        self._resolved_on_merge: set[str] = set()

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

    def _spend_so_far(self) -> usage_mod.Spend:
        """What this cycle has spent: the runs the chain records, plus what this run has added."""
        if self._spend_before is None:
            live, _ = event_chain.scan(self.repo.events)
            self._spend_before = run_record.cycle_spend(live, self.cycle_id)
        return self._spend_before + usage_mod.Spend.of(self.usage_totals())

    def _stop_if_over_ceiling(self) -> None:
        """Stop the run when this cycle has spent its ceiling. Raises :class:`StopLoop`.

        **Between batches, never inside one**, and that placement is the whole design:

        * A leaf that is running has been paid for, and this file already refuses to throw away a
          batch that earned its merge because something else went wrong (`_run_batch`). So the
          ceiling stops the *next* batch, and a run can cross it by the batch that crossed it —
          which the message says rather than implying a precision it does not have.
        * Raised here, it is a `StopLoop` on the run's own thread, which `_run_loop` turns into a
          message and `EXIT_HUMAN_NEEDED`. Raised inside a leaf it would become that task's
          verdict (`_safe_run_task`), and **a spend figure that can fail a task is a spend figure
          the judgement path reads** — the one thing `00-concept.md` (論点 A) forbids of this
          number. No gate, review or lens sees it; the only thing it decides is whether the loop
          keeps launching.
        * **Not in a dry run**, for `_preflight`'s reason: a dry run launches nothing and enters
          no sandbox, so it adds nothing to the figure this compares. Refusing to print the
          control flow because earlier runs spent the ceiling would withhold the one answer a dry
          run exists to give — and withhold it exactly from the person the ceiling just handed
          the cycle back to, who is reading it to decide whether to raise the number.
        """
        if self.dry_run:
            return
        if reason := usage_mod.over_ceiling(self.config.max_cost_usd, self._spend_so_far()):
            self._escalate("cost_ceiling", reason)
            raise StopLoop(reason)

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
        reviewer is the one whose findings `brief.residual_findings` carries to acceptance.
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
        """Record a status, with the evidence and diagnostics this run holds for the task."""
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
        contained = self._agent_sandbox()
        while True:
            # Counted per attempt, inside the loop, because a retry is another launch: the same
            # argv goes to the provider again and is paid for again. Counting once per `_launch`
            # under-reported every retried task, and put two fields called `launches` in the same
            # `run_measured` event disagreeing with each other — the byte counter saying 1 where
            # the billed one said 3.
            self._spend(role or where, prompt_bytes, resumed=resumed)
            with common.Heartbeat(where):
                if contained is None:
                    rc, out = _run(argv, cwd=cwd, timeout=self.config.timeout_agent, env=env)
                else:
                    rc, out = self._launch_contained(contained, argv, cwd=cwd, where=where, env=env)
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
                    # What session this launch *opened*, for a CLI that mints its own id and reports
                    # it (`Adapter.session_from_envelope`). Set on every successful launch, never
                    # only when someone might want it, so a caller can never read a stale one from
                    # an earlier launch on this thread. "" for a CLI that names no session.
                    self._local.session_opened = record.session_of(out) if record else ""
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

    def _write_dossier(self, task: dag.Task, cwd: str, role: str) -> str:
        """Assemble this task's dossier into `cwd` and return its repo-relative path ("" in a dry run).

        Written before every launch rather than once per task: the diff moves between attempts,
        and a dossier describing the tree as it was two retries ago is worse than none.
        """
        if self.dry_run:
            return ""
        changed, diff_cmd = self._review_scope(task, cwd)
        document = dossier.build(
            task,
            plan=self._plan,
            repo_path=self.repo.path,
            changed=changed,
            diff_cmd=diff_cmd,
            history=self._history_for(task),
            handoff=self._handoff_for(task),
            env={
                "role": role,
                "task_id": task.id,
                "run_id": self.run_id,
                "sandbox": self._launch_environment(),
                "control_plane": self.control is not None,
            },
        )
        written = dossier.write(cwd, document)
        self._spend_handover(role, dossier.handover_bytes(document, written, self.repo.path))
        return f"{dossier.RELATIVE_PATH}/{task.id}.json"

    def _agent_sandbox(self) -> models.ExecutorProfile | None:
        """The profile an agent CLI is launched in, or None to launch it on the host.

        None is the configured default and an honest one: the image has to carry the CLI, so no
        packaged image can cover every role anyone points at a CLI. What is refused is the third
        state — `executors.agent_profile` naming a profile that is not a sandbox. That reads like
        a boundary in the file a human approved, so it fails here rather than running the agent on
        the machine and saying nothing, which is precisely what `implementer_profile` used to do.
        """
        profile = self.config.raw.agent_profile
        if profile is None or profile.is_agent_sandbox:
            return profile
        raise common.ReinError(
            f"executors.agent_profile names {profile.name!r}, which is `kind: {profile.kind}`. An agent "
            "launch needs `kind: oci-agent` — the kind that is granted egress, because an agent that "
            "cannot reach its model API does nothing. Build one with `rein oci build --profile agent "
            "--build-arg AGENT_CLI=<npm package> --write-config`, or drop the key and the agent runs on "
            "the host."
        )

    def _launch_contained(
        self,
        profile: models.ExecutorProfile,
        argv: list[str],
        *,
        cwd: str,
        where: str,
        env: dict[str, str] | None,
    ) -> tuple[int, str]:
        """One agent launch inside `profile`, with the worktree and the control socket bound in.

        The worktree is mounted read-write because an implementer's whole job is to change it, and
        at the same absolute path rules as a gate step (`_mounts_for`) so a leaf's `.git` redirect
        resolves. The control socket is bound at a fixed path and the leaf is told that path, which
        is the one thing that has to be rewritten between the host's environment and the
        container's: the host's socket lives under `/run/user/<uid>/rein/<id>`, and mounting that
        directory would hand the container every other socket in it.

        `HOME` is the container's ephemeral tmpfs, not the operator's, so the CLI's own state dies
        with the launch. That is also why an adapter's `own_sandbox` has to be switched off in here
        — a second sandbox inside this one fails at the point it tries to write, and `doctor` says
        so before a run finds out.
        """
        spec = executors.ExecutionSpec(
            command=tuple(argv),
            profile=profile,
            mounts=self._mounts_for(profile, cwd),
            env=dict(env or os.environ),
            env_always=self._contained_wiring(env),
            workdir=_SANDBOX_WORKDIR,
            timeout_sec=self.config.timeout_agent,
        )
        try:
            result = executors.for_profile(profile).run(spec)
        except executors.ExecutorError as exc:
            raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=1, output=str(exc)) from exc
        return result.exit_code, result.output

    @staticmethod
    def _contained_wiring(env: dict[str, str] | None) -> dict[str, str]:
        """The control-plane variables as the container must see them.

        `REIN_CONTROL_SOCKET` is rewritten to the bound path; everything else the orchestrator
        minted travels as-is. Empty when there is no control plane (a dry run), which leaves the
        leaf with no socket and a `rein report` that refuses rather than writing into a worktree
        about to be deleted — the same behaviour a host launch has.
        """
        if not env or control_plane.SOCKET_ENV not in env:
            return {}
        wiring = {
            name: value for name, value in env.items() if name.startswith("REIN_") and name != control_plane.SOCKET_ENV
        }
        # The plan's `environment.env` was approved with the mandate, so it is passed whatever the
        # profile's allowlist says — the allowlist keeps the operator's shell out, not the plan.
        for name in filter(None, env.get(DECLARED_ENV, "").split(",")):
            if name in env:
                wiring[name] = env[name]
        wiring[control_plane.SOCKET_ENV] = _SANDBOX_CONTROL_SOCKET
        return wiring

    def _launch_environment(self) -> str:
        """What the agent is actually running inside, so it stops inferring it from its prompt.

        Derived from where the launch will really happen, never from a config key that says where
        it ought to. This used to report `executors.implementer_profile`'s `kind`, and since no
        launcher read that key, a repository that had pinned its images told the agent it was
        inside an OCI sandbox while the process ran on the machine.

        `own_sandbox` is the other thing that can change the answer, and it is the adapter's doing:
        `codex exec` establishes seccomp/landlock isolation around its own work. Saying which it is
        matters in both directions — an agent told it is already sandboxed does not try to build a
        second one, and an agent told it is not does not assume the tree is disposable.
        """
        profile = self.config.raw.agent_profile
        if profile is not None and profile.is_agent_sandbox:
            return f"oci-agent ({profile.name}; egress open, repository mounted at {_SANDBOX_WORKDIR})"
        adapter = self._implementer_adapter
        return f"host ({adapter.name} sandboxes itself)" if adapter and adapter.own_sandbox else "host"

    def _history_for(self, task: dag.Task) -> list[dict[str, Any]]:
        """What happened to this task before this launch, oldest first, out of the audit chain.

        Three kinds of line. An **attempt** — which step went red, and on the latest failure why:
        the handoff carried only the last failure, so a task on its fourth attempt arrived with no
        memory of the three before it and could — and did — re-try the same fix. A **join** round —
        a step that went red over the tree this task's batch merged into (`stage: integration`),
        which is not this task's own attempt and must not read as one. A **reset** — what the human
        wrote when they put the task back (`rein task reset --reason`). That sentence is addressed
        to the retry — "what you repaired", in `rein start`'s words — and it was recorded only in
        the chain, so one recorded task was reset twice with the missing input spelled out and its
        third launch asked for that input again. It comes from the chain rather than the handoff, so
        `--fresh` — which discards the handoff because the repair was made outside the tree — keeps
        exactly the note that says what that repair was.

        A `task_failed` that carries a `status` is the status write that ended the attempt, and it
        restates the failure its own round already recorded; counted, every blocked task showed one
        attempt more than it had.
        """
        if self.dry_run or not self.cycle_id:
            return []
        try:
            events = self.store.read_events()
        except Exception:  # noqa: BLE001 - a damaged chain is doctor's to report, not the loop's
            return []
        seen: list[dict[str, Any]] = []
        attempts = 0
        last_failure: dict[str, Any] | None = None
        for event in events:
            if task.id not in event.subject_ids:
                continue
            if event.event == "task_failed" and "status" not in event.detail:
                step = str(event.detail.get("step", "")) or str(event.detail.get("kind", ""))
                if not step:
                    continue
                if event.detail.get("stage") == "integration":
                    last_failure = {"step": step, "stage": "integration"}
                else:
                    attempts += 1
                    last_failure = {"attempt": attempts, "step": step}
                seen.append(last_failure)
            elif event.event == "decision_declared" and event.detail.get("kind") == "task_reset":
                seen.append(
                    {
                        "reset": str(event.detail.get("reason", "")),
                        "fresh": event.detail.get("handoff") == "discarded",
                    }
                )
        # The handoff holds one failure, the latest written (`_FAILURE_FIELDS`), and the round that
        # produced it is in the chain before or with it — so the latest line is the one it explains.
        handoff = self._handoff_for(task)
        if last_failure is not None and handoff.get("failure_summary"):
            last_failure["reason"] = str(handoff["failure_summary"])[-600:]
        return seen[-dossier.MAX_HISTORY :]

    # -- implementer launch and quality gate --

    def _implementer_prompt(
        self, task: dag.Task, failure_log: str, dossier_path: str = "", continued_from: str = "", review: str = ""
    ) -> str:
        return build_prompts.implementer_prompt(
            task,
            failure_log,
            gate_cmds=self.config.gate_cmds,
            has_baseline=self.repo.path("docs/05-current-state.md").exists(),
            pathspec=self.ws.pathspec,
            handoff=self._handoff_for(task),
            dossier_path=dossier_path,
            continued_from=continued_from,
            continued_worktree=self.ws.worktree_path(continued_from) if continued_from else "",
            review_findings=review,
        )

    def _inherited_session(self, task: dag.Task) -> tuple[str, str]:
        """`(upstream task, its session)` for a task that can start where its upstream stopped.

        Only a task with **exactly one** upstream: that one's implementer read the codebase this task
        builds on, and there is no choosing between two. The launch forks that session rather than
        resuming it, so the upstream's session is unchanged and two leaves under one foundation do
        not see each other's conclusions — the same property the acceptance reading relies on when
        it hands one reading to two stages (`review_transport.SharedReading`).

        `("", "")` sends the task in cold, as every task went before: a CLI that is not told its
        session id or cannot fork, an upstream this cache never saw finish, or a cache that is off.
        The last is said once, because an unexplained cold start is a cost nobody can see.
        """
        adapter = self._implementer_adapter
        if self.dry_run or len(task.blocked_by) != 1 or adapter is None:
            return "", ""
        if not (adapter.forkable and adapter.session_flags):
            return "", ""
        if self.sessions.unavailable:
            if not self._sessions_said:
                self._sessions_said = True
                print(f"    [session] every task starts cold this run: {self.sessions.unavailable}")
            return "", ""
        upstream = task.blocked_by[0]
        found = self.sessions.get(self.cycle_id, upstream, adapter.name)
        return (upstream, found) if found else ("", "")

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

    @property
    def _implementer_mints_its_own_session(self) -> bool:
        """Whether the implementer CLI names the session rather than being told one.

        The two shapes need different handling here and nowhere else: a caller-chosen id exists
        before the first launch, a CLI-chosen one exists only after it. "Start a fresh session" is
        therefore "mint a new uuid" for the first and "forget the one we were given" for the second.
        """
        adapter = self._implementer_adapter
        return bool(adapter and adapter.session_from_envelope and adapter.resume_argv)

    def _invoke_implementer(
        self,
        task: dag.Task,
        cwd: str,
        failure_log: str,
        session: str = "",
        resume: bool = False,
        fork: tuple[str, str] = ("", ""),
        review: str = "",
    ) -> str:
        """One headless implementer launch; `session`/`resume` thread retry-session continuity.

        `fork` is `(upstream task, its session)` when this launch opens `session` as a branch of the
        session that finished the task's one upstream (`_inherited_session`). A fork that fails is
        treated like a resume that fails: one fresh launch, told nothing about a session it lacks.

        Returns the session this launch is continuing or opened, "" when there is none — which is
        how a CLI that mints its own id gets one back to the caller (`Adapter.session_of`).

        With a session, the implementer keeps its own context across its retries instead of
        re-reading ticket/design/code cold. How it is carried depends on the CLI: claude is *told*
        an id (`--session-id` then `--resume`), codex *reports* one and is resumed by verb
        (`codex exec resume <id>`). `adapters.command` places either; this only decides which
        session the next launch should continue.

        A failed resume falls back to one fresh launch (session files can expire) rather than
        stopping the loop on a continuity optimization — but only when a fresh launch could
        plausibly do better. An exhausted session limit or a CLI that is not on PATH gets no second
        attempt: nothing was going to launch.
        """
        if self.dry_run:
            print(f"    [dry-run] launch implementer (cwd={cwd}) task={task.id}")
            return ""
        upstream, parent = fork
        dossier_path = self._write_dossier(task, cwd, "implementer")
        prompt = self._implementer_prompt(task, failure_log, dossier_path, continued_from=upstream, review=review)
        where = f"{task.id}: implementer"
        allowed = self._allow_argv(task)
        if upstream:
            print(f"    [session] {task.id}: starting from {upstream}'s implementer session")
        try:
            self._launch(
                adapters.command(
                    self.config.adapter_argv,
                    prompt,
                    access=adapters.WRITE,
                    extra=allowed,
                    session=session,
                    resume=resume,
                    fork_from=parent,
                ),
                cwd=cwd,
                where=where,
                env=self._leaf_env(task),
                task_id=task.id,
                role="implementer",
                session=session,
                resumed=resume,
            )
            return session or getattr(self._local, "session_opened", "")
        except EnvironmentFault as fault:
            # The case this branch is most worth having is a session that outgrew the model's
            # window: what did not fit is its own accumulated context, and a cold launch is exactly
            # how it stops being carried. It reaches here already — transient, not capacity — which
            # is why `faults.is_context_overflow` stays a predicate for callers that have no
            # session to reset rather than a classification that would take this retry away.
            if not (resume or parent) or not fault.retryable or faults.is_capacity(fault.output):
                raise
            print(f"    [resume] {task.id}: resuming session failed (rc={fault.rc}); relaunching fresh")
            if upstream:
                prompt = self._implementer_prompt(task, failure_log, dossier_path, review=review)
        # A fresh token: the first one was spent on the launch that failed, and the server
        # accepts each nonce once.
        self._launch(
            adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE, extra=allowed),
            cwd=cwd,
            where=where,
            env=self._leaf_env(task),
            task_id=task.id,
            role="implementer",
        )
        return getattr(self._local, "session_opened", "")

    def _allow_argv(self, task: dag.Task) -> tuple[str, ...]:
        """The per-launch permission flags for `task.allow`, or () with a line saying why none.

        Scoped to this one launch on purpose: the other way to let an implementer run its task's
        measurement script was a standing rule in the repository's `permissions.allow`, which
        every later session inherits and the security review then has to find.
        """
        if not task.allow:
            return ()
        record = adapters.adapter_for(self.config.adapter_argv)
        flags = record.allow_argv(task.allow) if record is not None else ()
        if not flags:
            print(
                f"    [allow] {task.id}: this agent CLI has no per-launch permission setting; "
                f"{len(task.allow)} declared command prefix(es) not granted"
            )
        return flags

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
        declared = task_environment(task)[0] if role == "implementer" else {}
        return {
            **os.environ,
            **declared,
            control_plane.SOCKET_ENV: str(self.control.socket_path),
            control_plane.TOKEN_ENV: token,
            "REIN_ROLE": role,
            "REIN_TASK_ID": task.id,
            "REIN_RUN_ID": self.run_id,
            "REIN_SANDBOX": self._launch_environment(),
            **({DECLARED_ENV: ",".join(sorted(declared))} if declared else {}),
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
        bought several times. An operator decides that at the mandate; no task can.
        """
        return tuple(step for step in self._steps_effective if step.stage in {stage, "both"})

    def _steps_for(self, task: dag.Task, cwd: str = "") -> tuple[GateStep, ...]:
        """The gate steps for one task: this stage's DoD, minus any step whose `paths:` this
        task's diff does not touch.

        Still not a knob an implementer can turn: `paths:` and `stage:` are frozen by the mandate in
        config.yaml alongside every other DoD step, not read from the task or its ticket. A step
        naming no `paths:` — every packaged step ships this way — runs for every task exactly as
        before. The diff is computed fresh (`_review_scope`, the same source the review prompt's
        scope uses) so an empty/unresolved diff (a fresh worktree, dry-run, no `cwd` given) runs
        every step rather than guessing an empty scope means nothing to check.
        """
        steps = self._steps_at("task")
        if not cwd:
            return steps
        changed, _ = self._review_scope(task, cwd)
        return tuple(step for step in steps if step.matches_paths(changed))

    def _review_scope(self, task: dag.Task, cwd: str) -> tuple[list[str], str]:
        """The changed-path list + exact diff command that scope the review step's read.

        Computed fresh at review time (the tree moves between retries): everything the task's
        worktree holds since it forked off its target branch. A caller outside a task's worktree
        (dry-run, the repository root) degrades to the unscoped prompt.
        """
        if self.dry_run or cwd == self.root:
            return [], ""
        paths = self.ws.branch_changed_paths(task.id, cwd=cwd)
        return paths, f"git diff {self.ws.target_branch(task.id)}...HEAD"

    def _frozen_code_lenses(self) -> tuple[list[str], list[str]]:
        """`(applied, proposed)` as one line each, for the code-stage reviewers."""
        applied_ids, proposed_ids = lenses.frozen(self._plan, stage="code")
        library = lenses.library()
        applied, missing_a = lenses.by_id(library, applied_ids)
        proposed, missing_p = lenses.by_id(library, proposed_ids)
        for lens_id in missing_a + missing_p:
            logger.warning(
                f"{lens_id} was frozen into this plan and is no longer in the lens library — "
                "this cycle's code review runs without it"
            )

        def line(lens: lenses.Lens) -> str:
            return f"**{lens.id}** — {' '.join(lens.attack.split())}"

        return ([line(lens) for lens in applied], [line(lens) for lens in proposed])

    def _fingerprint(self, cwd: str) -> str:
        """The content digest of the tree at `cwd` ("" when it cannot be computed)."""
        return "" if self.dry_run else self.ws.fingerprint(cwd)

    def _batch_review_steps(self) -> tuple[GateStep, ...]:
        """The agent steps a batch is read by before it merges: every one the task stage runs.

        An agent step is not part of a task's pipeline any more (`_run_pipeline`). It is asked once
        per batch, after every leaf has finished its deterministic gate, by one launch that reads
        all of them (`_review_batch`).
        """
        return tuple(step for step in self._steps_at("task") if step.kind == "agent")

    def _review_subject(self, task: dag.Task) -> build_prompts.ReviewSubject:
        cwd = self.ws.worktree_path(task.id)
        written = self._write_dossier(task, cwd, "code_reviewer")
        where = Path(cwd).relative_to(self.root) if Path(cwd).is_relative_to(self.root) else Path(cwd)
        branch = self.ws.branch_for(task.id)
        return build_prompts.ReviewSubject(
            task=task,
            branch=branch,
            diff_cmd=f"git diff {self.ws.target_branch(task.id)}...{branch}",
            dossier_path=str(where / written) if written else "",
        )

    def _review_batch(self, tasks: Sequence[dag.Task], results: dict[str, LeafOutcome]) -> None:
        """Read every leaf that passed its deterministic gate in one launch, and act on the answer.

        **One reviewer launch per batch and round, not one per task.** Each leaf used to get a
        reviewer of its own, and a batch of two or more then got one more over the join, reading
        the union of what the others had read. Independence needs a launch that is not the
        implementer's; it does not need one per task. The reader answers per task, because the
        answer per task is what acts: a leaf's `must_fix` findings go back to *that* leaf's
        implementer through the same send-back a red step takes (`_run_task_to_done`), which resumes
        its session and re-establishes the whole deterministic gate over the fixed tree. Only the
        leaves that were sent back are read again, from cold.

        A leaf still holding `must_fix` findings when the step's `retries` run out does not land;
        the rest of the batch does. A leaf the reader wrote nothing about was not reviewed, and it
        does not land either. A leaf whose tree moved while it was being read goes back through the
        same send-back: what is on its branch is no longer what the gate established.

        A task that operates is not here: it was read before its run (`_review_before_operate`),
        because a finding read after the run costs the run again.

        Mutates `results`: whatever this decides about a leaf replaces what its pipeline said.
        """
        for step in self._batch_review_steps():
            subjects = [
                task
                for task in sorted(tasks, key=lambda t: t.id)
                if _landable(results[task.id])
                and not task.operate
                and step.matches_paths(self.ws.branch_changed_paths(task.id) if not self.dry_run else [])
            ]
            rounds = max(0, step.retries)
            for attempt in range(rounds + 1):
                if not subjects:
                    break
                if self.dry_run:
                    print(f"    [dry-run] review {', '.join(t.id for t in subjects)} in one launch ({step.name})")
                    break
                send_back: dict[str, str] = {}
                for task_id, answer in self._read_batch(step, subjects).items():
                    if answer.fault is not None:
                        results[task_id] = LeafOutcome(ok=False, log=answer.fault.summary(), fault=answer.fault)
                    elif answer.stop:
                        results[task_id] = LeafOutcome(ok=False, log=answer.stop)
                    elif answer.send_back:
                        send_back[task_id] = answer.send_back
                if not send_back:
                    break
                if attempt == rounds:
                    for task_id, text in send_back.items():
                        unresolved = f"the reviewer's findings were not resolved within {rounds} round(s)"
                        results[task_id] = LeafOutcome(ok=False, log=f"{task_id}: {unresolved}:\n{text}")
                    break
                returned = [t for t in subjects if t.id in send_back]
                print(f"    [review] {', '.join(t.id for t in returned)}: back to the implementer")
                with ThreadPoolExecutor(max_workers=max(1, self.config.max_parallel)) as pool:
                    futures = {
                        t.id: pool.submit(self._safe_run_task, t, self.ws.worktree_path(t.id), send_back[t.id])
                        for t in returned
                    }
                    for task_id, future in futures.items():
                        results[task_id] = future.result()
                subjects = [t for t in returned if _landable(results[t.id])]

    def _read_batch(self, step: GateStep, subjects: Sequence[dag.Task]) -> dict[str, ReviewAnswer]:
        """One reviewer launch over `subjects`, and what it said about each of them.

        Every subject gets an answer. One the reader cannot be held to — no entry, an unreadable
        file — is a `stop`. When the launch never happened every subject gets the `fault`: the
        machine stopping a reviewer is not a verdict about any task.
        """
        role = step.agent_role or "code_reviewer"
        ids = [task.id for task in subjects]
        argv = step.agent_argv or self.config.adapter_argv
        target = dossier.findings_path(self.root, _BATCH_REVIEW_SUBJECT.format(ids="+".join(ids)))
        target.unlink(missing_ok=True)  # a stale file from the previous round is not this answer
        findings_rel = f"{dossier.RELATIVE_PATH}/{target.name}"
        prompt = build_prompts.batch_review_prompt(
            [self._review_subject(task) for task in subjects],
            gate_cmds=self.config.gate_cmds,
            findings_path=findings_rel,
            reviews=step.reviews,
            questions=self.config.questions,
            # Keyed on the argv this step is launched with, not the default one: offering a
            # discipline the launched CLI does not have is the dangling reference this replaced.
            disciplines=adapters.disciplines_for(argv),
            lenses_applied=self._code_lenses[0],
            lenses_proposed=self._code_lenses[1],
        )
        # Taken after the dossiers are written, so what the loop itself put in a worktree is never
        # mistaken for the reviewer having moved it.
        before = {task.id: self._fingerprint(self.ws.worktree_path(task.id)) for task in subjects}
        try:
            # `REVIEW`, not `WRITE`: the findings file is the only thing it needs to produce, and
            # naming it is what lets an adapter that can scope a write grant exactly that one.
            self._launch(
                adapters.command(argv, prompt, access=adapters.REVIEW, writable=findings_rel),
                cwd=self.root,
                where=f"{', '.join(ids)}: the '{step.name}' agent step",
                role=role,
            )
        except EnvironmentFault as fault:
            return {task_id: ReviewAnswer(fault=fault) for task_id in ids}
        try:
            by_task, missing = dossier.parse_batch_findings(target.read_text(encoding="utf-8"), ids)
        except FileNotFoundError:
            unreadable = (
                f"the reviewer wrote no findings file ({findings_rel}). "
                "A review that produced nothing readable is not a review that found nothing."
            )
            return {task_id: ReviewAnswer(stop=f"{task_id}: {unreadable}") for task_id in ids}
        except (dossier.FindingsError, OSError) as exc:
            unreadable = f"the reviewer's findings could not be read — {exc}"
            return {task_id: ReviewAnswer(stop=f"{task_id}: {unreadable}") for task_id in ids}
        answers = {
            task_id: ReviewAnswer(
                stop=f"{task_id}: the reviewer wrote no entry for this task, so it was not reviewed and does not land."
            )
            for task_id in missing
        }
        if by_task:
            self._event(
                "reviews_applied", sorted(by_task), {"step": step.name, "stage": "task", "reviews": list(step.reviews)}
            )
        for task_id, findings in by_task.items():
            self._add_review_findings(task_id, findings)
            outstanding = dossier.must_fix(findings)
            moved = before[task_id] != self._fingerprint(self.ws.worktree_path(task_id)) or not before[task_id]
            text = dossier.render_findings(outstanding) if outstanding else ""
            if moved:
                moved_line = (
                    "  - the tree changed while it was being reviewed; the gate has to be established over it again"
                )
                text = f"{text}\n{moved_line}" if text else moved_line
            if findings and not outstanding:
                print(f"    [review] {task_id}: {len(findings)} finding(s), none blocking")
            answers[task_id] = ReviewAnswer(send_back=text)
        return answers

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
        ones — `stage` and `paths` came from the mandate like every other part of the DoD.
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
        if step.junit:
            # Removed before the run, so a report present afterwards is one this run wrote. A suite
            # that crashed before writing had the previous run's report read as its own — and when
            # that one listed only reds the change inherited, the crash was routed away as green.
            (Path(cwd) / step.junit).unlink(missing_ok=True)
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
            with common.Heartbeat(where):
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
        """The profile a step runs in: its own if it names one, else `executors.quality_gate_profile`.

        **A name that resolves to nothing raises.** It used to fall back to a bare host profile, so
        a misspelt `executor_profile`, or a `quality_gate_profile` naming a profile that is not in
        `executor_profiles`, ran the step on the machine and reported nothing — the one outcome the
        setting exists to prevent, reached by a typo. The schema requires `quality_gate_profile`
        and `rein doctor` checks that it resolves, so arriving here with nothing is a config that
        was edited past both.
        """
        config = self.config.raw
        if step.executor_profile:
            if named := config.profiles.get(step.executor_profile):
                return named
            raise common.ReinError(
                f"quality-gate step {step.name!r} names executor_profile "
                f"{step.executor_profile!r}, which is not in executor_profiles"
            )
        if profile := config.quality_gate_profile:
            return profile
        raise common.ReinError(
            "executors.quality_gate_profile names no profile in executor_profiles — "
            "a step would otherwise run repository code on this machine without saying so"
        )

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

        An agent sandbox takes the same mounts plus the control socket, and takes them read-write
        whatever `mount_repo` says: an agent that cannot write the checkout it was handed cannot do
        the one thing it was launched for, so a `read_only` there would be a config error dressed
        as a preference.
        """
        if not profile.runs_contained:
            return ()
        mode = "read_write" if profile.is_agent_sandbox else str(profile.raw.get("mount_repo", "read_write"))
        if mode == "none":
            return ()
        access = "ro" if mode == "read_only" else "rw"
        mounts = [(Path(cwd), _SANDBOX_WORKDIR, access)]
        git_dir = _worktree_common_git_dir(Path(cwd))
        if git_dir is not None:
            mounts.append((git_dir, str(git_dir), access))
        if profile.is_agent_sandbox and self.control is not None:
            mounts.append((Path(self.control.socket_path), _SANDBOX_CONTROL_SOCKET, "rw"))
        return tuple(mounts)

    def _run_pipeline(self, task: dag.Task, cwd: str) -> tuple[str | None, str]:
        """Run the deterministic quality-gate steps (the DoD's command steps) in order.

        Returns (failed_step_name, failure_summary), or (None, "") when every step passed. The
        agent steps are not in here: a batch is read by one reviewer once every leaf has passed
        this (`_review_batch`), and what it finds comes back through the same send-back.
        """
        steps = tuple(step for step in self._steps_for(task, cwd) if step.kind == "command")
        if self.dry_run:
            shown = " → ".join(f"{s.name}({s.kind})" for s in steps)
            print(f"    [dry-run] quality gate: {shown} (cwd={cwd})")
            return None, ""
        passed: list[GateStep] = []
        forked_from = self.ws.fork_point(self.ws.target_branch(task.id), cwd)
        for step in steps:
            if not step.command:
                print(f"    [gate] skip {step.name}: no command configured")
                continue
            failure = self._attribute_red([task], step, cwd, forked_from, self._run_cmd_step(step, cwd))
            if failure:
                return step.name, failure
            passed.append(step)
        failed, failure = self._negative_control(task, cwd, passed)
        if failed:
            return failed, failure
        return self._run_acceptance(task, cwd)

    def _attribute_red(self, tasks: Sequence[dag.Task], step: GateStep, cwd: str, base: str, failure: str) -> str:
        """The part of a red step that `tasks`' change owns. "" when none of it is.

        A red was charged to whoever was under test, because that is who was being judged — not to
        whoever owns the failing test. A flaky test, or one another task put in the suite, then
        blocked a task that could fix neither, and every such stop reached a person whose only
        move was to read who owned it (#90). With the step's JUnit report the loop reads it
        instead, and decides with two experiments and no judgement:

          * **Re-run on the same tree, in the same image.** Green the second time is a flaky test:
            recorded against its owner, and the step passes.
          * **Run it on the tree the change forked from.** A test red there too was red before this
            change, whoever owns it; only a test this change turned red is this change's failure.
            When every failing test was red already, the red is routed to the task whose scope
            holds it and the step passes here.

        Deliberately not asked: whether the change *imports* the failing test's code. A behaviour
        can break a test through no import at all, and handing that red to its owner would be a
        green this change never earned. The comparison with the forked-from tree has no such gap.

        Without a report nothing below the step is known, and the red stays this change's.
        """
        if not failure or not step.junit or not step.runs_tests or not base or self.dry_run:
            return failure
        report = Path(cwd) / step.junit
        first = junit_mod.failing(report)
        if not first:
            return f"{failure}\n(no failing test in a readable JUnit report at {step.junit}: charged to the step)"
        if not self._run_cmd_step(step, cwd):
            owners = self._owners_of(first, exclude=tasks)
            self._event(
                "decision_declared",
                owners or [t.id for t in tasks],
                {"kind": "flaky", "step": step.name, "nodes": sorted(first)[:_NODES_SHOWN]},
            )
            print(f"    [gate] {step.name}: red, then green on the same tree — flaky: {', '.join(sorted(first))}")
            return ""
        now = junit_mod.failing(report)
        if not now:
            unread = f"the re-run left no failing test in a readable report at {step.junit}: charged to the step"
            return f"{failure}\n({unread})"
        before = self._failing_at(step, base, owner="-".join(t.id for t in tasks))
        if before is None:
            return f"{failure}\n(could not read the same step at {base[:12]}: the red is charged to this change)"
        # A test red before the change is still this change's when this task owns it: the owner is
        # the one whose attempt is meant to fix it, and routing it anywhere else would pass it on.
        owned_here = {
            node for node in now & before if set(self._owners_of(frozenset({node}), exclude=())) & {t.id for t in tasks}
        }
        mine = (now - before) | owned_here
        if mine:
            inherited = sorted((now & before) - mine)
            already = f"\n(already red before this change, not counted against it: {', '.join(inherited)})"
            return f"This change turned these tests red: {', '.join(sorted(mine))}\n{failure}" + (
                already if inherited else ""
            )
        self._route_red(tasks, step, now)
        return ""

    def _failing_at(self, step: GateStep, base: str, *, owner: str) -> frozenset[str] | None:
        """The tests `step` fails on the tree at `base`; None when that could not be read.

        The scratch checkout is named for `owner` (the task or join asking) as well as the step:
        parallel leaves ask this at the same time, and two of them sharing one checkout would each
        remove the other's tree from under it.
        """
        try:
            with build_git.scratch_worktree(
                self.repo, self.config.worktree_dir, f"red-{owner}-{step.name}", base, _late_run
            ) as path:
                if not self._run_cmd_step(step, path, note=False):
                    return frozenset()
                return junit_mod.failing(Path(path) / step.junit)
        except (EnvironmentFault, StopLoop) as exc:
            logger.warning(f"[gate] {step.name} could not be run at {base[:12]}: {exc}")
            return None

    def _owners_of(self, nodes: frozenset[str], *, exclude: Sequence[dag.Task]) -> list[str]:
        """The tasks whose declared scope holds the files these tests live in, `exclude` aside.

        A task with no `scope.include` is unbounded, which covers every path and so says nothing
        about ownership; it is never counted as an owner.
        """
        if self._plan is None:
            return []
        skip = {t.id for t in exclude}
        paths = {path for node in nodes if (path := junit_mod.node_path(node, self.repo.root))}
        return sorted(
            t.id
            for t in dag.join(self._plan, self.store.read_state()).tasks
            if t.id not in skip
            and t.scope_include
            and any(not common.outside_scope([path], t.scope_include, t.scope_exclude) for path in paths)
        )

    def _route_red(self, tasks: Sequence[dag.Task], step: GateStep, nodes: frozenset[str]) -> None:
        """Hand a red that was there before this change to the task that owns it.

        A `done` owner with nothing started on top of it goes back on the frontier with the red in
        its handoff, and its next attempt fixes its own test. Anything else — an owner other work
        already stands on, a test in nobody's scope — is a person's call, and is escalated against
        the owner rather than against the task that happened to be running.
        """
        listed = ", ".join(sorted(nodes))
        by = ", ".join(t.id for t in tasks)
        owners = self._owners_of(nodes, exclude=tasks)
        routed = ", ".join(owners) or "nobody"
        print(f"    [gate] {step.name}: red before {by} changed anything — {listed}; routed to {routed}")
        graph = self._load_graph()
        for owner in owners:
            task = graph.get(owner)
            started = {
                tid
                for tid in graph.dependents_closure([owner])
                if graph.get(tid).status in {"done", "awaiting-evidence", "in-progress"}
            }
            message = (
                f"{owner}: '{step.name}' fails {listed} on the tree {by} forked from, before {by} changed "
                f"anything. The test lives in {owner}'s scope."
            )
            if task.status == "done" and not started:
                self._note_diagnostic(owner, {"failure_summary": message[-_HANDOFF_SUMMARY_MAX:]})
                self._set_status(owner, "todo")
                self._event(
                    "decision_declared",
                    owner,
                    {"kind": "red_routed", "step": step.name, "nodes": sorted(nodes)[:_NODES_SHOWN], "from": by},
                )
            elif task.status in {"done", "awaiting-evidence"}:
                self._escalate(
                    "owned_red",
                    f"{message} Work already stands on {owner} ({', '.join(sorted(started))}), so it is not "
                    "reopened by itself: decide whether to reset it or repair the test directly.",
                    task=owner,
                )
        if not owners:
            self._escalate(
                "owned_red",
                f"'{step.name}' fails {listed} on the tree {by} forked from, and no task's declared scope "
                "holds those tests. Nothing was blocked for it; somebody has to own the fix.",
            )

    def _negative_control(self, task: dag.Task, cwd: str, passed: Sequence[GateStep]) -> tuple[str | None, str]:
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

        **Only the steps that run the tests are re-established** (`runs_tests`). The question is
        whether any test in the change exercises it, and only a test run answers that. A linter or
        type checker over the base goes red on every new test file that imports a module the base
        does not have — a test whose body is `pass` included — so taking its red as the answer
        landed five of eight tasks of one recorded cycle on a fact true of any new test file.

        **Read the two outcomes for what each is worth — they are not symmetric.** A green control
        is the strong one: it is a fact about every test in the change at once, and no reading of
        the test files could establish it more cheaply or more surely. A red one says only that
        the test half is *not inert* against the old code — the step that went red may have gone
        red because a test asserted something false there, or because the test imports a symbol
        the base does not have and never got as far as asserting anything. This experiment cannot
        separate those without parsing a test runner's output, which is a thing this loop does not
        do. So `discriminating` is the absence of the failure, not the presence of a good test;
        what asks whether the tests are *any good* is the per-task reviewer, which reads them.

        Four answers are not passes, and each says so rather than being folded into one:

        * **no test path changed** — there is no control to take. Not a failure: a task whose work
          is genuinely covered by tests that already existed is a real thing, and blocking it would
          make the loop demand a test per task rather than evidence per claim. It is recorded, so
          "this task's green rests on tests nobody wrote for it" is on the record instead of being
          the silence it has always been.
        * **the control could not be set up** — no base, no command step in the DoD to re-establish,
          a diff git would not give up, an unapplied patch, a worktree that would not create, a
          sandbox that would not run the control's own steps. Recorded with the reason. Never a
          pass, never a block, and never an abort: a broken experiment is not evidence in either
          direction, and inventing a verdict from one is the thing the rest of this module refuses
          to do.
        * **every changed path is a test path** — the mirror of the first: nothing to remove where
          that one had nothing to apply. Base plus the test half *is* the head tree, so the control
          would compare head against head and come back green whatever the tests assert. Recorded
          as undetermined, never taken: an experiment with no contrast is the broken kind below,
          known before it is run.
        * **every step green** — the block. It comes back through the same channel a red step does,
          so it spends that attempt's budget and the implementer is told what is missing.
        """
        commands = [step for step in passed if step.kind == "command" and step.command and step.runs_tests]
        if self.dry_run:
            return None, ""
        if not commands:
            # Recorded rather than returned in silence, for the same reason `no_tests_changed` is:
            # a quality gate with no step that runs the tests — or none that ran for this task —
            # leaves the task's `done` resting on nothing this experiment can negate. Saying so is
            # the whole point of the record, and `brief._control` reads it.
            return self._control_undetermined("no quality-gate step that ran for this task declares `runs_tests`")
        changed, _ = self._review_scope(task, cwd)
        tests = [path for path in changed if diff_facts.classify_path(path) == "test"]
        if not tests:
            self._note_control("no_tests_changed", detail="the change touched no test path")
            print(f"    [control] {task.id}: no test path changed — the DoD's green is not controlled")
            return None, ""
        if len(tests) == len(changed):
            return self._control_undetermined(
                "every path in this change is a test path, so base plus the test half is the head tree "
                "— there is no contrast to measure"
            )
        control_base = self.ws.fork_point(self.ws.target_branch(task.id), cwd)
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
        except (EnvironmentFault, StopLoop) as exc:
            # Including the environment fault, which used to be re-raised. That made a third
            # ending the three above do not name: the task's *own* DoD had already gone green,
            # and a container that would not start for the control run aborted the leaf, reset it
            # to `todo` and made the next run implement it again. A broken experiment is not
            # evidence in either direction — that is this method's whole stated posture — and an
            # abort is the strongest verdict of the three. The steps that decide the task run
            # outside this call and still fault normally; only the control's own does not.
            return self._control_undetermined(str(exc))

    def _take_control(
        self, task: dag.Task, control_base: str, patch: str, commands: Sequence[GateStep]
    ) -> tuple[str | None, str]:
        """Run `commands` over `control_base` + `patch` and report which of them went red."""
        result, said = self._red_without(f"control-{task.id}", control_base, patch, commands)
        if result == "undetermined":
            return self._control_undetermined(said)
        if result == "discriminating":
            self._note_control("discriminating", base=control_base, step=said)
            print(f"    [control] {task.id}: '{said}' goes red without the change — the test half is not inert")
            return None, ""
        # Deliberately not noted: `evidence.negative_control` justifies a `done`, and this verdict
        # is the one that stops there being one. It travels as a task failure instead — the same
        # channel a red step uses, under the step name `NEGATIVE_CONTROL` — so the event chain
        # carries it and the next attempt inherits the summary.
        return NEGATIVE_CONTROL, (
            f"The quality gate is green, and it is green without your change. Re-running "
            f"{said} over {control_base[:12]} with only this task's test files applied passed "
            "every step, which means no test in this change exercises it: whatever the code now "
            "does, the suite would say the same if the code were not there.\n"
            "Add or fix a test that fails against the code as it was and passes against the code "
            "as it is. If this task genuinely cannot be tested that way — it changes no behaviour "
            "anything can observe — say so with `rein report --outcome needs-revision` and name "
            "the acceptance criterion that has no observable form, rather than writing a test that "
            "cannot fail."
        )

    def _red_without(self, label: str, base: str, patch: str, commands: Sequence[GateStep]) -> tuple[str, str]:
        """Apply `patch` (a test half) onto `base` alone and run `commands` there.

        `("discriminating", step)` when a step goes red — the tests fail against the code as it
        was; `("inert", names)` when every step stays green; `("undetermined", why)` when the
        experiment could not be run, which is evidence in neither direction. The one experiment
        both the task's negative control and a repair's proof take (`_verify_repair`).
        """
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False, encoding="utf-8") as handle:
            handle.write(patch)
            patch_file = handle.name
        try:
            with build_git.scratch_worktree(self.repo, self.config.worktree_dir, label, base, _late_run) as control_cwd:
                rc, out = _late_run(["git", "apply", patch_file], cwd=control_cwd)
                if rc != 0:
                    return "undetermined", f"the test half did not apply onto {base[:12]}: {out[-300:]}"
                for step in commands:
                    # `note=False`: this green is a fact about the control tree, not about the
                    # task's, and the task's `evidence.steps` is the list of what its own DoD
                    # established. The ledger still records it — it is a true fact about a real
                    # tree, keyed on that tree's fingerprint, so it can never be mistaken for one
                    # about the task's.
                    if self._run_cmd_step(step, control_cwd, note=False):
                        return "discriminating", step.name
        finally:
            Path(patch_file).unlink(missing_ok=True)
        return "inert", ", ".join(step.name for step in commands)

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
                continue  # prose only, and honest about it: acceptance is where a human reads it
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

    def _warm_reading(self, task: dag.Task) -> list[review_reading.ReadOut]:
        """Take acceptance's readings that this task's landing completes, now rather than at the gate.

        The gate reads the change in the readings `review_reading.plan_readings` derives — one per
        dependency chain, split task by task where one launch cannot hold a chain — and it asks each
        one the same question this does: same measure, same key. Answering it here means the gate
        finds it answered, and a review regenerated after a fix re-reads only the reading whose code
        moved. A reading is taken once **every** task in it has landed: a chain's reading is warmed
        when its last task lands, not once per task, because the gate would never look a partial
        chain up.

        **A warm-up never fails a build.** It is an optimization over a cache the gate does not
        depend on: if it does not happen, `rein review generate` takes the reading itself, at the
        cost this exists to avoid and with nothing else different. So an adapter that will not
        answer stops the warming for the rest of the run — retrying it once per task would spend a
        session limit on it — and says so once, rather than stopping the build.

        Skipped for a task with no declared scope: an undeclared scope means *unbounded*, so it is in
        no reading but the whole change's, which is not what the gate will ask for.

        **And skipped once the change is `critical`**, because the gate will not compose there:
        `review_reading.plan_readings` reads a critical change whole whatever the configuration
        says, so every reading warmed after that point is one nothing will ever look up. The risk is
        a property of the whole change and it only ever rises, so this stops the warming for the
        rest of the run the same way an unanswerable adapter does.

        **The `ReadOut`s are the point of the return type.** This launched a security reviewer and
        then threw its answer away: the finding was paid for here and first read at acceptance,
        by which time the code it names has been built on. `_repair_warm_findings` reads them. An
        empty list means no reading was taken — a skip, a chain still landing, or an adapter that
        would not answer — which is not the same as a reading that found nothing.
        """
        if self.dry_run or self._warming_off or self.config.raw.composition == review_reading.WHOLE:
            return []
        if not task.scope_include:
            return []
        if not (self.config.readings.actual_extraction or self.config.readings.security):
            # Nothing the gate reads is a launch: a reading taken here would warm no key.
            return []
        try:
            # The same base the gate will resolve, not the plan's field: they differ whenever the
            # plan names a commit this checkout does not have, and a warm-up taken against a
            # different base answers a question nobody asks.
            base = review_reading.resolve_base(self.repo, self._plan, None)
            head = self.repo._git_rc("rev-parse", "HEAD")[1].strip()
            if not base or not head:
                return []
            exclude = review_reading.not_the_product(self.repo, self.state)
            limits = {**human_review.DEFAULT_BUDGET, **self.config.raw.budgets}
            # One analysis of one whole diff answers all three: the floor the gate will key on,
            # whether the gate will compose at all, and which readings it will take.
            graph = self._load_graph()
            readings, risk_floor, effective = review_reading.change_readings(
                self.repo,
                self._plan,
                graph.tasks,
                base=base,
                head=head,
                exclude=exclude,
                mode=self.config.raw.composition,
                ceiling=int(limits["max_diff_bytes"]),
            )
            if models.risk_at_least(effective, "critical"):
                self._warming_off = True
                print(
                    f"    [review] {task.id}: the change is {effective} — acceptance reads it whole, "
                    "so no reading is warmed from here on"
                )
                return []
            landed = {t.id for t in graph.tasks if t.status in _LANDED_STATUSES}
            due = [r for r in readings if task.id in r.members and set(r.members) <= landed]
            return [
                review_reading.warm(
                    self.repo,
                    review_transport.StagedReviewers(self.repo, config=self.config.raw, readings=self.config.readings),
                    reading=reading,
                    base=base,
                    head=head,
                    exclude=exclude,
                    limits=limits,
                    evidence=self._plan.artifact_paths if self._plan is not None else (),
                    risk_floor=risk_floor,
                    host_surface=review_reading.host_surface_digest(self.repo, head),
                    config=self.config.raw,
                    cache=review_cache.StageCache(self.repo.root),
                    readings=self.config.readings,
                )
                for reading in due
            ]
        except (
            review_policy.ReviewPolicyError,
            review_transport.TransportError,
            common.ReinError,
            OSError,
        ) as exc:
            self._warming_off = True
            print(f"    [review] {task.id}: the acceptance reading was not taken here ({exc}); the gate will take it")
            return []

    def _repair_warm_findings(
        self, task: dag.Task, readouts: Sequence[review_reading.ReadOut], *, at_tip: bool
    ) -> bool:
        """Hand `task` the open security findings its readings found in its own code — at the tip only.

        **The readings were already taken and already paid for** (`_warm_reading`); until now their
        answers were written to the stage cache and read by nobody. So a security finding about
        code a task wrote was first seen at acceptance — after every later task had been built on
        top of it. Reading it here costs nothing that was not already spent, and the judge is still
        a different one: the finding comes from the security reviewer's own launch, validated by
        `security_review.run_security_review`, and the fixer is an implementer.

        **Only a finding `task` owns, and only while `task`'s own work is the tip of the branch.** A
        repair here is committed on top of the work branch, and a pull-request stack is cut along
        the tasks' `completed_commit`s: whatever sits above a task's commit belongs to the slice of
        whichever task comes next. So the one task a repair here can be charged to is the one whose
        work is directly under it — and it is charged by moving that task's `completed_commit` up
        to the repair. A finding about an earlier task of a chain, or about a leaf another leaf
        merged on top of, would land in somebody else's pull request; it is left to acceptance
        instead, where it is read again from the same cache and `_repair` puts it on the slice that
        introduced the code. Attribution is `findings.owner_of_path` over the effective graph — the
        same function and the same task set acceptance routes by — so nothing is guessed.

        **One round, and no new knob for it.** The failure this has to survive is a false
        positive, where repairing converges on nothing; a single round at the task boundary is
        cheap and bounded, and whatever still stands is exactly what `review_policy.repair_rounds`
        is for. Whether the finding closed is not this launch's account of itself either: the
        re-warm below reads the slice again from cold, and acceptance reads it once more after that.

        True when it repaired, which is the caller's cue to write `task`'s status again with the
        repaired tip as its commit: the evidence beside it has been re-pointed at the repaired tree.
        """
        if not readouts or not at_tip or self.dry_run:
            return False
        tasks = self._load_graph().tasks
        owned: list[findings_mod.Attribution] = []
        for readout in readouts:
            for finding in readout.security.findings:
                # Every open one, whatever its severity: severity decides whether a finding holds
                # acceptance shut, not whether code a task owns gets repaired (`repair.route`).
                if finding.get("status") in {"resolved", "disputed"}:
                    continue
                paths = [
                    str(anchor.get("path", ""))
                    for anchor in (finding.get("code_anchors") or [])
                    if isinstance(anchor, Mapping) and anchor.get("path")
                ]
                hit = next((p for p in paths if findings_mod.owner_of_path(tasks, p) == task.id), "")
                if hit:
                    owned.append(findings_mod.Attribution(str(finding.get("id", "SEC-?")), "security", task.id, hit))
        if not owned:
            return False
        print(
            f"    [review] {task.id}: the security review found {len(owned)} finding(s) in its own "
            "scope — repairing them here rather than at acceptance"
        )
        self._repair(task, repair_mod.Repair(task.id, tuple(owned)), where="review")
        # The repair moved the reading's content, so the answer just cached is about a tree that no
        # longer exists and the gate would re-read it regardless. Reading it again here is what
        # decides whether the finding closed — from cold, by a reader with no memory of having
        # raised it — and it leaves the gate's cache warm rather than stale.
        self._warm_reading(task)
        return True

    def _completion_status(self, task: dag.Task) -> str:
        """`done`, or `awaiting-evidence` when a criterion nobody here can establish is still open.

        Asked **after the work has landed on the work branch**, not while it sat in a worktree,
        and that placement is the whole design. An external criterion is something a person has
        to go and look at — a staging deployment, a device, a screen — and none of that is
        observable about code that only exists on an unmerged leaf branch. So the code merges
        (it passed the entire DoD; nothing about it is in question) and only the *task* waits,
        which is enough: acceptance cannot open while a task is not done.

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

    def _record_task_evidence(self, task: dag.Task, cwd: str) -> None:
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
        with self._evidence_lock:
            self._evidence[task.id] = record

    def _read_report(self, task: dag.Task) -> dict[str, Any]:
        """What the implementer said about this attempt, through `rein report`. {} when it said nothing."""
        if self.dry_run:
            return {}
        report = self._handoff_for(task).get("report")
        return dict(report) if isinstance(report, dict) else {}

    def _check_implementer_output(self, task: dag.Task, cwd: str) -> tuple[str, str]:
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

        The empty-diff check needs a resolved scope to mean anything: an unresolved one (a dry
        run) is read as "not known", never as "nothing" — a fail-open the gate itself already
        takes for its `paths:` filtering.
        """
        report = self._read_report(task)
        outcome = str(report.get("outcome", ""))
        if outcome in {"blocked", "needs-revision"}:
            summary = str(report.get("summary", "")).strip() or "(no summary given)"
            return f"agent_{outcome.replace('-', '_')}", f"{task.id}: the implementer reported {outcome} — {summary}"

        changed, diff_cmd = self._review_scope(task, cwd)
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
                "belongs to another task, or the plan drew this one's scope too small."
                + self._scope_way_out(task.id, outside),
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
          (the baseline the mandate froze). Sending an implementer back to fix a break it did not cause, in
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

    def _scope_way_out(self, task_id: str, outside: Sequence[str]) -> str:
        """The sentence that ends a scope violation: how the task gets past it.

        A path `guard.scope_additions` already allows is one the human decided ahead of time may be
        added without re-deciding the mandate, and `rein task scope-add` is that addition. This
        sentence used to name the roll back for every path, and a field cycle took one — a
        re-approval of the whole mandate — for a test file one task's scope was short of.
        """
        commands = status_api.scope_additions_for(
            self.store.read_state(), self.store.read_plan(), self.store.read_config(), task_id, outside
        )
        if commands:
            return (
                " Every path is inside `guard.scope_additions`, so no roll back is needed: "
                + "; ".join(f"`{c}`" for c in commands)
                + f", then `rein task reset {task_id} --reason ...`."
            )
        return (
            " The second is answered with `rein revise --to mandate`, widening `scope.include`, and a "
            "re-approval, never by editing the frozen plan in place."
        )

    def _stop_before_the_gate(
        self, task: dag.Task, kind: str, message: str, *, tree: str, futile: str = "", paths: Sequence[str] = ()
    ) -> None:
        """Record an attempt that ended before the quality gate, and the status that ending calls for.

        `_escalate`'s counterpart for this one path, and it replaces it rather than joining it: the
        `knowledge_gap` a human reads and the record the next attempt inherits are one write, so a
        terminal killed between them cannot leave an escalation in the chain with nothing saying
        what the next run already knows.
        """
        logger.warning(f"[escalation] {message}")
        if not self.dry_run and self.cycle_id:
            record_escalation(self.repo, task.id, kind=kind, message=message, tree=tree, futile=futile, paths=paths)
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
        if kind == "scope_violation":
            # The verdict was about the scope as much as the tree. A scope widened since (`rein task
            # scope-add`) is new input over the same tree, and replaying the old answer would refuse
            # the very addition that answered it.
            current = next((t for t in self._load_graph().tasks if t.id == task.id), task)
            paths = [str(p) for p in recorded.get("paths", []) if isinstance(p, str)]
            if paths and not dossier.scope_violations(current, paths):
                return None
        return kind, str(recorded.get("message", "")), tree

    def _run_task_to_done(self, task: dag.Task, cwd: str, review: str = "") -> tuple[bool, str]:
        """Take one task to done via implementer implementation + the quality-gate pipeline.

        `review` is a batch reviewer's `must_fix` findings about a change that already passed
        (`_review_batch`). They arrive through this same send-back: the implementer resumes the
        session that wrote the change, and the whole deterministic gate is established again over
        whatever it does about them.

        Each cmd step carries its own send-back budget (step.retries); a failure consumes only
        that step's budget. Returns (ok, log); ok=False means some step's budget ran out
        (the caller marks the task blocked).

        **The gate is not the first question asked.** What the implementer produced is checked
        first (`_check_implementer_output`), because a DoD that passes over an empty diff is not
        evidence about this task, and because running a reviewer and a full test suite against an
        attempt that already said "I am blocked" spends a model on a question nobody asked.
        """
        budgets = {s.name: s.retries for s in self._steps_for(task, cwd) if s.kind == "command"}
        # Every other verdict `_run_pipeline` can return. Neither the negative control nor an
        # acceptance criterion is a configured step, and both come back through this channel, so
        # without an entry here each one got the silent zero `.get(name, 0)` produced.
        budgets[NEGATIVE_CONTROL] = SEND_BACK_RETRIES
        for step in task.operate:
            budgets[f"{_OPERATE_PREFIX}{step.get('name', '?')}"] = SEND_BACK_RETRIES
        if task.operate:
            # A task that operates is read before its run (`_review_before_operate`), so a reader's
            # verdict comes back through this channel on the step's own `retries`.
            for reader in self._batch_review_steps():
                budgets[f"{_REVIEW_PREFIX}{reader.name}"] = max(0, reader.retries)
        # How many times this call has started the task's priced run (`_count_priced_run`).
        operated = 0
        for entry in task.acceptance:
            budgets[f"{_ACCEPTANCE_PREFIX}{entry.get('id', '?')}"] = SEND_BACK_RETRIES
        # What an earlier, interrupted attempt left behind. Restoring the budgets is the load-
        # bearing half: a run killed mid-task and restarted otherwise came back with a full
        # allowance every time, so a task that can never pass could burn retries forever.
        handoff = self._handoff_for(task)
        # The failure fields describe one failure (`_FAILURE_FIELDS`): a gate step's log, or the
        # verdict an attempt stopped on before the gate — whichever came last is the one recorded.
        recorded = handoff.get("escalation")
        escalation: Mapping[str, Any] = recorded if isinstance(recorded, Mapping) else {}
        failure_log = "" if review else str(handoff.get("failure_summary") or escalation.get("message") or "")
        inherited = handoff.get("retries_left")
        if isinstance(inherited, dict):
            budgets = {name: min(left, inherited.get(name, left)) for name, left in budgets.items()}
        if failure_log:
            stopped = handoff.get("failed_step") or escalation.get("kind") or "?"
            print(f"    [handoff] {task.id}: resuming after '{stopped}'")
        # Retry-session continuity: the implementer resumes its own session across its retries. A
        # step's final retry is forced fresh — a resumed session re-reads its own failed reasoning,
        # and the last attempt deserves an unanchored mind working from the compact failure summary
        # alone. The review agent step is never resumed (independence).
        #
        # Who names the session decides what "fresh" costs. A CLI told an id (claude) gets a new
        # uuid; a CLI that names its own (codex) gets "" and is handed back whatever id its next
        # launch opens. Both are continuity — the distinction is only about *when* the id exists.
        continuity = self._resume_capable and not self.dry_run
        mints_own = self._implementer_mints_its_own_session
        session = str(uuid.uuid4()) if continuity and not mints_own else ""
        resume = False
        # The first launch may open that session as a branch of the one that finished this task's
        # upstream; every later round resumes this task's own session as before.
        fork = self._inherited_session(task) if session and not review else ("", "")
        if review and continuity and (adapter := self._implementer_adapter) is not None:
            # The session that wrote the change is the one that knows why it is the way it is.
            own = self.sessions.get(self.cycle_id, task.id, adapter.name)
            if own:
                session, resume = own, True
        # (step, failure digest, tree fingerprint) of the previous round — what `_futile` compares
        # this round against. "" for the fingerprint means "unknown", which never matches.
        seen: tuple[str, str, str] = ("", "", "")
        # A review is new input over the tree the last verdict was reached on, so that tree having
        # been answered already says nothing about this round.
        answered = None if review else self._already_answered(task, cwd)
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
            session = self._invoke_implementer(
                task, cwd, failure_log, session=session, resume=resume, fork=fork, review=review
            )
            fork, review = ("", ""), ""
            if not self.dry_run:
                changed, _ = self._review_scope(task, cwd)
                violations = self._gate_violations(changed)
                if violations:
                    raise GateViolationFault(violations)
                kind, message = self._check_implementer_output(task, cwd)
                if kind:
                    # Not a gate failure, so it spends no step's budget: no step ever ran. The
                    # attempt is over, and the reason — which the loop now actually holds — goes
                    # to the human rather than being reconstructed from an unchanged tree. The
                    # tree goes with it, so the next `rein build` can tell "try again" from
                    # "ask the same question a second time".
                    outside = dossier.scope_violations(task, changed) if kind == "scope_violation" else []
                    self._stop_before_the_gate(task, kind, message, tree=self._fingerprint(cwd), paths=outside)
                    return False, message
            # What the implementer produced is committed before anything reads it: the gate, the
            # reviewer and the merge then read one thing, the branch, and "the change" is never
            # two different trees depending on who asks (`_commit_attempt`).
            if not self._commit_attempt(task, cwd):
                return False, f"{task.id}: its change could not be committed; the worktree is kept as it is"
            self._local.steps, self._local.acceptance, self._local.negative_control = [], [], {}
            after_implementer = self._fingerprint(cwd)
            failed, failure_log = self._review_before_operate(task) if task.operate else (None, "")
            if failed is None and task.operate and not self.dry_run:
                if operated and (spent := self._count_priced_run(task)):
                    self._stop_before_the_gate(task, "attempt_budget_spent", spent, tree=after_implementer)
                    return False, spent
                operated += 1
            if failed is None:
                failed, failure_log = self._run_operate(task, cwd)
            if failed is None and task.operate and not self.dry_run:
                # What an operate step wrote lands with the task (`finalize_commit` adds the whole
                # tree), and the scope check above read the tree before the step ran. Checked
                # again here, or a run's output lands anywhere the step chose to put it.
                changed, _ = self._review_scope(task, cwd)
                if violations := self._gate_violations(changed):
                    raise GateViolationFault(violations)
                if outside := dossier.scope_violations(task, changed):
                    message = (
                        f"{task.id}: its operate steps wrote {', '.join(outside)}, which its declared scope "
                        f"does not cover (include={list(task.scope_include)}, exclude={list(task.scope_exclude)}). "
                        "Either the steps write to the wrong place, or the plan drew the task's scope without "
                        "the run's output." + self._scope_way_out(task.id, outside)
                    )
                    self._stop_before_the_gate(
                        task, "scope_violation", message, tree=self._fingerprint(cwd), paths=outside
                    )
                    return False, message
                if not self._commit_attempt(task, cwd):
                    return (
                        False,
                        f"{task.id}: what its operate steps wrote could not be committed; the worktree is kept",
                    )
            if failed is None:
                failed, failure_log = self._run_pipeline(task, cwd)
            if failed is None:
                self._record_task_evidence(task, cwd)
                if session and (adapter := self._implementer_adapter) is not None:
                    self.sessions.put(self.cycle_id, task.id, adapter.name, session)
                # Whatever failure the handoff described is over. Left in place, it rode into every
                # stop that follows a green — a merge conflict, a red join — as that stop's cause.
                self._note_diagnostic(task.id, _FAILURE_RESOLVED)
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
            read = failed.startswith(_REVIEW_PREFIX)
            if read:
                # A reader's findings, not a red: the next launch is told them as findings to
                # resolve or dispute, never as a failure to make green — and by the session that
                # wrote the change, on every round. The fresh final retry below is for a step the
                # session keeps failing to make green; a finding is new input, which is how the
                # batch's send-back treats it too.
                review, failure_log = failure_log, ""
            if continuity and budgets[failed] <= 0 and not read:  # final retry for this step → fresh session
                session, resume = ("" if mints_own else str(uuid.uuid4())), False
            else:
                resume = bool(session)

    def _commit_attempt(self, task: dag.Task, cwd: str) -> bool:
        """Put everything the attempt left in the worktree on the task's branch. False = could not.

        The implementer is told to commit and sometimes does not, and the loop used to finalize
        only at the merge. Until then the branch and the worktree disagreed about what the change
        was: the gate read the worktree, a reviewer handed `git diff <target>...<branch>` read the
        branch, and a step's `paths:` filter asked the branch too — so work left uncommitted was
        tested and never read. Committing before the first reader makes the branch the change, for
        every reader, at every round. A tree that cannot be committed keeps its worktree
        (`GitWorkspace.cleanup_worktree` will not remove what it could not preserve).
        """
        return self.dry_run or self.ws.finalize_commit(cwd, f"{task.id}: {task.title}")

    def _review_before_operate(self, task: dag.Task) -> tuple[str | None, str]:
        """Read a task that operates **before** its priced run. `(None, "")` when every reader passed.

        `operate` is the long run a task exists for — hours on real data, or the irreversible act
        itself — and whatever changes the code after it has run makes its output a statement about
        code that is no longer there, so the run has to happen again. A reader's findings are the
        change most worth having *before* that run: read afterwards, each `must_fix` bought one
        more priced run, uncounted by the approval that priced it. So such a task is read here, on
        its own, by the same agent steps a batch is read by, and it is not read again with its batch
        (`_review_batch`): the tree that ran is the tree that was read, plus the run's own output,
        which the gate checks.

        A `must_fix` comes back as the verdict `review:<step>`, on the step's own `retries`, through
        the send-back a red step takes. An answer nobody can hold to anything stops the task; a
        reader the machine never ran raises, as every launch does.
        """
        if self.dry_run:
            return None, ""
        changed = self.ws.branch_changed_paths(task.id)
        for step in self._batch_review_steps():
            if not step.matches_paths(changed):
                continue
            answer = self._read_batch(step, [task])[task.id]
            if answer.fault is not None:
                raise answer.fault
            if answer.stop:
                raise StopLoop(answer.stop)
            if answer.send_back:
                return f"{_REVIEW_PREFIX}{step.name}", answer.send_back
        return None, ""

    def _count_priced_run(self, task: dag.Task) -> str:
        """Count one more start of `task`'s operate run against its approval. "" = it may start.

        `attempts.max` is what the approval priced — how many runs, each costing `attempts.cost` —
        and it was counted where it is cheapest to count, not where the run happens: once per
        `in-progress`, while one `in-progress` could start the run again after every red step and
        every reader's finding. A run the approval did not cover is the thing the budget exists to
        stop, and for an irreversible task each one is another crossing of the point a human
        approved once. So every start after the first in one attempt is recorded as an attempt of
        its own (`task_started`, `attempts` + 1) before it runs, and one past `max` is not started.
        """
        current = next((t for t in self._load_graph().tasks if t.id == task.id), task)
        if current.attempt_max and current.attempts >= current.attempt_max:
            return (
                f"{task.id} has used the {current.attempt_max} run(s) its approval covers "
                f"(each: {current.attempt_cost}); its operate steps are not started again. Raising the "
                f"budget changes what was approved: `rein revise --to mandate`, then change `attempts.max` "
                f"for {task.id}."
            )
        self._set_status(task.id, "in-progress")
        print(
            f"    [operate] {task.id}: run {current.attempts + 1}"
            + (f" of {current.attempt_max}" if current.attempt_max else "")
        )
        return ""

    def _run_operate(self, task: dag.Task, cwd: str) -> tuple[str | None, str]:
        """Run `task.operate` in order, in the task's worktree. (None, "") when all passed.

        This is the loop doing the long, deterministic part of a task itself. It used to be the
        implementer's, inside one agent turn: the turn ended while a multi-hour run was still going
        and took the child with it, a session limit parked the run for an hour at a time, and the
        only lever was prose in a reset reason telling the next agent to keep waiting.

        **Where a gate step runs, not on the host.** What a step runs is code the implementer just
        wrote, which is what `quality_gate_profile` exists to keep away from the operator's
        credentials. Running it as a host process handed every task with an `operate` block the one
        boundary the gate is configured to hold. A step that needs more names a profile that grants
        it (`executor_profile`), and that name is frozen with the plan the human approved.

        A step the machine could not run (not installed, killed from outside, no network) raises
        :class:`EnvironmentFault` and spends nothing — nothing is known about the code. A step that
        ran and failed is a verdict on what the implementer wrote, and goes back to it as
        `operate:<name>` with the log's tail, on that step's own budget.
        """
        if not task.operate or self.dry_run:
            if task.operate:
                print(f"    [dry-run] operate: {' → '.join(str(s.get('name')) for s in task.operate)}")
            return None, ""
        declared, _ = task_environment(task)
        # Under the main checkout, not the worktree: a worktree is deleted after its merge, and the
        # log of a run that took hours is the one record of it worth keeping past that.
        log_dir = self.repo.path(dossier.RELATIVE_PATH) / task.id / "operate"
        log_dir.mkdir(parents=True, exist_ok=True)
        for index, step in enumerate(task.operate, start=1):
            name = str(step.get("name", f"step{index}"))
            command = [str(part) for part in step.get("command", [])]
            timeout = step.get("timeout_sec")
            profile = self._operate_profile(task, step)
            log = log_dir / f"{index:02d}-{name}.log"
            where = f"{task.id}: operate {name}"
            print(f"    [operate] {task.id}: {name} — {' '.join(command)} in {profile.name!r} (log: {log})")
            spec = executors.ExecutionSpec(
                command=tuple(command),
                profile=profile,
                mounts=self._mounts_for(profile, cwd),
                env={**os.environ, **declared} if not profile.runs_contained else {},
                env_always=declared if profile.runs_contained else {},
                workdir=_SANDBOX_WORKDIR if profile.runs_contained else cwd,
                timeout_sec=float(timeout) if isinstance(timeout, int) else None,
            )
            started = time.monotonic()
            try:
                with common.Heartbeat(where):
                    result = executors.for_profile(profile).run(spec)
            except executors.ExecutorError as exc:
                raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=1, output=str(exc)) from exc
            rc, output = result.exit_code, result.output
            log.write_text(output, encoding="utf-8")
            print(f"    [operate] {task.id}: {name} exited {rc} after {int(time.monotonic() - started)}s")
            if rc == 0:
                continue
            fault = faults.classify_step(rc, output)
            if fault is not faults.Fault.CONTENT:
                raise EnvironmentFault(fault, where=where, rc=rc, output=output[-4000:])
            return f"{_OPERATE_PREFIX}{name}", (
                f"operate step '{name}' ({' '.join(command)}) exited {rc}. The full log is {log}; its tail:\n"
                f"{output[-4000:]}"
            )
        return None, ""

    def _operate_profile(self, task: dag.Task, step: Mapping[str, Any]) -> models.ExecutorProfile:
        """The profile an `operate` step runs in: its own if it names one, else the quality gate's."""
        named = str(step.get("executor_profile", ""))
        if not named:
            return self._profile_for(GateStep(name=f"{task.id} operate", kind="command"))
        if profile := self.config.raw.profiles.get(named):
            return profile
        raise common.ReinError(
            f"{task.id}'s operate step {step.get('name')!r} names executor_profile {named!r}, "
            "which is not in executor_profiles"
        )

    # -- post-merge integration gate --

    def _integration_fix_prompt(self, ids: str, failure_log: str) -> str:
        return build_prompts.integration_fix_prompt(
            ids, failure_log, gate_cmds=self.config.gate_cmds, pathspec=self.ws.pathspec
        )

    def _invoke_integration_fixer(self, tasks: Sequence[dag.Task], prompt: str) -> None:
        """One implementer launch over the merged tree. The caller says what it is being sent.

        The prompt is the caller's because the join has two send-backs and they are not the same
        work: a red command step, and a reviewer's findings about what only the join shows. Both
        used to be framed as "the combined state fails the deterministic gate", which was true of
        one of them (`integration_fix_prompt`, `integration_review_fix_prompt`).

        What it changed is committed onto the join before anything reads the tree again. The fixer
        is told to commit and nothing checked: the gate re-ran over the working tree, so a green
        join could stand on edits HEAD did not have — the next leaf forked without them — and a red
        one could not be taken off, `reset --keep` refusing over the fixer's uncommitted paths.
        """
        ids = ",".join(t.id for t in tasks)
        self._launch(
            adapters.command(self.config.adapter_argv, prompt, access=adapters.WRITE),
            cwd=self.root,
            where=f"{ids}: the integration fixer",
            role="implementer",
        )
        if not self.ws.finalize_commit(self.root, f"{ids}: integration fix", subjects=[t.id for t in tasks]):
            raise StopLoop(f"{ids}: the integration fixer's change could not be committed; the tree is kept as it is")

    def _integration_gate(self, tasks: list[dag.Task], before_join: str) -> tuple[bool, str]:
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
            reread = bool(self._resolved_on_merge & {t.id for t in tasks})
            for step in self._steps_at("integration"):
                if step.kind == "agent" and step.stage == "both" and not reread:
                    # The batch's reviewer read every leaf before the merge, and a merge that resolved
                    # no conflict is exactly the union of what it read. Reading it again is the
                    # second launch over one diff that this step used to cost.
                    continue
                if step.kind == "agent":
                    # `stage:` moves *when* a step runs, never whether — and an agent step declared
                    # at the integration stage was being skipped, which made the join the one tree
                    # no reviewer ever read. The command steps have just run over it; this is the
                    # half of the question they cannot answer.
                    self._run_integration_agent_step(step, tasks, before_join)
                    continue
                if not step.command:
                    continue
                failure = self._attribute_red(
                    tasks, step, self.root, before_join, self._run_cmd_step(step, cwd=self.root)
                )
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
            self._invoke_integration_fixer(tasks, self._integration_fix_prompt(ids, failure_log))

    def _run_integration_agent_step(self, step: GateStep, tasks: Sequence[dag.Task], before_join: str) -> None:
        """Read the tree the merge produced, which no per-task reviewer ever saw.

        **`before_join` is what makes "the join" a real subject.** The diff this reviewer is
        pointed at used to start at `plan.base_commit`, which is the base of the *cycle*: every
        batch after the first re-read every batch before it, at full price, and every finding
        about code that landed two batches ago came back again. Worse, the attribution below only
        knows this batch's tasks — so a finding about an earlier one matched no owner, was printed
        instead of filed, and never reached the human. The join is the commits these merges added,
        and that is the range.

        Its `must_fix` findings go back to the integration fixer within this step's own retries,
        the same shape a red command step takes; unresolved ones stop the batch rather than being
        reported as passed. Its `question` findings are attributed to the merged task whose
        declared scope owns the anchor — the same derivation acceptance uses to decide which task
        answers a finding (`findings.owner_of_path`) — so they reach the human through
        `brief.residual_findings` beside that task's own review, stamped with the tree they were
        made against. A finding no task's scope owns is printed rather than filed against a task
        that does not own it: acceptance's seam reading covers exactly that region, and inventing an
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
                        diff_cmd=f"git diff {before_join}..HEAD",
                        findings_path=findings_rel,
                        reviews=step.reviews,
                        questions=self.config.questions,
                        disciplines=adapters.disciplines_for(step.agent_argv or self.config.adapter_argv),
                        lenses_applied=self._code_lenses[0],
                        lenses_proposed=self._code_lenses[1],
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
            self._event(
                "reviews_applied",
                [t.id for t in tasks],
                {"step": step.name, "stage": "integration", "reviews": list(step.reviews)},
            )
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
                tasks,
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

    def _safe_run_task(self, task: dag.Task, cwd: str, review: str = "") -> LeafOutcome:
        """Call _run_task_to_done safely from a thread, so one leaf cannot strand the batch.

        A `StopLoop` becomes a failed verdict; an `EnvironmentFault` is carried out **as itself**
        so the caller can tell "this leaf's code did not pass" from "no one ever asked this
        leaf's code anything". Flattening the second into the first is what marked tasks blocked
        for a rate limit.
        """
        try:
            ok, log = self._run_task_to_done(task, cwd=cwd, review=review)
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
            f"{task_id}: {where} touches paths the gate guard refuses, for the reason given beside each — "
            f"the task is blocked for human review (gate rule 3: never land next-phase edits silently).\n{listing}",
            task=task_id,
        )

    def _block_for_gate_violation(self, task_id: str, where: str, violations: list[tuple[str, str]]) -> None:
        """Block `task_id`: it touched a path the gate guard refuses.

        Shared by every place that runs this same check — right after an attempt's implementer
        (`_run_task_to_done`) and the merge-time check — so there is one call site for "early"
        and one for "final" rather than separate copies of set-status-and-escalate.
        """
        self._set_status(task_id, "blocked")
        self._escalate_gate_violation(task_id, where, violations)

    def _cleanup_worktree(self, task: dag.Task) -> None:
        self.ws.cleanup_worktree(task.id)

    def _load_landing(self) -> None:
        """Which tasks already have an open pull request, and on which branch their work belongs.

        Read from the audit log, not configured: `pr-stack` records every pull request it opens,
        and that record is what says a task's next commit belongs on a slice branch rather than on
        the work branch. A slice already *ready* is excluded — past acceptance a change is a human's
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
            # The merged tree now holds code the batch's reviewer was never shown.
            self._resolved_on_merge.add(task.id)
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
        "unresolved escalation" on acceptance's screen for a machine's bad afternoon, in a log that
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
        if self.state.gate_status("mandate") != "approved":
            logger.error(
                "the mandate is not approved, so there is no frozen plan to build against. "
                "Finish /tasks and get the plan approved first."
            )
            return common.EXIT_CANNOT_PROCEED
        if self.state.plan_status != "frozen":
            logger.error(
                f"the plan is '{self.state.plan_status}', not 'frozen'. The mandate's approval freezes it; "
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
        if problems := self._source_problems():
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
                    if refusal := self._tree_refusal():
                        return refusal
                    self._close_repair_grant(
                        reason="a grant left open when this run started — the run that opened it never closed it"
                    )
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

    def _tree_refusal(self) -> int:
        """`EXIT_CANNOT_PROCEED` when the working tree is not this run's to measure; 0 to proceed.

        **Asked inside the build lock, on purpose.** A dirty tree and a run already in progress are
        the same picture from outside — the other run's implementer is editing the root *right now*
        — so asking before the lock would tell the second `rein build` to commit or stash the
        first one's live work, and to do it under "cannot proceed" where the honest answer is
        "another run holds the repository, retry". The lock answers first; what is uncommitted
        underneath it belongs to nobody but this run.

        A git that cannot answer raises rather than reporting a clean tree
        (`build_git.GitWorkspace._lines_of`), and the raise is handled here because this is the
        first thing the locked section does — before the batch loop that handles the rest.
        """
        try:
            problems = self._tree_problems()
        except StopLoop as exc:
            logger.error(str(exc))
            return exc.code
        for problem in problems:
            logger.error(problem)
        return common.EXIT_CANNOT_PROCEED if problems else 0

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
        """Why the prose this build would read is not the prose the mandate approved.

        A ticket edited after the freeze changes what gets built, and `plan.yaml` being
        digest-frozen said nothing about it. Refusing here is the same posture `rein guard`
        already takes towards a frozen document — the way forward is `rein revise --to mandate`
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
                f"{len(moved)} document(s) the build reads changed since the mandate froze them: "
                f"{', '.join(moved)}. Building now would implement text nobody approved — "
                "roll back with `rein revise --to mandate`, re-approve, and run again."
            ]
        return []

    def _tree_problems(self) -> list[str]:
        """Why this run's merges would meet work nobody committed.

        Every task forks from the work branch's last commit and lands by a merge into this
        checkout. An uncommitted change here is in no task's fork, so no gate ever ran over it,
        and a merge that touches the same path is refused by git after the task has been paid for
        and passed. Refused before the first launch instead, where committing or stashing costs
        nothing.

        `.rein/` and the leaf worktree root are excluded, as they are everywhere else
        (`build_git.GitWorkspace.excluded`): neither is any task's work.
        """
        if self.dry_run:
            return []
        dirty = self.ws.dirty_paths(self.root)
        if not dirty:
            return []
        shown = ", ".join(dirty[:_DIRTY_PATHS_SHOWN])
        if len(dirty) > _DIRTY_PATHS_SHOWN:
            shown += f", and {len(dirty) - _DIRTY_PATHS_SHOWN} more"
        return [
            f"{len(dirty)} uncommitted change(s) in the working tree: {shown}. Every task forks from "
            f"the last commit on `{self.branch}` and is merged back into this checkout, so these are "
            "in no task's tree and a merge that touches them fails after the task passed. Commit "
            f"them on `{self.branch}` or stash them, then run again."
        ]

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

    def measure_baseline(self) -> dict[str, Any]:
        """Run the task-stage DoD over the work branch's tip and return the record to freeze.

        The reported case: a run's tasks stopped, one after another, on a `check` that a
        dependency drift had broken weeks earlier. Each one spent its whole send-back budget —
        three implementer launches — on a failure it had not caused and could not have fixed
        within its own scope, and the audit chain recorded three `task_failed` verdicts about the
        code each of them wrote.

        The loop could not tell those apart from a real regression because it had never asked the
        one question that separates them: *was this step red before the task touched anything?*

        It is asked here, and **at the mandate rather than inside the build**. Taken just before the
        first batch, it was taken after the approval that had already decided this plan was
        implementable against this tree — so a cycle could be approved and started on a tree that
        had been red for weeks, and the discovery was the first task's to make and to pay for. The
        gate is where a red step is either fixed or frozen as a deliberate, recorded decision.

        Answered by the same runner and the same evidence ledger the gate itself uses, so on an
        unmoved tree it is a cache hit and costs nothing.

        Recording, not refusing. A cycle whose first task is "fix the failing tests" is a
        legitimate thing to start, and the implementer runs *before* the gate: if it fixed the
        step, the step goes green and none of this applies. What a frozen red changes is only what
        happens when it is still red — the loop stops rather than buying the same failure three
        more times.
        """
        red: list[dict[str, str]] = []
        for step in self._steps_at("task"):
            if step.kind != "command" or not step.command:
                continue
            failure = self._run_cmd_step(step, self.root)
            if failure:
                red.append({"name": step.name, "failure": failure[:4000]})
        return {
            "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "tree_digest": self._fingerprint(self.root),
            "red_steps": red,
        }

    def _load_baseline(self) -> None:
        """Read the baseline the mandate froze. Refuse the run when there is none to read.

        A pure read: the measurement belongs to the gate, and re-taking it here would measure
        whatever tree this run happens to be resuming into — a tree with tasks already landed in
        it, which is not a baseline at all.
        """
        if self.dry_run or self._baseline_taken:
            return
        self._baseline_taken = True
        recorded = self.state.baseline if self.state else {}
        if not recorded:
            raise StopLoop(
                "no baseline is recorded for this work branch, so a step that was already red "
                "cannot be told apart from one this run broke. Measure it with "
                "`rein baseline measure` — the mandate is what it belongs to.",
                code=common.EXIT_HUMAN_NEEDED,
            )
        self._baseline_red = dict(self.state.baseline_red() if self.state else {})
        tree = self._fingerprint(self.root)
        moved = tree and recorded.get("tree_digest") and tree != recorded["tree_digest"]
        if self._baseline_red:
            print(
                f"    [baseline] frozen red at the mandate: {', '.join(sorted(self._baseline_red))}"
                + (" (measured over a tree this branch has since moved past)" if moved else "")
            )

    def _run_loop(self) -> int:
        """Consume the DAG, then close acceptance. One handler for both, which is the point.

        `StopLoop` and `EnvironmentFault` used to be caught per batch, inside the `while` — so the
        two calls that are *not* in a batch, `_load_baseline` and `_close_gate4`, had nowhere to
        land. `common.StopLoop` is not a `common.ReinError`, so `cli.main` does not catch it
        either: a build with no recorded baseline reported the sentence it had been given to say
        as a traceback and exit 1, instead of that sentence and `EXIT_HUMAN_NEEDED`. The boundary
        belongs where the run ends, not where one batch does.
        """
        try:
            return self._consume()
        except StopLoop as exc:
            logger.error(str(exc))
            return exc.code
        except EnvironmentFault as fault:
            # Never start the next batch into the same broken environment: whatever stopped this
            # launch would stop the next one, one wasted task at a time. The same answer serves
            # acceptance, where no task is running and there is nothing to mark either way.
            return self._abort_run(fault)

    def _consume(self) -> int:
        self._recover_in_progress()
        while True:
            self._stop_if_over_ceiling()
            graph = self._load_graph()
            if self._promote_observed(graph):
                graph = self._load_graph()
            counts = graph.counts()
            unfinished = len(graph.tasks) - counts["done"]
            if unfinished == 0:
                return self._close_gate4(graph)

            if self._observe_premises(graph):
                continue  # an observation may have swapped criteria or parked tasks: re-read the graph
            settled, owed = self._settle_person_tasks(graph)
            if settled:
                continue  # a deliverable landed: its dependents may be on the frontier now
            owed.update(self._unmet_on_frontier(graph))
            provisional = self._resting_on_the_unobserved(graph)
            # The approval priced a number of launches; one more is a spend nobody approved. Held
            # back like the rest rather than dropped from a batch already cut: a batch of one (a
            # foundation, or `max_parallel: 1`) would otherwise stop the run with independent work
            # still startable, and a larger one would name the same task again on every pass.
            spent = [t for t in graph.frontier() if t.attempt_max and t.attempts >= t.attempt_max]
            runnable = graph
            if owed or provisional or spent:
                # Off the frontier for this run, not off the plan: nothing about them is a verdict,
                # and the next `rein build` asks again.
                held_back = set(owed) | provisional | {t.id for t in spent}
                runnable = dag.Graph.from_tasks(
                    [replace(t, status="blocked") if t.id in held_back else t for t in graph.tasks]
                )
            batch = plan_batch(runnable, self.config.max_parallel)
            if batch is None and spent:
                self._report_spent(spent)
                if not owed:
                    return common.EXIT_HUMAN_NEEDED
            if batch is None and owed:
                return self._present_owed(graph, owed)
            if batch is None:
                # frontier empty & there are unfinished ones = all blocked/needs-revision. To the human.
                # With the one command that moves it, taken from the same table `rein next` reads:
                # "help needed" and nothing else is what sent a reported cycle round reset, salvage
                # and re-approval until one of them happened to be the right move.
                blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
                recovery = status_api.blocked_recovery(self.store.read_state())
                self._escalate(
                    "no_runnable",
                    f"No runnable tasks and {unfinished} unfinished ({', '.join(blocked)})."
                    + (f"\n{recovery.reason}\n  {recovery.command}" if recovery else " Help needed."),
                )
                return common.EXIT_HUMAN_NEEDED

            mode, tasks = batch
            waiting = [t for t in tasks if self._awaits_crossing(t.id)]
            if waiting:
                # Everything else in the batch first: a contact point stops the work that has to
                # cross it, not the work beside it. The presentation happens when nothing runnable
                # is left, which is also what makes it happen once per run.
                tasks = [t for t in tasks if t.id not in {w.id for w in waiting}]
                if not tasks:
                    self._report_spent(spent)
                    return self._present_crossings(waiting)
            # Here rather than at the top of the run: a `rein build` that finds every task done goes
            # straight to acceptance, and a full gate run at the root to answer a question no task is
            # going to ask is exactly the waste this exists to end.
            self._load_baseline()
            print(f"[batch] mode={mode} tasks={[t.id for t in tasks]}")
            self._consume_batch(tasks)
            # Recompute at the top of the loop after each batch (reassemble the chain).

    def _report_spent(self, spent: Sequence[dag.Task]) -> None:
        """Name each task whose approved launches are used up. Called on the way out of a run —
        when nothing else is left to start, the same moment a crossing is presented — so once."""
        for task in spent:
            self._escalate(
                "attempt_budget_spent",
                f"{task.id} has used the {task.attempt_max} launch(es) its approval covers "
                f"(each: {task.attempt_cost}). Not launched again. Raising the budget changes what "
                f"was approved: `rein revise --to mandate`, then change `attempts.max` for {task.id}.",
                task=task.id,
            )

    # -- premises nobody has measured (CR-39) --

    def _unobserved(self) -> set[str]:
        state = self.store.read_state()
        observed = state.premises if state is not None else {}
        return {p.id for p in (self._plan.premises if self._plan else ()) if p.id not in observed}

    def _resting_on_the_unobserved(self, graph: dag.Graph) -> set[str]:
        """Frontier tasks with a criterion resting on a premise nobody has observed yet.

        Such a criterion is provisional, so a `done` it contributed to would be too: the task waits
        for the observation, and the probe runs as soon as its observer is done.
        """
        if self.dry_run:
            return set()  # a dry run observes nothing, so it cannot wait for an observation either
        unobserved = self._unobserved()
        return {t.id for t in graph.frontier() if set(t.assumes) & unobserved}

    def _observe_premises(self, graph: dag.Graph) -> bool:
        """Probe every premise whose observer is done (or that has none) and is not yet observed.

        True when anything was recorded. The probe is run by the loop, not by an implementer, so
        the observation is not the implementer's account of it. A falsified premise with a fallback
        the human approved with the mandate is applied, and nothing stops. One without a fallback
        parks only the tasks whose criteria rest on it and asks a person about those — the rest of
        the plan, its state and its receipts stand.
        """
        if self.dry_run or self._plan is None:
            return False
        recorded = False
        unobserved = self._unobserved()
        for premise in self._plan.premises:
            if premise.id not in unobserved:
                continue
            if premise.observed_by and not graph.get(premise.observed_by).is_done:
                continue
            failure = self._probe(
                GateStep(
                    name=f"premise {premise.id}",
                    kind="command",
                    command=premise.probe,
                    executor_profile=premise.executor_profile,
                )
            )
            held = not failure
            fallback = bool(premise.fallback_criteria)
            resting = [t.id for t in graph.tasks if premise.id in t.assumes and t.status not in ("done", "in-progress")]
            park = [] if held or fallback else resting
            record_premise(self.repo, premise.id, held=held, output=failure, fallback=fallback, park=park)
            recorded = True
            if held:
                print(f"  [premise] {premise.id} holds: {premise.says}")
            elif fallback:
                print(
                    f"  [premise] {premise.id} is false ({failure}) — applying the fallback approved with the mandate"
                )
            elif not park:
                # Nothing unfinished rests on it, so nobody has anything to decide: recorded, not asked.
                print(f"  [premise] {premise.id} is false ({failure}), and nothing unfinished rests on it")
            else:
                self._escalate(
                    "premise_falsified",
                    f"{premise.id} is false: {premise.says}\n  {failure}\nThe plan approved no fallback for it, so "
                    "the criteria resting on it cannot be met as written. "
                    f"Parked: {', '.join(park)}. Everything else continues. Change what "
                    f"rests on it with `rein revise --to mandate --impacted {','.join(park)}`; the re-approval "
                    "shows only what changed.",
                    task=park,
                )
        return recorded

    # -- what only a person can provide (CR-38) --

    def _unmet_preconditions(self, task: dag.Task) -> list[str]:
        """Each of `task`'s preconditions that does not hold right now, as the line a person reads.

        Asked before every launch and never cached: a browser that was up an hour ago says nothing
        about now, and a ledger keyed on the tree would carry that hour forward. A dry run asks
        nothing — it launches nothing either.
        """
        if self.dry_run:
            return []
        declared, unresolved = task_environment(task)
        unmet: list[str] = [
            f"environment.env {name} refers to a variable this machine does not set — set it, or change the plan"
            for name in unresolved
        ]
        probes = [
            GateStep(
                name=f"{task.id}:requires[{index}]",
                kind="command",
                command=tuple(str(part) for part in requirement.get("probe", [])),
                executor_profile=str(requirement.get("executor_profile", "")),
            )
            for index, requirement in enumerate(task.requires)
            if not requirement.get("file")
        ]
        # Expanded here, on this machine, and handed as-is to whatever runs contained: `$HOME/x` there
        # names a directory of the host that the sandbox does not have, and the work fails as if the
        # code were wrong. A literal value means the same thing in both places.
        if task.env and (contained := self._contained_env_users(task, probes)):
            unmet += [
                f"environment.env {name} expands from this machine's environment ({value!r}), but "
                f"{', '.join(contained)} run(s) contained, where that value names nothing — declare a "
                "literal value, or change the plan"
                for name, value in task.env
                if name not in unresolved and (_ENV_REF.search(value) or value.startswith("~"))
            ]
        pending = iter(probes)
        for requirement in task.requires:
            says, path = str(requirement.get("says", "")), str(requirement.get("file", ""))
            if path:
                if not self.repo.path(path).exists():
                    unmet.append(f"{says} ({path} does not exist)")
                continue
            if failure := self._probe(next(pending), env=declared):
                unmet.append(f"{says} ({failure})")
        return unmet

    def _contained_env_users(self, task: dag.Task, probes: Sequence[GateStep]) -> list[str]:
        """What receives `task`'s declared environment inside a sandbox: its implementer, its
        `operate` steps, its probes — each named with its profile."""
        users: list[str] = []
        agent = self.config.raw.agent_profile
        if agent is not None and agent.runs_contained:
            users.append(f"the implementer ({agent.name})")
        for step in task.operate:
            if (profile := self._operate_profile(task, step)).runs_contained:
                users.append(f"operate {step.get('name')} ({profile.name})")
        for probe in probes:
            if (profile := self._probe_profile(probe)).runs_contained:
                users.append(f"{probe.name} ({profile.name})")
        return users

    def _probe(self, step: GateStep, env: Mapping[str, str] | None = None) -> str:
        """Run one probe's argv. "" when it exits 0, what it said when it ran and exited nonzero.

        **Three outcomes, not two.** A probe that ran and exited nonzero is an observation: the
        precondition does not hold, the premise is false. A probe the machine did not let answer — no
        container runtime, a timeout, a signal from outside, the sandbox's memory ceiling —
        observed nothing, and raises :class:`EnvironmentFault` like a gate step that cannot be run
        does. It
        used to come back as a nonzero like any other, so a runtime that was down for a minute was
        recorded as a falsified premise, for good.

        **Where it runs is where the implementer will.** A precondition is asked on the
        implementer's behalf — "can the work run here" — so without an `executor_profile` of its
        own the probe runs in `executors.agent_profile`, or on the host when agents do. It used to
        default to the quality gate's profile, which the recommended sandbox gives no network: a
        probe for a browser on the host failed there every time, and the task waited forever for a
        browser that was running.

        Not `_run_cmd_step`: that records a green in the evidence ledger against the tree, and what
        a probe observes is not a fact about a tree.
        """
        profile = self._probe_profile(step)
        # A task's declared environment is what its work runs with, so its probes ask under it too:
        # a model importable only with the plan's PYTHONPATH is importable, and a probe run without
        # it would park the task on a precondition that holds.
        declared = dict(env or {})
        spec = executors.ExecutionSpec(
            command=tuple(step.command),
            profile=profile,
            mounts=self._mounts_for(profile, self.root),
            env={**os.environ, **declared} if declared and not profile.runs_contained else {},
            env_always=declared if profile.runs_contained else {},
            workdir=_SANDBOX_WORKDIR if profile.runs_contained else self.root,
            timeout_sec=self.config.timeout_cmd,
        )
        where = f"probe {step.name}"
        try:
            result = executors.for_profile(profile).run(spec)
        except executors.ExecutorError as exc:
            raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=1, output=str(exc)) from exc
        if result.exit_code == 0:
            return ""
        if result.timed_out:
            raise EnvironmentFault(
                faults.Fault.ENV_TRANSIENT, where=where, rc=result.exit_code, output="did not answer in time"
            )
        # Not `faults.classify_step`: that separates the code from the machine, and a probe has no
        # code side — "could not resolve host" or "command not found" is exactly what a probe of the
        # network or of an installed tool is there to observe. Only a run the machine ended before
        # the argv could answer (a signal from outside, the sandbox's memory ceiling) observed nothing.
        if faults.is_sandbox_oom(result.output):
            raise EnvironmentFault(faults.Fault.ENV_PERMANENT, where=where, rc=result.exit_code, output=result.output)
        if faults.killed_externally(result.exit_code):
            raise EnvironmentFault(faults.Fault.ENV_TRANSIENT, where=where, rc=result.exit_code, output=result.output)
        tail = result.output.strip().splitlines()[-1:] if result.output.strip() else []
        return f"`{step.display}` exited {result.exit_code}" + (f": {tail[0][:200]}" if tail else "")

    def _probe_profile(self, step: GateStep) -> models.ExecutorProfile:
        """The profile a probe runs in: its own, else the agents', else the host (`_probe`)."""
        config = self.config.raw
        if step.executor_profile:
            if named := config.profiles.get(step.executor_profile):
                return named
            raise common.ReinError(
                f"{step.name} names executor_profile {step.executor_profile!r}, which is not in executor_profiles"
            )
        return config.agent_profile or models.ExecutorProfile(name="host", raw={"kind": "host"})

    def _unmet_on_frontier(self, graph: dag.Graph) -> dict[str, list[str]]:
        """The launchable tasks on the frontier whose preconditions do not all hold."""
        owed: dict[str, list[str]] = {}
        for task in graph.frontier():
            if task.produced_by != "person" and (unmet := self._unmet_preconditions(task)):
                owed[task.id] = unmet
        return owed

    def _person_owes(self, task: dag.Task) -> list[str]:
        """What a person-produced task is still waiting for. [] once its deliverable is in and holds.

        The deliverable counts once it is committed on the work branch: a file in somebody's
        working tree is not something a dependent can fork from. Then its own mechanized criteria
        are established at the root, where it lives, exactly as a launched task's are in its
        worktree.
        """
        paths = [
            str(path)
            for entry in task.acceptance
            if isinstance(entry.get("evidence"), dict) and str(entry["evidence"].get("kind", "")) == "artifact"
            for path in entry["evidence"].get("paths", [])
        ]
        missing = [path for path in paths if self.ws.authored(path) is None]
        if missing:
            return [f"commit {', '.join(missing)} on `{self.branch}`"]
        self._local.steps, self._local.acceptance, self._local.negative_control = [], [], {}
        failed, failure = self._run_acceptance(task, self.root)
        return [f"{failed}: {failure.splitlines()[0]}" if failure else failed] if failed else []

    def _settle_person_tasks(self, graph: dag.Graph) -> tuple[bool, dict[str, list[str]]]:
        """Finish each person-produced task on the frontier whose deliverable is in; name the rest.

        Returns `(finished any, {task id: what it still owes})`. No implementer is launched at one,
        ever: the two outcomes of sending an agent at a person's deliverable are a stop and a
        fabrication, and a reviewer caught the second in the cycle this exists for.
        """
        finished = False
        owed: dict[str, list[str]] = {}
        for task in graph.frontier():
            if task.produced_by != "person":
                continue
            if self.dry_run:
                print(f"    [dry-run] {task.id}: a person produces this — not launched")
                self._set_status(task.id, "done")
                finished = True
                continue
            if waiting := self._person_owes(task):
                owed[task.id] = waiting
                continue
            authored = []
            for entry in task.acceptance:
                spec = entry.get("evidence")
                if isinstance(spec, dict) and str(spec.get("kind", "")) == "artifact":
                    for path in spec.get("paths", []):
                        found = self.ws.authored(str(path))
                        if found is not None:
                            authored.append({"path": str(path), "commit": found[0], "author": found[1][:300]})
            record: dict[str, Any] = {"authored": authored, "reported": "none"}
            if self._current_acceptance:
                record["acceptance"] = list(self._current_acceptance)
            if fingerprint := self._fingerprint(self.root):
                record["tree"] = fingerprint
            with self._evidence_lock:
                self._evidence[task.id] = record
            print(f"  [person] {task.id}: the deliverable is committed — {', '.join(a['path'] for a in authored)}")
            self._set_status(task.id, self._completion_status(task), commit=self._landed(task.id))
            finished = True
        return finished, owed

    def owed_everywhere(self, graph: dag.Graph, known: Mapping[str, list[str]] | None = None) -> dict[str, list[str]]:
        """What a person owes across every unfinished task, in the order the work will need it.

        Every task rather than the one the frontier reached first: one launch finding one missing
        thing, then the next launch the next, is the sequence of stops this replaces. The critical
        path first, then by depth.

        `known` is what the caller has already found, kept as it was found rather than asked again:
        a probe answering differently the second time must not empty the list the caller is about
        to stop on.
        """
        known = known or {}
        depth = {tid: level for level, ids in enumerate(graph.layers()) for tid in ids}
        critical = set(graph.critical_path())
        owed: dict[str, list[str]] = {}
        for task in sorted(graph.tasks, key=lambda t: (t.id not in critical, depth.get(t.id, 0), t.id)):
            if task.id in known:
                owed[task.id] = known[task.id]
                continue
            if task.is_done or task.status in ("awaiting-evidence", "in-progress"):
                continue
            if task.produced_by == "person":
                if waiting := self._person_owes(task):
                    owed[task.id] = waiting
            elif unmet := self._unmet_preconditions(task):
                owed[task.id] = unmet
        return owed

    def _present_owed(self, graph: dag.Graph, found: Mapping[str, list[str]]) -> int:
        """Stop once, when nothing else can run, naming everything a person owes (`owed_everywhere`).

        `found` is what stopped the frontier, and it is never probed again here: the stop names at
        least those tasks, so it is one a task finishing can close.
        """
        owed = self.owed_everywhere(graph, found)
        blocked = [t.id for t in graph.tasks if t.status in ("blocked", "needs-revision")]
        message = (
            f"{len(owed)} task(s) wait on something only a person can provide, and nothing else can run. "
            "Everything the rest of the plan needs, in the order it will be needed:\n"
            + render_owed(graph, owed)
            + "\nProvide them, then run `rein build` again; it checks each one before launching anything."
            + (f"\nAlso stopped for another reason: {', '.join(blocked)} (`rein next`)." if blocked else "")
        )
        print("\n========== waiting on a person ==========\n" + message)
        self._escalate("awaiting_operator", message, task=list(owed))
        return common.EXIT_HUMAN_NEEDED

    def _present_crossings(self, waiting: Sequence[dag.Task]) -> int:
        """Hand back at an irreversible point: say what it is, and the one command that moves it.

        **Printed, not escalated.** Reaching the acceptance gate does the same thing
        (`_present_gate4`) and for the same reason: the pending gate in `state.yaml` *is* the
        record that the work stopped and a human has to act, and the chain records how it ends
        (`gate_approved`, `changes_requested`). Filing a `knowledge_gap` beside it wrote a second
        record of one fact — one that no approval ever closes, that `rein next` then recommended
        `rein events --summary` for instead of the approval, and that `events.stops` counted a
        second time. `run_aborted` is out of `ATTENTION_EVENTS` on exactly this reasoning.

        Once per run, because the batch that contains nothing else is where this lands.
        """
        print("\n========== an irreversible point ==========")
        for task in waiting:
            print(f"  {task.id}  {task.title}")
            if task.attempt_max:
                print(f"      covers {task.attempt_max} launch(es), {task.attempts} used — each: {task.attempt_cost}")
        print(
            "\nThe plan froze this work as something that cannot be taken back, so it is its own\n"
            "contact point and the loop stops in front of it. Running it first would make the\n"
            "decision by doing it.\n"
            "\nNext:\n"
            "  1. rein ui — read the ticket and the ADR the declaration points at"
        )
        for number, task in enumerate(waiting, start=2):
            print(f"  {number}. rein approve {task.id} — readiness check, then your confirmation at the terminal")
        print(
            "\nNothing here can open it, and neither can anything but a human: a gate opens only on\n"
            "the gate name typed at an interactive terminal, recorded by `rein approve` itself."
        )
        return common.EXIT_HUMAN_NEEDED

    def _awaits_crossing(self, task_id: str) -> bool:
        """Is this task an irreversible point whose gate nobody has approved yet?

        Read fresh rather than off `self.state`: the whole point of stopping is that a human then
        approves, and the next `rein build` must see that. A task with no gate of its own is every
        task in a cycle that declared nothing irreversible, and `gate_status` answers `pending` for
        a name it does not hold — so the membership test comes first and the absence of a gate is
        never read as an unapproved one.
        """
        state = self.store.read_state()
        if state is None:
            return False
        return task_id in state.crossing_gates and state.gate_status(task_id) != "approved"

    def _consume_batch(self, tasks: list[dag.Task]) -> None:
        """Implement a batch worktree-isolated up to max_parallel, then merge in ascending id order.

        **Every task is isolated, a foundation task included.** A serial batch is one task, and
        what makes it serial is the order it runs in, not where it runs. It used to run in the
        repository root and commit straight onto the work branch, and its change was then derived
        from history: the commits since a base pinned at its first attempt. That is right only
        while nothing else lands above the base, and the rules require exactly that — the rollback
        record, the design and tasks deltas and the approval record are all committed on the work
        branch between two attempts. The task was charged with them, the guard refused `.rein/`
        at landing, and nothing short of rewriting history could clear it (#88). On its own
        branch the change is what the branch holds, the same answer every leaf already had.

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
        self._review_batch(tasks, results)

        blocked_any = False
        merged: list[dag.Task] = []
        landed: dict[str, str] = {}
        # The first fault in id order, so which one is reported does not depend on thread timing.
        fault = next((results[t.id].fault for t in sorted(tasks, key=lambda t: t.id) if results[t.id].fault), None)
        # What the work branch was before any of this batch landed on it. The integration gate's
        # reviewer reads the join, and the join is what these merges added — not the cycle. Taken
        # here because it is the last moment it is still true.
        before_join = self.ws.head()
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
        joined = [t for t in merged if not self.ws.landing.get(t.id)]
        if joined and (len(joined) >= 2 or self._steps_at("integration") != self._steps_at("task")):
            ok, log = True, ""  # a fault reaches no verdict; it is handled where it is caught
            try:
                ok, log = self._integration_gate(joined, before_join)
            except StopLoop as stopped:
                # Findings the join's reviewer left unresolved, a fixer's change git would not
                # commit: whatever stopped it, the gate did not go green, and the join it leaves on
                # the branch is exactly as unverified as a red one.
                ok, log = False, str(stopped)
            except EnvironmentFault as raised:
                # No verdict about the join — but the join is on the branch, unverified, and the
                # next run must not build on it. Nor can it "re-play" these tasks over it: a leaf
                # forked from a branch that already holds its own work produces no change, and a
                # green over no change is refused. Taken off, the work is back on each leaf branch
                # and the next attempt restores it from there.
                #
                # Raised at the end like a leaf's fault, never from here: the leaves that landed on
                # a slice branch were never in this join, and leaving through here skipped their
                # record — `in-progress`, reset to `todo` by the next run, re-implemented over a
                # slice that already held their work, refused as `no_implementation` for good.
                if not self._take_off_join(joined, before_join, "todo", ""):
                    blocked_any = True
                if fault is None:
                    fault = raised
                else:  # a leaf's fault is the one raised; this one is still said
                    logger.error(raised.summary())
                joined = []
            if not ok:
                self._take_off_join(joined, before_join, "blocked", log)
                blocked_any = True
                joined = []
        # Every leaf recorded first, in one pass — the ones that landed on a slice branch as much as
        # the ones the join verified: they passed their gate and merged exactly like the rest.
        # `landed` was captured per leaf as each one merged: `_landed` reads the branch tip, so
        # asking it here would name the last merge for every member of the batch.
        recorded = [t for t in merged if t.id in {j.id for j in [*joined, *elsewhere]}]
        for task in recorded:
            self._set_status(task.id, self._completion_status(task), commit=landed.get(task.id, ""))
        # Then the readings, which can raise — a repair that reaches outside its scope stops the
        # run. Interleaved with the pass above, that would leave the leaves after it unrecorded.
        for task in recorded:
            # Asked before the repair moves the branch: is this leaf's own merge still the tip? Only
            # then can a repair made on top of it be charged to it (`_repair_warm_findings`).
            at_tip = bool(landed.get(task.id)) and landed[task.id] == self._landed(task.id)
            if self._repair_warm_findings(task, self._warm_reading(task), at_tip=at_tip):
                self._set_status(task.id, self._completion_status(task), commit=self._landed(task.id))
        if recorded:
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

    def _take_off_join(self, joined: Sequence[dag.Task], before_join: str, status: str, log: str) -> bool:
        """Take a join nothing verified off the work branch, and send its tasks back.

        It used to stay: the tasks went `blocked` with their merges on the branch, told to "fix the
        work branch, then set these tasks back to done" — which no verb does. Resetting them instead
        re-ran each as a leaf forked from a branch that already held its work, so the implementer
        had nothing to change, the empty diff was refused as `no_implementation`, and the task could
        not come back. And everything the loop ran next stood on a tree the gate had just called red.

        The work branch holds verified work only; that is what every later fork, diff and gate
        assumes. So the join comes off (`take_off_join` keeps it on a branch), each task's work is
        where it was before the merge — its leaf branch — and the next attempt resumes it from there
        with the join's failure in its handoff. `status` is `blocked` on a red verdict, and `todo`
        when a machine fault left none.

        False when git would not move the branch: the tasks are then `blocked` whatever `status`
        said, and the escalation names the reset a human has to make.
        """
        ids = ",".join(t.id for t in joined)
        try:
            kept = self.ws.take_off_join(before_join)
        except StopLoop as refused:
            # Git would not move the branch — a local change in the canonical checkout on a path
            # the join touched. The join stays, unverified, and the one thing that must not follow
            # is the next run resetting these tasks from `in-progress` and re-playing each over a
            # branch that already holds its work. `blocked` is never reset by a run; a human is.
            message = (
                f"{ids}: the join was not verified and could not be taken off {self.branch}: {refused}\n"
                f"Nothing may build on it. Clear what git names, run `git reset --keep {before_join}` on "
                f"{self.branch}, then reset each task — its work is on its leaf branch."
            )
            for task in joined:
                self._note_diagnostic(
                    task.id,
                    {
                        "failure_summary": (log or message)[-_HANDOFF_SUMMARY_MAX:],
                        "escalation": {"kind": "join_stuck", "message": message[-_HANDOFF_SUMMARY_MAX:]},
                    },
                )
                self._set_status(task.id, "blocked")
            self._escalate_batch("join_stuck", f"{message}\n{log}" if log else message, joined)
            return False
        where = f" (kept on {kept})" if kept else ""
        if log:
            message = (
                f"{ids}: each passed its own gate, and the tree they joined into did not pass the "
                f"integration gate. The join was taken off {self.branch}{where}; each task's work is on "
                "its leaf branch, and its next attempt resumes it with this failure in hand."
            )
            for task in joined:
                self._note_diagnostic(
                    task.id,
                    {
                        "failure_summary": log[-_HANDOFF_SUMMARY_MAX:],
                        "escalation": {"kind": "integration_red", "message": message[:_HANDOFF_SUMMARY_MAX]},
                    },
                )
        for task in joined:
            self._set_status(task.id, status)
        if log:
            self._escalate_batch("integration_red", f"{message}\n{log}", joined)
        else:
            print(f"    [join] {ids}: the join was taken off {self.branch}{where} — nothing verified it")
        return True

    def _warn_on_review_outlook(self) -> None:
        """Say it at task 9 of 17, not at acceptance, when the change outgrows what a review can read.

        The loop already knows the diff after each task lands, and it said nothing: a cycle with a
        reading past `max_diff_bytes` and a cycle carrying a committed binary both went the whole
        way to acceptance before anyone heard, and by acceptance narrowing the scope of the task that
        reading covers is not a move that exists — every task is merged and `done`.

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
                f"              no single launch can read {view.unit} — narrow its scope, or split "
                "the task, at the mandate. Once every task is merged that stops being possible."
                + (f"\n              {view.made_of()}" if view.made_of() else "")
            )
        if view.unreadable:
            print(
                f"    [outlook] {len(view.unreadable)} binary/unsupported file(s) in this change make "
                f"coverage `insufficient`"
                + (f", which blocks acceptance at {view.effective_risk} risk" if view.coverage_blocks_gate else "")
                + f": {', '.join(view.unreadable[:5])}"
            )

    # -- handing over to the review pipeline -----------------------------------

    def _close_gate4(self, graph: dag.Graph) -> int:
        """All tasks done: read the change, repair what this loop may, present the rest.

        **Inside a task, judging and repairing were both automated; at acceptance only judging was.**
        `_run_agent_step` runs a reviewer, hands its `must_fix` findings to an implementer, and
        has the reviewer look again — no human in it. The acceptance gate produced findings and printed three
        commands for somebody to type, and the only route back into the code was `rein revise
        --to build --from-review`, which marks the task *and its whole dependent closure*
        `needs-revision` — the status reserved for a defect in the specification. `status_api`
        then demanded a `/tasks` reconcile and a re-approval of the mandate, for a repair that changes
        no requirement, no claim and no plan. Reset, salvage, re-approve, round again.

        So the same shape runs here: read, repair what a task's declared scope owns, read again
        from cold. What reaches a human is what a human is actually for — deciding whether a
        `diverged` claim means the code is wrong or the plan is, and whether an extra behaviour
        nobody asked for is unwanted (`repair.route`).

        Four things keep this from being an agent marking its own work, and none of them is new:

        * The fixer is an implementer, never a reviewer. Whoever judges does not repair.
        * A repair that reaches outside the task's declared scope blocks rather than lands, by the
          same check every task's work goes through.
        * A repair cannot become a plan change: `gate_guard` denies a write to `plan.yaml` or
          `config.yaml` while the plan is frozen, which it is from the mandate onward.
        * Whether a finding closed is decided by the *next* round's review — a blind reading with
          no memory of having raised it — never by the fixer's account of its own work.

        And it is bounded. The failure this has to survive is a false positive, where repairing
        converges on nothing; `review_policy.repair_rounds` is the ceiling, after which what still
        stands goes to the human with the record of what was tried.

        It still cannot open the gate, and neither can anything else: a gate opens only on the
        gate name typed at an interactive terminal, recorded by `rein approve` itself.
        """
        print("\n========== all tasks done ==========")
        print(dag.render(graph))
        for measured in (self.ledger.summary(), self.spend_summary()):
            if measured:
                print(measured)
        print(
            "\nWhat the tasks established: every task's code passed the configured quality gate.\n"
            "What that did NOT establish: that the code does what the plan claims. Green tests plus\n"
            "an agent's summary is not evidence of conformance — the grounded review below is what\n"
            "asks that question, and a human is what answers what it cannot."
        )
        rounds = self.config.raw.repair_rounds
        routing = repair_mod.Routing()
        if self.dry_run:
            print("\n[dry-run] the grounded review and its repair rounds are not run.")
            return self._present_gate4(routing, rounds, read=False)
        if rounds == 0:
            # `repair_rounds: 0` says this loop does not repair its own findings, and reading the
            # change here would then buy nothing it could act on — the human runs `rein review
            # generate` and reads it in the dashboard, exactly as before this loop existed.
            print("\n[acceptance] `review_policy.repair_rounds` is 0 — this run takes no reading.")
            return self._present_gate4(routing, rounds, read=False)

        # The same frozen baseline every task ran against. `_consume` reads it before a batch, and
        # a run that finds every task already done never reaches that line — which is exactly the
        # run that repairs here. Without it `_repair` has no record of which steps the mandate approved
        # as already red, and it stops the loop over a failure the plan was approved on top of.
        self._load_baseline()
        self._revert_refused_expansions()
        repaired, read = 0, False
        for round_no in range(rounds + 1):
            print(f"\n[acceptance] reading the change ({'first reading' if not round_no else f'round {round_no}'})")
            if not self._generate_review():
                break
            read = True
            routing = repair_mod.route(graph.tasks, self.store.read_review())
            print(routing.render())
            # The other half of `unknown_at_mandate`. Findings the loop could not sort into code or
            # plan are the ones a human has to judge, and the claim being tested is that admitting
            # what the mandate did not know is what makes this number small. Recorded per round
            # because that is when the number exists; never read back by anything here.
            if routing.judgement or routing.unowned:
                observations.record(
                    "judgement_raised",
                    project=self.repo.root.name,
                    cycle_id=self.cycle_id,
                    value=len(routing.judgement) + len(routing.unowned),
                )
            if not routing.repairable or round_no == rounds:
                break
            for item in routing.code:
                owner = next((t for t in graph.tasks if t.id == item.task_id), None)
                if owner is None:  # a finding attributed to a task the graph no longer has
                    print(
                        f"    [acceptance] {item.task_id} is not in the plan any more — leaving its findings to a human"
                    )
                    continue
                self._repair(owner, item)
                repaired += len(item.items)
        return self._present_gate4(routing, rounds, repaired, read=read)

    def _generate_review(self) -> bool:
        """Take the grounded review. False when it could not be taken, and why is printed.

        **A reading that fails does not un-finish the build.** The tasks are done, their evidence
        is recorded, and the review is a separate question asked afterwards — so anything but a
        capacity stop is reported and handed over exactly as it was before this loop existed. The
        gate stays shut either way: `approve.readiness` refuses an acceptance with no generated review,
        so nothing here can turn a failed reading into an approval.

        A capacity stop is the one thing worth waiting for, and it is `main`'s to wait on:
        `EXIT_RETRY_LATER` is what `--supervise` retries, and every stage that did answer is
        already in the review cache, so the retry re-reads only what is missing.
        """
        from rein import review as review_mod

        try:
            review_mod.generate(
                self.repo,
                review_transport.StagedReviewers(self.repo, readings=self.config.readings),
                actor="rein build",
            )
            return True
        except review_policy.AdapterFailure as failure:
            if faults.classify_launch(failure.rc, failure.output) is faults.Fault.ENV_TRANSIENT:
                raise StopLoop(
                    f"the grounded review could not be taken: {failure}. Nothing is lost — every stage that "
                    "answered is cached. Re-run `rein build` (or `rein review generate --supervise`).",
                    code=common.EXIT_RETRY_LATER,
                ) from None
            print(f"\n[acceptance] the reading could not be taken: {failure}")
        except (
            review_reading.ReviewError,
            review_policy.ReviewPolicyError,
            review_transport.TransportError,
            adapters.LaunchRefused,
            models.DocumentError,
            store_mod.StoreError,
        ) as exc:
            print(f"\n[acceptance] the reading could not be taken: {exc}")
        print("Run `rein review generate` yourself once that is repaired — the gate needs one either way.")
        return False

    def _repair(self, task: dag.Task, item: repair_mod.Repair, *, where: str = "acceptance") -> None:
        """One implementer launch against one task's share of the review's findings, then the DoD.

        **Where the fix is committed is decided by whether this cycle is shipping as a stack.**

        A single pull request has one place for it: the work branch, where every task is already
        merged and where the findings are about the only tree that exists.

        A stack does not. Its slices are cut along the tasks' `completed_commit`s, so a commit made
        at the tip belongs to the tail — a pull request that is not the one holding the code the
        finding is about, which is where a reviewer looking at that pull request would go to see
        whether it was answered. `AGENTS.md` says what to do instead, and it is what `--restack`
        exists for: **the fix is committed onto the slice that introduced the code, and carried
        upward by merging.** Nothing is rewritten, so no open pull request is force-pushed and no
        `completed_commit` is stranded.

        So the slice's branch is checked out in a scratch worktree, the implementer runs *there*,
        and `pr_stack.restack` walks it up the chain into the work branch. The DoD then runs at the
        root, over the merged result — because what the quality gate is asked about is the tree the
        acceptance reading will read, never the slice in isolation.

        It takes the `dag.Task` rather than the graph because both callers already hold one: the
        acceptance path resolves the id against the graph before it calls this, and the task boundary
        (`_repair_warm_findings`) has the task in hand. The graph was an argument for one lookup.

        `where` is which of those two called, and it is only ever a label: every line this path
        printed said `[acceptance]`, including the ones a task-boundary repair produced several tasks
        before the gate was reached. A log that names the wrong phase is a log that has to be
        read against the code to be believed.
        """
        print(f"    [{where}] {task.id}: {len(item.items)} finding(s) → the implementer")
        slice_branch = self._slice_branch(task.id, where=where)
        # The repair may write where the mandate's `include` does not reach, for as long as it runs
        # and no longer (`gate_guard.outside_the_mandate`): every path it does is put to a human.
        self._open_repair_grant(task.id)
        try:
            if slice_branch:
                landed = self._repair_on_slice(task, item, slice_branch, where=where)
            else:
                landed = self._repair_on_work_branch(task, item, where=where)
        finally:
            self._close_repair_grant(reason="the repair launch ended")
        if landed:
            self._restate_evidence(task.id, self._gate_after_repair(task, where=where))

    def _slice_branch(self, task_id: str, *, where: str = "acceptance") -> str:
        """The stack branch that introduced this task's code, or "" when this is not a stack.

        Read from the same `pr_stack.derive` the stack itself is cut with, and only counting a
        branch that actually exists: before `rein pr-stack` materialises them, `derive` still names
        branches, and committing onto a name nothing points at would be inventing the stack rather
        than joining it. Any refusal `derive` makes — no plan, no base commit, a work branch that
        does not resolve — means the same thing here: there is no stack, so the work branch is the
        place.
        """
        if self.dry_run:
            return ""
        try:
            docs = pr_stack.Documents.read(self.repo)
            slices = pr_stack.derive(self.repo, docs)
        except (pr_stack.StackError, dag.DagError, models.DocumentError, store_mod.StoreError) as exc:
            print(f"    [{where}] no stack to place this on ({exc}) — the work branch it is")
            return ""
        found = next((s for s in slices if s.task_id == task_id), None)
        if found is None or found.branch == self.branch:
            return ""
        return found.branch if review_reading.commit_exists(self.repo, found.branch) else ""

    def _repair_prompt(self, task: dag.Task, item: repair_mod.Repair) -> str:
        return build_prompts.gate_four_fix_prompt(
            task,
            item.render(),
            gate_cmds=self.config.gate_cmds,
            refused=self._repair_refusals.get(task.id, ""),
        )

    def _repair_on_work_branch(self, task: dag.Task, item: repair_mod.Repair, *, where: str = "acceptance") -> bool:
        """The single-pull-request case: repair where everything is already merged. True when it landed."""
        before = self.ws.head()
        self._launch(
            adapters.command(self.config.adapter_argv, self._repair_prompt(task, item), access=adapters.WRITE),
            cwd=self.root,
            where=f"{task.id}: the repair",
            task_id=task.id,
            role="implementer",
        )
        return self._accept_repair(task, item, self.root, before, where=where)

    def _repair_on_slice(
        self, task: dag.Task, item: repair_mod.Repair, branch: str, *, where: str = "acceptance"
    ) -> bool:
        """The stacked case: repair on the slice that introduced the code, then merge it upward."""
        print(f"    [{where}] {task.id}: on its own slice {branch}, then up the stack")
        with build_git.scratch_worktree(self.repo, self.config.worktree_dir, _GATE4_WORKTREE, branch, common.run) as (
            path
        ):
            before = self.ws.head(cwd=path)
            self._launch(
                adapters.command(self.config.adapter_argv, self._repair_prompt(task, item), access=adapters.WRITE),
                cwd=path,
                where=f"{task.id}: the repair on {branch}",
                task_id=task.id,
                role="implementer",
            )
            landed = self._accept_repair(task, item, path, before, where=where)
        if landed:
            self._propagate(task, where=where)
        return landed

    def _accept_repair(
        self, task: dag.Task, item: repair_mod.Repair, cwd: str, before: str, *, where: str = "acceptance"
    ) -> bool:
        """Check what the repair touched, commit it, and land it only if it proves itself. True when landed.

        **Where it may write is the mandate's scope, not the task's.** The task's declared scope says
        where its work was expected to land, which is how a finding is charged to it
        (`findings.owner_of_path`); it is not a boundary a human drew. The cause of a defect is
        where it is, and a repair held to the task's paths can only treat the symptom that showed up
        there. Crossing into another task's paths stays inside what the mandate delegated, so it
        lands and is recorded. What the mandate does not cover, the guard still refuses
        (`_gate_violations`) — that line a human drew.

        **And it lands only with a test that fails without it** (`_verify_repair`). A repair the
        loop cannot show reproduces the defect is a claim, and the reading that follows may simply
        not see the symptom any more. Refused, it is undone, recorded with why, and handed to the
        next round's implementer; a finding no round could prove repaired is what reaches a human.

        **The next reading is over committed history**, so an uncommitted repair is one that never
        happened: `review.generate` resolves HEAD and digests the committed tree, the machine half
        comes out byte-identical, and "nothing this review is made of has moved" reads as the
        finding still standing. The implementer is told to commit; that is an instruction, not
        evidence, which is why every task finalizes its own diff (`build_git.finalize_commit`).
        """
        phase = where.replace(" ", "-")
        changed = self.ws.changed_since(before, cwd=cwd) if before else []
        if violations := self._gate_violations(changed):
            raise StopLoop(
                f"{task.id}: the {phase} repair changed paths the gate guard refuses:\n"
                + "\n".join(f"  - {path}: {why}" for path, why in violations),
                code=common.EXIT_HUMAN_NEEDED,
            )
        if not self.ws.finalize_commit(cwd, f"{task.id}: {phase} repair"):
            raise StopLoop(
                f"{task.id}: the {phase} repair could not be committed. The fix is in the tree and nothing "
                "has been lost; commit it yourself, then re-run `rein build`.",
                code=common.EXIT_HUMAN_NEEDED,
            )
        after = self.ws.head(cwd=cwd)
        findings = [a.finding_id for a in item.items]
        detail: dict[str, Any] = {"findings": findings, "before": before, "after": after}
        if beyond := dossier.scope_violations(task, changed):
            detail["beyond_task_scope"] = list(beyond)
        result, said = self._verify_repair(task, cwd, before, changed)
        detail["result"] = result
        if said:
            detail["detail"] = said[:500]
        if result in _REPAIR_REFUSED:
            # `--keep`, not `--hard`: the root holds the orchestration state uncommitted (every
            # repair commit excludes `.rein/`), and a hard reset would take the chain with it.
            rc, out = _late_run(["git", "reset", "--keep", before], cwd=cwd)
            if rc != 0:
                raise StopLoop(
                    f"{task.id}: the {phase} repair was refused ({said}) and could not be undone: {out[-300:]}",
                    code=common.EXIT_HUMAN_NEEDED,
                )
            self._repair_refusals[task.id] = said
            self._event("repair_refused", [task.id], detail)
            print(f"    [{where}] {task.id}: repair refused and undone — {said}")
            return False
        self._repair_refusals.pop(task.id, None)
        self._event("repair_verified", [task.id], detail)
        print(f"    [{where}] {task.id}: repair landed ({result})")
        self._record_expansions(task, changed, findings, after)
        return True

    def _update_state(
        self, event: str, subjects: Sequence[str], detail: dict[str, Any], change: Callable[[dict[str, Any]], bool]
    ) -> None:
        """Change state.yaml and record why, in one transaction. `change` edits the raw document in
        place and says whether it changed anything; nothing is written or recorded when it did not."""
        if self.dry_run or not self.cycle_id:
            return
        with self.store.transaction() as tx:
            state = tx.store.read_state()
            if state is None:
                raise StopLoop("state.yaml vanished while the build was running — `rein doctor`")
            raw = copy.deepcopy(dict(state.raw))
            if not change(raw):
                return
            tx.write("state", raw)
            tx.append(event, cycle_id=self.cycle_id, subject_ids=list(subjects), detail=detail)

    def _open_repair_grant(self, task_id: str) -> None:
        def change(raw: dict[str, Any]) -> bool:
            raw["repair_grant"] = {"task_id": task_id, "opened_at": event_chain.now_iso()}
            return True

        self._update_state("repair_grant_opened", [task_id], {}, change)

    def _close_repair_grant(self, *, reason: str) -> None:
        """Close the repair grant if one is open. A no-op otherwise, so it is safe to call on every exit."""

        def change(raw: dict[str, Any]) -> bool:
            return raw.pop("repair_grant", None) is not None

        self._update_state("repair_grant_closed", [self.cycle_id], {"reason": reason}, change)

    def _record_expansions(self, task: dag.Task, changed: Sequence[str], findings: Sequence[str], commit: str) -> None:
        """Put every path this repair wrote outside the mandate's `include` to a human, as `proposed`.

        Measured with the grant closed — the same rule the guard applies, read the way acceptance
        will read it — so a path a human already adopted is not asked about again, and one the
        mandate covers is not asked about at all.
        """
        state = self.store.read_state()
        plan = self._plan
        if state is None or plan is None:
            return
        include, exclude = plan.scope
        guarded = gate_guard.guard_settings(self.repo).paths
        statuses = {path: str(entry.get("status")) for path, entry in state.scope_expansions.items()}
        widened = [
            path
            for path in changed
            if gate_guard.outside_the_mandate(
                path, include=include, exclude=exclude, guarded=guarded, expansions=statuses, granted=False
            )
        ]
        if not widened:
            return

        def change(raw: dict[str, Any]) -> bool:
            table = dict(raw.get("scope_expansions") or {})
            for path in widened:
                table[path] = {"status": "proposed", "task_id": task.id, "findings": list(findings), "commit": commit}
            raw["scope_expansions"] = table
            return True

        self._update_state(
            "scope_expanded", [task.id], {"paths": widened, "findings": list(findings), "commit": commit}, change
        )
        print(f"    [repair] {task.id}: {len(widened)} path(s) outside the mandate's scope — put to you at acceptance")

    def _revert_refused_expansions(self) -> None:
        """Take back out every path a human refused to widen the mandate to, while the change still has it.

        The refusal is the human's answer to a card; this is the loop doing what that answer means,
        mechanically and in one commit: each such path goes back to what the base had (or away, if
        the base had none). The finding the repair was about is then open again, and the next
        repair cannot write there (`outside_the_mandate` refuses a refused path even under a grant)
        — so it is fixed inside the mandate, or it reaches a human as a finding.
        """
        if self.dry_run or self.state is None:
            return
        state = self.store.read_state()
        expansions = state.scope_expansions if state is not None else {}
        refused = [path for path, entry in expansions.items() if entry.get("status") == "refused"]
        if not refused:
            return
        try:
            base = review_reading.resolve_base(self.repo, self._plan, None)
        except review_reading.ReviewError as exc:
            raise StopLoop(
                f"refused scope expansions cannot be taken back out: {exc}", code=common.EXIT_HUMAN_NEEDED
            ) from None
        rc, out = self.repo._git_rc("diff", "-z", "--name-only", base, "--", *refused)
        still = [path for path in out.split("\0") if path] if rc == 0 else []
        if not still:
            return
        for path in still:
            present = self.repo._git_rc("cat-file", "-e", f"{base}:{path}")[0] == 0
            cmd = ["git", "checkout", base, "--", path] if present else ["git", "rm", "-q", "-f", "--", path]
            rc, out = _late_run(cmd, cwd=self.root)
            if rc != 0:
                raise StopLoop(
                    f"could not take {path} back to {base[:12]}: {out[-300:]}", code=common.EXIT_HUMAN_NEEDED
                )
        if not self.ws.finalize_commit(self.root, "revert the scope expansions a human refused"):
            raise StopLoop(
                "the refused scope expansions were taken back in the tree but could not be committed — commit "
                "them yourself, then re-run `rein build`.",
                code=common.EXIT_HUMAN_NEEDED,
            )
        self._event("scope_expansion_reverted", [self.cycle_id], {"paths": still, "base": base})
        print(f"\n[acceptance] took {len(still)} refused scope expansion(s) back out: {', '.join(still)}")

    def _verify_repair(self, task: dag.Task, cwd: str, before: str, changed: Sequence[str]) -> tuple[str, str]:
        """Does a test in this repair fail against the code as it was? `(result, what it found)`.

        The same experiment as a task's negative control (`_red_without`), aimed at the repair:
        the commit before it, plus only the test half of what it changed. A step that goes red
        there is a test that reproduces the defect and passes once the fix is in — the DoD that
        runs next establishes the second half. Read it for what it is worth: red says the tests are
        *not inert* against the old code, which a test that only imports a new symbol also is. It
        is the mechanical half; whether the test is about the finding is the next reading's.

        `untested` and `inert` refuse the repair (`_REPAIR_REFUSED`). `reproduced` lands it.
        `undetermined` lands it too, recorded: an experiment that could not be run is evidence in
        neither direction, and refusing on it would make a broken sandbox a human's problem.
        """
        if self.dry_run:
            return "undetermined", "a dry run takes no control"
        commands = [
            step for step in self._steps_at("task") if step.kind == "command" and step.command and step.runs_tests
        ]
        if not commands:
            return "undetermined", (
                "no quality-gate step declares `runs_tests`, so no test can be run against the code as it was"
            )
        if not changed:
            return "untested", (
                "the repair changed nothing. A finding that is wrong is said so in `rein report --summary` "
                "and reaches a human as it stands; one that is right is fixed with a test that fails without the fix"
            )
        tests = [path for path in changed if diff_facts.classify_path(path) == "test"]
        if not tests:
            return "untested", (
                "the repair changed no test, so nothing shows the defect was there or that this change is "
                "what removed it. Write a test that fails against the code as it was and passes with the fix"
            )
        if len(tests) == len(changed):
            return "undetermined", "every path the repair changed is a test path — there is no fix to take away"
        patch = self.ws.diff_from(before, cwd, tests)
        if patch is None or not patch.strip():
            return "undetermined", f"the test half of the repair against {before[:12]} could not be read out of git"
        try:
            result, said = self._red_without(f"repair-{task.id}", before, patch, commands)
        except (EnvironmentFault, StopLoop) as exc:
            return "undetermined", str(exc)
        if result == "discriminating":
            return "reproduced", f"'{said}' fails against the code as it was"
        if result == "inert":
            return "inert", (
                f"the repair's tests pass against the code as it was ({said} green over {before[:12]} with only "
                "the test half applied), so they do not reproduce the defect and nothing shows the fix is what "
                "removed it. That is the shape a fix of the symptom takes. Find where the defect comes from, "
                "write a test that fails there before the fix, and fix it there"
            )
        return result, said

    def _propagate(self, task: dag.Task, *, where: str = "acceptance") -> None:
        """Carry a slice's repair up the stack by merging, never by rewriting.

        The same walk `rein pr-stack --restack` performs, run here because the repair is only half
        done while the work branch does not have it: the DoD, the next reading and the gate receipt
        are all about the work branch. A conflict is classified before it is resolved and only the
        mechanical kind is carried through — the rest stops the run, exactly as it does for a human
        who runs `--restack` by hand.
        """
        docs = pr_stack.Documents.read(self.repo)
        result = pr_stack.restack(
            self.repo,
            docs,
            pr_stack.derive(self.repo, docs),
            implement=self.resolve_conflict,
            quality_gate=self.task_gate,
        )
        if not result.ok:
            resolution = result.resolution
            raise StopLoop(
                f"{task.id}: the repair is committed on its own slice, and carrying it up the stack stopped at "
                f"{result.stopped_at}: {resolution.escalation if resolution else 'the merge did not complete'}. "
                "Nothing is lost and nothing was rewritten — resolve it and run `rein pr-stack --restack`.",
                code=common.EXIT_HUMAN_NEEDED,
            )
        print(f"    [{where}] {task.id}: {len(result.merged)} branch(es) advanced to carry the repair up the stack")

    def _gate_after_repair(self, task: dag.Task, *, where: str = "acceptance") -> list[dict[str, Any]]:
        """The task-stage DoD over the work branch, which is where the repair has now landed.

        A step the baseline froze red at the mandate is not this repair's to answer: it was red before
        any task ran, a human approved the plan over it on the record, and stopping the run here
        would re-stage the discovery that the frozen baseline exists to prevent.

        Returns the evidence these re-runs produced. They were run and then dropped: the task's
        record still named the runs from before the repair, so `done` cited a green nobody had
        established over the tree that ended up on the branch.
        """
        established = len(self._current_step_evidence)
        for step in self._steps_at("task"):
            if step.kind != "command" or not step.command:
                continue
            if failure := self._run_cmd_step(step, self.root):
                if step.name in self._baseline_red:
                    print(f"    [{where}] '{step.name}' is red, and was already red at the mandate — not this repair's")
                    continue
                raise StopLoop(
                    f"{task.id}: the repair left '{step.name}' red:\n{failure}",
                    code=common.EXIT_HUMAN_NEEDED,
                )
        return [dict(entry) for entry in self._current_step_evidence[established:]]

    def _restate_evidence(self, task_id: str, steps: Sequence[Mapping[str, Any]]) -> None:
        """Re-point a repaired task's evidence at the tree the repair produced.

        `done` means the DoD went green against the tree the task actually produced, and the
        fingerprint recorded beside the status is what says which tree that was. A repair moves
        the tree, so the digest taken before it names one that is no longer on the branch — and
        the caller re-records `completed_commit` immediately after, which would leave the commit
        half current beside a stale fingerprint. Stale together was at least self-consistent;
        half-refreshed is a record that contradicts itself.

        Steps replace the ones of the same name — the rule `_record_task_evidence` already
        deduplicates by — so what the repair re-ran is the run that holds, and a step it did not
        re-run keeps the account it had.
        """
        if self.dry_run:
            return
        fingerprint = self._fingerprint(self.root)
        with self._evidence_lock:
            record = self._evidence.get(task_id)
            if record is None:
                return
            by_name = {str(entry["name"]): entry for entry in record.get("steps", [])}
            for entry in steps:
                by_name[str(entry["name"])] = dict(entry)
            record["steps"] = list(by_name.values())
            if fingerprint:
                record["tree"] = fingerprint

    def _present_gate4(self, routing: repair_mod.Routing, rounds: int, repaired: int = 0, *, read: bool = True) -> int:
        """What is left after the repair rounds, and what a human has to do about it.

        This deliberately does not invite an approval. The review is read in the dashboard, the
        Decision Cards are answered there, and the gate is opened at a terminal by a person.
        """
        print("\n========== acceptance ==========")
        # An empty routing means two different things and only one of them is "nothing blocking":
        # a reading that was taken and found nothing, and a reading that could not be taken at all.
        # Printing the first over the second put the reassurance two lines under the failure.
        print(routing.render() if read else "  no reading was taken, so nothing here has been judged.")
        if repaired:
            print(
                f"\n{repaired} finding(s) repaired. Each fix is committed where a review fix belongs: on the "
                "slice that introduced the code when this cycle is a stack — carried up by merging, so nothing "
                f"is rewritten — and on {self.branch or 'the work branch'} when it is a single pull request."
            )
        if routing.repairable:
            print(
                f"\n{rounds} repair round(s) are spent and findings still stand. A finding that survives "
                "being repaired and re-read is either real and harder than it looked, or was never true — "
                "and this loop cannot tell those apart. Read them, and dispute the ones that are wrong.\n"
            )
        print(
            "Next:\n"
            "  1. rein ui                — read the scope and the orient brief, then answer the\n"
            "                                   Decision Cards and freeze the review\n"
            # Spelled from the constant, not typed: this line said `rein approve build` for two
            # releases after that gate stopped existing — a handover whose one command exits 2.
            f"  2. rein approve {models.GATE_LAST} — readiness check, then your confirmation at the terminal\n"
            "\nAn answer of `revise_implementation` on a card hands that subject back to this loop: it\n"
            "says the code is what is wrong, which is the one thing the review cannot decide for itself.\n"
            "Re-run `rein build` afterwards and it will be repaired like any other finding.\n"
            "\nThis loop cannot open acceptance, and neither can anything but a human: a gate opens only on\n"
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


def baseline_main(argv: list[str] | None = None) -> int:
    """`rein baseline measure` — what the work branch's quality gate says before any task runs.

    Its own verb, and the mandate's rather than the build's, because the question is about the *tree*:
    is this plan implementable against what is here now? Taken inside `rein build` it was taken
    after the approval that had already answered that, so a cycle could start on a tree that had
    been red for weeks and the first task paid three implementer launches to find out.

    Runs the DoD, so it takes the build lock and goes through the executor profiles like any other
    step — this runs repository code, and it does not get a pass for being a measurement.
    """
    parser = argparse.ArgumentParser(description="measure the work branch's quality gate before any task runs")
    sub = parser.add_subparsers(dest="action", required=True)
    measure = sub.add_parser("measure", help="run the task-stage DoD over the work branch and record the result")
    measure.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    measure.add_argument(
        "--freeze",
        action="store_true",
        help="approve the red steps as known and expected — without it a red baseline holds the mandate shut",
    )
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        config = Config.load(repo)
    except (OSError, ValueError, adapters.LaunchRefused, models.DocumentError, strict_yaml.StrictParseError) as exc:
        logger.error(f"cannot load .rein/config.yaml: {exc} — `rein doctor` validates it")
        return 1

    orchestrator = Orchestrator(config, dry_run=False, repo=repo)
    store = store_mod.Store(repo)
    try:
        with build_lock(repo):
            record = orchestrator.measure_baseline()
    except EnvironmentFault as fault:
        logger.error(f"the baseline could not be measured: {fault}")
        return 1
    record["frozen"] = bool(args.freeze)

    # Checked before the transaction opens rather than inside it: a `return` out of the `with`
    # leaves the block without an exception, which commits the empty transaction it was refusing
    # to fill.
    if (existing := store.read_state()) is None or not existing.cycle_id:
        logger.error("no .rein/state.yaml — run `rein init` first")
        return 1
    with store.transaction() as tx:
        state = tx.store.read_state()
        assert state is not None
        tx.write("state", {**state.raw, "baseline": record})
        tx.append(
            "baseline_measured",
            cycle_id=state.cycle_id,
            detail={"red_steps": [row["name"] for row in record["red_steps"]], "frozen": record["frozen"]},
        )

    red = record["red_steps"]
    if not red:
        print(f"baseline: the work branch is green on every task-stage step ({record['tree_digest'][:12]})")
        return 0
    names = ", ".join(row["name"] for row in red)
    if record["frozen"]:
        print(f"baseline: {names} already red, frozen as known. The mandate may be approved over this.")
        return 0
    print(
        f"baseline: {names} already red on the work branch.\n"
        "The mandate stays shut until this is a decision rather than a discovery: fix it, or re-run "
        "with `--freeze` to approve it as known — a task that then fails one of these is stopped "
        "instead of being sent back to an implementer whose scope does not contain the break."
    )
    return 0


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
