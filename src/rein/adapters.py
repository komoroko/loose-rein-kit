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


#: What a launch is allowed to do. Three levels, because the loop makes exactly three kinds of
#: launch and used to be able to name two of them:
#:
#: * ``READ`` — the gate-④ stages. They are handed their payload on stdin, answer on stdout, and
#:   must change nothing. Not the same as "no flags": a CLI whose tools are all deny-by-default
#:   without a grant cannot even open the file it was sent to read.
#: * ``REVIEW`` — the per-task reviewer. It reads the change, runs `git diff`, and writes exactly
#:   one file: the findings its prompt names. It must not touch the code it is judging.
#: * ``WRITE`` — the implementer, the review fixer, the conflict and integration fixers. The tree
#:   is theirs to change.
#:
#: There was one field, `write_flags`, and everything that was not an implementer got nothing. That
#: collapsed READ and REVIEW into "no flags", which is only survivable on a CLI that grants its
#: tools by default: `codex exec` is read-only without them, so its reviewer could not write the
#: findings file the loop then refused to proceed without, and `copilot` grants no tool at all, so
#: its reviewer could not read the code.
READ = "read"
REVIEW = "review"
WRITE = "write"


#: What an agent CLI reads out of a project directory *before* it reads the prompt it was sent:
#: instruction files, and the settings, hooks, skills, sub-agents and MCP servers that decide which
#: tools it may run without asking. Whoever writes these decides what the launch is allowed to do.
#:
#: Named here rather than per adapter because the point is the union: one launch may sit in a tree
#: carrying another host's surfaces, and a directory is dangerous to hand a reviewer if *any* host
#: would act on what is in it. Broader than `gate_guard.HOOK_REGISTRATION`, which answers a
#: different question — what an agent may never edit — and is deliberately narrow so that a denial
#: names something an agent actually did.
#:
#: `.github/workflows/` is deliberately absent: CI configuration is not read by a CLI at launch,
#: and it is exactly the kind of change a security reviewer should be looking at.
PROJECT_CONFIG: tuple[str, ...] = (
    "CLAUDE.md",
    "AGENTS.md",
    "GEMINI.md",
    ".cursorrules",
    ".mcp.json",
    ".claude/",
    ".codex/",
    ".agents/",
    ".gemini/",
    ".cursor/",
    ".github/agents/",
    ".github/hooks/",
    ".github/instructions/",
    ".github/prompts/",
    ".github/copilot-instructions.md",
)


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
    #: How to grant each access level above, keyed by it. A level this CLI already has without
    #: being told maps to `()` — claude's `-p` carries the project's own permissions, and `codex
    #: exec` reads without being asked. A level absent from the mapping is `()` as well.
    grants: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    #: How to narrow a `REVIEW` grant to the one file the reviewer must produce. Each element is
    #: `str.format`-ed with `path=`, and the result is appended to the `REVIEW` grant when the
    #: launch names its findings file. Empty for a CLI that cannot say "this path and no other" —
    #: which is most of them, and is why the review prompt's "you have no write access to the
    #: code" is an enforced fact under `copilot` and a convention (plus the loop's before/after
    #: fingerprint) everywhere else.
    scoped_write: tuple[str, ...] = ()
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
    #: What introduces a prompt passed on the command line, placed immediately before it. For a
    #: CLI whose flag *takes the prompt as its value* (`gemini -p <text>`, `copilot -p <text>`)
    #: nothing may come between the two, and everything in `argv`, `model_flags` and `usage_flags`
    #: is inserted before it rather than after. Putting such a flag in `argv` is what would make
    #: `gemini -p --model X <text>` send `--model` as the prompt and the model name as a stray
    #: argument — invisible until a model was configured, which is why `gemini` carried no
    #: `model_flags` and worked.
    #:
    #: Empty for a CLI whose prompt is a bare positional (`codex exec <text>`) — and for one whose
    #: flag is a *boolean* non-interactive switch (`claude -p`), which belongs in `argv` because
    #: the review transport needs it too, and there the payload arrives on stdin with no prompt
    #: argument at all.
    prompt_flags: tuple[str, ...] = ()
    #: How a human installs this CLI. **Never run** — printed by `doctor` and `preflight` when the
    #: binary is missing. Installing a third-party agent CLI on someone's behalf decides for them
    #: what lands on their PATH and what it is allowed to reach; naming the command is the whole
    #: job, and the choice stays theirs.
    install_hint: str = ""

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

    def access_flags(self, level: str, writable: str = "") -> tuple[str, ...]:
        """What to pass so a launch at `level` can do its job — and nothing past it.

        `writable` is the one path a `REVIEW` launch must be able to write. It is used only by a
        CLI that can scope a write to a path; the others grant the least they can express, which
        is why this returns what the CLI *can* promise rather than what the caller asked for.
        """
        flags = self.grants.get(level, ())
        if level == REVIEW and writable and self.scoped_write:
            flags = (*flags, *(part.format(path=writable) for part in self.scoped_write))
        return flags

    def prompt_argv(self, prompt: str) -> tuple[str, ...]:
        """The tail of a command line: whatever introduces the prompt, then the prompt.

        Always the last thing appended, after `launch_argv` and after any write flags — that
        ordering is the whole reason `prompt_flags` is a separate field.
        """
        return (*self.prompt_flags, prompt)

    def read_output(self, output: str) -> tuple[str, usage_mod.Usage]:
        """`(what it said, what it cost)`. An adapter with no envelope says it did not measure."""
        if self.envelope is None:
            return output, usage_mod.Usage.unavailable()
        return self.envelope(output)


#: Every agent CLI this release knows how to launch.
ADAPTER_TABLE: dict[str, Adapter] = {
    "claude": Adapter(
        name="claude",
        # `-p` is in `argv`, not `prompt_flags`: for claude it is the boolean "print and exit",
        # needed just as much when the payload arrives on stdin (the review transport), and the
        # prompt is then a plain positional.
        argv=("claude", "-p"),
        session_flags=("--session-id",),
        resume_flags=("--resume",),
        fork_flags=("--fork-session",),
        model_flags=("--model",),
        usage_flags=usage_mod.CLAUDE_JSON_FLAGS,
        envelope=usage_mod.parse_claude_envelope,
        install_hint="curl -fsSL https://claude.ai/install.sh | bash",
        # Every level is empty: a `-p` launch carries the permissions the project already
        # configured, and there is no flag here that would narrow them per launch. So what keeps
        # this reviewer off the code is the prompt and the loop's before/after fingerprint, not
        # the launcher — stated here rather than left to be inferred from three blank tuples.
        grants={},
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
        # `codex exec` reads without being asked, so READ needs nothing. It has three sandbox
        # modes and no way to name a writable file, so a reviewer that must produce one file gets
        # the same grant an implementer does — `workspace-write`. That is what this CLI can
        # promise, and saying so is better than a reviewer that cannot write its findings at all,
        # which is what "no flags" bought.
        grants={WRITE: ("--sandbox", "workspace-write"), REVIEW: ("--sandbox", "workspace-write")},
        own_sandbox=True,
        usage_flags=usage_mod.CODEX_JSONL_FLAGS,
        envelope=usage_mod.parse_codex_envelope,
        install_hint="npm install -g @openai/codex",
    ),
    "gemini": Adapter(
        name="gemini",
        argv=("gemini",),
        prompt_flags=("-p",),
        model_flags=("--model",),
        # `default` auto-approves the read-only tools and stops at everything else; `--yolo` is
        # deprecated upstream in favour of naming the mode. `auto_edit` would cover the findings
        # file but not the `git diff` the review prompt points the reviewer at, and an approval
        # nobody is there to give is a run that hangs — so a reviewer takes `yolo` too.
        grants={WRITE: ("--approval-mode", "yolo"), REVIEW: ("--approval-mode", "yolo")},
        usage_flags=usage_mod.GEMINI_JSON_FLAGS,
        envelope=usage_mod.parse_gemini_envelope,
        install_hint="npm install -g @google/gemini-cli",
    ),
    "copilot": Adapter(
        name="copilot",
        # `--no-ask-user` because a launch from this loop has no human at the other end: without
        # it the agent may pause for input and the run hangs until `agent_timeout_sec` (default:
        # no limit) rather than failing. `-s` because gate ④ asks its stages for "one JSON object
        # and no other text" and parses the whole of stdout strictly — a stats footer around the
        # object is not a smaller answer, it is an unreadable one. It is what the CLI's own
        # programmatic reference offers for exactly this ("outputting only the agent's response").
        argv=("copilot", "--no-ask-user", "-s"),
        prompt_flags=("-p",),
        model_flags=("--model",),
        install_hint="npm install -g @github/copilot",
        # Deny-by-default, and the only CLI here that can say exactly what a reviewer may write.
        # So this is the one adapter under which the review prompt's "you do not change the code,
        # you have no write access to it" is a fact the launcher enforces rather than an
        # instruction the model is asked to respect: read the tree, run git, write one file.
        grants={
            READ: ("--allow-tool", "read"),
            REVIEW: ("--allow-tool", "read", "--allow-tool", "shell(git:*)"),
            WRITE: ("--allow-all-tools",),
        },
        scoped_write=("--allow-tool", "write({path})"),
        # Everything below is empty on purpose, and each for its own reason:
        #
        # `session_flags` / `resume_flags` / `fork_flags` — `--resume` picks a session
        # interactively and there is no flag that stamps one, so with `max_parallel` leaves in
        # flight there is no way to say which session a retry means. The same reason `codex` is
        # empty: guessing would hand one leaf another leaf's context. Every retry is a fresh read.
        #
        # `usage_flags` / `envelope` — the programmatic reference documents no machine-readable
        # envelope, so a launch is recorded as *unmeasured* rather than as zero. `-s` above is
        # what makes the answer readable; it does not make the bill knowable, and those are not
        # the same thing.
        #
        # `disciplines` — no `/code-review` equivalent. The prompts state each question in full
        # beside the command, which is the floor a host without one lands on.
        #
        # `own_sandbox` — `--cloud` is an opt-in that runs the session somewhere else entirely,
        # not isolation around the launch this loop makes.
    ),
    "cursor": Adapter(
        name="cursor",
        # `--trust` is documented as headless-only, and it is the same bargain `--no-ask-user` is
        # for copilot: there is no human here to answer a workspace-trust prompt, so without it
        # the run hangs instead of failing.
        argv=("cursor-agent", "-p", "--trust"),
        model_flags=("--model",),
        # `-f, --force` — "force allow commands unless explicitly denied" (`--yolo` is its alias).
        # Reading is not a command it needs forcing for, which is why READ is empty.
        grants={WRITE: ("--force",), REVIEW: ("--force",)},
        usage_flags=usage_mod.CURSOR_JSON_FLAGS,
        envelope=usage_mod.parse_cursor_envelope,
        install_hint="curl https://cursor.com/install -fsS | bash",
        # `--resume [chatId]` resumes an existing chat and there is no flag that stamps a new one,
        # so a retry cannot be told which of `max_parallel` leaves it belongs to — the same reason
        # `codex` and `copilot` are cold on every retry.
    ),
    "amp": Adapter(
        name="amp",
        # `-x` takes the message as its value, so it goes last. Execute mode "prints its final
        # message and exits", which is already the shape gate ④ parses — no envelope needed, and
        # none offered: nothing here reports what a launch cost, so it is recorded as unmeasured.
        argv=("amp",),
        prompt_flags=("-x",),
        # Empty, and not for lack of looking: the execute-mode reference documents no model flag
        # (only `--fast`) and no tool-permission flag. `launch_refusal` therefore rejects a
        # `model:` on this adapter rather than launching amp's default under another model's name.
        grants={},
        install_hint="npm install -g @sourcegraph/amp",
    ),
    "opencode": Adapter(
        name="opencode",
        # The prompt is `run`'s positional `[message..]`, so no flag introduces it.
        argv=("opencode", "run"),
        model_flags=("--model",),
        # `--auto` — "auto-approve permissions not explicitly denied".
        grants={WRITE: ("--auto",), REVIEW: ("--auto",)},
        usage_flags=usage_mod.OPENCODE_JSON_FLAGS,
        envelope=usage_mod.parse_opencode_envelope,
        install_hint="npm install -g opencode-ai",
        # `--session <id>` and `--continue` resume an existing session; neither creates one under
        # an id this loop chose, so there is nothing to stamp and nothing to resume by name.
    ),
}


#: binary name → the record that launches it. `ADAPTER_TABLE` is keyed by the name a human writes
#: in `agents.<role>.adapter`, and that is not always the name of the executable: `cursor` launches
#: `cursor-agent`. Looking a record up by `argv[0]` against the table's own keys worked only while
#: every adapter happened to be named after its binary — and it failed *silently*, handing back
#: `None`, which `command()` reads as "an argv this module does not know" and launches with no
#: access flags at all. So the index is built once, and a collision is a startup error rather than
#: one record quietly shadowing another.
_BY_BINARY: dict[str, Adapter] = {}
for _record in ADAPTER_TABLE.values():
    if _record.argv[0] in _BY_BINARY:
        raise RuntimeError(f"two adapters launch {_record.argv[0]!r}; a launch could not be attributed to either")
    _BY_BINARY[_record.argv[0]] = _record
del _record


def adapter_for(argv: Sequence[str]) -> Adapter | None:
    """The capability record of whatever CLI `argv` launches, or None for an unknown one."""
    return _BY_BINARY.get(argv[0]) if argv else None


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
    not know. Keyed on the CLI's own name, like the access grants, because the build loop holds an
    argv rather than a role."""
    adapter = adapter_for(argv)
    return adapter.disciplines if adapter else {}


def command(
    argv: Sequence[str], prompt: str, *, access: str = READ, writable: str = "", extra: Sequence[str] = ()
) -> list[str]:
    """The whole command line for one launch: `argv`, the access it needs, then the prompt.

    The one place that knows the prompt goes last and that `prompt_flags` goes immediately before
    it. Assembling it at each launch site is what let the access flags be appended *after* a
    prompt-introducing flag, which no CLI but claude survives (`Adapter.prompt_flags`).

    `access` is one of `READ` / `REVIEW` / `WRITE`; `writable` is the one file a `REVIEW` launch
    must produce. `extra` carries the caller's own flags (the session id a retry resumes).
    """
    record = adapter_for(argv)
    granted = record.access_flags(access, writable) if record is not None else ()
    tail = record.prompt_argv(prompt) if record is not None else (prompt,)
    return [*argv, *granted, *extra, *tail]


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
