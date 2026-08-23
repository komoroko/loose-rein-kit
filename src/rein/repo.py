"""Repository-root discovery and the absolute-path bundle.

Installed as an external CLI the tool can be launched from anywhere, so the root is
*discovered* once per invocation and carried as resolved absolute paths. Resolution
precedence (the first hit wins; an explicit choice that does not hold an ``.rein/``
directory is an error, never silently walked past):

  1. ``--repo PATH``   (the CLI's global flag → the ``override`` argument)
  2. ``$REIN_ROOT``
  3. walking up from ``start`` (default: cwd) to the first directory containing ``.rein/``

**Repository identity.** Runtime state (locks, the control socket) and the evidence cache no
longer live inside the working tree — they live under ``$XDG_RUNTIME_DIR`` and
``$XDG_CACHE_HOME`` keyed by :attr:`Repo.repo_id`. That id is derived from the realpath of the
*git common dir*, which is shared by a repository and all of its worktrees. A leaf worktree
and the canonical checkout therefore resolve to the same id — which is the whole point: a
per-worktree lock inode would let two leaves hold "the" lock simultaneously, and a decision
recorded in a leaf would vanish when the worktree was removed (plan §11.1).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT_SEC = 10

#: The SSOT directory as a POSIX prefix. Everything under it is orchestration state and never the
#: product, so it is excluded from four answers that have to agree: what "the tree" is
#: (`build_git.fingerprint`), what a task is credited with changing (`dirty_paths`), what its commit
#: carries (`finalize_commit`, and the same pathspec in the implementer's own instructions), and
#: what a review is bound to (`review.change_digest`). One constant, because two places able to
#: answer differently is the whole defect — a fingerprint that moved whenever the loop wrote down a
#: fact would invalidate the fact by recording it, and a review that wrote review.yaml would
#: invalidate itself.
SSOT_DIR = ".rein/"

#: The same exclusion as a git pathspec, for the commands that take one. `.` is explicit because a
#: pathspec containing only an exclusion matches nothing.
SSOT_PATHSPEC: tuple[str, ...] = (".", f":(exclude){SSOT_DIR.rstrip('/')}")


class RepoNotFoundError(RuntimeError):
    """No .rein/ directory was found — the command has no repository to operate on."""


def _has_marker(candidate: Path) -> bool:
    return (candidate / ".rein").is_dir()


def find_root(start: Path | None = None, override: str | None = None) -> Path:
    """The repository root, per the module-docstring precedence. Always absolute, resolved."""
    if override:
        root = Path(override).resolve()
        if not _has_marker(root):
            raise RepoNotFoundError(f"--repo {override}: no .rein/ directory there — not a Loose Rein repository")
        return root
    env = os.environ.get("REIN_ROOT", "")
    if env:
        root = Path(env).resolve()
        if not _has_marker(root):
            raise RepoNotFoundError(f"REIN_ROOT={env}: no .rein/ directory there — unset it or fix the path")
        return root
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if _has_marker(candidate):
            return candidate
    raise RepoNotFoundError(
        f"no .rein/ found walking up from {current} — run `rein init` there, pass --repo PATH, or set REIN_ROOT"
    )


@dataclass(frozen=True)
class Repo:
    """The discovered repository: one absolute root plus every derived path.

    Constructed once per invocation (see :func:`get`); everything downstream reads paths from
    here, so no tool depends on the process cwd.
    """

    root: Path
    _cache: dict[str, object] = field(default_factory=dict, repr=False, compare=False)

    def path(self, rel: str) -> Path:
        """The absolute path of repo-relative posix path `rel`."""
        return self.root / rel

    def rel(self, p: str | Path) -> str | None:
        """`p` as a repo-relative posix path, or None when `p` lies outside the root.

        A hook fired from a subdirectory or from a leaf worktree still resolves against the
        discovered root.
        """
        resolved = Path(p) if Path(p).is_absolute() else self.root / p
        try:
            return resolved.resolve().relative_to(self.root).as_posix()
        except ValueError:
            return None

    # --- the four SSOT artifacts (plan §5.1) ---------------------------------

    @property
    def rein_dir(self) -> Path:
        return self.root / ".rein"

    @property
    def plan(self) -> Path:
        """The Expected Model — frozen at gate ③."""
        return self.root / ".rein/plan.yaml"

    @property
    def state(self) -> Path:
        """Mutable state only; written exclusively by the Central Store's transaction."""
        return self.root / ".rein/state.yaml"

    @property
    def review(self) -> Path:
        return self.root / ".rein/review.yaml"

    @property
    def events(self) -> Path:
        return self.root / ".rein/events.ndjson"

    # --- satellites ----------------------------------------------------------

    @property
    def config(self) -> Path:
        return self.root / ".rein/config.yaml"

    @property
    def lock(self) -> Path:
        return self.root / ".rein/rein.lock"

    @property
    def prompts(self) -> Path:
        return self.root / ".rein/prompts"

    @property
    def scaffold(self) -> Path:
        return self.root / ".rein/scaffold"

    @property
    def rules(self) -> Path:
        return self.root / ".rein/AGENTS.rein.md"

    @property
    def docs(self) -> Path:
        return self.root / "docs"

    # --- identity and checkout kind ------------------------------------------

    def _git(self, *args: str) -> str:
        """One read-only git query against this root; "" on any failure (git absent, not a repo)."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return proc.stdout.strip() if proc.returncode == 0 else ""

    def _git_rc(self, *args: str) -> tuple[int, str]:
        """Like :meth:`_git` but returns `(returncode, stdout)` *unstripped*.

        The return code separates "no such blob" from "an empty blob", and the unstripped
        stdout keeps a file's real line count — both matter when validating a code anchor
        against the committed tree (`review_policy.validate_anchor`).
        """
        try:
            proc = subprocess.run(
                ["git", "-C", str(self.root), *args],
                capture_output=True,
                text=True,
                timeout=GIT_TIMEOUT_SEC,
            )
        except (OSError, subprocess.SubprocessError):
            return 1, ""
        return proc.returncode, proc.stdout

    @property
    def git_common_dir(self) -> Path | None:
        """The realpath of the git *common* dir — shared by the repository and its worktrees.

        None when this is not a git checkout. Used for identity rather than the root path so
        that a leaf worktree and the canonical checkout agree on one lock and one store.
        """
        cached = self._cache.get("git_common_dir")
        if cached is None:
            raw = self._git("rev-parse", "--path-format=absolute", "--git-common-dir")
            resolved = Path(raw).resolve() if raw else None
            self._cache["git_common_dir"] = resolved or False
            return resolved
        return cached if isinstance(cached, Path) else None

    @property
    def repo_id(self) -> str:
        """A stable, filesystem-safe identity for this repository (16 hex chars).

        Derived from the git common dir when there is one, else from the resolved root — so a
        non-git directory still gets a private runtime/cache namespace instead of colliding
        with every other one.
        """
        cached = self._cache.get("repo_id")
        if isinstance(cached, str):
            return cached
        anchor = self.git_common_dir or self.root
        value = hashlib.sha256(str(anchor).encode("utf-8")).hexdigest()[:16]
        self._cache["repo_id"] = value
        return value

    @property
    def repository_id(self) -> str:
        """This repository's identity in a gate receipt: the origin URL, else the resolved root.

        Recorded so that a receipt read out of context still says which repository it approved.
        """
        return self._git("config", "--get", "remote.origin.url") or str(self.root)

    @property
    def is_canonical_checkout(self) -> bool:
        """True when this is the main checkout rather than a linked worktree.

        Only the canonical checkout may mutate the store directly; a leaf worktree has to go
        through the control plane, or its decisions die with the worktree (plan §11.4).
        """
        common = self.git_common_dir
        if common is None:
            return True  # not a git checkout: there are no worktrees to be a leaf of
        git_dir = self._git("rev-parse", "--path-format=absolute", "--absolute-git-dir")
        return bool(git_dir) and Path(git_dir).resolve() == common


def get(override: str | None = None, start: Path | None = None) -> Repo:
    """find_root + Repo — the one constructor every entry point calls."""
    return Repo(find_root(start=start, override=override))
