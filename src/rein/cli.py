"""The `rein` console entry point — every verb of the installed harness.

One dispatcher, one implementation per operation: each verb hands its remaining arguments to
the owning module's entry function, so nothing is implemented twice. The daily verbs are
`start` / `next` / `ui`; the rest are the setup and operational commands.

**The verb table is the help.** `VERBS` carries each verb's one-line summary and whether a human
ever types it, and argparse renders the listing, the usage line and the `invalid choice` error
from that table — there is no second, hand-written copy to drift from it. Verbs a human never
types (`human=False`: the recorders an implementer calls, the hooks, the CI checks) are
`argparse.SUPPRESS`ed out of the default listing and named in the epilog instead, because the
agents that *do* call them read `rein --help` too: a verb whose name appears nowhere is
discoverable only through prompt prose, and prose forgets.

`approve` is the one verb that can open a gate, and it does so only after a human types the
gate name at an interactive terminal. What keeps an agent out is that it is never
pre-authorized (AGENTS.md "Gate rules" 2) — not anything in this dispatcher.

Every invocation runs the cheap lock check (lock.startup_warning), except `guard` and `doctor`
— see main() for why a hook must never be silenced by a version check.
"""

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import textwrap
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import rein
from rein import common
from rein import lock as lock_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Verb:
    """One dispatchable verb: where it lives, what it does, and whether a person ever types it.

    ``spec`` is ``"module"`` or ``"module:function"`` (the function defaults to ``main``).
    Resolution is lazy and happens per call, so a verb's module is only imported when invoked
    (and monkeypatching a module's entry function in tests keeps working).
    """

    spec: str
    summary: str
    human: bool = True


VERBS: dict[str, Verb] = {
    # setup
    "init": Verb("init_cmd", "seed this repo with Loose Rein state (wizard on a TTY; brownfield auto-detected)"),
    "install": Verb("install:cmd_install", "add an agent's surfaces (claude / codex / copilot)"),
    "uninstall": Verb("install:cmd_uninstall", "retract integration surfaces (pristine files only)"),
    "agent": Verb("agent_cli", "point the AI roles at an adapter (--show lists them and their groups)"),
    "oci": Verb("oci_cli", "build the sandbox images and pin their digests"),
    "sync": Verb("install:cmd_sync", "rematerialize .rein/prompts|schema|rules from the installed package"),
    "upgrade": Verb("install:cmd_upgrade", "changelog transition + sync + refresh installed integrations"),
    # daily
    "start": Verb("resume", "first run: setup wizard; afterwards: what moved since you last looked"),
    "next": Verb("status_api", "only the next recommended command (deterministic; --json for integrations)"),
    "ui": Verb("ui", "local dashboard — read gates, do the gate-4 human review, run doctor/revise"),
    # gates and shipping
    "approve": Verb("approve", "readiness check, then the human's confirmation at this terminal"),
    "changes": Verb("change_request", "ask for changes instead of approving (holds the gate shut)"),
    "revise": Verb("revise", "roll back upstream (gates reset in a chain; --from-review derives the tasks)"),
    "review": Verb("review", "the grounded machine review (generate --supervise waits out a capacity stop)"),
    "build": Verb("build_loop", "the deterministic /build orchestrator (--supervise: retry in-process on exit 3)"),
    "doctor": Verb("doctor", "read-only diagnosis: format, integrations, sandbox, plan, review"),
    "cycle-close": Verb("cycle", "archive the finished delta cycle and reset"),
    "pr-draft": Verb("pr_draft", "assemble a PR body from the SSOT (read-only)"),
    "pr-stack": Verb("pr_stack", "cut the work branch into one draft PR per task (--push confirms at a terminal)"),
    "version": Verb("cli:_print_version", "print the tool version"),
    # agent / hook / CI only — suppressed from the default listing, named in the epilog
    "report": Verb(
        "control_plane:report_main",
        "how an implementer ends its attempt (implemented|blocked|needs-revision)",
        human=False,
    ),
    "decision": Verb("control_plane", "record an implementation decision (routes via the control plane)", human=False),
    "knowledge-gap": Verb("control_plane:knowledge_gap_main", "record what could not be found out", human=False),
    "evidence": Verb("evidence_cmd", "acceptance evidence this loop cannot obtain (record what you saw)", human=False),
    "dag": Verb("dag", "derive/inspect the task DAG (read-only; /tasks & /status use it)", human=False),
    "events": Verb("events", "read the hash-chained audit log (--cost sums what runs billed)", human=False),
    "task": Verb("task_cmd", "task reset <id> --reason … — put a blocked task back on the frontier", human=False),
    "guard": Verb("gate_guard", "the gate-guard hook / commit-stage check", human=False),
    "policy-check": Verb("policy_check", "base-side CI meta-policy (rejects head weakening)", human=False),
    "issue-sync": Verb("issue_sync", "one-way mirror of plan.yaml's tasks -> GitHub Issues (opt-in)", human=False),
    "project": Verb("registry", "the named repos the ui switches between (add/list/remove/use)", human=False),
}


def _resolve(spec: str) -> Callable[[list[str] | None], int]:
    mod_name, _, func = spec.partition(":")
    module = importlib.import_module(f"rein.{mod_name}")
    entry: Callable[[list[str] | None], int] = getattr(module, func or "main")
    return entry


def _print_version(argv: list[str] | None = None) -> int:
    print(rein.__version__)
    return 0


def _epilog() -> str:
    """The agent/CI verbs by name, plus where their descriptions and arguments live."""
    hidden = [name for name, verb in VERBS.items() if not verb.human]
    wrapped = textwrap.fill(" ".join(hidden), width=76, initial_indent="  ", subsequent_indent="  ")
    return (
        f"agent & CI verbs (called by the loop, the hooks and CI):\n{wrapped}\n\n"
        "  descriptions: rein help --all     arguments: rein <verb> --help"
    )


def _build_parser(*, show_all: bool = False) -> argparse.ArgumentParser:
    """The whole CLI surface, derived from VERBS. ``show_all`` un-suppresses the agent/CI verbs."""
    parser = argparse.ArgumentParser(
        prog="rein",
        description="Human-on-the-Loop development harness.",
        epilog=None if show_all else _epilog(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", metavar="PATH", default=None, help="repository root (default: discovered from cwd)")
    parser.add_argument("--version", "-V", action="version", version=rein.__version__)
    sub = parser.add_subparsers(dest="verb", metavar="<verb>")
    for name, verb in VERBS.items():
        # add_help=False so `rein build --help` is not answered here: REMAINDER carries it down to
        # the module's own parser, which is the one that knows the flags.
        # A subparser is listed only when `help` is passed at all (argparse appends the pseudo-
        # action from that keyword), so an omitted `help` is how a verb stays dispatchable and
        # unlisted. `help=argparse.SUPPRESS` does not hide it — it prints "==SUPPRESS==".
        if show_all or verb.human:
            child = sub.add_parser(name, add_help=False, help=verb.summary)
        else:
            child = sub.add_parser(name, add_help=False)
        child.add_argument("rest", nargs=argparse.REMAINDER)
    return parser


def _lock_check(repo_flag: str | None) -> int:
    """The cheap per-invocation lock check. 0 = go on; 1 = hard stop (newer lock format)."""
    try:
        repo = repo_mod.get(repo_flag)
    except repo_mod.RepoNotFoundError:
        return 0  # verbs that need a repo will say so themselves, with their own message
    try:
        warning = lock_mod.startup_warning(repo, rein.__version__)
    except lock_mod.LockError as exc:
        logger.error(f"rein: {exc}")
        return 1
    if warning:
        logger.warning(f"rein: {warning}")
    return 0


def _flag_value(args: list[str], flag: str) -> str | None:
    """The value following `flag` in `args`, or None.

    One reader, because `--repo` may be typed on either side of the verb and the dispatcher and
    the verb must not end up pointed at two different repositories.
    """
    if flag in args:
        index = args.index(flag)
        if index + 1 < len(args):
            return args[index + 1]
    return None


def _start(rest: list[str]) -> int:
    """First run → the init wizard; an initialized repo → what moved since you last looked."""
    from rein import init_cmd, resume, status_api
    from rein import store as store_mod

    try:
        repo = repo_mod.get(_flag_value(rest, "--repo"))
        root = repo.root
    except repo_mod.RepoNotFoundError:
        # No repository at all: the wizard is the only thing that can help, and only on a TTY.
        # Off a TTY there is nothing to report either, so say what to run and stop.
        if not sys.stdin.isatty():
            logger.error(
                "this directory is not initialized and stdin is not a TTY — run the"
                " non-interactive `rein init --name <product>` instead."
            )
            return 2
        return init_cmd.wizard()
    # Two document reads, not a status collection: `resume` below runs the one `collect_status`
    # this verb should cost, and asking the whole object whether the repo is still a template
    # would run its git subprocesses, digests and readiness probes for a boolean the config and
    # the state already carry. Only a TTY can act on the answer, so only a TTY pays even that.
    if sys.stdin.isatty():
        store = store_mod.Store(repo)
        if status_api.is_uninitialized(store.read_config(), store.read_state()):
            return init_cmd.wizard(Path(root))
    # Off a TTY a repo still waiting for `init` gets the packet anyway: it already leads with
    # `rein init --name <product>`, which is the answer. Refusing there is what kept the hook on
    # a second verb.
    return resume.main(rest)


def main(argv: list[str] | None = None) -> int:
    common.configure_logging()
    args = sys.argv[1:] if argv is None else list(argv)

    # The global --repo (also accepted by every verb) may precede the verb, so it comes off first:
    # everything below is about what was actually asked for. `rein --repo X --version` answering
    # `unknown verb '--version'` is what happens when the identity spellings are read at argv[0].
    #
    # The split is done here rather than by parse_args because a verb's own flags must reach that
    # verb untouched: argparse would refuse `rein next --json` as an unrecognized top-level
    # argument.
    repo_flag: str | None = None
    if args[:1] == ["--repo"] and len(args) >= 2:
        repo_flag, args = args[1], args[2:]

    # `help` as a verb, alongside argparse's -h/--help, and `--all` on either spelling.
    if not args or args[0] in ("help", "-h", "--help"):
        _build_parser(show_all="--all" in args[1:]).print_help()
        return 0
    # `--version`/`-V` alongside the `version` verb, which dispatches below. A version check that
    # fails is read as a broken install, which is the opposite of what it was asked.
    if args[0] in ("--version", "-V"):
        return _print_version()

    verb, rest = args[0], args[1:]
    if verb not in VERBS:
        try:  # argparse owns the wording of the usage line and the exit code
            _build_parser().error(f"unknown verb '{verb}' — run `rein --help` for the verb list")
        except SystemExit as exc:
            return int(exc.code or 2)
    # `--repo` may have been typed on either side of the verb. Resolve one value and hand the verb
    # exactly one, so the lock check below and the verb itself cannot read different repositories.
    if repo_flag is None:
        repo_flag = _flag_value(rest, "--repo")
    elif "--repo" not in rest:
        rest = [*rest, "--repo", repo_flag]

    # `guard`, `doctor` and `version` are exempt from the startup lock check on purpose.
    #
    # guard is a PreToolUse hook. If it exits on a lock problem it prints no decision, and every
    # host reads "no decision" as allow — so a version-skew check would silently turn the gate
    # guard off. It resolves its own repository from the hook payload's cwd anyway.
    #
    # doctor exists to diagnose exactly the states that make the lock unreadable; refusing to run
    # it there would leave the human with an error and no way to look into it.
    #
    # version answers the question "what is installed here", which is the first thing anyone asks
    # of a lock the tool refuses to read. Hard-stopping on it would report a broken install.
    if verb not in ("guard", "doctor", "version"):
        rc = _lock_check(repo_flag)
        if rc != 0:
            return rc

    try:
        if verb == "start":
            return _start(rest)
        return _resolve(VERBS[verb].spec)(rest)
    except common.ReinError as exc:
        # Every raise behind this line has already worded its own reason (`common.ReinError`), and
        # until this clause existed none of them reached a reader as anything but a traceback: a
        # schema-invalid `config.yaml` after an upgrade, a comparator answer the policy refused,
        # a review whose diff blew its budget. A traceback says "rein is broken" about a
        # repository that is merely out of shape, and it hides the sentence that says which.
        #
        # EXIT_CANNOT_PROCEED, not 1: the named thing has to be repaired first, and re-running
        # before that is wasted — which is exactly what that code means.
        logger.error(f"rein {verb}: {exc}")
        return common.EXIT_CANNOT_PROCEED


if __name__ == "__main__":
    raise SystemExit(main())
