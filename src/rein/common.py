"""Shared primitives: diagnostics logging, safe subprocess execution, failure summarization.

Nothing here reads a document: the vocabulary lives in :mod:`rein.models`, parsing in
:mod:`rein.strict_yaml`, and paths on :class:`rein.repo.Repo`. That is what keeps this
module stdlib-only and cheap to import on the gate-guard hook path, which fires on every
editor write.

:func:`run` gives every launch its own process group and kills the whole group on expiry.
Killing only the process started leaves its children alive — a `make test` that spawned pytest,
which spawned a server, would leave the server holding the port and fail the next run for the
wrong reason.

That same group kill is the only way a launch can be ended *early*, which is what
:class:`Cancellation` exposes. Two things depend on it. A caller that has stopped wanting an
answer — gate ④'s security stage once its sibling has already failed — can stop paying for it,
which `concurrent.futures` cannot express (`Future.cancel()` is a documented no-op once the task
is running). And a human pressing Ctrl-C can actually stop an agent. The child is in its own
session, so the terminal's SIGINT reaches this process and not it — while `Popen.__exit__`
explicitly declines to wait on a KeyboardInterrupt, "assuming the SIGINT was also already sent to
our child processes". Here it was not. Measured: the launch was orphaned and went on running,
holding its quota, with nothing left to read what it eventually said.
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
import threading
from collections.abc import Iterable, Iterator
from typing import Any

# --- diagnostics logging ------------------------------------------------------
#
# Command *results* go to stdout via `print`; *diagnostics* (errors/warnings/notes) go through
# per-module `logging.getLogger(__name__)` loggers — children of the "rein" logger set up here.


class _StderrHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Re-targets the current sys.stderr on each emit (as logging.lastResort does), so it follows
    a later stderr swap — e.g. pytest's capsys — instead of a stream captured at configure time."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def configure_logging(*, level: int = logging.INFO) -> None:
    """Send rein diagnostics to stderr, message-only. Idempotent, so every verb may call it."""
    root = logging.getLogger("rein")
    if not any(isinstance(h, _StderrHandler) for h in root.handlers):
        handler = _StderrHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
    root.setLevel(level)


# --- subprocess ---------------------------------------------------------------

#: rc for a command killed after its timeout — the coreutils `timeout` convention.
RC_TIMEOUT = 124
#: rc for a command a caller cancelled through :class:`Cancellation`. Deliberately distinct from
#: :data:`RC_TIMEOUT`: nothing ran out of time, somebody stopped wanting the answer.
RC_CANCELLED = 125
#: Output past this is truncated with a marker. A runaway command must not exhaust memory
#: before its timeout fires.
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


def stdin_is_terminal() -> bool:
    """Whether stdin is an interactive terminal.

    Kept here because two places need the same rule and a second copy is how one of them
    eventually grows a flag that skips it. What the terminal establishes is not that a human
    answered but that a piped stdin, a CI job, or an agent's captured subprocess cannot answer
    by accident.
    """
    return sys.stdin.isatty()


def ask_yes_no(prompt: str) -> bool:
    """`[y/N]` on stdin. **The default being no is the load-bearing half**: a stray Enter declines.

    Deliberately not "type the word back": retyping something already on the command line
    establishes nothing, since whoever would reflexively press `y` would as reflexively type it.
    What carries weight is the pause, the terminal :func:`stdin_is_terminal` insists on, and the
    default.
    """
    print(f"\n{prompt} [y/N] ", end="", flush=True)
    return sys.stdin.readline().strip().lower() in ("y", "yes")


class Cancellation:
    """A handle another thread can trip to end a :func:`run` before it finishes.

    `concurrent.futures.Future.cancel()` is a no-op once a task has started, and a
    `ThreadPoolExecutor` joins its workers on the way out of its `with` block *and* again at
    interpreter exit (`concurrent.futures.thread` registers `_python_exit`). So "stop waiting for
    that stage" cannot be said with futures at all: the only thing that ends a launch early is
    killing the process it started, and that is what this is.

    Bound to the thread that does the running (:func:`cancelling`) rather than threaded through
    the call chain, because what gets cancelled here is reached through an *injected* callable —
    a reviewer transport whose signature belongs to whoever supplied it, fakes included.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Every launch currently held, not the last one: a token bound to two threads that both
        # ran would otherwise have kept only the second, and `cancel` would have left the first
        # running while reporting that it had stopped everything.
        self._procs: set[Any] = set()
        self._cancelled = False

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def cancel(self) -> None:
        """Kill every launch this token holds, and refuse every later one bound to it."""
        with self._lock:
            self._cancelled = True
            held = list(self._procs)
        for proc in held:
            _kill_group(proc)

    def _attach(self, proc: Any) -> bool:
        """Take the launch; False when this token was already tripped, so the caller kills it.

        The window between `Popen` returning and this call is why :meth:`cancel` cannot simply be
        "kill the process": for a moment there is not one yet, and losing that race would leave a
        launch running that somebody had already stopped waiting for.
        """
        with self._lock:
            self._procs.add(proc)
            return not self._cancelled

    def _detach(self, proc: Any) -> None:
        with self._lock:
            self._procs.discard(proc)


_bound_cancellation = threading.local()


@contextlib.contextmanager
def cancelling(token: Cancellation) -> Iterator[Cancellation]:
    """Bind `token` to this thread: every :func:`run` on it is then killable from outside."""
    previous = getattr(_bound_cancellation, "token", None)
    _bound_cancellation.token = token
    try:
        yield token
    finally:
        _bound_cancellation.token = previous


def run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: float | None = None,
    *,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
) -> tuple[int, str]:
    """Run `cmd` (an argv list, never a shell string); return (returncode, stdout+stderr).

    A hang past `timeout` kills the command **and every process it started** (the launch gets
    its own process group / session) and returns :data:`RC_TIMEOUT`. Without the group kill a
    stuck server outlives the run and poisons the next one. On a platform with no `killpg` the
    group is created and only the leader is killed — the children are left, which is one of the
    reasons the supported-environment table calls Windows native unvalidated.

    `timeout` is `None` by default and every agent launch leaves it that way. A wall clock cannot
    tell a model that is working from one that is stuck, and the two mistakes do not cost the same:
    killing a working agent throws away the whole run's output and quota and makes the retry pay
    for it again, while failing to kill a stuck one only stalls — and a stall is now interruptible,
    because the `BaseException` path below kills the group on Ctrl-C. A step whose runtime *is*
    knowable (a test suite, a build, a network call) still passes one.

    `env`, when given, *replaces* the environment rather than extending it — an executor
    profile's allowlist is only an allowlist if nothing leaks in around it.
    """
    import subprocess  # lazy: keep `import common` light for the hook path (gate_guard on every edit)

    token: Cancellation | None = getattr(_bound_cancellation, "token", None)
    popen_kwargs: dict[str, Any] = {}
    if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):  # Windows
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True  # POSIX: own session, so killpg reaches children

    try:
        proc = subprocess.Popen(  # noqa: S603 - argv list, never a shell string
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            **popen_kwargs,
        )
    except OSError as exc:  # command not found, not executable, cwd missing
        return 127, f"could not run {cmd[0]!r}: {exc}"

    with proc:
        if token is not None and not token._attach(proc):
            _kill_group(proc)  # cancelled in the window between Popen and the attach
        try:
            output, _ = proc.communicate(input=input_text, timeout=timeout)
            if token is not None and token.cancelled:
                # Whatever the killed process managed to say is not an answer to anything.
                return RC_CANCELLED, _cap(f"{output or ''}\ncancelled by the caller (process group killed)")
            return proc.returncode, _cap(output or "")
        except subprocess.TimeoutExpired:
            # subprocess's own timeout handling kills the direct child only, so anything it
            # spawned survives — a stuck server keeps the port and the next run fails for a
            # reason that has nothing to do with the code. Kill the whole group instead.
            _kill_group(proc)
            output, _ = proc.communicate()
            elapsed = int(timeout) if timeout is not None else 0
            return RC_TIMEOUT, _cap(f"{output or ''}\ntimed out after {elapsed}s (process group killed)")
        except BaseException:
            # A Ctrl-C, a supervisor's SIGTERM, anything. `start_new_session=True` put the child
            # in its own session, so the terminal's SIGINT reached this process and not it — and
            # `Popen.__exit__` declines to wait on a KeyboardInterrupt precisely because it
            # assumes the opposite ("the SIGINT was also already sent to our child processes").
            # So Ctrl-C used to return promptly and leave the agent running, orphaned, still
            # spending. Kill the group, then let the exception carry on: stopping is the caller's
            # decision and this only makes it true of the process as well as of us.
            _kill_group(proc)
            raise
        finally:
            if token is not None:
                token._detach(proc)


def _kill_group(proc: Any) -> None:
    """SIGKILL the whole process group, falling back to the single process where that fails."""
    import os
    import signal

    if hasattr(os, "killpg"):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone, or a platform that will not let us — fall through
    proc.kill()


#: What :func:`_cap` appends when it truncates. Named so a caller that needs the *whole* output
#: (anything hashing it) can tell "this is the output" from "this is the start of the output".
TRUNCATION_MARKER = f"… (output truncated at {MAX_OUTPUT_BYTES} bytes)"


def _cap(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return f"{kept}\n{TRUNCATION_MARKER}"


def was_truncated(output: str) -> bool:
    """True when :func:`run` capped this output.

    A capped stream is a prefix, not an answer. Hashing one would give two different trees the
    same fingerprint as soon as they agreed for the first 4 MiB, so every caller that content-
    addresses command output has to ask this first and fall back to "unknown".
    """
    return output.endswith(TRUNCATION_MARKER)


# --- failure summarization (retry-friendly, token-lean) ---------------------
#
# make test / make check can emit huge output (full tracebacks, every passing test line). Feeding that
# raw into the implementer retry prompt / escalation log wastes tokens and buries the actionable lines,
# so we keep only the salient lines and cap the size — the retry and the human escalation both get a
# compact, actionable failure (retry-friendly error design) instead of a raw dump.

# Match genuine failure/error/diagnostic lines across the pytest/ruff/mypy default stack and the documented
# frontend (eslint/tsc), without pulling in passing-test noise. The markers are word-bounded so "error"/
# "…Error" inside an identifier (e.g. a passing "test_error_handling" or "test_raises_ValueError PASSED")
# is skipped, while a real exception line ("ValueError: msg") is kept via the colon-anchored branch.
_SALIENT_RE = re.compile(
    r"""
      ^E\s                                                  # pytest assertion / exception detail ("E   " prefixed)
    | ^=+.*\b(failed|error|passed|no\ tests\ ran)\b.*=+$    # pytest summary rule line
    | \bFAILED\b                                            # pytest failure marker (summary or verbose inline)
    | :\d+:\d+:\s                                           # ruff/mypy/eslint "file:line:col:" locations
    | \(\d+,\d+\):\s                                        # tsc "file(line,col):" locations
    | \berror\b                                             # error diagnostics (eslint/tsc/mypy), word-bounded
    | \b\w*(?:Error|Exception):                             # exception line "ValueError: ..." (colon skips test names)
    | ^Traceback\b                                          # traceback header
    """,
    re.IGNORECASE | re.VERBOSE,
)

_FAILURE_MAX_LINES = 40
_FAILURE_MAX_CHARS = 1500


def summarize_failure(cmd: str, rc: int, output: str) -> str:
    """Reduce a quality-gate command's raw output to a compact, salient failure summary.

    Keeps only the lines carrying the actionable signal (pytest FAILED / assertion lines, ruff/mypy
    error locations, exception markers); when nothing matches, falls back to the non-empty tail (the
    failure is usually last). Capped to a small line/char budget so retries and escalations stay
    token-lean. Pure and deterministic.
    """
    header = f"$ {cmd} (rc={rc})"
    lines = output.splitlines()
    salient = [ln for ln in lines if _SALIENT_RE.search(ln)]
    if salient:
        kept, note = salient, "salient lines only"
    else:
        kept, note = [ln for ln in lines if ln.strip()], "tail"
    kept = kept[-_FAILURE_MAX_LINES:]
    # Char-budget guard for pathological long lines: drop whole leading lines first so the disclosed
    # omitted-count stays accurate, then keep the head of the remainder as a last resort (a single huge
    # line) — the head holds the actionable "file:line:col: error:" prefix, not the trailing message text.
    while len(kept) > 1 and len("\n".join(kept)) > _FAILURE_MAX_CHARS:
        kept = kept[1:]
    omitted = len(lines) - len(kept)
    body = "\n".join(kept)[:_FAILURE_MAX_CHARS]
    out_lines = [header]
    if body and omitted > 0:  # a bare "N omitted" with no body (e.g. whitespace-only output) would confuse
        out_lines.append(f"… ({omitted} line(s) omitted; kept {note})")
    if body:
        out_lines.append(body)
    return "\n".join(out_lines)


# --- how a build run ends -----------------------------------------------------
#
# `rein build` is routinely driven by something that will decide, unattended, whether to run it
# again — a supervisor script, a scheduler, an agent's session that has just been restarted. That
# decision is only ever as good as what the exit code says, so the codes separate the three
# answers rather than the three internal causes:

#: Every task is done. Nothing to re-run.
# --- repo-relative path patterns ----------------------------------------------


def path_covered(path: str, pattern: str) -> bool:
    """Does `pattern` cover the repo-relative `path`?

    One rule, shared by `guard.paths` in config.yaml and by a task's `scope` in plan.yaml: a
    pattern covers the path it names and everything beneath it, and **a trailing slash changes
    nothing**.

    It used to be load-bearing. `src/rein/` covered the subtree; `src/rein` named an exact *file*,
    which no changed-path list ever contains, so every file under a directory someone wrote
    without the slash came back as a scope violation. A convention a human has to remember, whose
    failure mode is silent and total, is not a convention worth keeping.

    The prefix match is anchored at a separator, so `src/app` never covers `src/app.py` — the
    reason the exact/prefix distinction existed at all is preserved without the spelling.
    """
    prefix = pattern.rstrip("/")
    if not prefix:
        return False
    return path == prefix or path.startswith(prefix + "/")


def longest_cover(path: str, patterns: Iterable[str]) -> str | None:
    """The most specific pattern covering `path` ("" is never one), or None.

    Longest wins, so the answer does not depend on the caller's iteration order — and an exact
    match wins over any prefix automatically, because a prefix that covers a path can only be
    shorter than it.
    """
    best: str | None = None
    for pattern in patterns:
        if path_covered(path, pattern) and (best is None or len(pattern.rstrip("/")) > len(best.rstrip("/"))):
            best = pattern
    return best


EXIT_DONE = 0
#: A task could not pass the quality gate, or the frontier is empty with work left. The verdict
#: is real and a human has to act on it — re-running changes nothing.
EXIT_HUMAN_NEEDED = 1
#: The run refused to start, or the machine failed in a way waiting cannot fix (an unapproved
#: gate, a draft plan, an agent CLI that is not on PATH, an unpinned sandbox image). Repair the
#: named thing first; re-running before that is wasted.
EXIT_CANNOT_PROCEED = 2
#: The machine failed in a way time alone fixes — agent capacity exhausted, a supervisor's
#: SIGTERM, another run holding the build lock. **No task was marked**, no retry budget was
#: spent, and preserved work is waiting: re-run later and it continues from where it stopped.
EXIT_RETRY_LATER = 3


class StopLoop(Exception):
    """A cause to stop the build loop and escalate to the human. `code` is the exit code.

    Raised by the orchestration layers (build_loop and the git/worktree layer it drives);
    defined here so neither has to import the other for it.
    """

    def __init__(self, message: str, code: int = EXIT_HUMAN_NEEDED) -> None:
        super().__init__(message)
        self.code = code
