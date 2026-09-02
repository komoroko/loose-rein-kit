"""The mechanism layer: deny in code what the convention layer merely asks agents not to do.

Registered as a PreToolUse hook by `rein install <agent>`, this fires on every
editor write and answers one question — may this path be written right now? It also runs at
commit stage (`--check-diff`) over every changed path, so an agent whose environment cannot
intercept edits, or a write that bypassed the hook (a shell redirect, `sed -i`), is still
checked before the change lands.

Four rules, in order of severity:

1. **Machine-written artifacts are never hand-edited.** `state.yaml`, `review.yaml`,
   `events.ndjson` are written only inside a Central Store transaction.
   A hand edit produces a state change with no matching audit event — the exact invisible
   mutation the chain exists to make impossible.
2. **A frozen plan is frozen.** Once gate ③ closes, `plan.yaml`, `config.yaml`, the sandbox
   definitions, and the materialized prompts/schema are pinned by the receipt the human confirmed.
   Changing them goes through `rein revise --to tasks`, which resets the downstream
   gates in a chain (plan §16.4).
3. **A deliverable waits for its prerequisite gate.** docs/20-design.md needs `requirements`,
   docs/tasks/ needs `design`, src/ needs `tasks`, docs/test/ needs `build`. Configurable per
   repo via `guard.paths`; `tests/` is deliberately unguarded, because preparing fixtures
   while a gate is pending is sanctioned speculative work.
4. **Only humans open gates.** Any edit whose *result* would turn a gate `approved` is denied.

**There is no escape hatch**, and an `enforce_hook`-style key is rejected by the config schema.
A guard with an off switch an agent can reach is a convention, not a mechanism — and an agent
that hits this guard has found a gate boundary, not an obstacle to route around (AGENTS.md
"Gate rules").

Unreadable state **fails closed**, and so does an unreadable config: the rule map is as much a
thing this guard has to determine as the gates are (:func:`guard_settings`). What it does *not*
read is the config schema — a key a newer release added is not a config this guard may not read.
`guard.template_mode` relaxes only rule 3, and only because the template repository's scaffold
originals share paths with product deliverables; it never relaxes rules 1, 2, or 4.

I/O follows the hook convention shared by Claude Code, VS Code Copilot, and Codex: the event
JSON on stdin, a deny decision as JSON on stdout, and always exit 0. What differs is only how
a host names the paths it is about to write — see :func:`hook_paths`. A tool invocation
carrying no path at all always passes; some hosts fire the hook for reads and terminal
commands too.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rein import common, models, strict_yaml
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SEC = 30

#: Written only by a Central Store transaction. Rule 1 — never relaxed.
MACHINE_WRITTEN: tuple[str, ...] = (
    ".rein/state.yaml",
    ".rein/review.yaml",
    ".rein/events.ndjson",
    ".rein/rein.lock",
)

#: Where this guard is *registered*. Written by `rein install`, hashed in the lock, and — until now —
#: guarded by nothing: not rule 1, not rule 2, and not `guard.paths`, which covers deliverable
#: directories. So the one file an agent could edit to switch off edit-stage enforcement was the one
#: file no rule mentioned. Denied outright rather than gated behind an approval, because there is no
#: phase at which an agent rewriting the guard's own registration is the expected next step; a human
#: changing it does so at their editor, where no PreToolUse hook applies.
#:
#: This closes the tool-write path only. A CLI that writes its own project config directly (Codex
#: creating `.codex/config.toml` on trust) is not a tool call and no hook sees it — that is what the
#: commit-stage check and `doctor.check_hook` are for.
HOOK_REGISTRATION: tuple[str, ...] = (
    ".claude/settings.json",
    ".claude/settings.local.json",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".github/hooks/",
)

#: The document this guard reads its own rules out of, and therefore the one path an unreadable
#: config may not deny (:func:`_rule_three`).
CONFIG_PATH = ".rein/config.yaml"

#: Pinned by the gate ③ receipt. Rule 2 — denied once `plan.status` is frozen.
FROZEN_AFTER_GATE_THREE: tuple[str, ...] = (
    ".rein/plan.yaml",
    CONFIG_PATH,
    ".rein/prompts/",
    ".rein/schema/",
    ".rein/oci/",
)

#: Rule 3's built-in defaults, used when config carries no `guard.paths`. A key guards the path it
#: names and everything beneath it; the trailing slash is punctuation, not meaning.
DEFAULT_GUARD_PATHS: dict[str, str] = {
    "docs/20-design.md": "requirements",
    "docs/decisions/": "requirements",
    "docs/tasks/": "design",
    "docs/test/": "build",
    "src/": "tasks",
    "lib/": "tasks",
    "app/": "tasks",
    "backend/": "tasks",
    "frontend/": "tasks",
    "scripts/": "tasks",
}

_PHASE_LABEL = {
    "requirements": "/req (requirements)",
    "design": "/design (design)",
    "tasks": "/tasks (task plan)",
    "build": "/build (implementation)",
    "release": "/verify (release)",
}


def _repo_or_cwd(start: Path | None = None) -> repo_mod.Repo:
    """The discovered repo, or a cwd-anchored one when no .rein/ exists anywhere above.

    The fallback preserves the fail-closed posture outside a Loose Rein repository: state
    reads fail there, which denies guarded-path writes exactly as an unreadable state would.
    """
    try:
        return repo_mod.get(start=start)
    except repo_mod.RepoNotFoundError:
        return repo_mod.Repo((start or Path.cwd()).resolve())


def _matches(rel: str, patterns: tuple[str, ...]) -> str | None:
    """The pattern in `patterns` that covers `rel`, or None (:func:`rein.common.path_covered`)."""
    return common.longest_cover(rel, patterns)


@dataclass(frozen=True)
class GuardSettings:
    """Everything rule 3 reads out of `config.yaml`, and whether it could be read at all.

    `unreadable` carries the reason when the document is there and did not yield settings. It is
    a *deny*, never a default: the two fields below decide what this guard enforces, so guessing
    at them is the guard deciding it does not apply.
    """

    template_mode: bool = False
    paths: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_GUARD_PATHS))
    unreadable: str = ""


def guard_settings(repo: repo_mod.Repo) -> GuardSettings:
    """Rule 3's two settings, read from the config **document** and never through its schema.

    This used to go through `models.Config.parse`, which validates the whole file against
    `config.schema.json` and raises on the first key it does not know. Those are two different
    questions, and answering the second one here cost the guard its answer to the first: a
    repository written by a *newer* rein carries keys this release's schema has never heard of —
    `review_policy.composition` was the one that surfaced it — so the parse failed, the config
    came back `None`, and `template_mode` silently became `False`. A repository that had switched
    rule 3 off then blocked every edit under `src/`, with a message telling the human to complete
    `/tasks` and get a gate approved. The document was intact; the reader was old; and the repair
    the human was handed was the most expensive move in the workflow, aimed at nothing.

    `guard.template_mode` and `guard.paths` are what this guard needs, they are shaped here, and
    a release that widens some unrelated part of the schema cannot move them. So the document is
    parsed as YAML, those two are read and type-checked on their own, and anything else in the
    file is none of the guard's business.

    What is still fatal is the guard's own inputs being unreadable — YAML that does not parse, a
    `guard` block that is not a mapping, a `paths` list that is not a list of entries. That
    returns `unreadable` and rule 3 denies: "a guard that cannot determine its gates must not open
    them" applies to the map as much as to the gates, and the previous behaviour — substituting
    `DEFAULT_GUARD_PATHS` for a rule map it could not read — dropped every path a repository had
    added to its own guard, which is a *fail-open* in the one function that must not have one.

    An absent config.yaml is not unreadable: there is nothing to read and the built-in defaults
    are the whole rule map, which is the posture outside a Loose Rein repository too.
    """
    try:
        text = repo.config.read_text(encoding="utf-8")
    except FileNotFoundError:
        return GuardSettings()
    except OSError as exc:
        return GuardSettings(unreadable=f"config.yaml could not be read ({exc})")
    try:
        document = strict_yaml.load_mapping(text, what="config.yaml")
    except strict_yaml.StrictParseError as exc:
        return GuardSettings(unreadable=str(exc))
    guard = document.get("guard", {})
    if not isinstance(guard, Mapping):
        return GuardSettings(unreadable="config.yaml's `guard` is not a mapping")
    template_mode = guard.get("template_mode", False)
    if not isinstance(template_mode, bool):
        return GuardSettings(unreadable="config.yaml's `guard.template_mode` is not true or false")
    entries = guard.get("paths")
    if entries is None:
        return GuardSettings(template_mode=template_mode)
    if not isinstance(entries, list) or not all(isinstance(entry, Mapping) for entry in entries):
        return GuardSettings(unreadable="config.yaml's `guard.paths` is not a list of {path, requires_gate} entries")
    rules = {str(e.get("path", "")): str(e.get("requires_gate", "")) for e in entries if e.get("path")}
    if not all(rules.values()):
        return GuardSettings(unreadable="config.yaml has a `guard.paths` entry with no `requires_gate`")
    # An empty list is not "guard nothing": the defaults stand, exactly as they do for a config
    # that names no paths at all. Disarming rule 3 is what `template_mode` is for, and it says so.
    return GuardSettings(template_mode=template_mode, paths=rules or dict(DEFAULT_GUARD_PATHS))


def required_gate(file_path: str, rules: dict[str, str], repo: repo_mod.Repo | None = None) -> str | None:
    """The gate this edit requires under rule 3. None when the path is not guarded.

    The most specific entry wins, so the decision does not depend on the config's key order. An
    exact entry still beats every prefix — a prefix that covers a path can only be shorter than it
    — which is why that case no longer needs a branch of its own.

    `rules` is required rather than defaulted from the config: the caller has to have decided what
    to do about a config it could not read before it gets to ask this question.
    """
    repo = repo or _repo_or_cwd()
    rel = repo.rel(file_path)
    if rel is None:
        return None
    key = common.longest_cover(rel, rules)
    return rules[key] if key is not None else None


def _read_state(repo: repo_mod.Repo) -> models.State | None:
    """state.yaml as a validated State, or None when unreadable (the caller fails closed)."""
    try:
        text = repo.state.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return models.State(strict_yaml.load_mapping(text, what="state.yaml"))
    except strict_yaml.StrictParseError:
        return None


def evaluate(file_path: str, repo: repo_mod.Repo | None = None, *, stage: str = "edit") -> tuple[bool, str]:
    """(allowed, deny reason) for one path. `stage` selects which rules apply.

    ``edit`` (the hook) applies all of rules 1–3. ``commit`` applies rule 3 only, because
    rules 1 and 2 forbid *hand edits*, not commits: the Central Store writes `state.yaml` and
    a gate-3 freeze writes `plan.yaml`, and those writes have to be committable or the very
    first `git commit` after `rein init` would be blocked by the guard.

    Nothing is lost by that. At commit stage the same two properties are checked more
    strongly, by content rather than by path: :func:`_frozen_artifact_failures` compares a
    frozen artifact against the digest its receipt bound, and :func:`_flip_failures` requires
    a gate flip to be backed by an event and a receipt. A hand edit that was reverted and
    re-applied would pass a path rule and still fail those.
    """
    repo = repo or _repo_or_cwd()
    rel = repo.rel(file_path)
    if rel is None:
        return True, ""
    if stage == "commit":
        return _rule_three(repo, file_path)

    # Rule 1 — machine-written artifacts. Not relaxed by template_mode.
    if _matches(rel, MACHINE_WRITTEN):
        return False, (
            f"Blocked: {rel} is written only by an `rein` Central Store transaction, together with"
            " the audit events that explain the change. A hand edit produces a state change with no"
            " matching event, which `rein doctor` reports and no gate receipt will cover."
            " Use the command that owns this change instead."
        )

    # Rule 1, second half — the guard's own registration. Also never relaxed by template_mode: a
    # template whose hook can be switched off is a template that ships with the switch.
    if _matches(rel, HOOK_REGISTRATION):
        return False, (
            f"Blocked: {rel} is where this guard is registered with the host, so an edit to it can"
            " switch off edit-stage enforcement — including this very check. `rein install` writes"
            " it and the lock records its hash; there is no phase at which rewriting it is the"
            " expected next step. If a human wants it changed, they change it at their own editor,"
            " where no PreToolUse hook applies."
        )

    state = _read_state(repo)

    # Rule 2 — a frozen plan and its pinned toolchain.
    frozen_pattern = _matches(rel, FROZEN_AFTER_GATE_THREE)
    if frozen_pattern is not None:
        if state is None:
            return False, (
                f"Blocked: cannot read .rein/state.yaml, so the guard cannot tell whether the plan"
                f" is frozen and fails closed on {rel}. Repair state.yaml (restore it from git) first."
            )
        if state.plan_status == "frozen":
            return False, (
                f"Blocked: the plan froze at gate 3 and {rel} is bound by the receipt the human signed."
                " Changing it now would leave the approval covering bytes nobody read. Roll back first:"
                " `rein revise --to tasks` (this resets the downstream gates in a chain)."
            )
        return True, ""

    # Rule 3 — deliverables wait for their prerequisite gate.
    return _rule_three(repo, file_path)


def _rule_three(repo: repo_mod.Repo, file_path: str) -> tuple[bool, str]:
    """Rule 3 alone: a deliverable waits for its prerequisite gate."""
    settings = guard_settings(repo)
    rel = repo.rel(file_path)
    if settings.unreadable and rel != CONFIG_PATH:
        # The one exemption, and it is not a hole: a guard that denies every path because it could
        # not read config.yaml would be denying the repair it just asked for — including the
        # human's own `git commit` of the fix, since rule 3 runs at commit stage over every changed
        # path. Nothing else about this file loosens: rule 2 still refuses it once the plan is
        # frozen, and the commit-stage frozen-artifact check still compares it against the digest
        # gate ③ bound.
        return False, (
            "Blocked: the guard reads `guard.paths` and `guard.template_mode` from"
            f" .rein/config.yaml, and it could not: {settings.unreadable}. It does not know which"
            " paths this repository guards, so it fails closed on all of them rather than"
            " enforcing a rule map nobody wrote. Repair .rein/config.yaml (restore it from git);"
            " `rein doctor` reports what is wrong with it. There is deliberately no flag that"
            " turns this guard off."
        )
    gate = required_gate(file_path, settings.paths, repo)
    if gate is None:
        return True, ""
    if settings.template_mode:
        return True, ""
    state = _read_state(repo)
    if state is None:
        return False, (
            "Blocked: cannot read the gates from .rein/state.yaml (missing or malformed), so the"
            " gate guard fails closed. Repair state.yaml — restore it from git. There is deliberately"
            " no flag that turns this guard off."
        )
    if state.gate_status(gate) == "approved":
        return True, ""
    phase = _PHASE_LABEL.get(gate, gate)
    return False, (
        f"Blocked: gate '{gate}' is not approved, and this edit requires it."
        f" Complete {phase} first and get the human's signed approval."
    )


def _frozen_artifact_failures(repo: repo_mod.Repo) -> list[str]:
    """The commit-stage form of rule 2: the frozen artifacts must still hash to what gate ③ froze.

    Stronger than the path rule the hook applies, because it compares content. An edit that was
    made, reverted, and re-applied leaves no trace in a path list but moves the digest.

    Both artifacts are checked. Gate ③ freezes `config.yaml` for the same reason it freezes
    `plan.yaml` — it fixes the sandbox and the quality gate the evidence will be produced in — so
    covering only the plan left half the freeze resting on the path rule alone.

    config.yaml is compared by :meth:`models.Config.frozen_digest`, which excludes the image pins:
    rebuilding a pinned image mid-cycle is a rebuild of the *same* sandbox, and making that cost a
    rollback meant a task that legitimately added a dependency could not land at all. Everything
    else in the file — `kind`, `network_profile`, `mount_repo`, the quality gate, the budgets —
    still fails here the moment it moves.
    """
    from rein import store as store_mod

    store = store_mod.Store(repo)
    try:
        state = store.read_state()
    except (models.DocumentError, strict_yaml.StrictParseError):
        return ["state.yaml cannot be read, so a frozen plan cannot be checked against its receipt"]
    if state is None or state.plan_status != "frozen":
        return []

    def plan_now() -> str | None:
        document = store.read_plan()
        return document.digest() if document is not None else None

    def config_now() -> str | None:
        document = store.read_config()
        return document.frozen_digest() if document is not None else None

    failures: list[str] = []
    for label, recorded, live in (
        ("plan.yaml", state.plan_digest, plan_now),
        ("config.yaml", state.plan_config_digest, config_now),
    ):
        if not recorded:
            continue  # frozen before this digest was recorded: nothing to compare against
        try:
            current = live()
        except (models.DocumentError, strict_yaml.StrictParseError) as exc:
            failures.append(f"{label} is frozen but no longer valid: {exc}")
            continue
        if current is None:
            failures.append(f"{label} is frozen in state.yaml but the file is gone")
        elif current != recorded:
            failures.append(
                f"{label} has changed since gate 3 froze it (its digest no longer matches the receipt). "
                "Roll back with `rein revise --to tasks` instead of editing a frozen artifact."
            )
    return failures


# --- rule 4: gate-approval write protection -----------------------------------


def _proposed_text(current_text: str, tool_input: dict[str, Any]) -> str | None:
    """state.yaml's content as it would be after this Write/Edit/MultiEdit. None = unknown shape.

    Write carries the whole new content; Edit carries one old/new pair (both host spellings
    accepted); MultiEdit carries an `edits` list applied in order.
    """
    content = tool_input.get("content")
    if isinstance(content, str):
        return content
    edits = tool_input.get("edits")
    if not isinstance(edits, list):
        edits = [tool_input]
    text = current_text
    saw_edit = False
    for edit in edits:
        if not isinstance(edit, dict):
            continue
        old = edit.get("old_string") or edit.get("oldString")
        new = edit.get("new_string") if "new_string" in edit else edit.get("newString")
        if not isinstance(old, str) or not old or not isinstance(new, str):
            continue
        saw_edit = True
        if edit.get("replace_all") or edit.get("replaceAll"):
            text = text.replace(old, new)
        else:
            text = text.replace(old, new, 1)
    return text if saw_edit else None


def _gates_or_empty(text: str) -> dict[str, str]:
    """The gate statuses in a state.yaml text; {} for any unreadable case.

    {} is the fail-closed posture for the *current* text (every proposed `approved` then counts
    as a flip) and the harmless one for the *proposed* text (nothing to open).
    """
    try:
        raw = strict_yaml.load_mapping(text, what="state.yaml")
    except strict_yaml.StrictParseError:
        return {}
    state = models.State(raw)
    return {gate: state.gate_status(gate) for gate in state.gates}


def gate_flip_denial(tool_input: dict[str, Any], repo: repo_mod.Repo | None = None) -> str:
    """Deny reason when this edit would flip a gate to approved; "" to allow.

    Reached only for state.yaml, which rule 1 already denies outright — this stays as the
    specific, actionable message for the most likely reason an agent is editing that file.
    """
    repo = repo or _repo_or_cwd()
    try:
        current_text = repo.state.read_text(encoding="utf-8")
    except OSError:
        current_text = ""
    proposed_text = _proposed_text(current_text, tool_input)
    if proposed_text is None:
        logger.warning("gate_guard: state.yaml write with an unrecognized payload shape — rule 1 denies it anyway")
        return ""
    current = _gates_or_empty(current_text)
    flips = [g for g, v in _gates_or_empty(proposed_text).items() if v == "approved" and current.get(g) != "approved"]
    if not flips:
        return ""
    return (
        f"Blocked: this edit would set gates.{', gates.'.join(flips)} to approved. A gate opens only on a"
        " receipt `rein approve` wrote after a human typed the gate name at a terminal."
        " No hand-written gate line has ever opened a gate, and this one will not either."
    )


# --- commit-stage check --------------------------------------------------------


def _git(repo: repo_mod.Repo, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC, cwd=repo.root)
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout


def _head_gates(repo: repo_mod.Repo) -> dict[str, str] | None:
    """The gate statuses in HEAD's state.yaml; None when HEAD has no copy."""
    rc, out = _git(repo, "show", "HEAD:.rein/state.yaml")
    return _gates_or_empty(out) if rc == 0 else None


def _last_index(events: list[Any], kind: str, gate: str) -> int:
    """Index of the last `kind` event naming `gate`, or -1. Position in the chain is the ordering."""
    for index in range(len(events) - 1, -1, -1):
        if events[index].event == kind and gate in events[index].subject_ids:
            return index
    return -1


def _flip_failures(repo: repo_mod.Repo) -> list[str]:
    """Gate flips against HEAD that no *current* `gate_approved` event and receipt backs.

    `rein approve` writes the state change, the receipt, and the event in one Central
    Store transaction, so a legitimate approval always passes. A flip smuggled past the editor
    hook fails here, before it can be committed.

    Three things are checked, because any one of them alone is forgeable by an agent that can
    write `state.yaml` directly (a shell redirect, `sed -i`):

    * a `gate_approved` event for this gate that is **newer than the last `gate_revised`** —
      the audit chain keeps rolled-back approvals, so "this gate was approved at some point in
      history" is satisfied forever once a gate has ever opened and then been reset;
    * a receipt naming an approval id;
    * that id appearing in the very event above, so the receipt and the audit record cannot
      disagree about what was approved.
    """
    from rein import event_chain  # lazy: keep the edit-time hook path light

    try:
        worktree_text = repo.state.read_text(encoding="utf-8")
    except OSError:
        return []
    worktree = _gates_or_empty(worktree_text)
    head = _head_gates(repo)
    if head is None:
        return []
    flips = [g for g, v in worktree.items() if v == "approved" and head.get(g) != "approved"]
    if not flips:
        return []

    events, defects = event_chain.scan(repo.events)
    if defects:
        return [f"gates.{', gates.'.join(flips)}: flipped to approved, and the audit chain is damaged"]
    state = models.State(strict_yaml.load_mapping(worktree_text, what="state.yaml"))

    failures = []
    for gate in flips:
        approved_at = _last_index(events, "gate_approved", gate)
        if approved_at < 0:
            failures.append(
                f"gates.{gate}: flipped to approved with no gate_approved event — an approval is"
                f" recorded by `rein approve {gate}`, never by editing state.yaml"
            )
            continue
        if approved_at < _last_index(events, "gate_revised", gate):
            failures.append(
                f"gates.{gate}: the only gate_approved event for it predates the gate_revised that rolled"
                " it back — a rolled-back approval does not re-open the gate it used to hold"
            )
            continue
        receipt = state.gate_receipt(gate)
        if receipt is None:
            failures.append(f"gates.{gate}: approved with no receipt — the digests it should bind are missing")
            continue
        approval_id = str(receipt.get("approval_id", ""))
        if approval_id not in events[approved_at].subject_ids:
            failures.append(
                f"gates.{gate}: the receipt names approval {approval_id!r}, which the gate_approved"
                " event does not — the receipt and the audit record disagree about what was approved"
            )
    return failures


def _changed_paths(repo: repo_mod.Repo) -> list[str] | None:
    """Every path changed vs HEAD (worktree + index + untracked), repo-relative. None = git unusable.

    `git status --porcelain` covers all three in one call and, unlike `git diff HEAD`, works in
    a repository with no commit yet. `-uall` lists files inside untracked directories (the
    default collapses them to `dir/`, hiding a brand-new `docs/tasks/T-001.md`).
    """
    rc, out = _git(repo, "status", "--porcelain", "-uall")
    if rc != 0:
        return None
    paths = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:  # rename/copy: "R  old -> new" — the new path is what lands
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))  # git quotes paths with special characters
    return paths


def check_diff(repo: repo_mod.Repo | None = None) -> int:
    """Commit-stage check. Fails (1) on a rule-3 violation, an unaccounted gate flip, or a
    frozen artifact whose content no longer matches its receipt.

    Rules 1 and 2 are deliberately not applied by path here — see :func:`evaluate`'s `stage`.
    """
    repo = repo or _repo_or_cwd()
    common.configure_logging()
    paths = _changed_paths(repo)
    if paths is None:
        logger.warning("gate_guard --check-diff: git status unavailable; skipping.")
        return 0
    denied = [
        (p, reason) for p in paths for ok, reason in [evaluate(str(repo.path(p)), repo, stage="commit")] if not ok
    ]
    flips = _flip_failures(repo) if ".rein/state.yaml" in paths else []
    flips += _frozen_artifact_failures(repo)
    if not denied and not flips:
        return 0
    if denied:
        logger.error("gate_guard: changes to paths this phase may not write:")
        for path, reason in denied:
            logger.error(f"  {path}: {reason}")
    for failure in flips:
        logger.error(f"  {failure}")
    return 1


#: The apply_patch envelope's file headers. Codex names what it is about to write *inside* the
#: patch text rather than in a field of its own, so this grammar is load-bearing: it is the only
#: place the guard can learn which paths a Codex edit touches.
#: (openai/codex `codex-rs/core/src/tools/handlers/apply_patch.lark`.)
_PATCH_TARGET_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$|^\*\*\* Move to: (.+)$", re.M)

#: An apply_patch document always opens with this. Requiring it keeps a shell command that merely
#: *mentions* a header line from being read as a patch.
_PATCH_PREAMBLE = "*** Begin Patch"


def patch_targets(command: str) -> list[str]:
    """Every path an apply_patch document writes, in order, deduplicated.

    A rename is two paths — the source is deleted and the destination is created — so both are
    returned; denying only one of them would let a guarded file be moved out of its own rule.
    """
    if _PATCH_PREAMBLE not in command:
        return []
    out: list[str] = []
    for edited, moved in _PATCH_TARGET_RE.findall(command):
        target = (edited or moved).strip()
        if target and target not in out:
            out.append(target)
    return out


#: Every spelling a host uses for "the file this call is about to write". Several hosts, one
#: question. Claude Code sends `file_path` for Write/Edit and `notebook_path` for NotebookEdit;
#: VS Code Copilot camelCases both. A notebook is source like any other file — a `.ipynb` under a
#: guarded prefix was reaching the guard with no path it could read, so the edit-stage check passed
#: it and only the commit-stage one (extension-blind, walking `git status`) ever saw it.
PATH_KEYS: tuple[str, ...] = ("file_path", "filePath", "notebook_path", "notebookPath")

#: The Claude Code tools that write a file, and therefore all have to reach the guard. This tuple is
#: the claim; `doctor.check_hook` holds the installed PreToolUse matcher against it, and `PATH_KEYS`
#: is what makes the coverage real once a call actually arrives. A tool absent from both is a hole
#: nothing reports: the matcher never fires and the commit-stage check becomes the only layer left.
#:
#: `MultiEdit` is retired upstream and stays. This is a foreign host's tool namespace, not a format
#: of ours to keep tidy — a dead alternative in a regex costs nothing and keeps an older host covered.
CLAUDE_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit")


def hook_paths(tool_input: Mapping[str, Any]) -> list[str]:
    """The paths this tool call is about to write, as the host named them.

    :data:`PATH_KEYS` covers the direct spellings. Codex's `apply_patch` sends **no path field at
    all** — the raw patch text arrives as `command` and the paths live inside it
    (`pre_tool_use_payload` in openai/codex's apply_patch handler). Reading only the path fields
    would make a hook registered with Codex fire on every edit and allow every one of them, which
    is worse than having no hook: `doctor` would report the guard as registered while it guarded
    nothing.
    """
    for key in PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return [value]
    command = tool_input.get("command")
    return patch_targets(command) if isinstance(command, str) else []


#: The guard has exactly two invocations, and a human asking about them is a third thing entirely.
USAGE = """usage: rein guard [--check-diff]

  (no arguments)  PreToolUse hook mode: reads the host's JSON payload on stdin and answers
                  whether the paths it is about to write may be written right now.
  --check-diff    commit-stage mode: checks every path in the diff against HEAD. This is what
                  .pre-commit-config.yaml registers, and what `make check` runs.
"""


def main(argv: list[str] | None = None) -> int:
    common.configure_logging()
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["--check-diff"]:
        return check_diff()
    if argv:
        # Argument handling at all, which there was none of: anything that was not `--check-diff`
        # fell through to the stdin read below, so `rein guard --help` answered a human's question
        # with "unparseable hook payload — allowing without a gate check" and exited 0, the guard's
        # *allow* code. Two invocations exist (above) and nothing else does; an unrecognized one is
        # a misregistered hook, which denies rather than passes — a guard given arguments it cannot
        # read does not know what it is being asked.
        asked = argv[0] in ("-h", "--help")
        print(USAGE, end="", file=sys.stdout if asked else sys.stderr)
        return 0 if asked else 2
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Fail-open by design: some hosts fire hooks for every tool and a malformed payload must
        # not block path-less tools — but leave a trace, so a guard that stopped guarding is
        # visible in the hook log rather than silently absent. The commit-stage check still runs.
        logger.warning("gate_guard: unparseable hook payload on stdin — allowing without a gate check")
        return 0
    tool_input = payload.get("tool_input") or {}
    paths = hook_paths(tool_input)
    if not paths:
        return 0
    # The payload carries the session's cwd, so a hook fired from a subdirectory or a leaf
    # worktree still resolves the right root — and a patch's paths, which are relative to that
    # cwd rather than to the repository root, resolve against it too.
    payload_cwd = payload.get("cwd")
    start = Path(payload_cwd) if isinstance(payload_cwd, str) and payload_cwd else None
    repo = _repo_or_cwd(start)
    base = start or Path.cwd()

    for raw in paths:
        file_path = raw if Path(raw).is_absolute() else str(base / raw)
        allowed, reason = evaluate(file_path, repo)
        if allowed and repo.rel(file_path) == ".rein/state.yaml":
            denial = gate_flip_denial(tool_input, repo)
            if denial:
                allowed, reason = False, denial
        if allowed:
            continue
        # One patch may touch many files; a single guarded path denies the whole call, because
        # applying "the rest of it" is not something a hook can do. Which path it was has to be
        # said out loud then — rule 3's message names the gate, not the file.
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"{raw}: {reason}" if len(paths) > 1 else reason,
            }
        }
        print(json.dumps(decision, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
