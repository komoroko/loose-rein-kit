"""Tests for common.py — subprocess execution and failure summarization.

What is *not* here any more is the point: the front-matter parser, the gate-line surgery, and
the phase/gate vocabulary all moved out (to models/strict_yaml), so this file only covers
running a command safely and reducing its output to something a retry can act on.
"""

from __future__ import annotations

import os
import signal
import sys
import textwrap
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
