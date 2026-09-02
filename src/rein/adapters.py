"""What each agent CLI this release can launch is able to do, as data rather than as branches.

Every launch in the system goes through one of these records: the build loop launches
implementers, fixers and quality-gate agent steps; the review pipeline launches the three gate-④
reviewer stages. They ask the same three questions — how do I start this CLI, may it write, and
can it tell me what the launch cost — so the answers live here rather than inside either caller.

They lived inside `build_loop`, which made the review transport reach for a build-loop symbol and
`agent_cli`, `doctor` and `review` each write a function-local `from rein import build_loop` to
dodge the import cycle that reach created. The cycle was never the problem; the placement was.

One rule is enforced here rather than at each launch site: a role whose adapter this release does
not know, or whose configured `model` this release cannot pass to that CLI, is **refused**
(`launch_refusal`, `launch_argv`). Launching the CLI's default under another model's name would
declare a separation nothing performs, and the gate-④ independence check is derived from exactly
that name.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from rein import models
from rein import usage as usage_mod


class LaunchRefused(Exception):
    """A role cannot be launched as configured — an unknown adapter, or an unpassable model."""


#: The review disciplines rein's prompts ask for, by the neutral name they use. A host that has
#: its own is named in `Adapter.disciplines`; one that has not gets the question written out in
#: the prompt, which is the floor either way (`build_prompts`).
CORRECTNESS = "correctness"
SIMPLIFICATION = "simplification"
SECURITY = "security"


@dataclass(frozen=True)
class Adapter:
    """What one agent CLI can do, as a declaration rather than as branches spread through the loop.

    This started as two hard-coded dicts and one `if adapter == "claude"`, and every one of them
    was load-bearing in a way nothing could see from outside. The write flags decided whether an
    implementer could change a byte; the claude test decided whether a retry re-read the whole
    ticket from cold; and nothing recorded that `codex` brings its own process sandbox — which is
    what fails, unfixably, when `rein` is itself already running inside a container.

    Making these fields lets `doctor` reason about the combination instead of the operator
    discovering it as a task that mysteriously changed nothing.
    """

    name: str
    #: How to launch it headless. The prompt is appended as the last argv element.
    argv: tuple[str, ...]
    #: What makes it able to change the tree. Empty when it can already. `codex exec` runs
    #: read-only unless told otherwise, so without this every task produced an empty diff and
    #: nothing said why. The review transport (`review_transport`) deliberately never gets these — a
    #: reviewer that cannot write is the point.
    write_flags: tuple[str, ...] = ()
    #: How to stamp a launch with a session id, and how to resume that id. Both empty means the
    #: CLI gets a fresh launch per retry: it re-reads its ticket, its design slice and the code
    #: from cold every time, which is the single largest avoidable cost in a long build.
    #: `codex` is empty on purpose and not for lack of a resume verb — it has one, but it resumes
    #: *the last session*, and with `max_parallel` leaves in flight there is no way to say which
    #: session that is. Guessing would hand one leaf another leaf's context.
    session_flags: tuple[str, ...] = ()
    resume_flags: tuple[str, ...] = ()
    #: Whether the CLI establishes its own process isolation (seccomp/landlock/bwrap) around the
    #: work it does. Two nested sandboxes is not twice as safe: the inner one needs kernel
    #: features the outer one has already dropped, and it fails at the point where the agent tries
    #: to write, with a message that reaches nobody.
    own_sandbox: bool = False
    #: What makes it report what a launch cost — the token counts, the model that answered, the
    #: price — instead of bare text. Empty for a CLI whose envelope this release has not seen: an
    #: unreported launch is recorded as unmeasured, never as zero (`usage.Usage.unavailable`).
    #: Adding flags here without an `envelope` to read them is how the answer stops parsing.
    usage_flags: tuple[str, ...] = ()
    #: Reads that CLI's envelope into `(the answer, what it cost)`. Paired with `usage_flags`.
    envelope: Callable[[str], tuple[str, usage_mod.Usage]] | None = None
    #: How to tell this CLI which model to run. Empty for one whose flag this release has not
    #: verified — and an unapplied model is not a small thing here: `agents.<role>.model` is what
    #: the gate-④ independence check is derived from, so a config naming a model the launcher
    #: cannot pass would declare a separation nothing performs. `launch_argv` refuses that
    #: combination rather than launching the CLI's default under another model's name.
    model_flags: tuple[str, ...] = ()
    #: The host's own review disciplines, keyed by the neutral names above. Prompts name a
    #: discipline, never a host's command, and this is where the two meet — the same arrangement
    #: AGENTS.md's capability vocabulary makes for the human-facing verbs. rein's prompts carried
    #: `/code-review` and `/simplify` as bare prose for several releases: real commands, named in
    #: text that also runs under `codex` and `gemini`, where they mean nothing at all.
    #:
    #: A discipline named here is offered, never relied on: the prompt states the question in full
    #: beside the command, so a host where the command is missing, disabled or renamed asks the
    #: same thing itself. What it buys where it is present is a better reading than a
    #: re-derivation — `/code-review` fans out over the branch, `/simplify` over four cleanup
    #: angles — and the prompt is what keeps rein's own answer contract on top of it.
    disciplines: Mapping[str, str] = field(default_factory=dict)
    #: What makes a resume *branch* the session instead of continuing it: the forks share
    #: everything read before the fork and none of what any of them then concludes. That is the
    #: difference between sharing the reading and sharing the readings, and it is what lets two
    #: stages that must reach independent verdicts be handed one (large, expensive) diff once
    #: (`review_transport.SharedReading`). Empty for a CLI that cannot branch a session.
    fork_flags: tuple[str, ...] = ()

    @property
    def resumable(self) -> bool:
        return bool(self.session_flags and self.resume_flags)

    @property
    def forkable(self) -> bool:
        """Whether one reading can be handed to two launches without their answers meeting."""
        return bool(self.resumable and self.fork_flags)

    def launch_argv(self, model: str = "") -> tuple[str, ...]:
        """How to launch it: running `model` when one is named, and reporting what it cost.

        A named model this CLI cannot be told to run is refused by the caller
        (`launch_argv`), never quietly dropped — the model is
        what the independence check is derived from, so launching the default under another
        model's name is the exact lie this field exists to stop.
        """
        chosen = (*self.model_flags, model) if model and self.model_flags else ()
        return self.argv + chosen + self.usage_flags

    def read_output(self, output: str) -> tuple[str, usage_mod.Usage]:
        """`(what it said, what it cost)`. An adapter with no envelope says it did not measure."""
        if self.envelope is None:
            return output, usage_mod.Usage.unavailable()
        return self.envelope(output)


#: Every agent CLI this release knows how to launch.
ADAPTER_TABLE: dict[str, Adapter] = {
    "claude": Adapter(
        name="claude",
        argv=("claude", "-p"),
        session_flags=("--session-id",),
        resume_flags=("--resume",),
        fork_flags=("--fork-session",),
        model_flags=("--model",),
        usage_flags=usage_mod.CLAUDE_JSON_FLAGS,
        envelope=usage_mod.parse_claude_envelope,
        # Claude Code carries all three as commands of its own, and a headless `-p` launch reaches
        # them. `/code-review` has levels and `ultra` is billed and user-triggered — the prompts
        # forbid it rather than naming a level, because a level is an operator's choice and this
        # is not the file that makes it.
        disciplines={
            CORRECTNESS: "/code-review",
            SIMPLIFICATION: "/simplify",
            SECURITY: "/security-review",
        },
    ),
    "codex": Adapter(
        name="codex",
        argv=("codex", "exec"),
        write_flags=("--sandbox", "workspace-write"),
        own_sandbox=True,
    ),
    "gemini": Adapter(name="gemini", argv=("gemini", "-p")),
}


def adapter_for(argv: Sequence[str]) -> Adapter | None:
    """The capability record of whatever CLI `argv` launches, or None for an unknown one."""
    return ADAPTER_TABLE.get(argv[0]) if argv else None


def launch_refusal(config: models.Config | None, role: str) -> str:
    """Why `role` cannot be launched as configured, or `""` when it can.

    One rule, read at every moment that can act on it: `rein agent` refuses to *write* such a
    config, `rein doctor` names it, and the build loop and the review pipeline refuse to launch
    under it. It lived only at the two launch sites, which is the latest of those moments and the
    most expensive: `rein agent codex` — the bulk switch that command's own docstring documents —
    put `adapter: codex` beside the scaffold's `model: opus` on all three review roles, exited 0,
    warned about the independence of two models `codex` would never be told to run, and failed at
    `rein build`, three gates later.
    """
    adapter = (config.adapter(role) if config is not None else "") or "claude"
    record = ADAPTER_TABLE.get(adapter)
    if record is None:
        return (
            f"agents.{role}.adapter is {adapter!r}, which this release does not know how to launch "
            f"(one of: {', '.join(sorted(ADAPTER_TABLE))})"
        )
    model = config.model(role) if config is not None else ""
    if model and not record.model_flags:
        return (
            f"agents.{role}.model is {model!r} and this release cannot tell {adapter!r} which model "
            "to run, so the launch would take the CLI's default under that name. The gate-④ "
            "independence check is derived from the model, so that is a separation nothing performs "
            "— drop the model, or point the role at an adapter whose model flag is known."
        )
    return ""


def disciplines_for(argv: Sequence[str]) -> Mapping[str, str]:
    """The review disciplines the CLI `argv` launches carries, or none for one this release does
    not know. Keyed on the CLI's own name, like `write_flags`, because the build loop holds an
    argv rather than a role."""
    adapter = adapter_for(argv)
    return adapter.disciplines if adapter else {}


def write_flags(argv: tuple[str, ...]) -> tuple[str, ...]:
    """The write-enabling flags for the CLI `argv` launches, keyed on the CLI's own name."""
    adapter = adapter_for(argv)
    return adapter.write_flags if adapter else ()


def adapter_for_role(config: models.Config | None, role: str) -> Adapter:
    """The capability record of the CLI `role` is pointed at. Refuses one this release cannot launch.

    The refusal is checked here and nowhere else in this module: `launch_argv` goes through it, so
    there is one place that decides whether a configured role can be launched at all.
    """
    if refusal := launch_refusal(config, role):
        raise LaunchRefused(refusal)
    return ADAPTER_TABLE[(config.adapter(role) if config is not None else "") or "claude"]


def launch_argv(config: models.Config | None, role: str) -> tuple[str, ...]:
    """Exactly what launches `role` — the CLI, its model, and its usage flags.

    The one resolver. It existed twice, as `build_loop.Config._argv_for` and as
    `review._role_argv`, computing the same two lines and raising two different exceptions for the
    same refusal; a rule read in two places is a rule that drifts in one of them.
    """
    return adapter_for_role(config, role).launch_argv(config.model(role) if config is not None else "")
