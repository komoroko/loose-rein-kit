"""User-global project registry — the named set of Loose Rein repos, and which one the UI targets.

Every other verb resolves *one* repo per invocation (repo.py: ``--repo`` > ``REIN_ROOT`` >
cwd walk-up). This module adds a small, user-scoped index on top so the dashboard can enumerate the
repos you work across and switch the active target from a dropdown, instead of restarting the server
against a different ``--repo``. It is deliberately the only *user-global* state the tool keeps; each
repo's SSOT still lives in its own ``.rein/``.

Storage precedence for the file (``projects.yaml``):

  1. ``$REIN_CONFIG_HOME`` (a test/override hook)
  2. ``$XDG_CONFIG_HOME/rein``
  3. ``~/.config/rein``

    active: api
    projects:
      web: /home/koich/work/web-app
      api: /home/koich/work/api

Trust boundary: only the user's own ``rein project ...`` CLI (and the UI, running on their
machine) writes here. The dashboard never accepts a filesystem path from the browser — the client
selects a *registered name* and the server resolves it through this registry — so nothing a browser
supplies can widen what the dashboard operates on.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

from rein import common
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

# Same shape as ui._SLUG_RE / cycle's slug rule: a lowercase, dash-joined token.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class RegistryError(ValueError):
    """A rejected registry mutation (bad name, unknown project, non-Loose Rein path)."""


def config_home() -> Path:
    """The directory holding ``projects.yaml``, per the module-docstring precedence."""
    override = os.environ.get("REIN_CONFIG_HOME")
    if override:
        return Path(override).expanduser()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "rein"


def registry_path() -> Path:
    return config_home() / "projects.yaml"


def slug_for(root: Path) -> str:
    """A registry-name candidate derived from a repo directory name (sanitized to the slug rule)."""
    base = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    return base or "project"


@dataclass
class Registry:
    """The in-memory registry: registered ``name -> root`` plus the active name (or None)."""

    projects: dict[str, Path] = field(default_factory=dict)
    active: str | None = None

    def add(self, name: str, path: Path) -> Path:
        """Register ``name`` -> resolved ``path``; the path must be a Loose Rein repo. Returns the root."""
        if not _SLUG_RE.match(name):
            raise RegistryError(f"project name '{name}' must match [a-z0-9][a-z0-9-]*")
        resolved = path.expanduser().resolve()
        if not repo_mod._has_marker(resolved):
            raise RegistryError(f"{resolved}: no .rein/ directory there — not a Loose Rein repository")
        self.projects[name] = resolved
        return resolved

    def remove(self, name: str) -> None:
        if name not in self.projects:
            raise RegistryError(f"no project named '{name}'")
        del self.projects[name]
        if self.active == name:
            self.active = None

    def set_active(self, name: str) -> None:
        if name not in self.projects:
            have = ", ".join(sorted(self.projects)) or "none registered"
            raise RegistryError(f"no project named '{name}' (have: {have})")
        self.active = name

    def entries(self) -> list[dict[str, object]]:
        """A JSON-serializable view for the UI; ``exists`` re-checks the ``.rein/`` marker."""
        return [
            {
                "name": name,
                "root": str(path),
                "active": name == self.active,
                "exists": repo_mod._has_marker(path),
            }
            for name, path in self.projects.items()
        ]


def record_use(reg: Registry, root: Path) -> str:
    """Ensure ``root`` is registered; return the name it lives under. Existing entries win.

    Called on ``rein ui`` startup so the repo you launched from always appears in the switcher
    even if you never ran ``rein project add`` — a most-recently-used convenience.
    """
    resolved = root.expanduser().resolve()
    for name, path in reg.projects.items():
        if path == resolved:
            return name
    name = slug_for(resolved)
    if name in reg.projects:  # a different repo already holds the derived name — disambiguate
        i = 2
        while f"{name}-{i}" in reg.projects:
            i += 1
        name = f"{name}-{i}"
    reg.projects[name] = resolved
    return name


def load(path: Path | None = None) -> Registry:
    """Read the registry; a missing file is an empty registry (not an error)."""
    p = path or registry_path()
    if not p.exists():
        return Registry()
    import yaml  # lazy: keep `import registry` stdlib+repo only (matches common.py's convention)

    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    raw = data.get("projects") or {}
    projects = {str(k): Path(str(v)) for k, v in raw.items()}
    active = data.get("active")
    active = str(active) if active in projects else None
    return Registry(projects=projects, active=active)


def save(reg: Registry, path: Path | None = None) -> None:
    p = path or registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    import yaml

    body = {
        "active": reg.active,
        "projects": {name: str(root) for name, root in reg.projects.items()},
    }
    p.write_text(yaml.safe_dump(body, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _render_list(reg: Registry) -> str:
    if not reg.projects:
        return "no projects registered — `rein project add <name> <path>`"
    lines = []
    for e in reg.entries():
        mark = "*" if e["active"] else " "
        missing = "" if e["exists"] else "  (missing .rein/)"
        lines.append(f"{mark} {str(e['name']):<16} {e['root']}{missing}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    common.configure_logging()
    args_list = list(sys.argv[1:] if argv is None else argv)
    # cli.py appends a global --repo/--root for every verb; it is meaningless for the user-global
    # registry (which is not scoped to any one repo), so drop the pair before parsing.
    for flag in ("--repo", "--root"):
        if flag in args_list:
            i = args_list.index(flag)
            del args_list[i : i + 2]

    parser = argparse.ArgumentParser(prog="rein project", description="manage the user-global project registry")
    sub = parser.add_subparsers(dest="cmd")
    p_add = sub.add_parser("add", help="register a repo under a name")
    p_add.add_argument("name")
    p_add.add_argument("path", nargs="?", default=".", help="repository path (default: current directory)")
    p_add.add_argument("--use", action="store_true", help="also make it the active project")
    sub.add_parser("list", help="list registered projects (the active one is marked *)")
    p_rm = sub.add_parser("remove", help="unregister a name")
    p_rm.add_argument("name")
    p_use = sub.add_parser("use", help="set the active project")
    p_use.add_argument("name")
    args = parser.parse_args(args_list)

    if args.cmd in (None, "list"):
        print(_render_list(load()))
        return 0

    reg = load()
    try:
        if args.cmd == "add":
            root = reg.add(args.name, Path(args.path))
            if args.use or reg.active is None:
                reg.active = args.name
            save(reg)
            active = "  (active)" if reg.active == args.name else ""
            print(f"registered '{args.name}' -> {root}{active}")
        elif args.cmd == "remove":
            reg.remove(args.name)
            save(reg)
            print(f"removed '{args.name}'")
        elif args.cmd == "use":
            reg.set_active(args.name)
            save(reg)
            print(f"active project: {args.name} -> {reg.projects[args.name]}")
    except RegistryError as exc:
        logger.error(f"rein project: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
