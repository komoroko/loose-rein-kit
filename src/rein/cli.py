"""The `rein` console entry point — every verb of the installed harness.

One dispatcher, one implementation per operation: each verb hands its remaining arguments to
the owning module's entry function, so nothing is implemented twice. The daily verbs stay the
memorable four (start / next / ui / agent); the rest are the setup and operational commands.

`approve` is the one verb that can open a gate, and it does so only after a human types the
gate name at an interactive terminal. What keeps an agent out is that it is never
pre-authorized (AGENTS.md "Gate rules" 2) — not anything in this dispatcher.

Every invocation runs the cheap lock check (lock.startup_warning), except `guard` and `doctor`
— see main() for why a hook must never be silenced by a version check.
"""

from __future__ import annotations

import importlib
import logging
import sys
from collections.abc import Callable
from pathlib import Path

import rein
from rein import common
from rein import lock as lock_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

# verb → "module" or "module:function" (function defaults to `main`). Resolution is lazy and
# happens per call, so a verb's module is only imported when invoked (and monkeypatching a
# module's entry function in tests keeps working). `install` owns four verbs, hence its
# per-verb `cmd_*` names; every single-verb module exposes `main(argv)`.
VERBS: dict[str, str] = {
    "status": "status_api",
    "resume": "resume",
    "ui": "ui",
    "agent": "agent_cli",
    "project": "registry",
    "init": "init_cmd",
    "install": "install:cmd_install",
    "uninstall": "install:cmd_uninstall",
    "sync": "install:cmd_sync",
    "upgrade": "install:cmd_upgrade",
    "approve": "approve",
    "changes": "change_request",
    "revise": "revise",
    "review": "review",
    "build": "build_loop",
    "task": "task_cmd",
    "doctor": "doctor",
    "events": "events",
    "cycle-close": "cycle",
    "issue-sync": "issue_sync",
    "pr-draft": "pr_draft",
    "oci": "oci_cli",
    "guard": "gate_guard",
    "policy-check": "policy_check",
    "decision": "control_plane",
    "knowledge-gap": "control_plane:knowledge_gap_main",
    "report": "control_plane:report_main",
    "evidence": "evidence_cmd",
    "dag": "dag",
}


def _resolve(spec: str) -> Callable[[list[str] | None], int]:
    mod_name, _, func = spec.partition(":")
    module = importlib.import_module(f"rein.{mod_name}")
    entry: Callable[[list[str] | None], int] = getattr(module, func or "main")
    return entry


HELP = """usage: rein [--repo PATH] <verb> [args]

setup:
  init [--name N ...]     seed this repo with Loose Rein state (wizard on a TTY; brownfield auto-detected)
  install <agent>...      add an agent's surfaces (claude / codex / copilot) — opt-in per environment
  uninstall <name>|--all  retract integration surfaces (pristine files only)
  sync [--check|--force]  rematerialize .rein/prompts|schema|rules from the installed package
  upgrade [--dry-run]     changelog transition + sync + refresh installed integrations

daily verbs:
  start                   first run: interactive setup wizard; afterwards: where you are + what's next
  next [--json]           only the next recommended command (deterministic; --json for integrations)
  ui [args]               local dashboard — read gates, do the gate-4 human review, run doctor/revise
  agent <adapter>         point the AI roles at an adapter (--show lists them and their groups)
  project [add|use|...]   the named repos the ui switches between (add/list/remove/use)
  status [--json]         the full status object (/status reads this)
  resume [--full]         what changed since you last looked (delta first, snapshot on request)

operations:
  approve <gate> [--check]     readiness check, then the human's confirmation at this terminal
  changes add|list|address     ask for changes instead of approving (holds the gate shut)
  oci build|verify             build the sandbox images and pin their digests
  revise --to <phase> ...      roll back upstream (gates reset in a chain)
  review generate|complete|show  the grounded machine review (gate ④'s evidence)
  build [--dry-run|--supervise]  the deterministic /build orchestrator (--supervise: retry in-process on exit 3)
  task reset <id> --reason …   put a blocked task back on the frontier (recorded, never hand-edited)
  dag [--render|--trace|...]   derive/inspect the task DAG (read-only; /tasks & /status use it)
  doctor                       read-only diagnosis: format, integrations, sandbox, plan, review
  events [--summary|--verify]  read the hash-chained audit log (read-only)
  cycle-close --name <slug>    archive the finished delta cycle and reset
  issue-sync [--dry-run]       one-way mirror of plan.yaml's tasks -> GitHub Issues (opt-in)
  pr-draft [args]              assemble a PR body from the SSOT (read-only)
  evidence show|record         acceptance evidence this loop cannot obtain (record what you observed)
  report --outcome … --summary …  how an implementer ends its attempt (implemented|blocked|needs-revision)
  decision add --statement …   record an implementation decision (routes via the control plane)
  knowledge-gap add …          record what could not be found out
  guard [--check-diff]         the gate-guard hook / commit-stage check
  policy-check --base-sha … --head-sha …  base-side CI meta-policy (rejects head weakening)
  version                      print the tool version
"""


def _start(rest: list[str]) -> int:
    """First run → the init wizard; an initialized repo → a one-line where-you-are + what's next."""
    from rein import init_cmd, status_api

    if rest:
        logger.error(f"rein start takes no arguments (got: {' '.join(rest)})")
        return 2
    try:
        root = repo_mod.get().root
    except repo_mod.RepoNotFoundError:
        if not sys.stdin.isatty():
            logger.error(
                "this directory is not initialized and stdin is not a TTY — run the"
                " non-interactive `rein init --name <product>` instead."
            )
            return 2
        return init_cmd.wizard()
    status = status_api.collect_status(root)
    rec = status["next"]
    assert isinstance(rec, dict)  # asdict(Recommendation)
    if rec.get("kind") == "setup":
        if not sys.stdin.isatty():
            logger.error(
                "this repo is not initialized and stdin is not a TTY — run the"
                " non-interactive `rein init --name <product>` instead."
            )
            return 2
        return init_cmd.wizard(Path(root))
    gates = status.get("gates")
    gate_rows = gates if isinstance(gates, list) else []
    approved = sum(1 for g in gate_rows if g.get("status") == "approved")
    print(
        f"project: {status.get('project')}   phase: {status.get('current_phase')}"
        f"   gates: {approved}/{len(gate_rows)} approved"
    )
    print(status_api.render_next(rec))
    return 0


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


def main(argv: list[str] | None = None) -> int:
    common.configure_logging()
    args = sys.argv[1:] if argv is None else list(argv)
    # The global --repo (also accepted by every verb) may precede the verb.
    repo_flag: str | None = None
    if args[:1] == ["--repo"] and len(args) >= 2:
        repo_flag = args[1]
        args = args[2:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(HELP, end="")
        return 0
    verb, rest = args[0], args[1:]
    if repo_flag is not None:
        rest = [*rest, "--repo", repo_flag]

    if verb == "version":
        print(rein.__version__)
        return 0

    # `guard` and `doctor` are exempt from the startup lock check on purpose.
    #
    # guard is a PreToolUse hook. If it exits on a lock problem it prints no decision, and every
    # host reads "no decision" as allow — so a version-skew check would silently turn the gate
    # guard off. It resolves its own repository from the hook payload's cwd anyway.
    #
    # doctor exists to diagnose exactly the states that make the lock unreadable; refusing to run
    # it there would leave the human with an error and no way to look into it.
    if verb not in ("guard", "doctor"):
        rc = _lock_check(repo_flag)
        if rc != 0:
            return rc

    if verb == "start":
        return _start(args[1:])
    if verb == "next":
        return _resolve("status_api")(["--next", *rest])
    spec = VERBS.get(verb)
    if spec is None:
        logger.error(f"rein: unknown verb '{verb}' — run `rein --help` for the verb list")
        return 2
    return _resolve(spec)(rest)


if __name__ == "__main__":
    raise SystemExit(main())
