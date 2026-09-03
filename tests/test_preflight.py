"""What the loop refuses to start on, and why each refusal is cheaper than the failure it replaces.

Every problem here was already discoverable from config.yaml and the machine. What happened
instead was that the run took its lock, opened its control plane, cut a worktree, wrote a dossier
and launched a model — and *then* reported that podman was not installed. `faults` classifies those
correctly once the run is under way; this is the same line drawn one step earlier, where it costs
nothing.

The second thing pinned here is the narrowness: a profile no configured step names cannot stop this
build, so it is not checked. `rein doctor` is the whole-repository diagnosis, and refusing to start
over something that will never run would be a worse failure than the one being prevented.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest

from rein import adapters, models, preflight
from tests import conftest

_PINNED = {"kind": "oci", "image": "localhost/rein-python@sha256:" + "0" * 64, "network_profile": "none"}
_HOST = {"kind": "host"}


def _config(**profiles: dict[str, Any]) -> models.Config:
    return models.Config(
        {
            "project": {"name": "demo", "work_branch": "work"},
            "executors": {
                "implementer_profile": "impl",
                "reviewer_profile": "rev",
                "quality_gate_profile": "quality",
            },
            "executor_profiles": profiles or {"impl": _HOST, "rev": _HOST, "quality": _HOST},
        }
    )


def _step(name: str, **overrides: Any) -> models.GateStep:
    return models.GateStep({"name": name, "kind": "command", "command": ["make", name], **overrides})


_ON_PATH = {"implementer": ["sh"]}


# --- the required flag nothing read -------------------------------------------


def test_a_required_step_with_no_command_refuses_to_start() -> None:
    """`required` has sat in the schema and in `GateStep`'s docstring — "makes the loop refuse to
    start, before any implementer has been paid for" — read by nothing. An empty command is
    otherwise a silent skip, which is the DoD quietly not asking a question it was configured to
    ask."""
    steps = [_step("test", command=[], required=True)]
    problems = preflight.check(_config(), steps, _ON_PATH, runtime=None)
    assert [p.what for p in problems] == ["quality-gate step 'test' is required but has no `command:` to run"]


def test_an_optional_step_with_no_command_is_still_a_silent_skip() -> None:
    """Right for a library with no entry point — the packaged `smoke` step ships exactly this way."""
    steps = [_step("smoke", command=[], required=False)]
    assert preflight.check(_config(), steps, _ON_PATH, runtime=None) == []


def test_required_is_an_explicit_opt_in() -> None:
    """It read as True unless the key said `false`, contradicting its own documentation and the
    packaged config's own comment on `smoke`. Harmless while nothing consulted the flag; now that
    it decides whether a run starts, that default would refuse every repository whose config simply
    never mentioned it."""
    steps = [_step("test", command=[])]  # no `required:` key at all
    assert preflight.check(_config(), steps, _ON_PATH, runtime=None) == []


def test_an_agent_step_needs_no_command() -> None:
    steps = [models.GateStep({"name": "review", "kind": "agent", "agent_role": "code_reviewer", "required": True})]
    assert preflight.check(_config(), steps, _ON_PATH, runtime=None) == []


# --- the sandbox ----------------------------------------------------------------


def test_a_sandboxed_step_with_no_container_runtime_refuses_to_start() -> None:
    config = _config(impl=_HOST, rev=_HOST, quality=_PINNED)
    problems = preflight.check(config, [_step("test", executor_profile="quality")], _ON_PATH, runtime=None)
    assert len(problems) == 1
    assert "no container runtime on PATH" in problems[0].what and "'quality'" in problems[0].what


def test_a_host_only_run_needs_no_container_runtime() -> None:
    """Running unsandboxed is a policy finding `doctor` reports, not a reason this run cannot go."""
    assert preflight.check(_config(), [_step("test")], _ON_PATH, runtime=None) == []


def test_a_profile_no_step_reaches_is_not_checked() -> None:
    """It cannot stop this build. Refusing over it would be a worse failure than the prevented one."""
    config = models.Config(
        {
            "project": {"name": "demo", "work_branch": "work"},
            "executors": {"implementer_profile": "impl", "reviewer_profile": "rev"},
            "executor_profiles": {"impl": _HOST, "rev": _HOST, "unused": _PINNED},
        }
    )
    assert preflight.check(config, [_step("test", executor_profile="impl")], _ON_PATH, runtime=None) == []


def test_an_unresolvable_pin_names_the_command_that_builds_it() -> None:
    config = _config(impl=_HOST, rev=_HOST, quality=_PINNED)
    problems = preflight.check(config, [_step("test", executor_profile="quality")], _ON_PATH, runtime="nonesuch-engine")
    assert len(problems) == 1
    assert "rein oci build" in problems[0].remedy


# --- the agent CLIs -------------------------------------------------------------


def test_a_missing_agent_cli_refuses_to_start() -> None:
    """It exits 127 and `faults` reads it as ENV_PERMANENT — after the lock, the worktree and the
    dossier were all paid for."""
    problems = preflight.check(_config(), [_step("test")], {"implementer": ["definitely-not-a-real-cli"]}, runtime=None)
    assert len(problems) == 1
    assert "not on PATH" in problems[0].what


def test_a_missing_adapter_is_told_how_to_be_installed_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The remedy names the command; nothing here runs it. Installing an agent CLI on someone's
    behalf decides for them what reaches their machine."""
    monkeypatch.setattr(shutil, "which", lambda name: None)
    problems = preflight.check(_config(), [_step("test")], {"implementer": ["copilot"]}, runtime=None)
    assert adapters.ADAPTER_TABLE["copilot"].install_hint in problems[0].remedy
    assert "rein agent <cli> --role implementer" in problems[0].remedy


def test_a_role_with_no_command_configured_is_reported() -> None:
    problems = preflight.check(_config(), [_step("test")], {"code_reviewer": []}, runtime=None)
    assert [p.what for p in problems] == ["role 'code_reviewer' has no agent command configured"]


# --- exhaustiveness -------------------------------------------------------------


def test_every_problem_is_reported_at_once() -> None:
    """Same rule as `approve.readiness`: handing somebody one blocker, waiting for them to fix it,
    and only then mentioning the next is the friction that rule was written against."""
    config = _config(impl=_HOST, rev=_HOST, quality=_PINNED)
    steps = [_step("test", executor_profile="quality"), _step("check", command=[], required=True)]
    problems = preflight.check(config, steps, {"implementer": ["definitely-not-a-real-cli"]}, runtime=None)
    assert len(problems) == 3


def test_every_problem_renders_with_a_remedy() -> None:
    config = _config(impl=_HOST, rev=_HOST, quality=_PINNED)
    problems = preflight.check(config, [_step("test", executor_profile="quality")], {"implementer": []}, runtime=None)
    assert problems and all("→" in p.render() for p in problems)


# --- the suite's own environment ------------------------------------------------


def test_the_agent_cli_the_suite_preflights_is_the_stub_and_not_the_host_s() -> None:
    """Every full-loop test passes this check, so on a machine with `claude` installed it would
    pass for a reason that is not about the code — and refuse to start everywhere else. The stub
    `conftest` puts on PATH is what production's `shutil.which` must find."""
    for name in conftest.STUBBED_AGENT_CLIS:
        found = shutil.which(name)
        assert found is not None, f"{name} is not on the suite's PATH"
        assert Path(found).parent.name.startswith("agent-cli"), f"{name} resolved to the host's {found}"
