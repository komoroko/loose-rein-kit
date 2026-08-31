"""Where this install came from, and what the upstream has now.

Two questions with one answer between them, which is why they live in one module: **the command
that actually upgrades this install is a function of how this install was made**, and nothing else
can derive it.

The distribution ships as a git tag (`uv tool install git+<repo>@vX.Y.Z`), and a tag-pinned uv tool
re-resolves to its own pinned rev — so `uv tool upgrade loose-rein-kit`, which five places in this
codebase used to print, moves such an install nowhere at all. It is the right command only for an
install that tracks a branch. PEP 610's `direct_url.json` records which of the two this is, so the
command is *derived* here rather than guessed in prose, and every message calls
:func:`upgrade_command` instead of spelling one.

**Nothing here runs on a hot path.** `rein guard` and `rein start` must not make a network call —
`start` is the SessionStart hook and was deliberately made cheap — so the fetch happens only in
`rein doctor`, a command a human types, and the result is cached in the user's cache home.
`rein start` reads that cache and never reaches for the network; :func:`cached_note` is the whole
of its involvement.

**A check that could not run says so.** No `gh`, no network, a rate limit, a non-GitHub origin, an
install with no VCS coordinates: each yields `None`, never "you are up to date". Reporting an
unmeasured thing as fine is the one failure mode this module would otherwise introduce.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from packaging.version import InvalidVersion, Version

logger = logging.getLogger(__name__)

#: The name pip/uv know this project by — `pyproject.toml [project] name`, NOT the import name.
#: They differ, and asking `importlib.metadata` for the import name is how `detect_source` spent
#: its whole life returning "" on every real install. `scripts/template_lint.py` pins this to
#: pyproject so the two cannot drift again.
DIST_NAME = "loose-rein-kit"

#: Where releases are listed when the origin is unknown and no command can honestly be printed.
RELEASES_URL = "https://github.com/komoroko/loose-rein-kit/releases"

#: Set to anything non-empty to keep `rein doctor` off the network. Same shape as `REIN_NO_CACHE`:
#: an operator's environment, never a knob in a document a task could edit.
NO_CHECK_ENV = "REIN_NO_UPDATE_CHECK"

_CACHE_NAME = "upstream.json"
_GITHUB_HOST_RE = re.compile(
    r"^(?:https?://|git@|ssh://git@)github\.com[/:](?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?$"
)


@dataclass(frozen=True)
class Origin:
    """Where the running tool was installed from.

    `rev` is the revision that was *requested* — a tag for a pinned install, a branch name for one
    that tracks a branch, "" when neither was given. `pinned` is the distinction the upgrade
    command turns on, and it is decided by whether `rev` parses as a version: a tag is a fixed
    point uv will keep re-resolving to, a branch is not.
    """

    url: str
    rev: str = ""

    @property
    def pinned(self) -> bool:
        try:
            Version(self.rev.lstrip("vV"))
        except InvalidVersion:
            return False
        return True

    @property
    def slug(self) -> str:
        """``owner/repo`` when this is a GitHub origin, "" otherwise (nothing else is queryable)."""
        m = _GITHUB_HOST_RE.match(self.url)
        return f"{m.group('owner')}/{m.group('repo')}" if m else ""


# --- where this install came from ------------------------------------------------


def source_from_direct_url(raw: str) -> str:
    """Reconstruct a `git+<url>[@rev]` source from a PEP 610 direct_url.json body (pure).

    A VCS install records `url` + `vcs_info`; a plain file/dir/editable install (`dir_info`) or
    an archive has no VCS coordinates, so there is nothing to record and we return "". Anything
    malformed also yields "" — a missing source is a benign gap, never a failure.
    """
    try:
        data = json.loads(raw)
    except ValueError:
        return ""
    vcs = data.get("vcs_info") if isinstance(data, dict) else None
    url = data.get("url") if isinstance(data, dict) else None
    if not isinstance(vcs, dict) or not isinstance(url, str) or not url:
        return ""
    base = url if url.startswith(("git+", "hg+", "bzr+", "svn+")) else f"{vcs.get('vcs', 'git')}+{url}"
    rev = vcs.get("requested_revision") or vcs.get("commit_id")
    return f"{base}@{rev}" if isinstance(rev, str) and rev else base


def detect_source() -> str:
    """Best-effort recovery of the rein source URL from this install's PEP 610 metadata.

    Returns "" when the metadata is absent (a source tree run, an editable install) or the install
    is not from a VCS — the same silent skip the wizard's old free-text source question produced
    on Enter.
    """
    import importlib.metadata as md

    try:
        raw = md.distribution(DIST_NAME).read_text("direct_url.json")
    except (md.PackageNotFoundError, OSError):
        return ""
    return source_from_direct_url(raw) if raw else ""


def parse_source(source: str) -> Origin | None:
    """An `Origin` from a `git+<url>[@rev]` spelling, or None when it names no VCS url."""
    if not source.startswith("git+"):
        return None
    body = source[len("git+") :]
    # The last '@' separates the rev — unless what follows it still looks like a host or a path,
    # which is the scp-style `git@github.com:o/r` (and `ssh://git@host/repo`) with no rev at all.
    url, sep, rev = body.rpartition("@")
    if not sep or "/" in rev or ":" in rev:
        url, rev = body, ""
    return Origin(url, rev) if url else None


def origin(recorded_source: str = "") -> Origin | None:
    """Where the running tool came from: PEP 610 first, the lock's recorded source as fallback.

    PEP 610 is the only *fact* about the running process; `recorded_source` is whatever a human
    typed at `rein init --source`, which may describe a different install entirely. So it is the
    fallback, not the first answer.
    """
    return parse_source(detect_source()) or (parse_source(recorded_source) if recorded_source else None)


# --- the one place an upgrade command is spelled ---------------------------------


def upgrade_command(org: Origin | None, tag: str = "") -> str:
    """The command that actually moves *this* install forward, or a pointer when none can be named.

    A tag-pinned uv tool stays on its pinned rev under `uv tool upgrade`, so the only thing that
    moves it is a forced re-install at the new tag. An install tracking a branch is the opposite
    case, and is the one `uv tool upgrade` was ever right for.
    """
    if org is None:
        return f"see {RELEASES_URL} for the current release, then re-run your install command"
    if org.pinned:
        target = tag or "vX.Y.Z"
        return f"uv tool install --force 'git+{org.url}@{target}'"
    return f"uv tool upgrade {DIST_NAME}"


# --- what the upstream has now ---------------------------------------------------


def newer_available(running: str, tag: str) -> bool:
    """True when `tag` names a release strictly newer than `running`.

    False — not an exception — for anything unparseable on either side: a fork's `release-2024-06`
    tag is not a claim that an upgrade exists.
    """
    try:
        return Version(tag.lstrip("vV")) > Version(running)
    except InvalidVersion:
        return False


def latest_release(org: Origin | None, *, timeout: float = 15.0) -> str | None:
    """The newest release tag on `org`, or None when the question could not be answered.

    Uses `gh`, which `doctor` already checks for and `pr-stack` already depends on: it carries the
    user's own credentials, so it works against a private fork and is not the 60-per-hour
    anonymous rate limit. Every failure — no gh, no network, no releases yet, a non-GitHub
    origin — is None, and the caller must report that as "could not check", never as "current".
    """
    from rein import common

    if os.environ.get(NO_CHECK_ENV, ""):
        return None
    slug = org.slug if org else ""
    if not slug:
        return None
    rc, out = common.run(["gh", "api", f"repos/{slug}/releases/latest", "--jq", ".tag_name"], timeout=timeout)
    if rc != 0:
        logger.debug("gh api releases/latest failed (rc=%s): %s", rc, out.strip()[:200])
        return None
    tag = out.strip().splitlines()[-1].strip() if out.strip() else ""
    return tag or None


# --- the cache `rein start` reads and never writes --------------------------------


def cache_path() -> Path:
    from rein import store

    return store.cache_home() / "rein" / _CACHE_NAME


def write_cache(slug: str, tag: str) -> None:
    """Record what the fetch saw. Best-effort: a cache that cannot be written is not an error."""
    path = cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"repo": slug, "tag": tag, "checked_at": date.today().isoformat()}) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        logger.debug("could not write %s: %s", path, exc)


def read_cache() -> dict[str, Any] | None:
    """The last recorded fetch, or None when there is none or it is unreadable."""
    try:
        data = json.loads(cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("tag"), str) else None


def cached_note(running: str, recorded_source: str = "") -> str:
    """One line for `rein start` when a newer release was seen, "" otherwise. Never hits the network.

    Silence covers every uncertain case at once: no cache, an unparseable one, a tag that is not
    newer. `rein start` runs at every session start, and a line it cannot stand behind there is
    worse than no line.
    """
    data = read_cache()
    if data is None:
        return ""
    tag = str(data["tag"])
    if not newer_available(running, tag):
        return ""
    seen = str(data.get("checked_at", "")) or "unknown date"
    command = upgrade_command(origin(recorded_source), tag)
    return f"rein {running} → {tag} available (seen {seen}) · {command}"
