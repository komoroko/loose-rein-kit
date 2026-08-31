"""Materialized artifacts and per-agent integration surfaces — sync / install / uninstall / upgrade.

The harness ships everything non-code inside the wheel (see data.py); a repository holds only
*materializations* of it, each tracked by a content hash in `.rein/rein.lock`:

  sync       — (re)write the shared artifacts every agent reads from the repo: the prompt
               bodies (`.rein/prompts/`), the JSON schemas (`.rein/schema/`), and
               the rules body (`.rein/AGENTS.rein.md`). They must live in the repo
               because Claude Code's `@`-imports and Copilot's prompt files can only reference
               repo-relative paths. Pristine files (on-disk hash == lock hash) are refreshed;
               locally modified ones are skipped and listed (--force overrides); --check
               reports drift without writing (CI's canary).
  install    — opt-in per-agent surfaces: `rein install claude` writes the .claude/
               wrappers and merges settings.json; `install copilot` writes the .github/
               prompt/agent/hook/instruction files. Nothing agent-specific lands without
               being asked for. Re-running refreshes pristine files (an upgrade path).
  uninstall  — retract one integration (pristine files only; the settings merge is reverted
               entry-by-entry), or `--all` to remove every installed artifact and the lock.
  upgrade    — the version-transition report (CHANGELOG sections between the lock's recorded
               version and the running tool) + sync + refresh of the installed integrations.
               This refreshes what the code materialized, never the code: upgrading the *code*
               depends on how it was installed, which is what `upstream.upgrade_command` derives
               (a tag-pinned uv tool does not move under `uv tool upgrade`) and what `rein doctor`
               prints when a newer release exists.

The settings.json merge/unmerge and the CLAUDE.md/AGENTS.md marker blocks reuse the pure
settings-merge and marker-block functions (the copy-distribution model that first used them
is retired; the functions themselves are unit-tested and unchanged).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

import rein
from rein import common, gitignore, strict_yaml, upstream
from rein import data as data_mod
from rein import lock as lock_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

CLAUDE_IMPORT_MARKER = "<!-- rein-rules -->"
AGENTS_MARKER_END = "<!-- /rein-rules -->"
REIN_RULES_PATH = ".rein/AGENTS.rein.md"
SETTINGS_PATH = ".claude/settings.json"

# What sync materializes: data payload prefix (or file) → repo-relative destination.
MATERIALIZED: tuple[tuple[str, str], ...] = (
    ("prompts/", ".rein/prompts/"),
    ("schema/", ".rein/schema/"),
    # The Containerfiles are materialized so the sandbox a review ran in is reviewable in the
    # repository, not only inside the wheel that built it.
    ("oci/", ".rein/oci/"),
    ("rules/AGENTS.md", ".rein/AGENTS.rein.md"),
)

# Per-integration file surfaces: data prefix → repo-relative destination prefix.
INTEGRATIONS: dict[str, tuple[tuple[str, str], ...]] = {
    "claude": (
        ("integrations/claude/commands/", ".claude/commands/"),
        ("integrations/claude/agents/", ".claude/agents/"),
    ),
    "copilot": (
        ("integrations/copilot/prompts/", ".github/prompts/"),
        ("integrations/copilot/agents/", ".github/agents/"),
        ("integrations/copilot/hooks/", ".github/hooks/"),
        ("integrations/copilot/instructions/", ".github/instructions/"),
    ),
    # Codex's phase entry points are **skills**, not prompt files: custom prompts are deprecated
    # upstream and live only in the user's home, so they cannot be shipped with a repository at
    # all. Skills are repo-scoped, which is why they can be an integration surface.
    "codex": (
        ("integrations/codex/skills/", ".agents/skills/"),
        ("integrations/codex/agents/", ".codex/agents/"),
        ("integrations/codex/hooks.json", ".codex/hooks.json"),
        ("integrations/codex/rein.md", ".codex/rein.md"),
    ),
}


@dataclass(frozen=True)
class PlanItem:
    op: str  # install | update | unchanged | skip-modified | remove | leave-modified
    rel: str
    note: str = ""


# --- pure settings/marker helpers (unit-tested) --------------------------------


def claude_import_block() -> str:
    """The block appended to CLAUDE.md: the Claude Code capability mapping plus the rules
    import. The @import must stay the block's last line — remove_claude_import() strips
    marker→first-@ inclusive, so everything in between is retracted with it on uninstall."""
    return (
        f"\n{CLAUDE_IMPORT_MARKER}\n"
        "## Loose Rein\n"
        "This repo uses Loose Rein (Human-on-the-Loop, gated delta cycles). Claude Code realizes\n"
        "the rules' capability vocabulary as: `phase-invocation` → the /req … /status slash\n"
        "commands; `structured-question` → AskUserQuestion; `notify-and-wait` → PushNotification;\n"
        "`approval-presentation` → plan mode + ExitPlanMode; `session-compaction` → /compact\n"
        "(human-run); `role-delegation` → the subagents in .claude/agents/;\n"
        "`command-preauthorization` → permissions.allow in\n"
        ".claude/settings.json; `background-wait` → Bash with run_in_background (the run's exit\n"
        "re-invokes you). The implementation phase is `rein build` — one command whose completion\n"
        "is the signal, so wait for it and never poll it. The operating rules are imported from:\n"
        f"@{REIN_RULES_PATH}\n"
    )


def agents_pointer_block() -> str:
    """The block appended to AGENTS.md. AGENTS.md has no import mechanism, so the block is a
    plain pointer, closed by an end marker remove_agents_pointer() strips against."""
    return (
        f"\n{CLAUDE_IMPORT_MARKER}\n"
        "## Loose Rein\n"
        "This repo uses Loose Rein (Human-on-the-Loop, gated delta cycles). Read and follow the\n"
        f"operating rules in `{REIN_RULES_PATH}`. VS Code Copilot sessions also load the\n"
        "capability mapping in `.github/instructions/rein.instructions.md`; Codex sessions,\n"
        "the one in `.codex/rein.md`.\n"
        f"{AGENTS_MARKER_END}\n"
    )


def remove_claude_import(text: str) -> str:
    """Strip the marker..@import block appended to CLAUDE.md (idempotent; pure)."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == CLAUDE_IMPORT_MARKER), None)
    if start is None:
        return text
    end = next((i for i in range(start, len(lines)) if lines[i].startswith("@")), start)
    del lines[start : end + 1]
    if 0 < start <= len(lines) and lines[start - 1].strip() == "":
        del lines[start - 1]  # the one blank line the appended block contributed
    return "\n".join(lines)


def remove_agents_pointer(text: str) -> str:
    """Strip the marker..end-marker block appended to AGENTS.md (idempotent; pure)."""
    lines = text.split("\n")
    start = next((i for i, line in enumerate(lines) if line.strip() == CLAUDE_IMPORT_MARKER), None)
    if start is None:
        return text
    end = next((i for i in range(start, len(lines)) if lines[i].strip() == AGENTS_MARKER_END), start)
    del lines[start : end + 1]
    if 0 < start <= len(lines) and lines[start - 1].strip() == "":
        del lines[start - 1]
    return "\n".join(lines)


def _canon(obj: Any) -> str:
    """Canonical JSON for structural equality of settings fragments (dict order must not matter)."""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False)


def merge_settings(
    existing: dict[str, Any], template: dict[str, Any]
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Append the template's missing permissions.allow entries and hook groups.

    Additive only — nothing of the existing settings is removed or reordered. Hook groups are
    identified by their command strings, so a re-run appends nothing (idempotent).
    Returns (merged, notes, added) where `added` records exactly what this call appended
    (the lock keeps it so upgrade/uninstall can find *our* entries again later).
    """
    notes: list[str] = []
    added: dict[str, Any] = {"permissions_allow": [], "hooks": {}}
    allow = existing.setdefault("permissions", {}).setdefault("allow", [])
    for entry in template.get("permissions", {}).get("allow") or []:
        if entry not in allow:
            allow.append(entry)
            added["permissions_allow"].append(entry)
            notes.append(f"permissions.allow += {entry}")
    hooks = existing.setdefault("hooks", {})
    for event, groups in (template.get("hooks") or {}).items():
        have = {h.get("command") for g in hooks.get(event) or [] for h in g.get("hooks") or []}
        for group in groups:
            cmds = {h.get("command") for h in group.get("hooks") or []}
            if not cmds <= have:
                hooks.setdefault(event, []).append(group)
                added["hooks"].setdefault(event, []).append(group)
                notes.append(f"hooks.{event} += {len(cmds)} command(s)")
    return existing, notes, added


def upgrade_settings(
    existing: dict[str, Any], installed: dict[str, Any], template: dict[str, Any]
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Replace the previously installed settings entries with the new template's versions.

    Only **pristine** installed hook groups (canonically equal to the lock record) are
    retracted; a locally modified group is left alone and noted. The additive merge then
    re-adds whatever the new template wants, so a pristine group whose command changed
    upstream is replaced without duplication. Returns (merged, notes, added) like
    merge_settings — `added` is the fresh record of what is ours now.
    """
    notes: list[str] = []
    template_allow = set(template.get("permissions", {}).get("allow") or [])
    allow = existing.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        for entry in installed.get("permissions_allow") or []:
            if entry in allow:
                allow.remove(entry)  # the merge below re-adds it if the template still wants it
                if entry not in template_allow:
                    notes.append(f"permissions.allow -= {entry} (dropped by the template)")
    hooks = existing.get("hooks") or {}
    for event, groups in (installed.get("hooks") or {}).items():
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        for old_group in groups:
            idx = next((i for i, g in enumerate(lst) if _canon(g) == _canon(old_group)), None)
            if idx is None:
                notes.append(f"hooks.{event}: an installed group was locally modified or removed — left as-is")
            else:
                del lst[idx]
    retracted_allow = set(installed.get("permissions_allow") or [])
    retracted_groups = {_canon(g) for gs in (installed.get("hooks") or {}).values() for g in gs}
    merged, _merge_notes, added = merge_settings(existing, template)
    # Note only the net changes — re-adding what step 1 just retracted is not news.
    for entry in added["permissions_allow"]:
        if entry not in retracted_allow:
            notes.append(f"permissions.allow += {entry}")
    for event, groups in added["hooks"].items():
        for group in groups:
            if _canon(group) not in retracted_groups:
                notes.append(f"hooks.{event} += 1 group")
    for event in [e for e, v in (merged.get("hooks") or {}).items() if v == []]:
        del merged["hooks"][event]
    return merged, notes, added


def unmerge_settings(existing: dict[str, Any], installed: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Retract exactly the settings entries the lock says install appended (pristine groups only).

    A hook group the user has since modified no longer canonically matches the record — it is
    theirs now and stays, with a note. Event lists that end up empty are pruned.
    """
    notes: list[str] = []
    allow = existing.get("permissions", {}).get("allow")
    if isinstance(allow, list):
        for entry in installed.get("permissions_allow") or []:
            if entry in allow:
                allow.remove(entry)
                notes.append(f"permissions.allow -= {entry}")
    hooks = existing.get("hooks") or {}
    for event, groups in (installed.get("hooks") or {}).items():
        lst = hooks.get(event)
        if not isinstance(lst, list):
            continue
        for old_group in groups:
            idx = next((i for i, g in enumerate(lst) if _canon(g) == _canon(old_group)), None)
            if idx is None:
                notes.append(f"hooks.{event}: an installed group was locally modified — left as-is")
            else:
                del lst[idx]
                notes.append(f"hooks.{event} -= 1 group")
    for event in [e for e, v in (existing.get("hooks") or {}).items() if v == []]:
        del existing["hooks"][event]
    # Drop the containers the merge's setdefault may have introduced, once they empty out.
    perms = existing.get("permissions")
    if isinstance(perms, dict) and perms.get("allow") == []:
        del perms["allow"]
    for key in ("permissions", "hooks"):
        if existing.get(key) == {}:
            del existing[key]
    return existing, notes


_CHANGELOG_HEADING_RE = re.compile(r"^## \[?v?([0-9][^\]\s]*)\]?", re.MULTILINE)


def read_version(root: Path) -> str:
    """The [project] version of a checkout's pyproject.toml ("" if there is none).

    pyproject is the single version source — the release workflow and scripts/template_lint.py
    both read it and nothing else, so a second place to look could only ever disagree. The only
    caller is that canary (`check_version_changelog`), which is why this reads a *path* rather than
    answering from `rein.__version__`: it is asking what a checkout on disk says, not what the
    running interpreter was installed as.
    """
    try:
        text = (root / "pyproject.toml").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    return m.group(1) if m else ""


def changelog_between(text: str, installed: str, new: str) -> str:
    """The CHANGELOG sections newer than `installed`, up to and including `new`.

    Unknown/absent installed versions -> the newest section with a note; equal or unknown
    versions -> "". Purely presentational: upgrades never fail on a malformed changelog.
    """
    if installed == new:
        return ""
    headings = [(m.group(1), m.start()) for m in _CHANGELOG_HEADING_RE.finditer(text)]
    if not headings:
        return ""
    if installed and any(v == installed for v, _ in headings):
        stop = next(start for v, start in headings if v == installed)
        return text[headings[0][1] : stop].rstrip() + "\n"
    note = "(installed version unknown — showing the latest entry only)\n"
    stop = headings[1][1] if len(headings) > 1 else len(text)
    return note + text[headings[0][1] : stop].rstrip() + "\n"


# --- the file-map plumbing shared by sync and install ---------------------------


@cache
def _dest_sources(pairs: tuple[tuple[str, str], ...]) -> tuple[tuple[str, str], ...]:
    """(repo-relative destination, data-relative source) for a (data prefix, repo prefix) pair set.

    The single enumeration of where a pair set lands. Everything that needs the bytes reads them
    from the source path; everything that only needs the destinations — `present_surfaces`, which
    the dashboard reaches on every status read — never opens a payload file at all.

    Cached for the life of the process, because the payload lives inside the installed package and
    no `rein` verb writes it: `install`/`sync` read it and write the *repository*, and they are
    one-shot runs anyway. Only the long-lived process, `rein ui`, holds this across time, and it
    was walking the whole tree three times per status read to answer "is any agent surface here".
    """
    out: list[tuple[str, str]] = []
    for data_prefix, repo_prefix in pairs:
        entry = data_mod.path(data_prefix.rstrip("/"))
        if entry.is_file():
            out.append((repo_prefix, data_prefix))
            continue
        strip = len(data_prefix)
        out.extend((repo_prefix + rel[strip:], rel) for rel in data_mod.iter_names(data_prefix.rstrip("/")))
    return tuple(out)


def _dest_map(pairs: tuple[tuple[str, str], ...]) -> dict[str, bytes]:
    """repo-relative destination -> payload bytes, for a (data prefix, repo prefix) pair set."""
    return {dest: data_mod.read_bytes(src) for dest, src in _dest_sources(pairs)}


def _plan(repo: repo_mod.Repo, desired: dict[str, bytes], recorded: dict[str, str], force: bool) -> list[PlanItem]:
    """The per-file decision table: what a sync/install run would do to each destination.

    pristine (on-disk == lock record, or file absent) -> write; locally modified -> skip
    unless forced. A file already equal to the payload is "unchanged" (its hash is
    (re)recorded — crash-recovery convergence).

    A destination the lock records and the payload no longer ships is **removed**. Iterating
    `desired` alone left it on disk forever, and the lock rewrite that follows drops its entry
    (`sync`/`install` keep only keys still in `desired`) — so after one upgrade nothing in the
    repository knew the file had ever been installed, and `uninstall`, which works from the lock's
    record, could not retract it either. Removal has to be planned here so it happens *before*
    that rewrite; a release has only to drop a file for the alternative to strand it.
    """
    items: list[PlanItem] = []
    for rel in sorted(set(recorded) - set(desired)):
        try:
            on_disk = lock_mod.norm_hash(repo.path(rel).read_bytes())
        except OSError:
            continue  # already gone: nothing to delete, and `_record_after` stops recording it
        if on_disk == recorded[rel] or force:
            items.append(PlanItem("remove", rel, "no longer shipped by this release"))
        else:
            items.append(PlanItem("leave-modified", rel, "no longer shipped, but locally modified — kept"))
    for rel in sorted(desired):
        new_hash = lock_mod.norm_hash(desired[rel])
        path = repo.path(rel)
        try:
            current = lock_mod.norm_hash(path.read_bytes())
        except OSError:
            current = None
        if current is None:
            items.append(PlanItem("install", rel))
        elif current == new_hash:
            items.append(PlanItem("unchanged", rel))
        elif current == recorded.get(rel):
            items.append(PlanItem("update", rel))
        elif force:
            items.append(PlanItem("update", rel, "forced — local modification overwritten"))
        else:
            items.append(PlanItem("skip-modified", rel, "locally modified — kept (use --force to overwrite)"))
    return items


def present_surfaces(repo: repo_mod.Repo, name: str) -> list[str]:
    """The integration's destination files that exist on disk, whatever the lock records.

    Defined here so "where install writes" and "where anything else looks for what install wrote"
    cannot answer differently — both enumerate `_dest_sources`, the same reason check_materialized
    reuses _dest_map.
    """
    return sorted(dest for dest, _ in _dest_sources(INTEGRATIONS[name]) if repo.path(dest).is_file())


def _apply_plan(repo: repo_mod.Repo, items: list[PlanItem], desired: dict[str, bytes]) -> dict[str, str]:
    """Write the plan's install/update rows, delete its `remove` rows; return the lock hash per file."""
    hashes: dict[str, str] = {}
    removed: list[str] = []
    for item in items:
        if item.op == "remove":
            repo.path(item.rel).unlink(missing_ok=True)
            removed.append(item.rel)
            continue
        if item.op in ("install", "update"):
            dest = repo.path(item.rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(desired[item.rel])
        if item.op in ("install", "update", "unchanged"):
            hashes[item.rel] = lock_mod.norm_hash(desired[item.rel])
    _prune_empty_dirs(repo, removed)
    return hashes


def _record_after(recorded: dict[str, str], items: list[PlanItem], hashes: dict[str, str]) -> dict[str, str]:
    """The lock's per-file record once a plan has run: every row the plan still owns, then the
    fresh hashes on top.

    A row survives unless the plan deleted it, so three cases separate cleanly. A file this
    release no longer ships and that was pristine is gone from disk and from the record. One that
    was *locally modified* stays in both — its record is the only thing left tying it to the tool
    that installed it, so dropping it would put the file beyond `uninstall`'s reach as well as
    `sync`'s, which is the hole this exists to close. And an entry whose file is already absent
    yields no plan row at all, so it simply stops being recorded: there is nothing left to retract.

    A `skip-modified` row keeps its old hash rather than taking the payload's — the file on disk is
    still the one the record describes.
    """
    owned = {item.rel for item in items if item.op != "remove"}
    out = {rel: digest for rel, digest in recorded.items() if rel in owned}
    out.update(hashes)
    return out


def _print_plan(
    items: list[PlanItem],
    *,
    verbose_ops: tuple[str, ...] = ("install", "update", "skip-modified", "remove", "leave-modified"),
) -> None:
    for item in items:
        if item.op in verbose_ops:
            note = f"  ({item.note})" if item.note else ""
            print(f"  {item.op:<13} {item.rel}{note}")


def _lock_or_new(repo: repo_mod.Repo) -> dict[str, Any]:
    data = lock_mod.read(repo.lock)
    return data if data is not None else lock_mod.new(rein.__version__, "")


def _versions_differ(recorded: str, running: str) -> bool:
    """True when two version spellings denote different releases (canonical compare when possible)."""
    if recorded == running:
        return False
    try:
        return Version(recorded) != Version(running)
    except InvalidVersion:
        return True


def stale_integrations(data: dict[str, Any], running_version: str) -> dict[str, str]:
    """integration name → recorded version, for surfaces written by a different tool release.

    `sync` refreshes only the shared artifacts; the per-agent surfaces stay at whatever
    release last ran `install`/`upgrade`. Left unnoticed, a repo reads new prompts through
    old wrappers — so sync/doctor surface this skew and point at the refresh command.
    """
    integrations = data.get("integrations")
    if not isinstance(integrations, dict):
        return {}
    out: dict[str, str] = {}
    for name, record in integrations.items():
        recorded = str(record.get("version", "")) if isinstance(record, dict) else ""
        if _versions_differ(recorded, running_version):
            out[str(name)] = recorded
    return out


def _print_stale_integrations(stale: dict[str, str]) -> None:
    for name, recorded in sorted(stale.items()):
        print(
            f"  stale         integration '{name}' surfaces were written by rein "
            f"{recorded or '(unrecorded)'} — re-run `rein install {name}` (or `rein upgrade`)"
        )


# --- sync -----------------------------------------------------------------------


def _materialized_key(rel: str) -> str:
    """Lock key for a materialized file: its path under .rein/ (matches the plan's layout)."""
    return rel.removeprefix(".rein/")


def materialized_record(repo: repo_mod.Repo) -> dict[str, str]:
    """The lock's `prompts.files` map, keyed the way `_dest_map` keys it (repo-relative).

    Public because `doctor` compares against it and the two must key it identically — the same
    reason `present_surfaces` and `_dest_map` live here rather than being re-derived by each caller.
    An unreadable lock reads as "records nothing", which is what `check_lock` reports on separately.
    """
    try:
        data = lock_mod.read(repo.lock) or {}
    except lock_mod.LockError:
        return {}
    prompts = data.get("prompts")
    files = prompts.get("files") if isinstance(prompts, dict) else None
    return {".rein/" + str(k): str(v) for k, v in (files or {}).items()}


def refresh_ungenerated_review(repo: repo_mod.Repo) -> bool:
    """Re-materialize `review.yaml` while it holds no review. True when it was rewritten.

    The scaffold is written once by `init` and never again, so a release that changes the review
    document's shape strands every repository that has not reached gate ④ yet — and strands it on
    a file that is *machine-written and empty*: `status: not_generated`, no machine half, no human
    answers, nothing anybody recorded. A repo was left unable to run `rein revise` by a stub whose
    entire content was "nothing has happened here".

    Refused the moment it says otherwise. A generated review is evidence bound to a commit, and
    `rein review generate --force` is how that gets rewritten — by re-reading the code, not by
    this.
    """
    path = repo.path(".rein/review.yaml")
    packaged = data_mod.read_bytes("scaffold/rein/review.yaml")
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    if lock_mod.norm_hash(current.encode("utf-8")) == lock_mod.norm_hash(packaged):
        return False
    # Read loosely, not through `models.Review`: the case this exists for is a document the current
    # schema *refuses*, so validating it first would decline to repair exactly what needs repairing.
    # A file that will not parse as YAML at all is left alone — nothing here can tell whether it
    # holds a review, and `doctor` names its repair.
    try:
        document = strict_yaml.load_mapping(current, what="review.yaml")
    except strict_yaml.StrictParseError:
        return False
    machine = document.get("machine")
    status = str(machine.get("status", "")) if isinstance(machine, dict) else ""
    if status not in ("", "not_generated"):
        return False
    path.write_bytes(packaged)
    return True


def _documents_invalid(repo: repo_mod.Repo) -> list[str]:
    """Which SSOT documents this release's schema refuses, worded for the console ([] = none)."""
    from rein import models
    from rein import store as store_mod

    store = store_mod.Store(repo)
    problems: list[str] = []
    for name, reader in (
        ("config.yaml", store.read_config),
        ("state.yaml", store.read_state),
        ("plan.yaml", store.read_plan),
        ("review.yaml", store.read_review),
    ):
        try:
            reader()
        except (models.DocumentError, strict_yaml.StrictParseError, OSError) as exc:
            problems.append(f"{name}: {exc}")
    return problems


def sync(repo: repo_mod.Repo, *, check: bool = False, force: bool = False) -> int:
    """Materialize prompts/schema/rules from the package payload (see the module docstring)."""
    desired = _dest_map(MATERIALIZED)
    data = _lock_or_new(repo)
    prompts = data.get("prompts") if isinstance(data.get("prompts"), dict) else {}
    recorded_raw = prompts.get("files") if isinstance(prompts, dict) else {}
    recorded = {".rein/" + k: str(v) for k, v in (recorded_raw or {}).items()}
    items = _plan(repo, desired, recorded, force)

    drift = [i for i in items if i.op != "unchanged"]
    stale = stale_integrations(data, rein.__version__)
    if check:
        gitignore_stale = _refresh_gitignore(repo, write=False)
        if not drift and not stale and not gitignore_stale:
            print(f"sync --check: {len(items)} materialized file(s) match the packaged payload.")
            return 0
        _print_plan(drift)
        _print_stale_integrations(stale)
        if drift:
            print(f"sync --check: {len(drift)} file(s) differ from the packaged payload (rein {rein.__version__}).")
        if stale:
            print(f"sync --check: {len(stale)} integration surface(s) predate rein {rein.__version__}.")
        if gitignore_stale:
            print("sync --check: the Loose Rein runtime-artifact block in .gitignore is out of date.")
        return 1

    _print_plan(items)
    hashes = _apply_plan(repo, items, desired)
    if refresh_ungenerated_review(repo):
        print("  update        .rein/review.yaml (the scaffold stub — it held no review)")
    _refresh_gitignore(repo, write=True)
    files = {_materialized_key(rel): digest for rel, digest in _record_after(recorded, items, hashes).items()}
    data["prompts"] = {"version": rein.__version__, "files": files}

    # The lock's `tool_version` is what `upgrade` derives its "from" version from, so advancing it
    # over a repository this release's schema refuses erases the only record of where the repo came
    # from — and that is exactly what happened: `sync` materialized the new schema, stamped the new
    # version, and `rein upgrade` then reported "already current", printed no changelog, and named
    # none of the renames that had just broken the repo. Refused instead, with the transition
    # printed: the version this repo is on is worth more than a tidy lock.
    invalid = _documents_invalid(repo)
    if invalid:
        for problem in invalid:
            logger.error(f"sync: {problem}")
        sections = changelog_between(
            data_mod.read_text("CHANGELOG.md"), lock_mod.tool_version_of(data), rein.__version__
        )
        if sections:
            print("\n" + sections)
        logger.error(
            f"sync: the materialized artifacts are up to date, but {len(invalid)} repo document(s) are "
            f"not readable by rein {rein.__version__}. The lock still records "
            f"{lock_mod.tool_version_of(data) or '(unrecorded)'} so the transition above stays available. "
            "Repair them (`rein doctor` names the command per document), then run `rein sync` again."
        )
        return 1

    data["tool_version"] = rein.__version__
    lock_mod.write(repo.lock, data)
    skipped = sum(1 for i in items if i.op == "skip-modified")
    written = sum(1 for i in items if i.op in ("install", "update"))
    print(f"sync: {written} written, {skipped} kept (locally modified), {len(items) - written - skipped} unchanged.")
    _print_stale_integrations(stale)
    return 0


# --- install / uninstall ----------------------------------------------------------


def _template_mode(repo: repo_mod.Repo) -> bool:
    """guard.template_mode from config.yaml (False when unreadable — products default off)."""
    from rein import models

    try:
        return models.Config.parse(repo.config.read_text(encoding="utf-8")).template_mode
    except (OSError, models.DocumentError, strict_yaml.StrictParseError):
        return False


def _refresh_gitignore(repo: repo_mod.Repo, *, write: bool) -> bool:
    """Keep the runtime-artifact block in the repo's `.gitignore` current (creating the file if
    absent). Returns whether it was — or, with ``write=False``, would be — changed.

    Skipped in the template repository: it curates its own section by hand (same header, extra
    teaching comments), which `scripts/template_lint.py` checks against the derived set instead.
    """
    from rein import models

    if _template_mode(repo):
        return False
    path = repo.path(".gitignore")
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    try:
        config_text = repo.config.read_text(encoding="utf-8")
        merged, changed = gitignore.merge(existing, config_text)
    except (OSError, models.DocumentError, strict_yaml.StrictParseError):
        # An unreadable config is reported by sync's own `_documents_invalid` check; the
        # .gitignore block just waits for the next run.
        return False
    if changed and write:
        path.write_text(merged, encoding="utf-8")
        print("  merge         .gitignore (Loose Rein runtime-artifact block)")
    return changed


def _settings_template() -> dict[str, Any]:
    raw = data_mod.read_text("integrations/claude/settings.json")
    loaded = json.loads(raw)
    return loaded if isinstance(loaded, dict) else {}


def _read_settings(repo: repo_mod.Repo) -> tuple[dict[str, Any], bool]:
    """(settings mapping, existed) — a missing/broken settings.json starts empty."""
    path = repo.path(SETTINGS_PATH)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, path.exists()
    return (loaded if isinstance(loaded, dict) else {}), True


def _write_settings(repo: repo_mod.Repo, settings: dict[str, Any]) -> None:
    path = repo.path(SETTINGS_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def install_integration(
    repo: repo_mod.Repo, name: str, *, force: bool = False, dry_run: bool = False, announce_next: bool = True
) -> int:
    """Write one agent's surfaces and record them in the lock (see the module docstring).

    ``announce_next`` is off when the caller has its own closing instruction to give — the init
    wizard does, and two "here is what to do next" blocks in one run contradict each other about
    which one is the last word.
    """
    if name not in INTEGRATIONS:
        logger.error(f"unknown integration '{name}' (one of: {', '.join(sorted(INTEGRATIONS))})")
        return 2
    desired = _dest_map(INTEGRATIONS[name])
    data = _lock_or_new(repo)
    integrations = data.setdefault("integrations", {})
    previous = integrations.get(name) if isinstance(integrations.get(name), dict) else {}
    recorded = {k: str(v) for k, v in (previous.get("files") or {}).items()}
    items = _plan(repo, desired, recorded, force)
    _print_plan(items)
    if dry_run:
        print(f"[dry-run] install {name}: nothing written.")
        return 0
    hashes = _apply_plan(repo, items, desired)
    files = _record_after(recorded, items, hashes)

    record: dict[str, Any] = {
        "version": rein.__version__,
        "installed_at": str(previous.get("installed_at") or date.today().isoformat()),
        "files": files,
    }

    if name == "claude":
        settings, existed = _read_settings(repo)
        installed_record = previous.get("settings") if isinstance(previous.get("settings"), dict) else None
        template = _settings_template()
        if not existed and "$schema" in template:
            # A settings.json we bring into existence carries the editor schema pointer too;
            # merge_settings only tracks permissions/hooks, so seed it before the merge.
            settings = {"$schema": template["$schema"], **settings}
        if installed_record:
            merged, notes, added = upgrade_settings(settings, installed_record, template)
        else:
            merged, notes, added = merge_settings(settings, template)
        _write_settings(repo, merged)
        # `created` survives re-installs: whether *we* brought settings.json into existence
        # decides if a full unmerge may delete the file again on uninstall.
        if installed_record and "created" in installed_record:
            created = bool(installed_record["created"])
        else:
            created = not existed
        record["settings"] = {"created": created, **added}
        for note in notes:
            print(f"  settings      {note}")
        # The rules import block (CLAUDE.md) — appended at most once (marker-guarded), and only
        # where it adds anything: a CLAUDE.md that already references the rules body carries its
        # own wiring, and in template_mode the repo IS the template — its CLAUDE.md imports the
        # rules via @AGENTS.md, so appending the block would double-load the same rules text.
        claude_md = repo.path("CLAUDE.md")
        text = claude_md.read_text(encoding="utf-8") if claude_md.is_file() else ""
        if CLAUDE_IMPORT_MARKER in text:
            pass  # already installed (marker present)
        elif REIN_RULES_PATH in text:
            print(f"  skip          CLAUDE.md (already references {REIN_RULES_PATH})")
        elif _template_mode(repo):
            print("  skip          CLAUDE.md (guard.template_mode: the repo's own CLAUDE.md imports the rules)")
        else:
            claude_md.write_text(text + claude_import_block(), encoding="utf-8")
            print("  merge         CLAUDE.md (capability mapping + rules @import appended)")
        if shutil.which("rein") is None:
            logger.warning(
                "  ! `rein` is not on PATH — the hooks in .claude/settings.json need it:"
                " run `uv tool install git+<the rein repo>` (or add it to PATH)."
            )

    integrations[name] = record
    lock_mod.write(repo.lock, data)
    print(f"installed integration: {name} (recorded in {lock_mod.LOCK_NAME})")
    if announce_next:
        print("  note          open a new session (or restart the editor) to pick up the new commands —")
        print("                an already-running session won't see files added mid-session.")
        # Codex invokes its skills with `$`, not `/` — telling someone to type a command their host
        # does not have is how a working install gets reported as broken.
        entry = "$req" if name == "codex" else "/req"
        print(f"  next          in that new session the phase commands ({entry} …) work; `rein next`")
        print("                shows where you are and what to run.")
    return 0


def _prune_empty_dirs(repo: repo_mod.Repo, rels: list[str]) -> None:
    for rel in rels:
        parent = repo.path(rel).parent
        while parent != repo.root:
            try:
                parent.rmdir()  # fails (correctly) unless empty
            except OSError:
                break
            parent = parent.parent


def uninstall_integration(repo: repo_mod.Repo, name: str, *, force: bool = False, dry_run: bool = False) -> int:
    """Retract one agent's surfaces: pristine files only; the settings merge is reverted."""
    if name not in INTEGRATIONS:
        logger.error(f"unknown integration '{name}' (one of: {', '.join(sorted(INTEGRATIONS))})")
        return 2
    data = lock_mod.read(repo.lock)
    record = (data or {}).get("integrations", {}).get(name) if data else None
    if not isinstance(record, dict):
        print(f"integration '{name}' is not recorded in {lock_mod.LOCK_NAME} — nothing to uninstall.")
        return 0
    recorded = {k: str(v) for k, v in (record.get("files") or {}).items()}
    items: list[PlanItem] = []
    for rel in sorted(recorded):
        path = repo.path(rel)
        try:
            current = lock_mod.norm_hash(path.read_bytes())
        except OSError:
            items.append(PlanItem("unchanged", rel, "already gone"))
            continue
        if current == recorded[rel] or force:
            items.append(PlanItem("remove", rel))
        else:
            items.append(PlanItem("leave-modified", rel, "locally modified — left for manual removal"))
    _print_plan(items, verbose_ops=("remove", "leave-modified"))
    if dry_run:
        print(f"[dry-run] uninstall {name}: nothing removed.")
        return 0
    removed: list[str] = []
    for item in items:
        if item.op == "remove":
            repo.path(item.rel).unlink(missing_ok=True)
            removed.append(item.rel)
    _prune_empty_dirs(repo, removed)

    if name == "claude":
        installed_record = record.get("settings") if isinstance(record.get("settings"), dict) else None
        if installed_record:
            settings, existed = _read_settings(repo)
            if existed:
                unmerged, notes = unmerge_settings(settings, installed_record)
                # A file install itself created is deleted once nothing but the schema pointer
                # is left; a pre-existing file always survives (it is the repo's).
                remaining = {k: v for k, v in unmerged.items() if k != "$schema"}
                if installed_record.get("created") and not remaining:
                    repo.path(SETTINGS_PATH).unlink(missing_ok=True)
                    print(f"  remove        {SETTINGS_PATH} (created by install; empty after unmerge)")
                else:
                    _write_settings(repo, unmerged)
                    for note in notes:
                        print(f"  settings      {note}")
        claude_md = repo.path("CLAUDE.md")
        if claude_md.is_file():
            stripped = remove_claude_import(claude_md.read_text(encoding="utf-8"))
            if stripped.strip():
                claude_md.write_text(stripped, encoding="utf-8")
            else:
                claude_md.unlink()
                print("  remove        CLAUDE.md (held only the Loose Rein block)")
        _prune_empty_dirs(repo, [SETTINGS_PATH])  # .claude/ itself, once the settings file went too

    assert data is not None
    data.get("integrations", {}).pop(name, None)
    lock_mod.write(repo.lock, data)
    print(f"uninstalled integration: {name}")
    return 0


def uninstall_all(repo: repo_mod.Repo, *, force: bool = False, dry_run: bool = False) -> int:
    """Remove every installed artifact: integrations, then the materialized files, then the lock.

    The repo's own state (state.md, config.yaml, tasks.yaml, docs/) is deliberately left in
    place — it is the product's, not the tool's.
    """
    data = lock_mod.read(repo.lock)
    if data is None:
        print(f"no {lock_mod.LOCK_NAME} — nothing recorded to uninstall.")
        return 0
    for name in sorted(data.get("integrations") or {}):
        rc = uninstall_integration(repo, name, force=force, dry_run=dry_run)
        if rc != 0:
            return rc
    # The materialized artifacts (pristine-only, like the integrations).
    data = lock_mod.read(repo.lock) or data
    prompts = data.get("prompts") if isinstance(data.get("prompts"), dict) else {}
    recorded = {".rein/" + k: str(v) for k, v in ((prompts or {}).get("files") or {}).items()}
    items: list[PlanItem] = []
    for rel in sorted(recorded):
        path = repo.path(rel)
        try:
            current = lock_mod.norm_hash(path.read_bytes())
        except OSError:
            continue
        if current == recorded[rel] or force:
            items.append(PlanItem("remove", rel))
        else:
            items.append(PlanItem("leave-modified", rel, "locally modified — left for manual removal"))
    _print_plan(items, verbose_ops=("remove", "leave-modified"))
    if dry_run:
        print("[dry-run] uninstall --all: nothing removed.")
        return 0
    removed = []
    for item in items:
        if item.op == "remove":
            repo.path(item.rel).unlink(missing_ok=True)
            removed.append(item.rel)
    _prune_empty_dirs(repo, removed)
    # Retract the AGENTS.md pointer block (the CLAUDE.md block went with the claude integration).
    agents_md = repo.path("AGENTS.md")
    if agents_md.is_file():
        stripped = remove_agents_pointer(agents_md.read_text(encoding="utf-8"))
        if stripped.strip():
            agents_md.write_text(stripped, encoding="utf-8")
        else:
            agents_md.unlink()
            print("  remove        AGENTS.md (held only the Loose Rein block)")
    # Retract the runtime-artifact block from .gitignore (leaving the rest of the file alone).
    gi = repo.path(".gitignore")
    if gi.is_file():
        stripped = gitignore.remove(gi.read_text(encoding="utf-8"))
        if stripped.strip():
            gi.write_text(stripped, encoding="utf-8")
        else:
            gi.unlink()
            print("  remove        .gitignore (held only the Loose Rein block)")
    repo.lock.unlink(missing_ok=True)
    print("uninstalled: the lock is removed; repo state (.rein SSOT, docs/) is untouched.")
    return 0


# --- upgrade ----------------------------------------------------------------------


def upgrade(repo: repo_mod.Repo, *, dry_run: bool = False, force: bool = False) -> int:
    """Refresh the materialized artifacts and installed integrations to the running tool version."""
    data = lock_mod.read(repo.lock)
    if data is None:
        logger.error(f"no {lock_mod.LOCK_NAME} — run `rein init` (new repo) or `rein sync` first.")
        return 1
    installed = lock_mod.tool_version_of(data)
    source = lock_mod.source_of(data)
    running = rein.__version__
    print(f"rein: {installed or '(unrecorded)'} → {running}")
    if installed == running:
        print("already current — refreshing pristine artifacts anyway.")
    else:
        sections = changelog_between(data_mod.read_text("CHANGELOG.md"), installed, running)
        if sections:
            print("\n" + sections)
    if dry_run:
        desired = _dest_map(MATERIALIZED)
        prompts = data.get("prompts") if isinstance(data.get("prompts"), dict) else {}
        recorded = {".rein/" + k: str(v) for k, v in ((prompts or {}).get("files") or {}).items()}
        drift = [i for i in _plan(repo, desired, recorded, force) if i.op != "unchanged"]
        _print_plan(drift)
        print(
            f"[dry-run] upgrade: {len(drift)} file(s) would change; integrations: "
            f"{', '.join(sorted(data.get('integrations') or {})) or '(none)'}"
        )
        return 0
    rc = sync(repo, force=force)
    if rc != 0:
        return rc
    for name in sorted((lock_mod.read(repo.lock) or {}).get("integrations") or {}):
        rc = install_integration(repo, name, force=force)
        if rc != 0:
            return rc
    # Which command upgrades the tool itself depends on how it was installed, so it is derived
    # rather than quoted — `uv tool upgrade` never moves a tag-pinned install.
    print(f"upgrade complete. (Upgrading the tool itself is `{upstream.upgrade_command(upstream.origin(source))}`.)")
    return 0


# --- CLI entry points ----------------------------------------------------------------


def _repo_from(args: argparse.Namespace) -> repo_mod.Repo:
    return repo_mod.get(getattr(args, "repo", None))


def cmd_sync(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein sync", description="materialize prompts/schema/rules from the package")
    parser.add_argument("--check", action="store_true", help="report drift without writing (exit 1 on drift)")
    parser.add_argument("--force", action="store_true", help="overwrite locally modified files too")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        return sync(_repo_from(args), check=args.check, force=args.force)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1


def cmd_install(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein install", description="install per-agent integration surfaces")
    parser.add_argument(
        "names",
        nargs="+",
        choices=sorted(INTEGRATIONS),
        metavar="integration",
        help=f"one or more of: {', '.join(sorted(INTEGRATIONS))}",
    )
    parser.add_argument("--force", action="store_true", help="overwrite locally modified files too")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = _repo_from(args)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    for name in args.names:
        rc = install_integration(repo, name, force=args.force, dry_run=args.dry_run)
        if rc != 0:
            return rc
    return 0


def cmd_uninstall(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein uninstall", description="retract integration surfaces (pristine files only)"
    )
    parser.add_argument(
        "names",
        nargs="*",
        choices=sorted(INTEGRATIONS),
        metavar="integration",
        help=f"one or more of: {', '.join(sorted(INTEGRATIONS))}",
    )
    parser.add_argument("--all", action="store_true", dest="all_", help="remove every installed artifact and the lock")
    parser.add_argument("--force", action="store_true", help="remove locally modified files too")
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    if not args.names and not args.all_:
        parser.error("name an integration (claude | copilot) or pass --all")
    try:
        repo = _repo_from(args)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    if args.all_:
        return uninstall_all(repo, force=args.force, dry_run=args.dry_run)
    for name in args.names:
        rc = uninstall_integration(repo, name, force=args.force, dry_run=args.dry_run)
        if rc != 0:
            return rc
    return 0


def cmd_upgrade(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein upgrade", description="refresh materialized artifacts + installed integrations"
    )
    parser.add_argument("--force", action="store_true", help="overwrite locally modified files too")
    parser.add_argument("--dry-run", action="store_true", help="print the transition report only")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        return upgrade(_repo_from(args), dry_run=args.dry_run, force=args.force)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
