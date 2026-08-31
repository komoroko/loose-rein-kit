""".rein/rein.lock — the machine-written record of what the harness installed.

The lock answers three questions without a network call: **which rein release last wrote
this repo's artifacts** (and from which source), **which document format the repository is
on**, and **which files the tool owns** (the materialized prompts/schema under `.rein/`,
the per-agent integration surfaces, and the one-shot seeds) — each with a content hash, so
`sync`/`upgrade`/`uninstall` can tell a pristine file (safe to refresh/remove) from a locally
modified one (never touched silently).

The format is a single opaque `format:` string rather than a number, deliberately: a numeric
version invites "newer than I know, but probably close enough", which is how every
compatibility shim starts. :data:`FORMAT` must match **exactly** or the lock is refused — there
is no ordering, so there is nothing to be lenient about (plan §4.3).

Structure (YAML mapping, `sort_keys=False` so the file reads top-down):

  format: rein-grounded-v1  # exact match required
  tool_version: 0.1.0            # the release that last wrote the lock
  source: ''                     # where that release came from (git ref / path), when known
  prompts: {version, files:}     # the materialized artifacts (.rein/prompts|schema, rules)
  integrations: {claude: {...}}  # present only for installed agent surfaces (install.py)
  seeded: {path: hash}           # one-shot seeds the repo owns from then on (uninstall check only)
  created_at / updated_at

Writers: `init` (creates), `sync`/`upgrade` (prompts section), `install`/`uninstall`
(integrations). The lock is always rewritten *last* in an operation, so a crash leaves behind
at worst an under-recorded lock that the next run reconverges.
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

from rein import common
from rein import repo as repo_mod

#: The one document format this release reads. Not a number: there is no "close enough".
#:
#: **This string is about the four SSOT documents' shape, not about the release.** It went
#: unchanged across 0.3.6–0.3.8 while `config.yaml` renamed two keys and `review.yaml` changed the
#: type of one — so a repository crossing those releases had a format string saying it was fine
#: and a schema refusing to read it. Change this whenever a repo-owned document's shape changes;
#: what a repository does about the refusal is `install.sync`'s guard, which prints the renames
#: for the versions crossed and declines to advance the lock past a document it cannot read.
FORMAT = "rein-grounded-v1"
LOCK_NAME = ".rein/rein.lock"

_HEADER = (
    "# .rein/rein.lock — machine-written by `rein init|sync|install|uninstall|upgrade`.\n"
    "# Records the document format, the tool version/source, and a hash per installed file so\n"
    "# upgrades never overwrite local edits. Do not edit by hand.\n"
)


class LockError(common.ReinError, RuntimeError):
    """An unusable lock: unparseable, not a mapping, or written in a different document format."""


def norm_hash(blob: bytes) -> str:
    """sha256 of the CRLF-normalized bytes — a checkout's line-ending conversion is not an edit."""
    return "sha256:" + hashlib.sha256(blob.replace(b"\r\n", b"\n")).hexdigest()


def new(version: str, source: str) -> dict[str, Any]:
    """A fresh lock skeleton for `init` to fill."""
    today = date.today().isoformat()
    return {
        "format": FORMAT,
        "tool_version": version,
        "source": source,
        "prompts": {"version": version, "files": {}},
        "integrations": {},
        "seeded": {},
        "created_at": today,
        "updated_at": today,
    }


def read(path: Path) -> dict[str, Any] | None:
    """The lock mapping, or None when the file does not exist. LockError when unusable.

    A lock in any other format — a missing `format` key included — is refused outright.
    "Proceed and guess" is how a repository gets silently corrupted by a tool that does not
    understand it.
    """
    from rein import strict_yaml  # lazy: keep `import lock` cheap on the hook path

    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise LockError(f"cannot read {path}: {exc}") from None
    try:
        data = strict_yaml.load_mapping(text, what=str(path))
    except strict_yaml.StrictParseError as exc:
        raise LockError(f"{exc} — machine-written; restore it from git") from None

    found = data.get("format")
    if found != FORMAT:
        raise LockError(
            f"{path} is in format {found!r}, but this rein reads {FORMAT!r} only — "
            f"upgrade the tool ({_upgrade_hint()}) or re-initialize the repository"
        )
    return data


#: Every key this format has. `write` drops anything else, so a key an older spelling wrote is
#: gone on the next `sync` rather than carried forever: this repository's own lock still held a
#: `rein: {version: 0.1.0}` from a retired layout while `tool_version` said 0.3.12, which is a
#: machine-written file disagreeing with itself in front of anyone who opens it. There is no
#: migration to write — the lock is derived from the installed package and reconverges.
KEYS: tuple[str, ...] = (
    "format",
    "tool_version",
    "source",
    "prompts",
    "integrations",
    "seeded",
    "created_at",
    "updated_at",
)


def write(path: Path, data: dict[str, Any]) -> None:
    """Write the lock (stamping updated_at, dropping keys the format has no place for)."""
    import yaml  # lazy (see read)

    data = {k: v for k, v in data.items() if k in KEYS}
    data["format"] = FORMAT  # never take the caller's word for the format it just wrote
    data["updated_at"] = date.today().isoformat()
    data.setdefault("created_at", data["updated_at"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_HEADER + yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def source_of(data: dict[str, Any]) -> str:
    """The install source recorded in a lock mapping ("" when unrecorded)."""
    value = data.get("source")
    return str(value) if isinstance(value, str) else ""


def _upgrade_hint(recorded_source: str = "") -> str:
    """The command that actually upgrades *this* install, in backticks.

    Lazy on purpose: `rein guard` imports this module on every edit, and the answer needs the
    install's PEP 610 metadata — a cost worth paying only on the rare path that prints it.
    """
    from rein import upstream

    return f"`{upstream.upgrade_command(upstream.origin(recorded_source))}`"


def tool_version_of(data: dict[str, Any]) -> str:
    """The rein version recorded in a lock mapping ("" when unrecorded)."""
    value = data.get("tool_version")
    return str(value) if isinstance(value, str) else ""


def startup_warning(repo: repo_mod.Repo, running_version: str) -> str | None:
    """The cheap per-invocation check: one warning line, or None when all is well.

    A missing lock is silent (mid-init states are legitimate); an unusable or foreign-format
    lock raises LockError (the caller turns that into a hard error); a version skew between
    the running tool and the lock's writer gets one actionable stderr line.
    """
    data = read(repo.lock)
    if data is None:
        return None
    recorded = tool_version_of(data)
    if not recorded or recorded == running_version:
        return None
    try:
        recorded_v, running_v = Version(recorded), Version(running_version)
    except InvalidVersion:
        # A version string nothing can parse is a damaged lock. Saying nothing here and leaving
        # it for `doctor` means the one command that runs on every invocation stays quiet about
        # the file it just read — the shape of leniency this format was narrowed to avoid.
        return (
            f"{LOCK_NAME} records tool_version {recorded!r}, which is not a version — the lock is "
            f"damaged (running {running_version}). Run `rein doctor`."
        )
    if recorded_v == running_v:
        return None  # canonically equal despite differing spellings (0.1.01 vs 0.1.1)
    if recorded_v > running_v:
        return (
            f"rein {running_version} is older than the {recorded} that wrote {LOCK_NAME} — "
            f"upgrade the tool ({_upgrade_hint(source_of(data))})"
        )
    return (
        f"rein {running_version} is newer than the {recorded} recorded in {LOCK_NAME} — "
        "run `rein sync` to refresh the materialized artifacts (and the lock)"
    )
