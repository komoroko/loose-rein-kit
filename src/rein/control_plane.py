"""The Central Control Plane: how a leaf worktree records a decision that outlives it.

The problem this exists for (plan §11.1). During a parallel build each task runs in its own git
worktree. Recording a decision *inside that worktree* writes to the worktree's own
`.rein/` — a directory excluded from the task's commit and deleted when the leaf is cleaned
up, so the decision is recorded, looks recorded, and then vanishes. Per-worktree lock files have
the matching defect: two leaves can each hold "the" lock at once.

So the orchestrator (which runs in the canonical checkout) holds the only Store, and leaves
reach it over a Unix domain socket in the shared runtime directory:

    canonical checkout ── Store ── control.sock ──[scoped token]── leaf implementer

Three properties make that safe to hand to an LLM agent.

**A leaf cannot mint authority.** The HMAC key lives in a 0600 file the orchestrator creates
and never mounts into a leaf; the leaf receives only a token the orchestrator signed, scoped
to one run, one task, a capability list, and an expiry.

**A leaf cannot ask for what it was not granted.** Capabilities are checked against the
token, and :data:`models.CENTRAL_ONLY_CAPABILITIES` are refused *by the server* whatever the
token says. That second check is not redundant: it is what still holds if the secret leaks.
An agent that can approve its own work has not been reviewed by anyone.

**A replayed request is caught.** Each *request* carries a nonce, scoped by the token that
authorizes it; the server records the pair and refuses the second presentation, so a message
captured from a log cannot be re-sent. The nonce is per call rather than per token because a
token covers a whole task: burning it on the first request would mean an implementer could
record one decision and then be locked out of recording its knowledge gap.

The socket speaks newline-delimited strict JSON, one request per connection. Every mutation it
performs is an ordinary :class:`rein.store.Transaction`, so a leaf's decision lands in
the same chained log, under the same rules, as anything the orchestrator does itself.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import hmac
import json
import logging
import os
import socket
import socketserver
import threading
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from rein import common, digests, event_chain, models, strict_yaml
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

SOCKET_NAME = "control.sock"
SECRET_NAME = "control.secret"

#: Env vars the orchestrator sets for a leaf. The implementer profile's allowlist names them.
SOCKET_ENV = "REIN_CONTROL_SOCKET"
TOKEN_ENV = "REIN_CAPABILITY_TOKEN"

#: What a leaf is granted. Everything else — approving a gate, confirming as an expert,
#: completing a human review, replacing the machine review or the state, importing an
#: state replacement — is central-only and refused by the server regardless of the token.
LEAF_CAPABILITIES: frozenset[str] = models.CAPABILITY_VALUES - models.CENTRAL_ONLY_CAPABILITIES

#: A token outlives one task, not one build. Long enough that a slow implementer does not trip
#: over it, short enough that a token in a captured log is stale by the time anyone reads it.
DEFAULT_TTL_SEC = 3600

_MAX_REQUEST_BYTES = 256 * 1024
_SOCKET_TIMEOUT_SEC = 30


class ControlPlaneError(RuntimeError):
    """The control plane refused the request, or could not be reached."""


class TokenError(ControlPlaneError):
    """A token is malformed, unsigned, expired, replayed, or scoped for something else."""


# --- capability tokens ---------------------------------------------------------


@dataclass(frozen=True)
class Token:
    """One scoped grant. `capabilities` is what the *bearer* may ask for, not what exists."""

    run_id: str
    task_id: str
    capabilities: tuple[str, ...]
    expires_at: str
    nonce: str

    def payload(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "capabilities": list(self.capabilities),
            "expires_at": self.expires_at,
            "nonce": self.nonce,
        }

    def allows(self, capability: str) -> bool:
        return capability in self.capabilities


def new_secret() -> bytes:
    """A fresh HMAC key. Never written into a worktree, never mounted into a leaf."""
    return os.urandom(32)


def read_or_create_secret(runtime: Path) -> bytes:
    """The run's HMAC key, created 0600 on first use.

    Kept in the runtime directory rather than the repository for the obvious reason: a secret
    in the working tree is a secret in the diff, and the implementer container mounts that.
    """
    store_mod.ensure_private_dir(runtime)
    path = runtime / SECRET_NAME
    try:
        secret = path.read_bytes()
        if len(secret) >= 32:
            return secret
    except FileNotFoundError:
        pass
    secret = new_secret()
    store_mod.atomic_write(path, secret, mode=0o600)
    return secret


def mint(
    secret: bytes,
    *,
    run_id: str,
    task_id: str,
    capabilities: Sequence[str],
    ttl_sec: int = DEFAULT_TTL_SEC,
) -> str:
    """Sign a scoped token. Refuses to grant a central-only capability at all.

    The refusal is here as well as in the server because a mistake at this end would hand a
    leaf real authority, and "the server would have caught it" is not a thing to rely on when
    the alternative is one `if`.
    """
    requested = tuple(capabilities)
    forbidden = sorted(set(requested) & models.CENTRAL_ONLY_CAPABILITIES)
    if forbidden:
        raise TokenError(f"refusing to grant central-only capability/ies to a leaf: {', '.join(forbidden)}")
    unknown = sorted(set(requested) - models.CAPABILITY_VALUES)
    if unknown:
        raise TokenError(f"unknown capability/ies: {', '.join(unknown)}")

    token = Token(
        run_id=run_id,
        task_id=task_id,
        capabilities=requested,
        expires_at=(datetime.now().astimezone() + timedelta(seconds=ttl_sec)).isoformat(timespec="seconds"),
        nonce=event_chain.new_id(),
    )
    body = digests.canonical(token.payload())
    signature = hmac.new(secret, body, "sha256").hexdigest()
    return f"{base64.urlsafe_b64encode(body).decode('ascii')}.{signature}"


def verify(secret: bytes, raw: str, *, now: datetime | None = None) -> Token:
    """Parse and check a token. Raises :class:`TokenError` for every failure mode."""
    encoded, _, signature = raw.partition(".")
    if not encoded or not signature:
        raise TokenError("malformed token (expected '<payload>.<signature>')")
    try:
        body = base64.urlsafe_b64decode(encoded.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise TokenError(f"malformed token payload: {exc}") from None

    expected = hmac.new(secret, body, "sha256").hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise TokenError("token signature does not verify — it was not issued by this orchestrator")

    try:
        payload = strict_yaml.load_json_mapping(body.decode("utf-8"), limits=strict_yaml.EVENT_LIMITS, what="token")
    except (strict_yaml.StrictParseError, UnicodeDecodeError) as exc:
        raise TokenError(f"unreadable token payload: {exc}") from None

    try:
        token = Token(
            run_id=str(payload["run_id"]),
            task_id=str(payload["task_id"]),
            capabilities=tuple(str(c) for c in payload["capabilities"]),
            expires_at=str(payload["expires_at"]),
            nonce=str(payload["nonce"]),
        )
    except (KeyError, TypeError) as exc:
        raise TokenError(f"token is missing a required field: {exc}") from None

    try:
        deadline = datetime.fromisoformat(token.expires_at)
    except ValueError:
        raise TokenError(f"token carries an unreadable expiry {token.expires_at!r}") from None
    current = now or datetime.now().astimezone()
    if deadline.tzinfo is None:
        deadline = deadline.astimezone()
    if deadline <= current:
        raise TokenError(f"token expired at {token.expires_at}")
    return token


# --- the operations a leaf may request -----------------------------------------


@dataclass(frozen=True)
class Request:
    """One control-plane call: an operation, its arguments, and the token authorizing it.

    `nonce` is fresh per call and is what the server's replay check consumes — see the module
    docstring for why it cannot be the token's.
    """

    capability: str
    token: str
    args: dict[str, Any] = field(default_factory=dict)
    nonce: str = field(default_factory=event_chain.new_id)

    def to_json(self) -> str:
        return json.dumps(
            {"capability": self.capability, "token": self.token, "args": self.args, "nonce": self.nonce},
            sort_keys=True,
        )


def _decision_event(capability: str) -> str:
    return {
        "decision.declare": "decision_declared",
        "knowledge_gap.create": "knowledge_gap",
        "task.status": "task_started",
        "event.append": "decision_declared",
    }[capability]


#: The `run_id` a direct (non-socket) call carries. The audit record's actor is read by a human
#: deciding who did what, so "a leaf recorded this" and "the orchestrator recorded this" must
#: not print the same.
LOCAL_RUN = "local"


#: Risks at which an implementer's own decision stops being an implementation detail. Anything
#: at or above this parks the task rather than proceeding — the human decides, not the agent
#: that just invented the default (plan §16.5).
ESCALATION_FLOOR = "medium"


def apply_request(repo: repo_mod.Repo, token: Token, request: Request) -> dict[str, Any]:
    """Perform one authorized request as a Store transaction. Returns the result payload.

    Retried on a lost optimistic-concurrency race: leaves run concurrently and each touches its
    own task entry, so "somebody else committed between my read and my write" is an ordinary
    outcome here — re-read and re-apply, rather than dropping a leaf's record.
    """
    return store_mod.retry_on_stale(lambda: _apply_request_once(repo, token, request))


def _apply_request_once(repo: repo_mod.Repo, token: Token, request: Request) -> dict[str, Any]:
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ControlPlaneError("no .rein/state.yaml — the control plane has no cycle to record against")
    seen = store_mod.read_digest(state)

    args = request.args
    task_id = str(args.get("task") or token.task_id)
    statement = str(args.get("statement") or "").strip()
    if request.capability in {"decision.declare", "knowledge_gap.create"} and not statement:
        raise ControlPlaneError(f"{request.capability} needs a --statement")

    risk = str(args.get("risk") or "low")
    if risk not in models.RISK_VALUES:
        raise ControlPlaneError(f"unknown risk {risk!r} (one of {', '.join(models.RISK_ORDER)})")

    detail: dict[str, Any] = {
        "statement": statement,
        "category": str(args.get("category") or ""),
        "risk": risk,
        "anchor": str(args.get("anchor") or ""),
        "run_id": token.run_id,
    }
    detail = {k: v for k, v in detail.items() if v}

    # A decision at or above the escalation floor is not the implementer's to make: it changes
    # behaviour the plan did not describe. Park the task and let the human reconcile it.
    escalates = request.capability == "decision.declare" and models.risk_at_least(risk, ESCALATION_FLOOR)

    raw = json.loads(json.dumps(state.raw))
    if escalates or request.capability == "task.status":
        status = "needs-revision" if escalates else str(args.get("status") or "in-progress")
        if status not in models.TASK_STATUS_VALUES:
            raise ControlPlaneError(f"unknown task status {status!r}")
        tasks = raw.setdefault("tasks", {})
        entry = tasks.get(task_id) if isinstance(tasks.get(task_id), dict) else {}
        tasks[task_id] = {**entry, "status": status}
        raw["updated_at"] = event_chain.now_iso()

    with store.transaction() as tx:
        if escalates or request.capability == "task.status":
            tx.write("state", raw, expect_digest=seen)
        tx.append(
            _decision_event(request.capability),
            cycle_id=state.cycle_id,
            actor="canonical-checkout" if token.run_id == LOCAL_RUN else f"leaf:{token.task_id}",
            subject_ids=[task_id],
            detail=detail,
        )

    return {
        "recorded": request.capability,
        "task": task_id,
        "escalated": escalates,
        "note": (
            f"risk {risk} is at or above '{ESCALATION_FLOOR}': {task_id} is now needs-revision and waits "
            "for a human. Stop work on it."
            if escalates
            else "recorded in the central audit chain"
        ),
    }


# --- the server ----------------------------------------------------------------


class ControlServer(socketserver.ThreadingUnixStreamServer):
    """The orchestrator's listener. One request per connection, newline-delimited JSON."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, repo: repo_mod.Repo, secret: bytes) -> None:
        self.repo = repo
        self.secret = secret
        self.runtime = store_mod.runtime_dir(repo)
        store_mod.ensure_private_dir(self.runtime)
        self.socket_path = self.runtime / SOCKET_NAME
        # A stale socket from a killed run would make bind() fail; it holds no state, so
        # removing it is safe in a way that removing a lock file would not be.
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()
        super().__init__(str(self.socket_path), _Handler)
        os.chmod(self.socket_path, 0o600)
        self._seen_nonces: set[str] = set()
        self._nonce_lock = threading.Lock()

    def claim_nonce(self, token_nonce: str, request_nonce: str) -> bool:
        """True the first time this (token, request) pair is presented; False on every replay.

        Keyed by the pair so that one leaf's request nonce can never invalidate another's, and
        so that a replay has to reuse both halves — which is exactly what a captured message
        does. Consuming the *token's* nonce instead would spend the whole grant on one call.
        """
        with self._nonce_lock:
            key = f"{token_nonce}:{request_nonce}"
            if key in self._seen_nonces:
                return False
            self._seen_nonces.add(key)
            return True

    def server_close(self) -> None:
        super().server_close()
        with contextlib.suppress(FileNotFoundError):
            self.socket_path.unlink()


class _Handler(socketserver.StreamRequestHandler):
    timeout = _SOCKET_TIMEOUT_SEC

    def handle(self) -> None:
        server = self.server
        assert isinstance(server, ControlServer)
        try:
            raw = self.rfile.readline(_MAX_REQUEST_BYTES).decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            self._reply({"ok": False, "error": f"unreadable request: {exc}"})
            return
        try:
            self._reply({"ok": True, "result": self._dispatch(server, raw)})
        except (ControlPlaneError, models.DocumentError, store_mod.StoreError) as exc:
            self._reply({"ok": False, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - a leaf must never learn a traceback
            logger.exception("control plane: unhandled error")
            self._reply({"ok": False, "error": f"{type(exc).__name__}"})

    def _dispatch(self, server: ControlServer, raw: str) -> dict[str, Any]:
        try:
            payload = strict_yaml.load_json_mapping(raw, limits=strict_yaml.EVENT_LIMITS, what="control request")
        except strict_yaml.StrictParseError as exc:
            raise ControlPlaneError(str(exc)) from None

        capability = str(payload.get("capability", ""))
        # Refused by the server whatever the token says. Not redundant with mint(): this is
        # the check that still holds if the HMAC secret leaks.
        if capability in models.CENTRAL_ONLY_CAPABILITIES:
            raise ControlPlaneError(
                f"{capability} is central-only and is never served over the control plane — "
                "a leaf that could approve its own work has not been reviewed by anyone"
            )
        if capability not in LEAF_CAPABILITIES:
            raise ControlPlaneError(f"unknown capability {capability!r}")

        token = verify(server.secret, str(payload.get("token", "")))
        if not token.allows(capability):
            raise TokenError(f"this token does not grant {capability}")

        request_nonce = str(payload.get("nonce", ""))
        if not request_nonce:
            raise TokenError("the request carries no nonce — a call that cannot be told apart from a replay")
        if not server.claim_nonce(token.nonce, request_nonce):
            raise TokenError("request replayed — each (token, nonce) pair is accepted once")

        args = payload.get("args")
        request = Request(
            capability=capability,
            token="",
            args=args if isinstance(args, dict) else {},
            nonce=request_nonce,
        )
        return apply_request(server.repo, token, request)

    def _reply(self, payload: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self.wfile.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))


@contextlib.contextmanager
def serving(repo: repo_mod.Repo) -> Iterator[ControlServer]:
    """Run the control plane for the duration of a build, cleaning up the socket afterwards."""
    secret = read_or_create_secret(store_mod.runtime_dir(repo))
    server = ControlServer(repo, secret)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name="rein-control")
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


# --- the client ----------------------------------------------------------------


def call(socket_path: str | Path, request: Request) -> dict[str, Any]:
    """Send one request and return its result. Raises :class:`ControlPlaneError` on refusal."""
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(_SOCKET_TIMEOUT_SEC)
    try:
        client.connect(str(socket_path))
        client.sendall((request.to_json() + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        while b"\n" not in b"".join(chunks):
            chunk = client.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
    except OSError as exc:
        raise ControlPlaneError(f"cannot reach the control plane at {socket_path}: {exc}") from None
    finally:
        client.close()

    body = b"".join(chunks).decode("utf-8", errors="replace").strip()
    if not body:
        raise ControlPlaneError("the control plane closed the connection without answering")
    try:
        answer = strict_yaml.load_json_mapping(body, limits=strict_yaml.EVENT_LIMITS, what="control response")
    except strict_yaml.StrictParseError as exc:
        raise ControlPlaneError(f"unreadable control-plane response: {exc}") from None
    if not answer.get("ok"):
        raise ControlPlaneError(str(answer.get("error", "refused")))
    result = answer.get("result")
    return result if isinstance(result, dict) else {}


def route(repo: repo_mod.Repo, capability: str, args: dict[str, Any]) -> dict[str, Any]:
    """Perform a mutation the right way for wherever this process is running.

    In the canonical checkout the Store is right here, so the call is direct. In a leaf
    worktree it *must* go through the socket: writing to the leaf's own `.rein/` is the bug
    this module exists to close, so a leaf with no socket is refused rather than quietly falling
    back to a write that will be deleted with the worktree.
    """
    if repo.is_canonical_checkout:
        token = Token(
            run_id=LOCAL_RUN,
            task_id=str(args.get("task") or ""),
            capabilities=tuple(sorted(LEAF_CAPABILITIES)),
            expires_at="",
            nonce="",
        )
        return apply_request(repo, token, Request(capability=capability, token="", args=args))

    socket_path = os.environ.get(SOCKET_ENV, "")
    raw_token = os.environ.get(TOKEN_ENV, "")
    if not socket_path or not raw_token:
        raise ControlPlaneError(
            "this is a leaf worktree and no control plane is reachable "
            f"({SOCKET_ENV}/{TOKEN_ENV} are unset). A decision written here would live in the "
            "worktree's own .rein/ and be deleted with it — refusing rather than losing it."
        )
    return call(socket_path, Request(capability=capability, token=raw_token, args=args))


# --- the CLI (`rein decision add` / `rein knowledge-gap add`) ---------


def _build_parser(prog: str, capability: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=f"record a {prog.split()[-1]} in the central audit chain")
    parser.add_argument("action", choices=["add"], help="the only verb: a record is added, never edited or removed")
    parser.add_argument("--task", default="", help="the task this concerns (e.g. T-002)")
    parser.add_argument("--statement", required=True, help="what was decided, or what is not known")
    parser.add_argument("--category", default="", help="e.g. default | timeout | retry | error-handling")
    parser.add_argument(
        "--risk",
        default="low",
        choices=list(models.RISK_ORDER),
        help=f"at or above '{ESCALATION_FLOOR}' the task is parked for a human",
    )
    parser.add_argument("--anchor", default="", help="where in the code (path:line)")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    parser.set_defaults(capability=capability)
    return parser


def _run(parser: argparse.ArgumentParser, argv: list[str] | None) -> int:
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        result = route(
            repo,
            args.capability,
            {
                "task": args.task,
                "statement": args.statement,
                "category": args.category,
                "risk": args.risk,
                "anchor": args.anchor,
            },
        )
    except (ControlPlaneError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1
    print(result.get("note", "recorded"))
    return 2 if result.get("escalated") else 0


def main(argv: list[str] | None = None) -> int:
    """`rein decision add …` — exit 2 when the record parked the task for a human."""
    return _run(_build_parser("rein decision", "decision.declare"), argv)


def knowledge_gap_main(argv: list[str] | None = None) -> int:
    """`rein knowledge-gap add …` — what the implementer could not find out."""
    return _run(_build_parser("rein knowledge-gap", "knowledge_gap.create"), argv)


if __name__ == "__main__":
    raise SystemExit(main())
