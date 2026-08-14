"""`rein doctor` — one read-only diagnosis of everything the guarantees rest on.

Every failure mode the harness defends against surfaces late and cryptically if nobody looks:
a runtime directory another user owns, a sandbox profile that is quietly running repository
code on the host, an audit chain with a hole in it. This command asks all of those questions
at once and prints one line each, covering layout, integrations, sandboxing, the gate chain
and its receipts, and plan/review coverage.

Levels: FAIL = a broken invariant, fix before continuing (exit 1). WARN = suspicious or
weaker-than-intended. INFO = context worth knowing. PASS = checked and healthy.

Two rules make the output trustworthy:

**It never repairs anything.** A doctor that fixes what it finds is a doctor whose findings
nobody reads, and several of the things it checks (an approval, an audit record) must only
ever change by a deliberate human action.

**It never reports "not measured" as "fine".** An unreadable chain is not an empty one. Where
the honest answer is "this could not be checked", that is what the line says.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import rein
from rein import common, dag, dag_trace, event_chain, executors, install, models, strict_yaml
from rein import lock as lock_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

SETTINGS_PATH = ".claude/settings.json"
COPILOT_HOOKS_DIR = ".github/hooks"
#: Codex reads hooks from either form, so both are checked; a repository that ships neither has
#: no edit-time guard under Codex.
CODEX_HOOK_FILES = (".codex/hooks.json", ".codex/config.toml")


@dataclass(frozen=True)
class Finding:
    level: str  # FAIL | WARN | INFO | PASS
    area: str
    message: str


# --- format --------------------------------------------------------------------


def check_layout(repo: repo_mod.Repo) -> list[Finding]:
    """Are the four SSOT documents present?"""
    missing = [
        name
        for name, path in (
            ("config.yaml", repo.config),
            ("state.yaml", repo.state),
            ("plan.yaml", repo.plan),
            ("review.yaml", repo.review),
        )
        if not path.exists()
    ]
    if missing:
        return [Finding("FAIL", "format", f"missing SSOT document(s): {', '.join(missing)} — run `rein init`")]
    return [Finding("PASS", "format", "the four SSOT documents are present")]


def check_lock(repo: repo_mod.Repo) -> list[Finding]:
    try:
        data = lock_mod.read(repo.lock)
    except lock_mod.LockError as exc:
        return [Finding("FAIL", "format", str(exc))]
    if data is None:
        return [Finding("INFO", "format", f"no {lock_mod.LOCK_NAME} yet — `rein init`/`sync` writes it")]
    findings = [Finding("PASS", "format", f"{lock_mod.LOCK_NAME} readable (format {data.get('format')})")]
    warning = lock_mod.startup_warning(repo, rein.__version__)
    if warning:
        findings.append(Finding("WARN", "format", warning))
    return findings


def check_documents(repo: repo_mod.Repo) -> tuple[list[Finding], dict[str, object]]:
    """Every SSOT document against its strict schema and cross-references."""
    store = store_mod.Store(repo)
    findings: list[Finding] = []
    loaded: dict[str, object] = {}
    for name, reader in (
        ("config", store.read_config),
        ("state", store.read_state),
        ("plan", store.read_plan),
        ("review", store.read_review),
    ):
        try:
            value = reader()
        except (models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError) as exc:
            findings.append(Finding("FAIL", "format", f"{name}.yaml: {exc}"))
            continue
        if value is None:
            findings.append(Finding("INFO", "format", f"{name}.yaml is absent"))
            continue
        loaded[name] = value
        findings.append(Finding("PASS", "format", f"{name}.yaml valid (schema + cross-references)"))
    return findings, loaded


def check_materialized(repo: repo_mod.Repo) -> list[Finding]:
    """The materialized prompts/schema/oci/rules must equal the packaged payload.

    Reuses install's own destination map rather than re-deriving it, so "what sync writes" and
    "what doctor checks" cannot answer differently — a drift canary that drifts is worse than
    none at all.
    """
    desired = install._dest_map(install.MATERIALIZED)
    drifted: list[str] = []
    for rel, blob in sorted(desired.items()):
        try:
            if lock_mod.norm_hash(repo.path(rel).read_bytes()) != lock_mod.norm_hash(blob):
                drifted.append(rel)
        except OSError:
            drifted.append(f"{rel} (absent)")
    if drifted:
        return [
            Finding(
                "WARN",
                "format",
                f"{len(drifted)} materialized file(s) differ from the packaged payload "
                f"(e.g. {drifted[0]}) — `rein sync --check` lists them all",
            )
        ]
    return [Finding("PASS", "format", f"{len(desired)} materialized file(s) match the packaged payload")]


def check_integrations(repo: repo_mod.Repo, config: models.Config | None) -> list[Finding]:
    """Which agent surfaces exist on disk, and whether the lock knows the tool put them there.

    An empty `integrations` record means two opposite things depending on the repository. In the
    template it is correct: the .claude/.github files are the template's own dogfood copies, held
    to the payload by scripts/template_lint.py, and nothing installed them. In a product it means
    the files were *copied* — the usual route is cloning the template and running `rein init`,
    which deliberately installs no surface — and then `upgrade` never refreshes them and
    `uninstall` cannot retract them, so they stay pinned at whatever release was copied.

    Nothing on disk and nothing recorded is not reported at all: the surfaces are opt-in, so their
    absence is a choice rather than a defect.
    """
    try:
        recorded = (lock_mod.read(repo.lock) or {}).get("integrations") or {}
    except lock_mod.LockError:
        return []  # check_lock already reported the unusable lock
    findings: list[Finding] = []
    unrecorded: list[str] = []
    for name in sorted(install.INTEGRATIONS):
        present = install.present_surfaces(repo, name)
        if not present:
            continue
        if isinstance(recorded.get(name), dict):
            findings.append(
                Finding(
                    "PASS", "format", f"integration '{name}' installed ({len(present)} file(s), recorded in the lock)"
                )
            )
        else:
            unrecorded.append(f"{name} ({len(present)} file(s))")
    if not unrecorded:
        return findings
    if config is not None and config.template_mode:
        findings.append(
            Finding(
                "INFO",
                "format",
                f"the {', '.join(unrecorded)} surfaces are this repository's own dogfood copies, held to the "
                "packaged payload by scripts/template_lint.py — an empty `integrations` record is correct here",
            )
        )
    else:
        findings.append(
            Finding(
                "WARN",
                "format",
                f"{', '.join(unrecorded)} surface file(s) exist but the lock records no install — they were "
                "copied, not installed, so `rein upgrade` will never refresh them and `uninstall` cannot "
                "retract them. `rein install <name>` adopts them (pristine files are recorded, not overwritten).",
            )
        )
    return findings


# --- gate receipts ---------------------------------------------------------------


#: Gates approved at or after the freeze. Their receipts bind the *frozen* plan and config, so a
#: receipt of theirs that names a different digest is a real inconsistency. Gates ① and ② are
#: deliberately absent: they were approved while the plan was still a draft, and `/design` and
#: `/tasks` then moved it — legitimately. Comparing their receipts against the live document (as
#: the obvious reading of "check the digests" would) turns every healthy repository permanently red.
_POST_FREEZE_GATES = ("tasks", "build", "release")


def check_freeze_drift(
    state: models.State | None, plan: models.Plan | None, config: models.Config | None
) -> list[Finding]:
    """Has anything the gate ③ freeze covers moved since? (read-only half of `rein guard` rule 2)

    Separate from :func:`check_receipts`, which answers "does this receipt bind anything". This
    answers "does what it bound still exist" — a receipt can name every required digest while
    describing a document that has since been edited.

    Two comparisons, in this order, because they answer different questions: the freeze record in
    `state.yaml` against the document on disk (did the artifact move?), then each post-freeze
    receipt against that freeze record (does the approval still describe what was approved?).
    Reporting the second without the first would name the receipt for a drift the artifact caused.
    """
    if state is None:
        return []
    if state.plan_status != "frozen":
        return [Finding("INFO", "gates", f"the plan is '{state.plan_status}' — gate 3 has not frozen it yet")]

    findings: list[Finding] = []
    frozen = {"plan_digest": state.plan_digest, "config_digest": state.plan_config_digest}
    for key, label, live in (
        ("plan_digest", "plan.yaml", plan.digest() if plan is not None else ""),
        ("config_digest", "config.yaml", config.digest() if config is not None else ""),
    ):
        recorded = frozen[key]
        if not recorded:
            findings.append(Finding("WARN", "gates", f"the plan is frozen but records no {key} for {label}"))
        elif not live:
            findings.append(Finding("FAIL", "gates", f"{label} is frozen in state.yaml but cannot be read"))
        elif live != recorded:
            findings.append(
                Finding(
                    "FAIL",
                    "gates",
                    f"{label} has changed since gate 3 froze it: it now hashes to {live[:19]}… but the "
                    f"freeze records {recorded[:19]}…. Every gate approved since covers the older bytes — "
                    "roll back with `rein revise --to tasks` and re-approve rather than editing a frozen artifact.",
                )
            )
        else:
            findings.append(Finding("PASS", "gates", f"{label} still matches the digest gate 3 froze"))

    for gate in _POST_FREEZE_GATES:
        if state.gate_status(gate) != "approved":
            continue
        receipt = state.gate_receipt(gate) or {}
        for key, label in (("plan_digest", "plan.yaml"), ("config_digest", "config.yaml")):
            bound, recorded = receipt.get(key), frozen[key]
            if not bound or not recorded or bound == recorded:
                continue
            findings.append(
                Finding(
                    "FAIL",
                    "gates",
                    f"gate '{gate}' receipt {receipt.get('approval_id')} binds a {label} digest "
                    f"({str(bound)[:19]}…) that is not the one the freeze records ({recorded[:19]}…) — "
                    "the approval was taken against a different document than the one now frozen.",
                )
            )
    return findings


def check_receipts(state: models.State | None) -> list[Finding]:
    """Every approved gate must carry a receipt that actually binds something.

    `rein guard` refuses a hand-written gate flip at edit and commit stage; this is the read-only
    half — a gate that reached `approved` some other way shows up here as a receipt with nothing
    in it, rather than as a green board. Whether what it bound has since *moved* is
    :func:`check_freeze_drift`, deliberately a separate question.
    """
    if state is None:
        return []
    findings: list[Finding] = []
    for gate in models.GATE_ORDER:
        if state.gate_status(gate) != "approved":
            continue
        receipt = state.gate_receipt(gate) or {}
        approval_id = receipt.get("approval_id")
        bound = ("validation_digest", "attested_chain_root", "result_chain_root")
        missing = [key for key in bound if not receipt.get(key)]
        if not isinstance(approval_id, str) or not approval_id:
            findings.append(Finding("FAIL", "gates", f"gate '{gate}' is approved with no approval id"))
        elif missing:
            findings.append(
                Finding(
                    "FAIL",
                    "gates",
                    f"gate '{gate}' receipt {approval_id} binds nothing: missing {', '.join(missing)}",
                )
            )
        else:
            findings.append(Finding("PASS", "gates", f"gate '{gate}' receipt {approval_id} binds its digests"))
    if not findings:
        findings.append(Finding("INFO", "gates", "no gate is approved yet"))
    return findings


# --- runtime and sandbox ---------------------------------------------------------


def check_runtime(repo: repo_mod.Repo) -> list[Finding]:
    """The runtime directory, its privacy, and any leftovers from an interrupted run."""
    findings: list[Finding] = []
    base, private = store_mod.runtime_home()
    runtime = store_mod.runtime_dir(repo)
    if not private:
        findings.append(
            Finding(
                "WARN",
                "runtime",
                f"XDG_RUNTIME_DIR is unset; falling back to {base}. A temp directory is not guaranteed to be "
                "cleared at logout or unreachable by other users — the isolation is weaker, not equivalent.",
            )
        )
    if runtime.exists():
        try:
            store_mod.ensure_private_dir(runtime)
            findings.append(Finding("PASS", "runtime", f"runtime directory {runtime} is private (0700)"))
        except store_mod.StoreError as exc:
            findings.append(Finding("FAIL", "runtime", str(exc)))
        store = store_mod.Store(repo)
        if store.journal.exists():
            findings.append(
                Finding(
                    "WARN",
                    "runtime",
                    "a store journal is present — a transaction was interrupted. The next command recovers it "
                    "automatically (forward past the point events were appended, back before it).",
                )
            )
    else:
        findings.append(Finding("INFO", "runtime", f"no runtime directory yet ({runtime})"))

    if repo.git_common_dir is None:
        findings.append(
            Finding("WARN", "runtime", "not a git checkout — change digests and blob anchors are unavailable")
        )
    elif not repo.is_canonical_checkout:
        findings.append(
            Finding(
                "INFO",
                "runtime",
                "this is a linked worktree; mutations must go through the control plane, not the store directly",
            )
        )
    return findings


def check_sandbox(config: models.Config | None) -> list[Finding]:
    """Executor profiles: anything running repository code must be an OCI profile (plan §10.1)."""
    if config is None:
        return []
    findings: list[Finding] = []
    offenders = config.unsandboxed_code_profiles()
    if offenders:
        findings.append(
            Finding(
                "FAIL",
                "sandbox",
                f"profile(s) {', '.join(offenders)} run repository-derived code on the host. A test file is "
                f"code an agent wrote, and it would run with your credentials. Run "
                f"`{config.sandbox_setup_command()}` — it builds each packaged image, pins the digests here and "
                "flips those profiles to `kind: oci`. Then check that the image can actually run the step you "
                "pointed at it: the packaged images carry python, uv and pytest, so a step invoking `make` or "
                "needing a dependency closure needs its own Containerfile first.",
            )
        )
    for name, profile in sorted(config.profiles.items()):
        if profile.is_sandboxed and not profile.image_digest:
            findings.append(Finding("FAIL", "sandbox", f"profile '{name}' has no digest-pinned image"))
        elif profile.is_sandboxed:
            findings.append(Finding("PASS", "sandbox", f"profile '{name}' pinned to {profile.image_digest[:19]}…"))
        if profile.is_sandboxed and (profile.network_profile or "none") != "none":
            findings.append(
                Finding(
                    "WARN",
                    "sandbox",
                    f"profile '{name}' names network '{profile.network_profile}', which the executor refuses at "
                    "run time — egress needs an experiment receipt this release cannot check. Set it to 'none' "
                    "so the config says what will actually happen.",
                )
            )

    # Checked whenever a sandbox is configured *or still owed*. Gating this on "OCI profiles are
    # configured" meant a fresh repository — every profile still `kind: host` — was told to build
    # images and never told it needed a container runtime to do it, so the prerequisite surfaced
    # only as a failed build several minutes later.
    if offenders or any(p.is_sandboxed for p in config.profiles.values()):
        runtime = shutil.which("docker") or shutil.which("podman")
        if runtime:
            findings.append(Finding("PASS", "sandbox", f"container runtime found ({Path(runtime).name})"))
            for name, profile in sorted(config.profiles.items()):
                if not profile.is_sandboxed or not profile.image_digest:
                    continue  # covered above: not sandboxed, or already flagged as unpinned
                ok, message = executors.verify_pinned(profile, runtime=runtime)
                if ok:
                    findings.append(Finding("PASS", "sandbox", f"profile '{name}': {message}"))
                elif "no local image" in message:
                    # Not built here yet — expected on a fresh checkout, actionable before
                    # `rein build` opens rather than broken right now.
                    findings.append(Finding("WARN", "sandbox", f"profile '{name}': {message}"))
                else:
                    # A local image exists under a digest that does not match the pin — the
                    # config drifted from what gate 3 froze, or was rebuilt without re-pinning.
                    findings.append(Finding("FAIL", "sandbox", f"profile '{name}': {message}"))
        elif offenders:
            findings.append(
                Finding(
                    "FAIL",
                    "sandbox",
                    "no docker/podman on PATH, and the profiles above still need sandboxing — install one "
                    "first; there is nothing to build the images with.",
                )
            )
        else:
            findings.append(Finding("FAIL", "sandbox", "no docker/podman on PATH, but OCI profiles are configured"))
    return findings


def check_independence(config: models.Config | None) -> list[Finding]:
    """The actual-extractor / comparator pair (plan §12.4)."""
    if config is None:
        return []
    from rein import agent_cli

    # The level comes from the branch that produced the message, not from searching the message:
    # recovering it with `"share the independence group" in w` meant a reworded sentence in
    # agent_cli could downgrade a FAIL to a WARN with nothing going red.
    level, warnings = agent_cli.independence_status(config)
    if level == "PASS":
        left = config.independence_group("actual_extractor")
        right = config.independence_group("comparator")
        return [Finding("PASS", "review", f"independent groups: {left} vs {right}")]
    return [Finding(level, "review", w) for w in warnings]


def check_adapters(config: models.Config | None, state: models.State | None) -> list[Finding]:
    """The agent CLIs `rein build` launches.

    The implementation phase has no hand-driven equivalent, so an adapter that is not on PATH is
    simply what stops the build from starting — a precondition worth naming here rather than
    leaving to the run's exit `2`.
    """
    if config is None:
        return []
    from rein import agent_cli, build_loop

    findings: list[Finding] = []
    binaries: dict[str, list[str]] = {}
    for role in agent_cli.ROLES:
        adapter = config.adapter(role) or "claude"
        argv = build_loop.ADAPTERS.get(adapter)
        if argv is None:
            known = ", ".join(sorted(build_loop.ADAPTERS))
            findings.append(
                Finding("FAIL", "agents", f"agents.{role}.adapter is {adapter!r} — known adapters: {known}")
            )
        else:
            binaries.setdefault(argv[0], []).append(role)
    # Before the build phase a missing CLI is normal: nothing has needed it yet.
    level = "FAIL" if state is not None and state.current_phase == "build" else "WARN"
    for binary, roles in sorted(binaries.items()):
        who = ", ".join(roles)
        if shutil.which(binary):
            findings.append(Finding("PASS", "agents", f"{binary} found on PATH ({who})"))
        else:
            findings.append(
                Finding(
                    level,
                    "agents",
                    f"{binary} not found on PATH — `rein build` launches it for {who}. "
                    "Install it, or point the roles elsewhere with `rein agent <cli>`",
                )
            )
    return findings


def check_binaries() -> list[Finding]:
    findings: list[Finding] = []
    for name, level, why in (
        ("git", "FAIL", "change digests, blob anchors and worktrees are git operations"),
        ("uv", "WARN", "the documented way to run this tool and its quality gate"),
        ("gh", "INFO", "only needed when github.enabled is turned on"),
    ):
        if shutil.which(name):
            findings.append(Finding("PASS", "env", f"{name} found on PATH"))
        else:
            findings.append(Finding(level, "env", f"{name} not found on PATH — {why}"))
    return findings


#: Hook host → how it is named to a human.
_HOST_LABEL = {"claude": "Claude Code", "copilot": "VS Code Copilot", "codex": "Codex"}


def _mentions_guard(text: str) -> bool:
    return "rein guard" in text or "gate_guard" in text


def _reads_guard(path: Path) -> bool:
    """True when `path` exists and registers the guard. An unreadable file is not a registration."""
    try:
        return _mentions_guard(path.read_text(encoding="utf-8"))
    except OSError:
        return False


def check_hook(repo: repo_mod.Repo) -> list[Finding]:
    """The gate guard is only real if a PreToolUse hook actually invokes it.

    There is no `enforce_hook` knob to check any more: a guard with an off switch an agent can
    reach is a convention, so the only question left is whether a host carries it.
    """
    registered = {
        "claude": [repo.path(SETTINGS_PATH)],
        "copilot": sorted(repo.path(COPILOT_HOOKS_DIR).glob("*.json")),
        "codex": [repo.path(rel) for rel in CODEX_HOOK_FILES],
    }
    surfaces = [host for host, files in registered.items() if any(_reads_guard(f) for f in files)]
    if not surfaces:
        return [
            Finding(
                "WARN",
                "hook",
                f"the gate guard is registered in none of {SETTINGS_PATH}, {COPILOT_HOOKS_DIR}/*.json, "
                f"{', '.join(CODEX_HOOK_FILES)} — edit-time enforcement is absent. The commit-stage check "
                "(`rein guard --check-diff`) still applies if the pre-commit hook is installed.",
            )
        ]
    findings = [Finding("PASS", "hook", f"gate guard registered ({', '.join(surfaces)})")]
    missing = [_HOST_LABEL[host] for host in registered if host not in surfaces]
    if missing:
        findings.append(
            Finding("INFO", "hook", f"{' / '.join(missing)} sessions run without it — no hook host registered for them")
        )
    if "codex" in surfaces:
        findings.append(
            Finding(
                "INFO",
                "hook",
                "the Codex registration is project-scoped config, which Codex reads only once the project "
                "is trusted — until then that session falls back to the commit-stage check",
            )
        )
    return findings


#: The verbs whose pre-authorization breaks gate rule 2 outright: each one, run without a prompt,
#: opens a gate or closes a cycle with no human in the loop.
GATE_OPENING_VERBS = ("rein approve", "rein cycle-close")
#: Both permission files. The local one is the one that matters: it is gitignored, so an entry
#: added there appears in no diff, no code review, and no scripts/template_lint.py run.
PERMISSION_FILES = (SETTINGS_PATH, ".claude/settings.local.json")
_BASH_RULE_RE = re.compile(r"^Bash\((?P<cmd>.*)\)$")


def preauthorized_verbs(entry: str) -> list[str]:
    """The gate-opening verbs a `permissions.allow` entry would let through unprompted.

    Matching is prefix-based in both directions, because the host's own matching is: a rule for
    `rein approve build` reaches one of the verbs, and so does the broader `rein`,
    which pre-authorizes every verb including this one. What it cannot see is a wrapper —
    `Bash(uv run *)` reaches `uv run rein approve` — and enumerating every shell that could
    carry a command is not something a check can honestly claim to do, so the PASS below says
    only what was actually established.
    """
    m = _BASH_RULE_RE.match(entry.strip())
    if m is None:
        return []
    cmd = m.group("cmd").removesuffix(":*").strip()
    if not cmd:
        return []
    return [verb for verb in GATE_OPENING_VERBS if cmd.startswith(verb) or verb.startswith(cmd)]


def check_preauthorization(repo: repo_mod.Repo) -> list[Finding]:
    """No permissions file pre-authorizes a verb that opens a gate.

    Gate rule 2 — "never pre-authorize `rein approve`" — has until now existed only in prose,
    while being the whole of what stops an agent approving its own work: there is no key or
    authority to check, only a confirmation typed at an interactive terminal, so a
    pre-authorized `rein approve` would let an agent record that confirmation itself. So it is
    worth a check that runs in every repository, and specifically one that reads
    settings.local.json, which no other check can see.
    """
    findings: list[Finding] = []
    checked: list[str] = []
    for rel in PERMISSION_FILES:
        try:
            loaded = json.loads(repo.path(rel).read_text(encoding="utf-8"))
        except OSError:
            continue
        except ValueError:
            findings.append(Finding("WARN", "gates", f"{rel} is not valid JSON — its permissions cannot be checked"))
            continue
        if not isinstance(loaded, dict):
            continue
        checked.append(rel)
        permissions = loaded.get("permissions")
        allow = permissions.get("allow") if isinstance(permissions, dict) else None
        for entry in allow if isinstance(allow, list) else []:
            for verb in preauthorized_verbs(str(entry)):
                findings.append(
                    Finding(
                        "FAIL",
                        "gates",
                        f"{rel} pre-authorizes `{entry}`, which lets `{verb}` run unprompted — gate rule 2 "
                        "forbids it. Remove the entry: that rule, not any credential, is what keeps an "
                        "agent from approving its own work.",
                    )
                )
    if checked and not any(f.level == "FAIL" for f in findings):
        findings.append(Finding("PASS", "gates", f"no gate-opening verb is pre-authorized in {', '.join(checked)}"))
    return findings


def check_ci(repo: repo_mod.Repo) -> list[Finding]:
    """Does anything run the base-side meta-policy on a pull request?

    `policy-check` is the one check a pull request cannot fake, because it runs from the base
    side. Whether it is *wired* is a property of this repository's CI, which Loose Rein does not
    configure (that is repository administration, deliberately out of scope) — so this reports
    and never installs. WARN, not FAIL: a product may run it from a CI system that leaves no
    trace in `.github/workflows/`, and diagnosing that as broken would be a false alarm.
    """
    workflows = repo.path(".github/workflows")
    if not workflows.is_dir():
        return [Finding("INFO", "ci", "no .github/workflows/ — nothing here can tell whether CI runs policy-check")]

    findings: list[Finding] = []
    invoking: list[tuple[str, str]] = []
    for path in sorted(workflows.glob("*.y*ml")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        findings += _workflow_hazards(path.name, text)
        if "rein policy-check" in text:
            invoking.append((path.name, text))

    if not invoking:
        return [
            *findings,
            Finding(
                "WARN",
                "ci",
                "no workflow runs `rein policy-check`, so nothing verifies a pull request from the "
                "trusted base side — a head that weakens the policy judging it would go unnoticed. See "
                "'Repository settings you have to make yourself' in the README.",
            ),
        ]

    name, text = invoking[0]
    findings.append(Finding("PASS", "ci", f"the base-side meta-policy runs in {name}"))
    # A `policy-check` step is only a check if the event gives it a base the head cannot choose.
    for token, what in (
        ("pull_request", "a `pull_request` trigger — the only event where CI knows a trusted base"),
        ("github.event.pull_request.base.sha", "the base SHA from the event context, not a branch name"),
        ("github.event.pull_request.head.sha", "the head SHA from the event context"),
    ):
        if not any(token in body for _, body in invoking):
            findings.append(Finding("WARN", "ci", f"the policy-check workflow does not carry {what}"))
    if not any("fetch-depth: 0" in body for _, body in invoking):
        findings.append(
            Finding("WARN", "ci", "the policy-check workflow does not set `fetch-depth: 0` — it cannot read both trees")
        )
    return findings


_PINNED_ACTION = re.compile(r"uses:\s*(?!\./)([^\s@]+)@([^\s#]+)")
_SHA_REF = re.compile(r"[0-9a-f]{40}")


def _workflow_hazards(name: str, text: str) -> list[Finding]:
    """Workflow shapes that hand a pull request more than it should have.

    Diagnosed, never changed: a tool that can configure the checks that judge it is not a
    boundary, so repository administration is deliberately out of scope.
    """
    findings: list[Finding] = []
    if "pull_request_target" in text:
        findings.append(
            Finding(
                "WARN",
                "ci",
                f"{name} uses `pull_request_target`, which runs with the base repository's secrets "
                "against head-supplied code — a fork's pull request can reach them",
            )
        )
    elif "pull_request" in text and "contents: write" in text:
        # Only on a pull-request trigger: a release workflow writing tags is doing its job.
        findings.append(
            Finding("WARN", "ci", f"{name} grants `contents: write` to a workflow a pull request can trigger")
        )
    unpinned = sorted({action for action, ref in _PINNED_ACTION.findall(text) if not _SHA_REF.fullmatch(ref)})
    if unpinned:
        findings.append(
            Finding(
                "WARN",
                "ci",
                f"{name} uses third-party action(s) not pinned to a commit SHA: {', '.join(unpinned)} — "
                "a moving tag is a supply-chain hole in the job that judges this repository",
            )
        )
    return findings


# --- plan and traceability --------------------------------------------------------


def check_plan(repo: repo_mod.Repo, plan: models.Plan | None, state: models.State | None) -> list[Finding]:
    if plan is None:
        return [Finding("INFO", "plan", "no plan yet — /req and /design fill it")]
    findings: list[Finding] = []
    graph: dag.Graph | None = None
    try:
        graph = dag.join(plan, state)
        findings.append(Finding("PASS", "plan", f"the task DAG is acyclic ({len(graph.tasks)} task(s))"))
        orphan_claims = graph.claims_without_a_task(plan)
        if orphan_claims:
            findings.append(Finding("WARN", "plan", f"claims with no answerable task: {', '.join(orphan_claims)}"))
    except dag.DagError as exc:
        findings.append(Finding("FAIL", "plan", str(exc)))

    report = dag_trace.trace_repo(repo, plan, graph)
    findings += [Finding("FAIL", "trace", e) for e in report.errors]
    findings += [Finding("WARN", "trace", w) for w in report.warnings]
    if not report.checked:
        findings.append(Finding("WARN", "trace", "no requirement id on either side — the thread is unknown, not whole"))
    elif not report.errors and not report.warnings:
        findings.append(Finding("PASS", "trace", "the requirement → claim → task thread is whole"))
    return findings


def check_gate_chain(state: models.State | None) -> list[Finding]:
    if state is None:
        return []
    violations = state.gate_chain_violations()
    if violations:
        return [
            Finding(
                "FAIL",
                "gates",
                f"gate '{approved}' is approved while '{pending}' upstream is pending — an approval survived "
                "a roll back, so downstream work stands on a withdrawn decision",
            )
            for approved, pending in violations
        ]
    return [Finding("PASS", "gates", "the gate chain invariant holds")]


# --- review and audit chain --------------------------------------------------------


def check_chain(repo: repo_mod.Repo) -> list[Finding]:
    events, defects = event_chain.scan(repo.events)
    if defects:
        shown = "; ".join(str(d) for d in defects[:3])
        return [
            Finding(
                "FAIL",
                "event-chain",
                f"{len(defects)} defect(s): {shown}{' …' if len(defects) > 3 else ''}. "
                "Restore events.ndjson from git — never rewrite it to agree with the current state.",
            )
        ]
    return [
        Finding("PASS", "event-chain", f"{len(events)} event(s), chain intact, root {event_chain.chain_root(events)}")
    ]


#: How long a retryable abort can sit unattended before "wait for the reset" reads as "nobody
#: is re-running this" instead. A few multiples of the documented/--supervise retry interval
#: (900s) — long enough that a normal capacity-limit reset has already passed.
_STALE_ABORT_AFTER_SEC = 3 * 60 * 60


def _seconds_since(ts: str) -> float | None:
    """Wall-clock seconds between `ts` (an event's ISO-8601 timestamp) and now, or None if
    unparseable. Never touches the fault's own free-text `reported` field — that stays quoted,
    never parsed (see `faults.reset_hint`); this only compares event timestamps."""
    try:
        when = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if when.tzinfo is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


def check_last_run(repo: repo_mod.Repo) -> list[Finding]:
    """Did the last build run stop because the machine failed, and has nothing succeeded since?

    Worth saying out loud, because this is the state that looks like nothing happened: no task
    is `blocked`, no escalation is open, and the board reads exactly as it did before the run.
    Someone coming back to a repository whose build stopped on a session limit at 3am otherwise
    has to infer that from the absence of progress.
    """
    events, _ = event_chain.scan(repo.events)
    last_abort = next((e for e in reversed(events) if e.event == "run_aborted"), None)
    if last_abort is None:
        return []
    progressed = any(e.seq > last_abort.seq and e.event in ("task_completed", "task_started") for e in events)
    if progressed:
        return []
    detail = last_abort.detail
    where = str(detail.get("where", "a launch"))
    reported = str(detail.get("reported", ""))
    retryable = str(detail.get("fault", "")) == "environment_transient"
    idle_sec = _seconds_since(last_abort.ts)
    stale = retryable and idle_sec is not None and idle_sec >= _STALE_ABORT_AFTER_SEC
    if stale:
        assert idle_sec is not None  # `stale` is only True when the check above already held
        advice = (
            f"Nothing has re-run it in over {int(idle_sec // 3600)}h — well past any capacity "
            "limit that reports a same-day reset. Nothing is retrying this: run `rein build` "
            "(or `rein build --supervise` so a future stop like this one keeps retrying itself)."
        )
    elif retryable:
        advice = (
            "Nothing was marked and no retry budget was spent — re-run `rein build` and it "
            "continues from the preserved work."
        )
    else:
        advice = "Repair what it names first; re-running before that will stop the same way."
    return [
        Finding(
            "WARN" if (stale or not retryable) else "INFO",
            "build",
            f"the last build run stopped at {where} for a machine reason"
            f"{f' ({reported})' if reported else ''}. {advice}",
        )
    ]


def check_review(review: models.Review | None, head: str = "") -> list[Finding]:
    if review is None or not review.is_generated:
        return [Finding("INFO", "review", "no machine review generated yet")]
    findings: list[Finding] = []
    reviewed = review.subject_head_sha
    if head and reviewed and reviewed != head:
        findings.append(
            Finding(
                "FAIL",
                "review",
                f"the machine review is stale: generated against {reviewed[:12]}, HEAD is {head[:12]}. "
                "Re-run `rein review generate`.",
            )
        )
    elif head and reviewed:
        findings.append(Finding("PASS", "review", f"the machine review speaks for HEAD ({head[:12]})"))
    if review.coverage_sufficient:
        findings.append(
            Finding("PASS", "review", f"coverage sufficient; {len(review.extra_behaviors)} extra behaviour(s)")
        )
    else:
        findings.append(
            Finding(
                "FAIL",
                "review",
                "the coverage manifest is insufficient — extra-behaviour counts are undeterminable, not zero",
            )
        )
    blocking = review.blocking_security_findings
    findings.append(Finding("FAIL" if blocking else "PASS", "review", f"{len(blocking)} blocking security finding(s)"))
    findings.append(Finding("INFO", "review", f"human review: {review.human_status}"))
    return findings


# --- driver -----------------------------------------------------------------------


def run_checks(repo: repo_mod.Repo | None = None) -> list[Finding]:
    if repo is None:
        try:
            repo = repo_mod.get()
        except repo_mod.RepoNotFoundError:
            repo = repo_mod.Repo(Path.cwd().resolve())

    findings = check_layout(repo)
    findings += check_binaries()
    findings += check_lock(repo)
    document_findings, loaded = check_documents(repo)
    findings += document_findings
    findings += check_materialized(repo)

    config = loaded.get("config")
    state = loaded.get("state")
    plan = loaded.get("plan")
    review = loaded.get("review")
    assert config is None or isinstance(config, models.Config)
    assert state is None or isinstance(state, models.State)
    assert plan is None or isinstance(plan, models.Plan)
    assert review is None or isinstance(review, models.Review)

    findings += check_integrations(repo, config)
    findings += check_runtime(repo)
    findings += check_sandbox(config)
    findings += check_independence(config)
    findings += check_adapters(config, state)
    findings += check_hook(repo)
    findings += check_preauthorization(repo)
    findings += check_ci(repo)
    findings += check_gate_chain(state)
    findings += check_receipts(state)
    findings += check_freeze_drift(state, plan, config)
    findings += check_plan(repo, plan, state)
    findings += check_chain(repo)
    findings += check_last_run(repo)
    rc, head_out = repo._git_rc("rev-parse", "HEAD")
    findings += check_review(review, head_out.strip() if rc == 0 else "")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="diagnose the Loose Rein environment and SSOT (read-only)")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo) if args.repo else repo_mod.get()
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    findings = run_checks(repo)
    for f in findings:
        print(f"  [{f.level:<4}] {f.area}: {f.message}")
    fails = sum(1 for f in findings if f.level == "FAIL")
    warns = sum(1 for f in findings if f.level == "WARN")
    print(f"\ndoctor: {fails} FAIL / {warns} WARN / {len(findings)} checks")
    if fails:
        logger.error("fix the FAIL items before continuing.")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
