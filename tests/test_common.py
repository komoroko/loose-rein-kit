"""Tests for common.py — subprocess execution and failure summarization.

What is *not* here any more is the point: the front-matter parser, the gate-line surgery, and
the phase/gate vocabulary all moved out (to models/strict_yaml), so this file only covers
running a command safely and reducing its output to something a retry can act on.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from rein import common

# --- run: the timeout convention and the process-group kill -------------------


def test_run_returns_output_and_rc() -> None:
    rc, out = common.run([sys.executable, "-c", "print('ok')"])
    assert rc == 0
    assert "ok" in out


def test_run_kills_a_hung_process_with_rc_124() -> None:
    rc, out = common.run([sys.executable, "-c", "import time; time.sleep(30)"], timeout=0.2)
    assert rc == common.RC_TIMEOUT  # the coreutils convention; a hang must not stall the loop
    assert "timed out after 0s (process group killed)" in out


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
@pytest.mark.integration
def test_a_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    """The bug this guards: a timed-out step kills its own process but leaves the children running.

    A `make test` that spawned pytest, which spawned a server, left the server holding the
    port — and the next run failed for a reason that had nothing to do with the code.
    """
    marker = tmp_path / "child.pid"
    script = textwrap.dedent(f"""
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open({str(marker)!r}, "w").write(str(child.pid))
        time.sleep(60)
    """)
    rc, _ = common.run([sys.executable, "-c", script], timeout=1.5)
    assert rc == common.RC_TIMEOUT

    child_pid = int(marker.read_text())
    for _ in range(50):  # the kill is asynchronous; give the group a moment to die
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    os.kill(child_pid, signal.SIGKILL)  # do not leak the process if the assertion is about to fail
    raise AssertionError(f"child {child_pid} outlived the timed-out parent")


# --- cancellation: the only thing that ends a launch early --------------------
#
# `concurrent.futures.Future.cancel()` cannot stop a task that has started, and a
# ThreadPoolExecutor joins its workers on the way out of the `with` *and* again at interpreter
# exit. So a caller that stops wanting an answer has exactly one way to stop paying for it.


def test_a_cancelled_run_returns_at_once_instead_of_at_its_own_pace() -> None:
    token = common.Cancellation()
    threading.Timer(0.2, token.cancel).start()
    began = time.monotonic()
    with common.cancelling(token):
        rc, out = common.run([sys.executable, "-c", "import time; time.sleep(30)"])
    assert rc == common.RC_CANCELLED  # not RC_TIMEOUT: nothing ran out of time
    assert time.monotonic() - began < 10
    assert "cancelled by the caller" in out


def test_a_token_tripped_before_the_launch_kills_it_anyway() -> None:
    """The window between Popen returning and the token taking the process is why `cancel` cannot
    simply be "kill it": for a moment there is nothing to kill, and losing that race would leave a
    launch running that somebody had already given up on."""
    token = common.Cancellation()
    token.cancel()
    began = time.monotonic()
    with common.cancelling(token):
        rc, _ = common.run([sys.executable, "-c", "import time; time.sleep(30)"])
    assert rc == common.RC_CANCELLED
    assert time.monotonic() - began < 10


def test_the_token_only_binds_its_own_thread() -> None:
    token = common.Cancellation()
    with common.cancelling(token):
        pass
    token.cancel()  # a token nothing is bound to has nothing to kill and must not raise
    rc, out = common.run([sys.executable, "-c", "print('ok')"])
    assert (rc, out.strip()) == (0, "ok")


@pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process groups only")
@pytest.mark.integration
def test_an_interrupted_run_kills_the_launch_instead_of_orphaning_it(tmp_path: Path) -> None:
    """Ctrl-C used to leave the agent running.

    `start_new_session=True` puts the child in its own session, so the terminal's SIGINT reaches
    this process and not it — while `Popen.__exit__` declines to wait on a KeyboardInterrupt
    precisely because it assumes the opposite. Measured before the fix: the launch was orphaned and
    went on running, holding its quota, with nobody left to read what it said.
    """
    marker = tmp_path / "child.pid"
    script = textwrap.dedent(f"""
        import os, signal, sys, threading, time
        from rein import common
        threading.Thread(
            target=lambda: (time.sleep(0.4), os.kill(os.getpid(), signal.SIGINT)), daemon=True
        ).start()
        try:
            common.run([sys.executable, "-c",
                "import os, time; open({str(marker)!r}, 'w').write(str(os.getpid())); time.sleep(60)"])
        except KeyboardInterrupt:
            print("interrupted")
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert "interrupted" in proc.stdout
    pid = int(marker.read_text(encoding="utf-8"))
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.1)
    os.kill(pid, signal.SIGKILL)
    raise AssertionError(f"the launch {pid} outlived the interrupt")


def test_run_reports_a_missing_command_instead_of_raising() -> None:
    rc, out = common.run(["definitely-not-a-real-command-xyz"])
    assert rc == 127
    assert "could not run" in out


def test_run_replaces_the_environment_when_given_one() -> None:
    # An executor profile's allowlist is only an allowlist if nothing leaks in around it.
    os.environ["REIN_TEST_LEAK"] = "leaked"
    try:
        rc, out = common.run(
            [sys.executable, "-c", "import os; print(os.environ.get('REIN_TEST_LEAK', 'absent'))"],
            env={"PATH": os.environ.get("PATH", "")},
        )
    finally:
        del os.environ["REIN_TEST_LEAK"]
    assert rc == 0
    assert "absent" in out


def test_run_caps_pathological_output() -> None:
    original = common.MAX_OUTPUT_BYTES
    common.MAX_OUTPUT_BYTES = 1000
    try:
        rc, out = common.run([sys.executable, "-c", "print('x' * 50000)"])
    finally:
        common.MAX_OUTPUT_BYTES = original
    assert rc == 0
    assert "output truncated" in out
    assert len(out) < 2000


def test_run_passes_stdin() -> None:
    rc, out = common.run([sys.executable, "-c", "import sys; print(sys.stdin.read().strip())"], input_text="hello")
    assert rc == 0
    assert "hello" in out


# --- summarize_failure --------------------------------------------------------


def test_summary_keeps_only_the_salient_lines() -> None:
    output = "\n".join(
        [
            "collecting ...",
            "tests/test_a.py::test_ok PASSED",
            "tests/test_b.py::test_bad FAILED",
            "E   assert 1 == 2",
            "=========== 1 failed, 1 passed ===========",
        ]
    )
    summary = common.summarize_failure("make test", 1, output)
    assert "FAILED" in summary and "assert 1 == 2" in summary
    assert "PASSED" not in summary.split("\n", 2)[-1].replace("1 failed, 1 passed", "")
    assert summary.startswith("$ make test (rc=1)")


def test_summary_does_not_match_error_inside_an_identifier() -> None:
    # "test_error_handling PASSED" is a pass, not a failure. A match that was not word-bounded
    # would drag every green line carrying "error" in its name into the retry prompt.
    output = "\n".join(
        [
            "tests/test_x.py::test_error_handling PASSED",
            "tests/test_x.py::test_raises_ValueError PASSED",
            "tests/test_y.py::test_real FAILED",
        ]
    )
    summary = common.summarize_failure("make test", 1, output)
    assert "FAILED" in summary
    assert "PASSED" not in summary


def test_summary_falls_back_to_the_tail_when_nothing_matches() -> None:
    summary = common.summarize_failure("make check", 2, "one\ntwo\nthree\n")
    assert "three" in summary


def test_summary_is_budget_capped() -> None:
    output = "\n".join(f"tests/test_{i}.py::t FAILED" for i in range(500))
    summary = common.summarize_failure("make test", 1, output)
    assert len(summary) <= 1700
    assert "line(s) omitted" in summary


def test_summary_of_empty_output_is_just_the_header() -> None:
    assert common.summarize_failure("make test", 1, "   \n \n") == "$ make test (rc=1)"


# --- StopLoop -----------------------------------------------------------------


def test_stop_loop_carries_an_exit_code() -> None:
    exc = common.StopLoop("blocked", code=3)
    assert str(exc) == "blocked"
    assert exc.code == 3
    assert common.StopLoop("x").code == 1


# --- logging ------------------------------------------------------------------


def test_configure_logging_is_idempotent_and_follows_stderr(capsys: pytest.CaptureFixture[str]) -> None:
    import logging

    common.configure_logging()
    common.configure_logging()
    logger = logging.getLogger("rein.test")
    logger.error("a diagnostic")
    captured = capsys.readouterr()
    assert captured.err.count("a diagnostic") == 1  # one handler, not two
    assert captured.out == ""  # diagnostics never pollute the command's result on stdout


def test_common_stays_stdlib_only_at_import_time() -> None:
    """The gate guard imports this on every editor write; a heavy import would be felt."""
    source = Path(common.__file__).read_text(encoding="utf-8")
    head = source.split("# --- diagnostics logging", 1)[0]
    assert "import yaml" not in head
    assert "from rein" not in head


# --- repo-relative path patterns ----------------------------------------------
#
# One rule shared by `guard.paths` and a task's `scope`. The trailing slash used to decide between
# "prefix" and "exact file", which meant a directory written without it matched nothing at all.


def test_a_pattern_covers_its_own_path() -> None:
    assert common.path_covered("src/a.py", "src/a.py")


def test_the_trailing_slash_changes_nothing() -> None:
    assert common.path_covered("src/rein/brief.py", "src/rein/") is True
    assert common.path_covered("src/rein/brief.py", "src/rein") is True


def test_the_prefix_is_anchored_at_a_separator() -> None:
    """`src/app` must not swallow `src/app.py` — the reason the exact case existed."""
    assert common.path_covered("src/app.py", "src/app") is False
    assert common.path_covered("src/app/main.py", "src/app") is True


def test_an_empty_pattern_covers_nothing() -> None:
    """ "" and "/" would otherwise cover the whole repository by accident."""
    assert common.path_covered("src/a.py", "") is False
    assert common.path_covered("src/a.py", "/") is False


def test_the_most_specific_pattern_wins_regardless_of_order() -> None:
    patterns = ["docs/", "docs/tasks/", "docs/tasks/T-001.md"]
    assert common.longest_cover("docs/tasks/T-001.md", patterns) == "docs/tasks/T-001.md"
    assert common.longest_cover("docs/tasks/T-002.md", reversed(patterns)) == "docs/tasks/"
    assert common.longest_cover("src/a.py", patterns) is None
