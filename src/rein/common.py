"""Shared primitives: diagnostics logging, safe subprocess execution, failure summarization.

Nothing here reads a document: the vocabulary lives in :mod:`rein.models`, parsing in
:mod:`rein.strict_yaml`, and paths on :class:`rein.repo.Repo`. That is what keeps this
module stdlib-only and cheap to import on the gate-guard hook path, which fires on every
editor write.

:func:`run` gives every launch its own process group and kills the whole group on expiry.
Killing only the process started leaves its children alive — a `make test` that spawned pytest,
which spawned a server, would leave the server holding the port and fail the next run for the
wrong reason.
"""

from __future__ import annotations

import logging
import re
import sys
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
#: Output past this is truncated with a marker. A runaway command must not exhaust memory
#: before its timeout fires.
MAX_OUTPUT_BYTES = 4 * 1024 * 1024


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

    `env`, when given, *replaces* the environment rather than extending it — an executor
    profile's allowlist is only an allowlist if nothing leaks in around it.
    """
    import os
    import signal
    import subprocess  # lazy: keep `import common` light for the hook path (gate_guard on every edit)

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
        try:
            output, _ = proc.communicate(input=input_text, timeout=timeout)
            return proc.returncode, _cap(output or "")
        except subprocess.TimeoutExpired:
            # subprocess's own timeout handling kills the direct child only, so anything it
            # spawned survives — a stuck server keeps the port and the next run fails for a
            # reason that has nothing to do with the code. Kill the whole group instead.
            _kill_group(proc, os, signal)
            output, _ = proc.communicate()
            elapsed = int(timeout) if timeout is not None else 0
            return RC_TIMEOUT, _cap(f"{output or ''}\ntimed out after {elapsed}s (process group killed)")


def _kill_group(proc: Any, os_mod: Any, signal_mod: Any) -> None:
    """SIGKILL the whole process group, falling back to the single process where that fails."""
    if hasattr(os_mod, "killpg"):
        try:
            os_mod.killpg(os_mod.getpgid(proc.pid), signal_mod.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone, or a platform that will not let us — fall through
    proc.kill()


def _cap(text: str) -> str:
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return text
    kept = encoded[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
    return f"{kept}\n… (output truncated at {MAX_OUTPUT_BYTES} bytes)"


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
