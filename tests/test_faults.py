"""Tests for faults.py — the line between "the code failed" and "the machine failed".

The distinction this module draws is the one the build loop records into an append-only audit
chain, so getting it wrong is not recoverable by editing anything afterwards. Two asymmetries
are deliberate and defended here:

  - an **agent launch** can never be classified CONTENT (launching produces no verdict, so a
    nonzero rc is by construction about the machine), and
  - a **command step** is CONTENT unless it could not be run at all (it exists to judge code).
"""

from __future__ import annotations

import pytest

from rein import common, faults

UNLAUNCHABLE = "could not run 'claude': [Errno 2] No such file or directory: 'claude'"


# --- launches -----------------------------------------------------------------


@pytest.mark.parametrize(
    "rc, output",
    [
        (1, "You've hit your session limit · resets 3:30am (Asia/Tokyo)"),
        (143, ""),  # a supervisor's SIGTERM, as a shell reports it
        (-15, ""),  # the same kill, as subprocess reports it
        (-9, ""),  # OOM killer
        (-1, ""),  # the terminal closed (SIGHUP)
        (common.RC_TIMEOUT, "timed out after 3600s (process group killed)"),
        (1, "some failure nobody has a rule for"),
    ],
)
def test_a_launch_the_machine_stopped_is_worth_another_try(rc: int, output: str) -> None:
    assert faults.classify_launch(rc, output) is faults.Fault.ENV_TRANSIENT


@pytest.mark.parametrize(
    "rc, output",
    [
        (127, UNLAUNCHABLE),
        (1, "Invalid API key · Please run /login"),
        (1, "authentication_error: unauthorized"),
    ],
)
def test_a_launch_that_will_never_work_says_so(rc: int, output: str) -> None:
    """Credentials and a missing binary do not resolve by waiting.

    Classifying them transient would leave a supervisor loop retrying every fifteen minutes,
    forever, against a CLI that is never going to log itself in.
    """
    assert faults.classify_launch(rc, output) is faults.Fault.ENV_PERMANENT


def test_a_launch_is_never_the_codes_fault() -> None:
    """The structural claim: the gate steps run *after* the launch, so a launch has no verdict
    to give about any task's code, whatever it exits with."""
    for rc in (1, 2, 3, 42, 127, 137, 143, -15):
        assert faults.classify_launch(rc, "anything at all") is not faults.Fault.CONTENT


# --- command steps ------------------------------------------------------------


def test_a_step_that_could_not_be_run_is_the_machines_fault() -> None:
    assert faults.classify_step(127, "could not run 'make': [Errno 2] ...") is faults.Fault.ENV_PERMANENT


@pytest.mark.parametrize(
    "rc, output",
    [
        (1, "2 failed, 40 passed"),
        (common.RC_TIMEOUT, "timed out after 1800s"),  # a hanging test suite is a fact about the code
        (127, "make: cc: No such file or directory"),  # make's own 127: no marker, so CONTENT
    ],
)
def test_a_step_is_judging_the_code_unless_it_could_not_run(rc: int, output: str) -> None:
    assert faults.classify_step(rc, output) is faults.Fault.CONTENT


def test_the_unlaunchable_marker_needs_both_halves() -> None:
    """A test that merely *prints* the phrase must not be read as an environment fault."""
    assert faults.classify_step(1, "could not run 'x': ...") is faults.Fault.CONTENT
    assert faults.classify_step(127, "log line\ncould not run 'x': ...") is faults.Fault.CONTENT


@pytest.mark.parametrize(
    "output",
    [
        "pip._vendor.urllib3.exceptions.NewConnectionError: Temporary failure in resolving 'pypi.org'",
        "socket.gaierror: [Errno -2] Name or service not known",
        "npm error code ENOTFOUND\nnpm error errno ENOTFOUND\n"
        "npm error network request to https://registry.npmjs.org failed, reason: getaddrinfo ENOTFOUND",
        "curl: (6) Could not resolve host: github.com",
        "connect: Network is unreachable",
    ],
)
def test_a_dns_or_network_unreachable_step_is_the_sandboxs_fault(output: str) -> None:
    """`network: none` fails a dependency-resolving step the same way on every retry — that is a
    fact about the sandbox policy, not the code, so it must not spend the step's retry budget."""
    assert faults.classify_step(1, output) is faults.Fault.ENV_PERMANENT


def test_an_ordinary_content_failure_is_not_read_as_network() -> None:
    """`ConnectionError`/timeout-shaped words alone must not trip this — only the OS/tooling
    resolver's own unambiguous strings do, so a test asserting its own connection-handling logic
    is not misclassified."""
    assert faults.classify_step(1, "ConnectionError: could not connect (mocked)") is faults.Fault.CONTENT
    assert not faults.is_network_unreachable("2 failed, 40 passed")


# --- capacity, and what a human is told ---------------------------------------


@pytest.mark.parametrize(
    "output",
    [
        "You've hit your session limit · resets 3:30am (Asia/Tokyo)",
        "usage limit reached",
        "429 Too Many Requests",
        "rate_limit_error",
        "overloaded_error",
    ],
)
def test_capacity_exhaustion_is_recognized(output: str) -> None:
    assert faults.is_capacity(output)


def test_an_ordinary_failure_is_not_capacity() -> None:
    assert not faults.is_capacity("AssertionError: expected 3, got 4")


def test_the_reset_time_is_quoted_not_parsed() -> None:
    hint = faults.reset_hint("You've hit your session limit · resets 3:30am (Asia/Tokyo)")
    assert "3:30am" in hint and "Asia/Tokyo" in hint
    assert faults.reset_hint("nothing about time here") == ""


# --- the exception ------------------------------------------------------------


def test_classifying_a_success_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        faults.classify_launch(0, "")
    with pytest.raises(ValueError):
        faults.classify_step(0, "")


def test_an_environment_fault_is_not_a_stop_loop() -> None:
    """The whole point of the type. Every `except StopLoop` in the loop treats its subject as a
    task that needs a human; this one is not about a task at all, so it must not be caught there.
    """
    fault = faults.EnvironmentFault(faults.Fault.ENV_TRANSIENT, where="T-001: implementer", rc=1, output="")
    assert not isinstance(fault, common.StopLoop)


def test_the_summary_tells_the_reader_nothing_was_marked() -> None:
    fault = faults.EnvironmentFault(
        faults.Fault.ENV_TRANSIENT,
        where="T-018: implementer",
        rc=1,
        output="You've hit your session limit · resets 3:30am (Asia/Tokyo)",
    )
    summary = fault.summary()
    assert fault.retryable
    assert "agent capacity" in summary
    assert "Nothing was marked" in summary
    assert "3:30am" in summary


def test_a_permanent_fault_does_not_invite_a_re_run() -> None:
    fault = faults.EnvironmentFault(faults.Fault.ENV_PERMANENT, where="T-001: implementer", rc=127, output=UNLAUNCHABLE)
    assert not fault.retryable
    assert "will not help" in fault.summary()


def test_a_network_unreachable_fault_points_at_the_pinned_image_not_a_re_run() -> None:
    """A retry cannot fix a `network: none` sandbox — the advice must say what actually helps
    (bake the dependency into the image), not tell someone to wait or just try again."""
    fault = faults.EnvironmentFault(
        faults.Fault.ENV_PERMANENT,
        where="gate step 'test'",
        rc=1,
        output="pip.exceptions.NewConnectionError: Temporary failure in resolving 'pypi.org'",
    )
    summary = fault.summary()
    assert not fault.retryable
    assert "pinned image" in summary
    assert "Nothing was marked" not in summary
