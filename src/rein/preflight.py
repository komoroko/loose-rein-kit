"""What has to be true before the first implementer is launched.

Every check here answers a question that was already answerable — and was instead discovered
several agent launches and one quality-gate run later, phrased as a task's failure. A container
runtime that is not installed, a pinned image nobody built on this machine, an agent CLI that is
not on PATH, a DoD step marked `required` with no command to run: none of them are facts about
any task, and all of them are knowable from config.yaml and the machine before a model is paid
for. `faults` already refuses to record them as verdicts once the run is underway; this is the
same line drawn one step earlier, where it costs nothing at all.

Two rules keep it from becoming a second `doctor`:

  **Only what this run will use.** A profile no configured step names, a role no step launches —
  neither can stop this build, so neither is checked. `rein doctor` is the whole-repository
  diagnosis and says so; refusing to start over something that will never run would be a worse
  failure than the one being prevented.

  **Only what is decidable without running anything.** Whether `make test` passes is the quality
  gate's question. Whether `make` exists at all is not answerable here either — it runs inside a
  sandbox whose PATH this process cannot see — so it is left to the gate, where the failure at
  least carries the command's own output.
"""

from __future__ import annotations

import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from rein import adapters, executors, models


@dataclass(frozen=True)
class Problem:
    """One reason this run must not start, and the command that repairs it."""

    what: str
    remedy: str

    def render(self) -> str:
        return f"{self.what}\n    → {self.remedy}"


def _required_without_command(steps: Sequence[models.GateStep]) -> list[Problem]:
    """A DoD step declared `required` that has nothing to run.

    An empty `command` is normally a silent skip, which is right for a library with no entry point
    — and wrong the moment somebody marks the step required, because then the skip is the DoD
    quietly not asking the question it was configured to ask. `required` has been in the schema and
    in `GateStep`'s docstring, promising exactly this refusal, and nothing read it.
    """
    problems = []
    for step in steps:
        if step.kind == "command" and step.required and not step.command:
            problems.append(
                Problem(
                    f"quality-gate step {step.name!r} is required but has no `command:` to run",
                    "give it a command in .rein/config.yaml under quality_gate, or drop `required: true`",
                )
            )
    return problems


def _profiles_used(
    config: models.Config, steps: Sequence[models.GateStep], roles: Sequence[str]
) -> dict[str, models.ExecutorProfile]:
    """Every executor profile this run can actually reach, by name."""
    used: dict[str, models.ExecutorProfile] = {}
    profiles = config.profiles
    for step in steps:
        named = profiles.get(step.executor_profile) if step.executor_profile else None
        resolved = named or config.profile_for("quality_gate")
        if resolved is not None:
            used[resolved.name] = resolved
    for role in roles:
        resolved = config.profile_for(role)
        if resolved is not None:
            used[resolved.name] = resolved
    return used


def _sandbox_problems(used: dict[str, models.ExecutorProfile], *, runtime: str | None) -> list[Problem]:
    """Can each sandboxed profile this run needs actually be entered?

    A `host` profile has nothing to check — `doctor` reports separately that it is running
    repository code unsandboxed, which is a policy finding rather than a reason this run cannot
    proceed.
    """
    sandboxed = {name: p for name, p in used.items() if p.is_sandboxed}
    if not sandboxed:
        return []
    if runtime is None:
        return [
            Problem(
                "no container runtime on PATH, and "
                + ", ".join(f"{name!r}" for name in sorted(sandboxed))
                + " run in an OCI sandbox",
                "install docker or podman",
            )
        ]
    problems = []
    for name, profile in sorted(sandboxed.items()):
        _, problem = executors.resolve_pinned(profile, runtime=runtime)
        if problem:
            problems.append(
                Problem(f"profile {name!r} cannot be entered: {problem}", models.sandbox_setup_command([name]))
            )
    return problems


def _cli_problems(argv_by_role: Mapping[str, Sequence[str]]) -> list[Problem]:
    """Is each agent CLI this run launches on PATH?

    The one failure the loop already classifies correctly and still pays for: a missing CLI exits
    127 and `faults` reads it as ENV_PERMANENT — after the run has taken its lock, opened its
    control plane, cut a worktree and written a dossier for a launch that was never going to happen.
    """
    problems = []
    for role, argv in sorted(argv_by_role.items()):
        if not argv:
            problems.append(
                Problem(
                    f"role {role!r} has no agent command configured",
                    "set its `adapter:` in .rein/config.yaml under agents",
                )
            )
        elif shutil.which(argv[0]) is None:
            # The remedy names the install command rather than the word "install": `rein` never
            # runs a third-party installer — what lands on an operator's PATH is theirs to choose.
            record = adapters.adapter_for(argv)
            hint = record.install_hint if record is not None else ""
            problems.append(
                Problem(
                    f"role {role!r} launches {argv[0]!r}, which is not on PATH",
                    (f"install it with `{hint}`" if hint else f"install {argv[0]}")
                    + ", or point that role at a different adapter with `rein agent <cli> --role "
                    + f"{role}`",
                )
            )
    return problems


def check(
    config: models.Config,
    steps: Sequence[models.GateStep],
    argv_by_role: Mapping[str, Sequence[str]],
    *,
    runtime: str | None,
) -> list[Problem]:
    """Everything that would stop this run, found before it starts. Empty = go.

    `runtime` is the container engine the caller found (`executors.container_runtime()`), or None
    for "there is none". Passed in rather than probed here so that what this function decides is a
    function of its arguments — the machine it happens to run on is the one input a check like this
    must not read behind the caller's back.

    Exhaustive rather than short-circuiting, like `approve.readiness`: telling somebody their CLI
    is missing, waiting for them to install it, and only then mentioning the unbuilt image is the
    same friction one layer down from where that rule was written.
    """
    return [
        *_required_without_command(steps),
        *_sandbox_problems(_profiles_used(config, steps, ("implementer", "reviewer")), runtime=runtime),
        *_cli_problems(argv_by_role),
    ]
