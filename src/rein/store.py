"""The Central Store: the one write path, and the only place a state change becomes durable.

Three failure modes this module exists to close.

**Split writes.** Documents written independently by whichever module felt like it leave the
repository saying two different things after a crash between two writes, with nothing detecting
it. Here every mutation is a :class:`Transaction`: the documents and the audit events land
together or not at all.

**Worktree-local state.** A lock or runtime file under `.rein/` gives each leaf worktree
its own inode — two leaves can each hold "the" lock — and a decision recorded inside a leaf
dies with the worktree (plan §11.1). Runtime state lives under
``$XDG_RUNTIME_DIR/rein/<repo-id>/`` instead, keyed by
:attr:`rein.repo.Repo.repo_id`, which a repository and all of its worktrees share.

**Lost updates.** Every transaction records the digest of what it read and refuses to commit if
the file moved underneath it (:class:`StaleWriteError`) — which is also exactly the check that
stops a human review answer being merged into a machine review that has since been regenerated
(plan §17.5).

Durability follows a journal (plan §18.6): ``prepared`` → ``files_replaced`` →
``event_appended`` → committed. Recovery is asymmetric on purpose: a transaction that has not
yet replaced files is rolled back, and one that has already appended events is rolled
*forward*. An appended audit event is never removed — "we un-recorded it" is not a thing an
audit log may do.

Lock order is **build.lock → store.lock**, always. The build loop holds the build lock for a
whole run and takes the store lock per transaction; taking them the other way round would
deadlock the two against each other.
"""

from __future__ import annotations

import contextlib
import os
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, TypeVar

import yaml

import rein
from rein import common, digests, event_chain, models, strict_yaml
from rein import lock as lock_mod
from rein import repo as repo_mod

_DIR_MODE = 0o700
#: Runtime files (locks, journal, control socket) are private to the user.
_FILE_MODE = 0o600
#: Repository documents are ordinary tracked files — 0600 would make a shared checkout or a
#: CI artifact unreadable, and nothing secret is ever written into the working tree.
_REPO_FILE_MODE = 0o644

_T = TypeVar("_T")


class BehindError(common.ReinError, RuntimeError):
    """This tool is older than the release that wrote this repository, and was about to write it.

    Raised by :meth:`Store.transaction`, which is every SSOT mutation's one door. Placed there
    rather than in front of each caller that happens to matter, because the ones that matter are
    not knowable from here: a gate receipt binds digests the writing process computed and records
    which *channel* confirmed rather than which release wrote it, so afterwards nothing
    distinguishes a receipt written under a schema this tool has the narrow version of. Neither
    does a task status, a review freeze, or an evidence record.

    A subclass of nothing in particular on purpose — it is not a `StoreError`, because a caller
    that turns "the store failed" into "the document is bad" would say the wrong thing about a
    tool that is merely out of date.
    """


class DocumentBehindError(models.DocumentError):
    """A document this tool cannot parse *because it is older than what wrote the repository*.

    "Invalid" and "behind" are different facts with different repairs, and only one of them is
    about the document. A newer release widens a schema; this tool has the narrow one and rejects
    a key the repository is entitled to carry. Sending a human to `rein revise --to tasks` there
    asks them to rewind an approved gate — the most expensive move the workflow has — to fix
    nothing.

    A subclass of :class:`models.DocumentError` so every `except models.DocumentError` keeps
    catching it: the *category* is unchanged (a caller was reading a document and could not), and
    only the sentence and the repair differ. Raised from :class:`Store`, which is where all four
    documents are read, so no caller has to know to ask — the answer arrives with the failure.
    The schema errors are still on `.errors` for anyone who wants to look.
    """

    def __init__(self, what: str, errors: Sequence[str], behind: str) -> None:
        super().__init__(what, errors)
        self.behind = behind
        self.repair = f"{behind} — read it with the release that wrote it rather than treating it as damaged"
        # Deliberately does not name the document: every caller already has, because it was the
        # one it asked for. "cannot read config.yaml: this rein cannot parse config.yaml" is the
        # shape that gets skimmed past.
        self.args = (
            f"this rein is older than the release that wrote this repository, and that — not a "
            f"damaged document — is why it does not parse: {behind}. Restart any running "
            "`rein ui` after upgrading.",
        )


class StoreError(common.ReinError, RuntimeError):
    """The store cannot be used: unusable runtime directory, lock failure, damaged journal."""


class StaleWriteError(StoreError):
    """A document moved between the read and the commit — the caller's view is out of date.

    Surfaced as HTTP 409 by the review API: the human answered questions about a machine
    review that no longer exists, and merging the answer would attach it to the wrong bytes.
    """


class LockUnavailableError(StoreError):
    """Another process holds the lock."""


# --- XDG base directories ------------------------------------------------------
#
# The spec's fallbacks are not optional niceties: XDG_CONFIG_HOME and XDG_CACHE_HOME are unset
# on a plain login shell far more often than not, and a tool that only works when they happen
# to be exported is a tool that works on the author's machine.


def _xdg(var: str, fallback: str) -> Path:
    value = os.environ.get(var, "").strip()
    if value and Path(value).is_absolute():
        return Path(value)
    return Path.home() / fallback


def config_home() -> Path:
    """``$XDG_CONFIG_HOME`` or ``~/.config`` — where the cross-repository project registry lives."""
    return _xdg("XDG_CONFIG_HOME", ".config")


def cache_home() -> Path:
    """``$XDG_CACHE_HOME`` or ``~/.cache`` — this repository's disposable run output."""
    return _xdg("XDG_CACHE_HOME", ".cache")


def runtime_home() -> tuple[Path, bool]:
    """(runtime base, is_private). ``$XDG_RUNTIME_DIR`` when set, else a per-uid temp directory.

    The fallback is honestly weaker — a temp directory is not guaranteed to be cleared at
    logout or unreachable by other users — so it returns False and `doctor` says so out loud
    rather than pretending the isolation is the same.
    """
    value = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if value and Path(value).is_absolute():
        return Path(value), True
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"rein-{uid}", False


def read_digest(document: Any) -> str:
    """The optimistic-concurrency token for a document object the caller has just read.

    Taken from the mapping the caller actually holds, never from a fresh read of the file:
    re-reading the document inside the transaction compares the file against itself, so
    :class:`StaleWriteError` can never fire and the lost update it exists to catch goes through
    silently. ``None`` means "absent", matching :meth:`Store.document_digest`.
    """
    raw = getattr(document, "raw", None)
    return digests.of(raw) if raw is not None else ""


def retry_on_stale(operation: Callable[[], _T], *, attempts: int = 5) -> _T:
    """Run `operation`, re-running it from scratch when it loses an optimistic-concurrency race.

    A :class:`StaleWriteError` means "somebody else committed first, so re-read and re-apply" —
    which is exactly what this does, and what makes the check usable for writes that genuinely
    do compose (two leaves setting the status of two different tasks). It is deliberately *not*
    applied to a write where losing the race changes what the caller would have decided: a human
    review answer against a regenerated machine review has to surface as a 409, not be retried
    onto bytes the reviewer never saw.

    `operation` must do its own reading; retrying a closure over a stale read would just lose
    the same race again.
    """
    for remaining in range(attempts - 1, -1, -1):
        try:
            return operation()
        except StaleWriteError:
            if not remaining:
                raise
    raise AssertionError("unreachable")  # pragma: no cover


def runtime_dir(repo: repo_mod.Repo) -> Path:
    """This repository's runtime directory (locks, control socket, journal). Not created here."""
    base, _ = runtime_home()
    return base / "rein" / repo.repo_id


def cache_dir(repo: repo_mod.Repo) -> Path:
    """This repository's cache directory (disposable run output)."""
    return cache_home() / "rein" / repo.repo_id


def ensure_private_dir(path: Path) -> Path:
    """Create `path` 0700 and verify it is safe to use. Raises :class:`StoreError` otherwise.

    Refuses a symlink and a directory owned by somebody else: both are the classic way to get
    a privileged process to write into a location an attacker controls.
    """
    path.mkdir(parents=True, exist_ok=True, mode=_DIR_MODE)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise StoreError(f"{path} is a symlink — refusing to use it as a runtime directory")
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f"{path} exists and is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StoreError(f"{path} is owned by uid {info.st_uid}, not by this user — refusing to use it")
    if hasattr(os, "chmod") and stat.S_IMODE(info.st_mode) != _DIR_MODE:
        os.chmod(path, _DIR_MODE)
    return path


# --- atomic file replacement ---------------------------------------------------


def atomic_write(path: Path, data: bytes, *, mode: int = _FILE_MODE) -> None:
    """Replace `path` with `data`, atomically and durably (plan §18.4).

    temp on the same filesystem → write → flush → fsync → ``os.replace`` → fsync the parent
    directory. Skipping the final directory fsync is the classic "the file is there but the
    rename is not" crash on ext4.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    temp = Path(temp_name)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temp.unlink()
        raise
    _fsync_dir(path.parent)


def _fsync_dir(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):  # Windows: directories cannot be opened for fsync
        return
    fd = os.open(str(path), os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class _NoAliasDumper(yaml.SafeDumper):
    """A dumper that repeats a shared object instead of anchoring it.

    `strict_yaml` refuses anchors and aliases on read — deliberately, because an alias is an
    ambiguity and an expansion surface. The writer did not know that, so **rein could write an SSOT
    document it could not read back**: whenever one object was reachable from two places in the
    same document, `yaml.safe_dump` emitted `&id001` / `*id001` and the next load raised. It was
    not hypothetical. `review.assemble` hands the security findings to `decision_cards.derive_cards`
    and also stores them under `security.findings`, so *any* review with a security finding wrote a
    `review.yaml` that could never be parsed again — gate ④ unreadable, and the loader pointing at
    a line rather than at the cause.

    Repeating the object is the right resolution rather than deep-copying at each composition site:
    the two halves of one format have to agree, and only one of them can be enforced in one place.
    """

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_yaml(mapping: Mapping[str, Any]) -> bytes:
    """Serialize an SSOT document to bytes.

    Keys are *not* sorted: these files are read by humans in review, and a stable authored
    order reads far better than an alphabetical one. Ordering does not affect the digest,
    which is taken over the canonical form (:mod:`rein.digests`), not over these bytes.
    """
    return yaml.dump(dict(mapping), Dumper=_NoAliasDumper, sort_keys=False, allow_unicode=True, width=100).encode(
        "utf-8"
    )


# --- file locking ---------------------------------------------------------------


#: How long a caller waits for the store lock before giving up. The store lock is held for one
#: short transaction, so contention resolves in milliseconds; this ceiling exists only so a
#: process that died holding it cannot hang the next one forever.
STORE_LOCK_WAIT_SEC = 30.0


@dataclass
class FileLock:
    """An advisory exclusive lock on one path, held for the duration of a `with` block.

    `wait_sec` distinguishes the two locks, and the distinction matters. The **build lock** is
    a mutual-exclusion claim — "only one run at a time" — so it fails immediately: queueing a
    second build behind the first is never what anyone wanted. The **store lock** is a short
    critical section that several legitimate writers (the orchestrator, each leaf's control
    plane request) contend for constantly, so it waits: refusing a leaf's decision because
    another leaf happened to be writing at that instant would lose exactly the record this
    whole layer exists to keep.
    """

    path: Path
    wait_sec: float = 0.0
    _fd: int | None = field(default=None, repr=False)

    def __enter__(self) -> FileLock:
        ensure_private_dir(self.path.parent)
        self._fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, _FILE_MODE)
        deadline = time.monotonic() + self.wait_sec
        while True:
            try:
                _lock_fd(self._fd)
                return self
            except OSError as exc:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    waited = f" after waiting {self.wait_sec:g}s" if self.wait_sec else ""
                    raise LockUnavailableError(
                        f"another rein process holds {self.path.name}{waited} — wait for it to "
                        "finish, or run `rein doctor` if you believe it is stale"
                    ) from exc
                time.sleep(0.01)

    def __exit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._fd is not None:
            with contextlib.suppress(OSError):
                _unlock_fd(self._fd)
            os.close(self._fd)
            self._fd = None


def _lock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_fd(fd: int) -> None:
    try:
        import fcntl
    except ImportError:  # Windows
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        return
    fcntl.flock(fd, fcntl.LOCK_UN)


# --- the store -------------------------------------------------------------------

_DOCUMENTS = ("plan", "state", "review")


@dataclass
class Store:
    """Reads and transactional writes for one repository's SSOT.

    Every mutation goes through :meth:`transaction`; there is deliberately no `write_state`
    on the store itself, because a state write with no matching audit event is the gap this
    class exists to close.
    """

    repo: repo_mod.Repo

    # -- paths ---------------------------------------------------------------

    @property
    def runtime(self) -> Path:
        return runtime_dir(self.repo)

    @property
    def store_lock(self) -> Path:
        return self.runtime / "store.lock"

    @property
    def build_lock(self) -> Path:
        return self.runtime / "build.lock"

    @property
    def journal(self) -> Path:
        return self.runtime / "store.journal"

    def _document_path(self, name: str) -> Path:
        return {"plan": self.repo.plan, "state": self.repo.state, "review": self.repo.review}[name]

    # -- reads ---------------------------------------------------------------

    def read_raw(self, name: str) -> dict[str, Any] | None:
        """The parsed mapping of a document, or None when the file is absent."""
        path = self._document_path(name)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"cannot read {path}: {exc}") from None
        return strict_yaml.load_mapping(text, what=path.name)

    def behind(self) -> str | None:
        """Why this tool is older than the release that wrote this repository, or None.

        One question, asked at the layer that reads and writes the documents rather than by each
        caller that might want to mention it. `lock.startup_warning` is the per-invocation notice,
        which is the same thing as "once per process" for every rein but `ui` — a server that stays
        up for days, serves several repositories, and is invalidated by an upgrade in another
        terminal.

        A lock nobody can read answers None, and that is not it being swallowed: the question is
        whether being behind *explains* something else, and with no readable lock nothing has been
        established either way. Saying "behind" there would attach a cause to a failure on no
        evidence. The damaged lock has its own report — `cli` turns it into a hard error on every
        invocation and `doctor` names it — and :meth:`write_refusal` is where "cannot tell" and
        "must not" become the same answer.
        """
        try:
            return lock_mod.behind_summary(self.repo, rein.__version__)
        except lock_mod.LockError:
            return None

    def write_refusal(self) -> str | None:
        """Why this tool must not write this repository's SSOT, or None. Two reasons, one posture.

        The lock says a newer release wrote the documents, or the lock cannot be read and the
        question has no answer. Both are "this process cannot vouch for what is already here", and
        a write is not the place to assume otherwise.
        """
        try:
            behind = lock_mod.behind_summary(self.repo, rein.__version__)
        except lock_mod.LockError as exc:
            return (
                f"refusing to write this repository's SSOT: {exc}. Whether this tool is older than "
                "the release that wrote these documents has no answer while the lock is unreadable. "
                "Run `rein doctor`."
            )
        if behind is None:
            return None
        return (
            f"refusing to write this repository's SSOT: {behind}. A document written here would be "
            "written by a tool that does not fully parse what is already there."
        )

    def _read(self, reader: Callable[[], _T]) -> _T:
        """Run one document reader, and say "behind" instead of "invalid" when that is the truth."""
        try:
            return reader()
        except models.DocumentError as exc:
            behind = self.behind()
            if behind is None:
                raise
            raise DocumentBehindError(exc.what, exc.errors, behind) from None

    def read_plan(self) -> models.Plan | None:
        return self._read(self._plan)

    def read_state(self) -> models.State | None:
        return self._read(self._state)

    def read_review(self) -> models.Review | None:
        return self._read(self._review)

    def read_config(self) -> models.Config | None:
        """The validated config, or None when absent. Raises on a config that fails its schema."""
        return self._read(self._config)

    def _plan(self) -> models.Plan | None:
        raw = self.read_raw("plan")
        if raw is None:
            return None
        errors = models.schema_errors(raw, "plan")
        plan = models.Plan(raw)
        errors = errors or models.cross_reference_errors(plan)
        if errors:
            raise models.DocumentError("plan.yaml", errors)
        return plan

    def _state(self) -> models.State | None:
        raw = self.read_raw("state")
        if raw is None:
            return None
        errors = models.schema_errors(raw, "state")
        if errors:
            raise models.DocumentError("state.yaml", errors)
        return models.State(raw)

    def _review(self) -> models.Review | None:
        raw = self.read_raw("review")
        if raw is None:
            return None
        errors = models.schema_errors(raw, "review")
        if errors:
            raise models.DocumentError("review.yaml", errors)
        return models.Review(raw)

    def _config(self) -> models.Config | None:
        try:
            text = self.repo.config.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"cannot read {self.repo.config}: {exc}") from None
        return models.Config.parse(text)

    def read_events(self) -> list[models.Event]:
        """Every event, chain verified. Raises :class:`event_chain.ChainError` on damage."""
        return event_chain.load(self.repo.events)

    def chain_root(self) -> str:
        return event_chain.chain_root(self.read_events())

    def document_digest(self, name: str) -> str:
        """The digest of a document as it currently sits on disk ("" when absent).

        Taken over the canonical parsed form, so reformatting a file does not look like an
        edit to the optimistic-concurrency check.
        """
        raw = self.read_raw(name)
        return digests.of(raw) if raw is not None else ""

    # -- transactions ---------------------------------------------------------

    @contextlib.contextmanager
    def transaction(self, *, recover: bool = True) -> Iterator[Transaction]:
        """Hold the store lock and yield a :class:`Transaction`; commit on clean exit.

        An exception inside the block aborts: nothing is written, no event is appended.

        Refused outright while the repository is newer than this tool (:class:`BehindError`). This
        is the one door every SSOT mutation goes through, which is why the check is here: naming
        the writes that matter would mean maintaining that list, and a gate receipt, a task status
        and a review freeze are all written by a process whose reading of the documents is the
        thing in doubt.
        """
        refusal = self.write_refusal()
        if refusal is not None:
            raise BehindError(refusal)
        ensure_private_dir(self.runtime)
        with FileLock(self.store_lock, wait_sec=STORE_LOCK_WAIT_SEC):
            if recover:
                self._recover()
            tx = Transaction(store=self)
            yield tx
            tx._commit()

    # -- journal / recovery ----------------------------------------------------

    def _read_journal(self) -> dict[str, Any] | None:
        try:
            text = self.journal.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StoreError(f"cannot read the store journal {self.journal}: {exc}") from None
        try:
            return strict_yaml.load_json_mapping(text, what="store.journal")
        except strict_yaml.StrictParseError as exc:
            raise StoreError(f"the store journal is corrupt: {exc} — run `rein doctor`") from None

    def _write_journal(self, payload: Mapping[str, Any]) -> None:
        atomic_write(self.journal, digests.canonical(payload))

    def _clear_journal(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self.journal.unlink()
        _fsync_dir(self.journal.parent)

    def _recover(self) -> None:
        """Finish or discard an interrupted transaction. Caller holds the store lock.

        The asymmetry is the point (plan §18.6): before files were replaced there is nothing
        to preserve, so roll back; after events were appended there is a record that must not
        be un-recorded, so roll forward.
        """
        journal = self._read_journal()
        if journal is None:
            return
        phase = str(journal.get("phase", ""))
        tx_id = str(journal.get("tx_id", ""))

        if phase == "prepared":
            for temp in journal.get("temp_paths", {}).values():
                with contextlib.suppress(OSError, TypeError):
                    Path(str(temp)).unlink()
            self._clear_journal()
            return

        if phase == "files_replaced":
            # Files are in place but the audit record may be missing. Append it if absent —
            # a state change with no event is exactly the invisible mutation we forbid.
            existing = {e.tx_id for e in event_chain.scan(self.repo.events)[0]}
            if tx_id not in existing:
                payloads = journal.get("event_payloads") or []
                self._append_events([models.Event.from_mapping(p) for p in payloads if isinstance(p, dict)])
            self._clear_journal()
            return

        if phase == "event_appended":
            self._clear_journal()
            return

        raise StoreError(f"the store journal is in an unknown phase {phase!r} — run `rein doctor`")

    def _append_events(self, events: Sequence[models.Event]) -> list[models.Event]:
        """Chain `events` onto the current log and append them. Caller holds the store lock."""
        if not events:
            return []
        current = event_chain.load(self.repo.events)
        previous = current[-1] if current else None
        chained: list[models.Event] = []
        for event in events:
            linked = event_chain.link(previous, event)
            chained.append(linked)
            previous = linked
        event_chain.append_lines(self.repo.events, chained)
        return chained


@dataclass
class Transaction:
    """One atomic change: zero or more document writes plus the events that explain them.

    A transaction that writes a document and appends no event is refused. The whole reason
    this type exists is that "the state changed and nothing recorded why" must be impossible.
    """

    store: Store
    #: Fixed when the transaction opens, not at commit, so :meth:`projected_chain_root` can be
    #: answered while the block is still running.
    tx_id: str = field(default_factory=event_chain.new_id)
    _writes: dict[str, Mapping[str, Any]] = field(default_factory=dict)
    _expect: dict[str, str] = field(default_factory=dict)
    _events: list[models.Event] = field(default_factory=list)
    _committed: bool = False

    # -- staging --------------------------------------------------------------

    def write(self, name: str, mapping: Mapping[str, Any], *, expect_digest: str | None = None) -> None:
        """Stage a document write. `expect_digest` is what the caller read; None means "absent".

        Passing the digest is how a lost update becomes a :class:`StaleWriteError` instead of
        a silent overwrite.
        """
        if name not in _DOCUMENTS:
            raise StoreError(f"unknown document {name!r} (one of {', '.join(_DOCUMENTS)})")
        problems = models.schema_errors(mapping, name)
        if problems:
            raise models.DocumentError(f"{name}.yaml (staged write)", problems)
        self._writes[name] = mapping
        if expect_digest is not None:
            self._expect[name] = expect_digest

    def append(
        self,
        event: str,
        *,
        cycle_id: str,
        actor: str = "",
        subject_ids: Sequence[str] = (),
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Stage one audit event. Chained and sealed at commit, not here."""
        self._events.append(event_chain.make(event, cycle_id, actor=actor, subject_ids=subject_ids, detail=detail))

    def projected_chain_root(self) -> str:
        """The chain root the log will carry once this transaction's staged events land.

        Answerable before the commit because an event's payload never depends on the documents
        being written, and `tx_id` is fixed when the transaction opens. That is what lets a gate
        receipt record the root its own approval *results in* — an auditor recomputing the chain
        afterwards has something to compare against (the pre-approval root is already bound
        separately, as `attested_chain_root`).

        Only meaningful inside the transaction block, where the store lock is held.
        """
        current = self.store.read_events()
        previous = current[-1] if current else None
        chained: list[models.Event] = []
        for event in self._events:
            linked = event_chain.link(previous, self._with_tx(event, self.tx_id))
            chained.append(linked)
            previous = linked
        return event_chain.chain_root([*current, *chained])

    # -- commit ----------------------------------------------------------------

    def _commit(self) -> None:
        if self._committed:
            return
        if not self._writes and not self._events:
            self._committed = True
            return
        if self._writes and not self._events:
            raise StoreError(
                "a transaction that changes state must record why: append at least one event "
                "(an unexplained mutation is exactly what the audit chain exists to prevent)"
            )

        for name, expected in self._expect.items():
            current = self.store.document_digest(name)
            if current != expected:
                raise StaleWriteError(
                    f"{name}.yaml changed since it was read — refusing to overwrite. "
                    "Re-read it and re-apply the change."
                )

        tx_id = self.tx_id
        events = [self._with_tx(e, tx_id) for e in self._events]

        payload: dict[str, Any] = {
            "tx_id": tx_id,
            "phase": "prepared",
            "expected_old_digests": dict(self._expect),
            "new_file_digests": {name: digests.of(body) for name, body in self._writes.items()},
            "temp_paths": {},
            "event_payloads": [e.to_mapping() for e in events],
        }
        self.store._write_journal(payload)

        for name, body in self._writes.items():
            atomic_write(self.store._document_path(name), dump_yaml(body), mode=_REPO_FILE_MODE)

        payload["phase"] = "files_replaced"
        self.store._write_journal(payload)

        self.store._append_events(events)

        payload["phase"] = "event_appended"
        self.store._write_journal(payload)

        self.store._clear_journal()
        self._committed = True

    @staticmethod
    def _with_tx(event: models.Event, tx_id: str) -> models.Event:
        from dataclasses import replace

        return replace(event, tx_id=tx_id)
