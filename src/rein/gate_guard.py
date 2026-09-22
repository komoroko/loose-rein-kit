"""The mechanism layer: deny in code what the convention layer merely asks agents not to do.

Registered as a PreToolUse hook by `rein install <agent>`, this fires on every
editor write and answers one question — may this path be written right now? That is one of three
checkpoints, and the only one a host's capabilities decide:

* **edit-time** — this hook. `rein install` registers it on the four hosts that have one.
* **commit-stage** — `--check-diff`, over every path in the diff. Registered by a repository's own
  `.pre-commit-config.yaml`; `rein` neither installs one nor ships one, so this checkpoint is a
  fact about the repository and `rein doctor` reports whether it holds.
* **merge-stage** — `build_loop._gate_violations`, in code inside `rein build`, over every path a
  task changed before it lands. No host, hook or config can be missing it.

So an agent whose environment cannot intercept edits, or a write that bypassed the hook (a shell
redirect, `sed -i`), is still checked — by the third one, which is also the reason the first two
being absent degrades *when a violation is caught*, not *whether the boundary holds*. What has no
checkpoint rein installs is a change that never goes through `rein build` at all.

Four rules, in order of severity:

1. **Machine-written artifacts are never hand-edited.** `state.yaml`, `review.yaml`,
   `events.ndjson` are written only inside a Central Store transaction.
   A hand edit produces a state change with no matching audit event — the exact invisible
   mutation the chain exists to make impossible.
2. **A frozen plan is frozen.** Once the mandate closes, `plan.yaml`, `config.yaml`, the sandbox
   definitions, and the materialized prompts/schema are pinned by the receipt the human confirmed.
   Changing them goes through `rein revise --to mandate`, which resets the downstream
   gates in a chain (plan §16.4).
3. **Changing the product needs an approved mandate that covers the path.** Not "which gate does
   this path wait for" — that question was the five phases wearing a guard's clothes, and it
   guarded the mandate's own material (the design document, the task tickets) against the gate
   before it. What is guarded is the product: `guard.paths` says which paths that is, once and for
   every cycle, and `plan.scope.include` narrows it to what *this* mandate may change.
   `plan.scope.exclude` is the one half that binds wherever it points, guarded or not — it is a
   human writing "not this". `tests/` is deliberately unguarded, because preparing fixtures while
   the mandate is pending is sanctioned speculative work.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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

#: Pinned by the mandate receipt. Rule 2 — denied once `plan.status` is frozen.
FROZEN_AFTER_GATE_THREE: tuple[str, ...] = (
    ".rein/plan.yaml",
    CONFIG_PATH,
    ".rein/prompts/",
    # A Containerfile a profile's `dockerfile:` names — an input the evidence was produced in.
    # Not the packaged ones: those are no longer copied here, because nothing read the copy.
    ".rein/oci/",
)

#: Rule 3's built-in guarded set, used when config carries no `guard.paths`. Each guards the path
#: it names and everything beneath it; the trailing slash is punctuation, not meaning.
#:
#: Code, and only code. The deliverable documents used to be in here too, each waiting on the phase
#: gate before it — `docs/20-design.md` on requirements, `docs/tasks/` on design — which is the
#: shape of a guard enforcing an *order*. They are the material a mandate is written from, and are
#: written before there is a mandate to gate them on; what the guard is for is the thing a mandate
#: authorizes, which is changing the product.
#:
#: `docs/test/` is not here for the same reason: `/verify` writes it while the mandate is open, and
#: there is no gate between the mandate and acceptance for it to wait on.
DEFAULT_GUARD_PATHS: tuple[str, ...] = (
    "src/",
    "lib/",
    "app/",
    "backend/",
    "frontend/",
    "scripts/",
)


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
    paths: tuple[str, ...] = DEFAULT_GUARD_PATHS
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
    if not isinstance(entries, list) or not all(isinstance(entry, str) and entry for entry in entries):
        # A guarded set the guard cannot read in full is a guarded set it does not have. Dropping
        # the entries it could not parse would put this function back in the business it was
        # written to get out of: a mistyped entry making a rule vanish, silently.
        return GuardSettings(unreadable="config.yaml's `guard.paths` is not a list of repository paths")
    # An empty list is not "guard nothing": the defaults stand, exactly as they do for a config
    # that names no paths at all. Disarming rule 3 is what `template_mode` is for, and it says so.
    return GuardSettings(template_mode=template_mode, paths=tuple(entries) or DEFAULT_GUARD_PATHS)


def is_guarded(file_path: str, paths: Sequence[str], repo: repo_mod.Repo | None = None) -> bool:
    """Whether rule 3 governs this path — whether writing it needs an approved mandate.

    `paths` is required rather than defaulted from the config: the caller has to have decided what
    to do about a config it could not read before it gets to ask this question.
    """
    repo = repo or _repo_or_cwd()
    rel = repo.rel(file_path)
    if rel is None:
        return False
    return common.longest_cover(rel, {p: p for p in paths}) is not None


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
    the mandate freeze writes `plan.yaml`, and those writes have to be committable or the very
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
                f"Blocked: the plan froze at the mandate and {rel} is bound by the receipt the human signed."
                " Changing it now would leave the approval covering bytes nobody read. Roll back first:"
                " `rein revise --to mandate` (this resets the downstream gates in a chain)."
            )
        return True, ""

    # Rule 3 — the product waits for a mandate that covers it.
    return _rule_three(repo, file_path)


def _rule_three(repo: repo_mod.Repo, file_path: str) -> tuple[bool, str]:
    """Rule 3 alone: changing the product needs an approved mandate that covers the path."""
    settings = guard_settings(repo)
    rel = repo.rel(file_path)
    if settings.unreadable and rel != CONFIG_PATH:
        # The one exemption, and it is not a hole: a guard that denies every path because it could
        # not read config.yaml would be denying the repair it just asked for — including the
        # human's own `git commit` of the fix, since rule 3 runs at commit stage over every changed
        # path. Nothing else about this file loosens: rule 2 still refuses it once the plan is
        # frozen, and the commit-stage frozen-artifact check still compares it against the digest
        # the mandate bound.
        return False, (
            "Blocked: the guard reads `guard.paths` and `guard.template_mode` from"
            f" .rein/config.yaml, and it could not: {settings.unreadable}. It does not know which"
            " paths this repository guards, so it fails closed on all of them rather than"
            " enforcing a rule map nobody wrote. Repair .rein/config.yaml (restore it from git);"
            " `rein doctor` reports what is wrong with it. There is deliberately no flag that"
            " turns this guard off."
        )
    if settings.template_mode:
        return True, ""
    state = _read_state(repo)
    if state is not None and state.gate_status("mandate") == "approved":
        return _inside_the_mandate(repo, rel, settings.paths)
    if not is_guarded(file_path, settings.paths, repo):
        return True, ""
    if state is None:
        return False, (
            "Blocked: cannot read the gates from .rein/state.yaml (missing or malformed), so the"
            " gate guard fails closed. Repair state.yaml — restore it from git. There is deliberately"
            " no flag that turns this guard off."
        )
    return False, (
        "Blocked: no mandate is approved, and this path is one a mandate authorizes changes to."
        " Write what the change is for and what would make it true (/req, /design, /tasks — in"
        " whatever order suits it), then get the human's approval with `rein approve mandate`."
    )


def _inside_the_mandate(repo: repo_mod.Repo, rel: str | None, guarded: Sequence[str]) -> tuple[bool, str]:
    """(allowed, why not) for a write under an approved mandate, measured against `plan.scope`.

    Read off `plan.yaml`, which the mandate approval froze — so the scope a write is measured
    against is the one a human read. A plan that cannot be read denies, for the same reason an
    unreadable state does: a guard that cannot determine its scope must not open it.

    **`exclude` is absolute; `include` narrows the guarded set.** They are not symmetric and the
    asymmetry is the repair. `include` says which of the product's paths this cycle may change, so
    it has nothing to say about a path that was never guarded — a repository declares what its
    product is in `guard.paths`, once, rather than per cycle. `exclude` is a human writing "not
    this", and it used to be consulted only after `guard.paths` had already let the path through:
    an `exclude` entry naming anything outside the guarded set — a vendored tree, a generated
    directory, the one file this cycle must not touch — silently guarded nothing at all.

    An empty `include` is unbounded, so a cycle that has not narrowed itself is not one that has
    forbidden everything (`models.Plan.scope`).
    """
    if rel is None:
        return True, ""
    try:
        text = repo.plan.read_text(encoding="utf-8")
        document = strict_yaml.load_mapping(text, what="plan.yaml")
    except (OSError, strict_yaml.StrictParseError) as exc:
        return False, (
            "Blocked: the mandate's scope lives in .rein/plan.yaml and it could not be read"
            f" ({exc}), so the gate guard fails closed. Restore it from git; `rein doctor` reports"
            " what is wrong with it."
        )
    include, exclude = models.Plan(document).scope
    why = outside_the_mandate(rel, include=include, exclude=exclude, guarded=guarded)
    if not why:
        return True, ""
    return False, (
        f"Blocked: {rel} is {why}. Widening what the loop may change is a human's decision —"
        " `rein revise --to mandate` re-opens it."
    )


def outside_the_mandate(rel: str, *, include: Sequence[str], exclude: Sequence[str], guarded: Sequence[str]) -> str:
    """Why rule 3 refuses this path under an approved mandate, or `""` when it does not.

    The rule with nothing read off disk, so the same sentence decides a write at the hook and a
    whole cycle's diff at acceptance (`approve._boundary_blockers`). **Two callers, one rule.** A
    second spelling of "inside the mandate" is a boundary that can disagree with itself, and a
    boundary two answers can be given about is a convention.

    The asymmetry is :func:`_inside_the_mandate`'s: `exclude` binds wherever it points, `include`
    only narrows the guarded set, and an empty `include` is unbounded rather than empty.
    """
    if common.longest_cover(rel, {p: p for p in exclude}) is not None:
        return "excluded by the approved mandate's scope"
    if not common.longest_cover(rel, {p: p for p in guarded}):
        return ""
    if include and common.longest_cover(rel, {p: p for p in include}) is None:
        return f"outside the approved mandate's scope ({', '.join(include)})"
    return ""


def _frozen_artifact_failures(repo: repo_mod.Repo) -> list[str]:
    """The commit-stage form of rule 2: the frozen artifacts must still hash to what the mandate froze.

    Stronger than the path rule the hook applies, because it compares content. An edit that was
    made, reverted, and re-applied leaves no trace in a path list but moves the digest.

    Both artifacts are checked. The mandate freezes `config.yaml` for the same reason it freezes
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
                f"{label} has changed since the mandate froze it (its digest no longer matches the receipt). "
                "Roll back with `rein revise --to mandate` instead of editing a frozen artifact."
            )
    return failures


# --- rule 4: gate-approval write protection -----------------------------------


def _proposed_text(current_text: str, tool_input: Mapping[str, Any]) -> str | None:
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


def gate_flip_denial(tool_input: Mapping[str, Any], repo: repo_mod.Repo | None = None) -> str:
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
#: it and nothing looked at it again until `rein build` landed it.
PATH_KEYS: tuple[str, ...] = ("file_path", "filePath", "notebook_path", "notebookPath")

#: The Claude Code tools that write a file, and therefore all have to reach the guard. This tuple is
#: the claim; `doctor.check_hook` holds the installed PreToolUse matcher against it, and `PATH_KEYS`
#: is what makes the coverage real once a call actually arrives. A tool absent from both is a hole
#: nothing reports: the matcher never fires, and what the tool writes is not looked at again until
#: `rein build` lands it — a whole task later, as an escalation rather than a denied write.
#:
#: `MultiEdit` is retired upstream and stays. This is a foreign host's tool namespace, not a format
#: of ours to keep tidy — a dead alternative in a regex costs nothing and keeps an older host covered.
CLAUDE_WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit")

#: The same claim for Gemini CLI's `BeforeTool`, whose write-capable tools are named differently and
#: whose matcher `rein install gemini` ships. Checked by `doctor` exactly as claude's is: registering
#: a hook and registering it over the tools that write are two questions, and a host that only ever
#: got the first one asked is a host where `doctor` says PASS over a guard that never fires.
GEMINI_WRITE_TOOLS: tuple[str, ...] = ("write_file", "replace")

#: Every spelling a host uses for "the arguments of the call this hook is about". Claude Code and
#: the hosts that copied its `PreToolUse` payload send `tool_input`; Gemini CLI's `BeforeTool` sends
#: `tool_args`.
#:
#: This tuple exists because the *answer* was taught both dialects and the *question* was not. A
#: denial is written as `hookSpecificOutput` and as top-level `decision`/`reason` at once, on the
#: stated ground that one guard serves every host — while the payload was still read as
#: `payload["tool_input"]` alone. Under a host that names it otherwise there is no path to check,
#: `hook_paths` answers `[]`, and the guard exits 0: it allows every edit, silently, while `doctor`
#: reports it registered. That is the failure this module's own docstring names for Codex
#: (`patch_targets`) arriving through the door the response fixed.
TOOL_ARGS_KEYS: tuple[str, ...] = ("tool_input", "tool_args", "toolInput", "toolArgs")


def tool_arguments(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The call's arguments, whichever of :data:`TOOL_ARGS_KEYS` this host named them under.

    **None, not `{}`, when no spelling matched.** A tool invoked with no arguments and a payload
    this guard cannot read are the same empty mapping and *not* the same fact: the first is a call
    with no path to check, the second is every call on that host going unchecked. Returning one
    value for both is how the Gemini hole stayed invisible — `hook_paths` answered `[]`, the guard
    exited 0, and `doctor` reported it registered. The caller says so out loud instead.
    """
    for key in TOOL_ARGS_KEYS:
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return None


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


#: Where a repository registers the commit-stage check, when it registers one. `rein install` does
#: not write this file and `src/rein/data/` does not ship one, so whether the checkpoint holds is a
#: fact about the repository — looked at, never assumed.
PRE_COMMIT_PATH = ".pre-commit-config.yaml"

#: What :func:`commit_stage_registration` can answer. `NEUTERED` is the one worth naming: `rein
#: guard` **alone** is the *hook* invocation — it reads a host's JSON payload on stdin, so under
#: pre-commit it is handed none, warns, and allows. A hook that runs on every commit and checks
#: nothing is not the commit-stage check, and reading the bare name as one is what let the claim
#: "the commit-stage check still applies" be printed over it.
COMMIT_STAGE_REGISTERED = "registered"
COMMIT_STAGE_NEUTERED = "neutered"
COMMIT_STAGE_ABSENT = "absent"
COMMIT_STAGE_UNREADABLE = "unreadable"


def commit_stage_registration(text: str) -> str:
    """Which of the four a `.pre-commit-config.yaml` text is, read **as a config and not as text**.

    A substring search cannot answer this. pre-commit splits an invocation across two keys —
    `entry: rein guard` with `args: [--check-diff]` is the idiom the tool's own documentation
    uses — so a regex over one line reports a working registration as a neutered one, and a
    commented-out block as a working one. The word list is rebuilt from both keys before anything
    is asked of it (:func:`_invocation`), including *whether this hook is the guard at all*: a
    candidate chosen by `"rein guard" in entry` is still a substring test, and it is blind to the
    same split it exists to handle. One reader, because `doctor.check_hook` tells the repository's
    owner what it has and `policy_check` refuses a head that takes it away, and those two
    disagreeing is the drift that makes the refusal worthless.
    """
    from rein import strict_yaml  # lazy: keep `import gate_guard` cheap on the hook path

    if not text.strip():
        return COMMIT_STAGE_ABSENT
    try:
        document = strict_yaml.load_mapping(text, what=PRE_COMMIT_PATH)
    except strict_yaml.StrictParseError:
        return COMMIT_STAGE_UNREADABLE
    verdict = COMMIT_STAGE_ABSENT
    repos = document.get("repos")
    for repo_entry in repos if isinstance(repos, list) else []:
        hooks = repo_entry.get("hooks") if isinstance(repo_entry, dict) else ()
        for hook in hooks if isinstance(hooks, list) else ():
            if not isinstance(hook, dict):
                continue
            words = _invocation(hook)
            if not _invokes_guard(words):
                continue
            verdict = COMMIT_STAGE_REGISTERED if "--check-diff" in words else COMMIT_STAGE_NEUTERED
            if verdict == COMMIT_STAGE_REGISTERED:
                return verdict
    return verdict


def _invocation(hook: Mapping[str, Any]) -> list[str]:
    """The whole command line a pre-commit hook runs, `entry` and `args` as one word list.

    pre-commit splits an invocation across the two keys and imposes no rule about where the split
    falls, so every question about *what this hook runs* is a question about the concatenation.
    Asking it of `entry` alone is the defect this replaced twice over: first a regex over one line,
    then a substring test that selected candidate hooks on `entry` before joining the words, which
    reads `entry: rein` with `args: [guard, --check-diff]` as no registration at all.
    """
    words = str(hook.get("entry", "")).split()
    args = hook.get("args")
    if isinstance(args, list):
        words += [str(arg) for arg in args]
    return words


def _invokes_guard(words: Sequence[str]) -> bool:
    """Does this command line run `rein guard`? Adjacent words, because that is what a command is.

    `"rein guard" in text` said yes to `rein guardian`, to a `--exclude` naming the phrase, and to
    the word appearing in two unrelated places.
    """
    return any(word == "rein" and words[i + 1 : i + 2] == ["guard"] for i, word in enumerate(words))


#: The guard has exactly two invocations, and a human asking about them is a third thing entirely.
USAGE = """usage: rein guard [--check-diff]

  (no arguments)  pre-tool hook mode (Claude Code `PreToolUse`, Gemini CLI `BeforeTool`, and the
                  hosts that copied either): reads the host's JSON payload on stdin and answers
                  whether the paths it is about to write may be written right now.
  --check-diff    commit-stage mode: checks every path in the diff against HEAD. This is what a
                  repository's own .pre-commit-config.yaml registers — `rein` neither installs one
                  nor ships one, so `rein doctor` reports whether this repository has it.
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
        # visible in the hook log rather than silently absent. What the write changes is still
        # re-checked where `rein build` lands it.
        logger.warning("gate_guard: unparseable hook payload on stdin — allowing without a gate check")
        return 0
    tool_input = tool_arguments(payload)
    if tool_input is None:
        # Fail-open, for the same reason the unparseable payload above is — but never silently.
        # This is the shape of the hole that was just closed for Gemini CLI, and the next host to
        # name its arguments something else lands here: a warning in the hook log is what makes
        # "the guard stopped guarding" visible, instead of a green `doctor` over a guard that
        # allows every edit.
        logger.warning(
            f"gate_guard: this host names the tool call's arguments none of {', '.join(TOOL_ARGS_KEYS)} "
            "— allowing without a gate check. What this writes is re-checked where `rein build` lands it."
        )
        return 0
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
        # Both dialects in one object, because one guard serves every host. Claude Code and the
        # hosts that copied it read `hookSpecificOutput.permissionDecision`; Gemini CLI's
        # `BeforeTool` reads top-level `decision`/`reason` and ignores the rest. Detecting which
        # host asked would be a branch on a fact the answer does not need — an extra key each
        # host ignores costs nothing, and a host whose key is missing does not deny at all.
        said = f"{raw}: {reason}" if len(paths) > 1 else reason
        decision = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": said,
            },
            "decision": "deny",
            "reason": said,
        }
        print(json.dumps(decision, ensure_ascii=False))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
