"""Local web dashboard for the Loose Rein SSOT — the human's cockpit for a Human-on-the-Loop run.

Serves the ES-module page in `ui_assets/` (real files so the frontend is lintable and diffable, not
strings inside Python) over stdlib `http.server`, same-origin with zero external references so the
dashboard stays offline-safe. Four hash-routed tabs: **Overview** (lifecycle rail, next recommended
command, needs-attention — from status_api.collect_status()), **Review** (the gate under decision:
its deliverables rendered server-side by mdlite with the self-assessment pinned, gate ④'s diff and
security-review freshness — from review_api.collect_review() — ending in the approval footer),
**Tasks** (DAG, layer progress, frontier, traceability), and **Activity** (the events.ndjson feed
plus the operations console). The page also notifies the approval-wait: browser notifications are
opt-in behind the bell; the tab title and favicon always carry the waiting state.

What the page may do is bounded by principle, not habit: (a) **read** inside the target repository
— every GET resolves paths server-side from fixed specs, never from the client; (b) run **local,
side-effect-free diagnostics** whose argv is fixed here (`action_argv`: doctor, tests); (c) record
**human decisions that already have a single sanctioned CLI write path** (gate approval via
approve.py — a human privilege exercised by a human clicking — plus events-resolve / revise /
cycle-close). The client only ever sends an action id and typed parameters; command lines are built
server-side, so arbitrary command execution is structurally impossible. Phase execution (/req …
/build) and outward-facing operations (push / PR / merge) are deliberately absent.

The page polls `/api/status` for the whole life of a supervised run, and a run is mostly waiting —
so the endpoint answers `304 Not Modified` against an `ETag` taken over the payload *minus* its
`generated_at` stamp. An unchanged SSOT therefore costs one empty round trip: nothing is
transferred, nothing re-parsed, and the client re-renders nothing, so whatever the human has open
(a task's detail, a half-typed field, the scroll inside a long patch) survives until the state
actually moves. A backgrounded tab polls lazily.

Safety layers: binds 127.0.0.1 by default, and a non-loopback bind with the write endpoints enabled
requires an explicit `--allow-remote`; every POST requires the `X-Rein-Token` header whose value
is generated per server start and embedded only in the served page (a cross-origin page cannot set a
custom header without a CORS preflight, and no CORS headers are ever sent); `--read-only` disables
POST entirely — reviewing stays fully readable. Because this page holds that token, *everything*
agent-written that reaches it is constructed, never sanitized: deliverable markdown goes through
mdlite's escape-first renderer (see its threat model), and agent-written identifiers — task ids
above all, which tasks.yaml is free to spell any way it likes — reach the DOM only as escaped
attribute values read back by a delegated listener, never interpolated into a handler.

Usage:
  rein ui                 # serve on 127.0.0.1:8765 and open the browser
  rein ui --no-open  # print the URL only
  rein ui --read-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rein import approve, common, event_chain, human_review, models, review_api, revise, status_api
from rein import events as events_mod
from rein import registry as registry_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_SEC = 900
_OUTPUT_LIMIT = 8000  # tail shown per stream (failures are summarized, not dumped)
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# The frontend, shipped beside this module. Served by exact name only (no traversal surface);
# read per request so an edit shows up on reload during development. The dict stays explicit —
# no directory scan — so what the server can hand out is reviewable here; a test asserts it
# matches the files actually shipped in ui_assets/.
ASSETS_DIR = Path(__file__).resolve().parent / "ui_assets"
_JS = "text/javascript; charset=utf-8"
_ASSET_TYPES = {
    "app.css": "text/css; charset=utf-8",
    "app.js": _JS,
    "api.js": _JS,
    "view-overview.js": _JS,
    "view-review.js": _JS,
    "view-tasks.js": _JS,
    "view-activity.js": _JS,
    "notify.js": _JS,
}
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")

# events.ndjson, parsed at most once per version of the file. Both polled endpoints need the whole
# log — an escalation's open/closed state depends on a `resolve` that may appear anywhere in it — so
# /api/status (every poll, always) and the Activity feed (every poll while open) share this one
# parse. Keyed on the file's identity, so an append is picked up on the next poll and a stale entry
# cannot be served. One slot: the dashboard reads one project at a time, and a switch just re-reads.
_events_cache: tuple[tuple[str, int, int], list[models.Event]] | None = None
_events_lock = threading.Lock()
#: Defects found alongside the cached parse, so a damaged chain is reported by the same
#: poll that reads it rather than needing a second pass over the file.
_defects_cache: dict[str, list[event_chain.ChainDefect]] = {}


def _scan_cached(path: str | Path) -> tuple[list[models.Event], list[event_chain.ChainDefect]]:
    """The cached scan: (events, defects). One slot, keyed on the file's identity."""
    return _load_events_cached(path), _defects_cache.get(str(Path(path)), [])


def _load_events_cached(path: str | Path) -> list[models.Event]:
    global _events_cache
    path = Path(path)
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime_ns, stat.st_size)
    except OSError:
        return []  # an absent log is empty; a DAMAGED one is reported by /api/status, not here
    with _events_lock:
        if _events_cache is not None and _events_cache[0] == key:
            return _events_cache[1]
    loaded, defects = event_chain.scan(path)
    with _events_lock:
        _events_cache = (key, loaded)
        _defects_cache[str(path)] = defects
    return loaded


#: Gate readiness costs git subprocesses and snapshot digests — affordable once per `rein
#: status`, wasteful at poll frequency. One slot, keyed on (root, gate) and aged out, so the
#: dashboard's blocking rows are at most this many seconds behind the repository. A stat-based key
#: would not do: readiness also depends on where HEAD points, and nothing under .rein/ moves
#: when a commit lands.
_READINESS_TTL_SEC = 10.0
_readiness_cache: dict[tuple[str, str], tuple[float, list[str]]] = {}
_readiness_lock = threading.Lock()


def _readiness_cached(repo: repo_mod.Repo, gate: str) -> list[str]:
    """`approve.readiness` behind a short TTL, and never a reason for the dashboard to fall over."""
    key = (str(repo.root), gate)
    now = time.monotonic()
    with _readiness_lock:
        entry = _readiness_cache.get(key)
        if entry is not None and now - entry[0] < _READINESS_TTL_SEC:
            return entry[1]
    try:
        blockers = approve.readiness(repo, gate, already_approved_blocks=False)
    except (OSError, models.DocumentError, approve.ApprovalError) as exc:
        logger.warning(f"gate readiness for {gate!r} could not be computed: {exc}")
        blockers = []
    with _readiness_lock:
        _readiness_cache[key] = (time.monotonic(), blockers)
    return blockers


def _status_etag(status: dict[str, object]) -> str:
    """A quoted ETag identifying the *state* a status payload describes, ignoring when it was read."""
    identity = {k: v for k, v in status.items() if k != "generated_at"}
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    return '"' + digest.hexdigest()[:16] + '"'


class UiActionError(Exception):
    """A rejected UI action, carrying the HTTP status to answer with."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def action_argv(action: str, params: dict[str, object]) -> list[str]:
    """Build the command line for a whitelisted action id. Everything else is rejected (400).

    The argv mirrors the documented make targets one-to-one, so what the button runs is exactly what
    the human would have typed; typed parameters are validated and shell-quoted here, never client-side.
    """
    if action == "doctor":
        return ["make", "doctor"]
    if action == "tests":
        # Lets the reviewer confirm green with their own eyes from the review pane (gate ④/⑤)
        # instead of trusting a reported result. Parameterless — zero injection surface.
        return ["make", "test"]
    # There is deliberately no `events_resolve` action: a log an operator can close by hand is
    # not evidence of anything, and dispositions live in `review.yaml`, where they are signed.
    if action == "revise":
        phase = str(params.get("phase") or "")
        if phase not in revise.PHASE_GATE:
            raise UiActionError(
                HTTPStatus.BAD_REQUEST, f"revise 'phase' must be one of {', '.join(sorted(revise.PHASE_GATE))}"
            )
        reason = str(params.get("reason") or "").strip()
        if not reason:
            raise UiActionError(HTTPStatus.BAD_REQUEST, "revise needs a non-empty 'reason'")
        return ["make", "revise", f"ARGS=--to {phase} --reason {shlex.quote(reason)}"]
    if action == "cycle_close":
        slug = str(params.get("slug") or "")
        if not _SLUG_RE.match(slug):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "cycle_close 'slug' must match [a-z0-9][a-z0-9-]*")
        return ["make", "cycle-close", f"NAME={slug}"]
    raise UiActionError(HTTPStatus.BAD_REQUEST, f"unknown action '{action}'")


class DashboardServer(ThreadingHTTPServer):
    """The HTTP server plus the per-start context the handler needs (root, token, read_only).

    ``registry_path`` points at the user-global project registry so the dashboard can enumerate and
    switch targets. When it is None the server runs in *pinned* single-project mode: an ephemeral
    one-entry registry built from ``root`` (used by the direct-construction unit tests, and whenever
    no registry file backs the session), and ``/api/project/select`` is refused.
    """

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], *, root: Path, read_only: bool, registry_path: Path | None = None
    ) -> None:
        super().__init__(address, DashboardHandler)
        self.root = Path(root)
        self.read_only = read_only
        self.registry_path = registry_path
        self.token = secrets.token_hex(16)

    def registry(self) -> registry_mod.Registry:
        """The live registry: the backing file when one is set and non-empty, else a pinned view of root."""
        if self.registry_path is not None:
            reg = registry_mod.load(self.registry_path)
            if reg.projects:
                return reg
        name = registry_mod.slug_for(self.root)
        return registry_mod.Registry(projects={name: self.root}, active=name)

    def active_root(self) -> Path:
        """The repository every read/action currently targets — the registry's active project, or root."""
        reg = self.registry()
        if reg.active and reg.active in reg.projects:
            return reg.projects[reg.active]
        return self.root


def _tail(text: str) -> str:
    return text if len(text) <= _OUTPUT_LIMIT else "…(truncated)…\n" + text[-_OUTPUT_LIMIT:]


def _review_subjects(body: dict[str, object]) -> list[str]:
    """The id a human-review write is about, for the audit event's subject_ids (best-effort)."""
    for key in ("challenge_id", "subject_id", "card_id", "domain"):
        value = body.get(key)
        if isinstance(value, str) and value:
            return [value]
    return []


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer  # narrowed: only DashboardServer constructs this handler

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass  # keep the terminal quiet; the page itself is the monitor

    def _send(self, code: HTTPStatus, body: bytes, ctype: str, *, etag: str | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: HTTPStatus, obj: dict[str, object]) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        if self.path == "/":
            try:
                page = (ASSETS_DIR / "index.html").read_text(encoding="utf-8")
            except OSError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"cannot read ui_assets/index.html: {exc}"})
                return
            # The per-start token and mode are the only server-rendered values in the page.
            page = page.replace("__TOKEN__", self.server.token).replace(
                "__READ_ONLY__", "true" if self.server.read_only else "false"
            )
            self._send(HTTPStatus.OK, page.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/assets/"):
            name = self.path[len("/assets/") :]
            ctype = _ASSET_TYPES.get(name)
            if ctype is None:  # exact-name allowlist — anything else (including ../) is a 404
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                body = (ASSETS_DIR / name).read_bytes()
            except OSError as exc:
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": f"cannot read ui_assets/{name}: {exc}"})
                return
            self._send(HTTPStatus.OK, body, ctype)
        elif self.path == "/api/status":
            self._send_status()
        elif self.path == "/api/projects":
            reg = self.server.registry()
            self._send_json(HTTPStatus.OK, {"projects": reg.entries(), "active": reg.active})
        elif self.path == "/api/events" or self.path.startswith("/api/events?"):
            self._send_events()
        elif self.path.startswith("/api/review/"):
            self._send_review_get(self.path[len("/api/review/") :])
        else:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def _send_status(self) -> None:
        """The status payload, with an ETag over everything *except* `generated_at`.

        The dashboard polls this endpoint for the whole life of a supervised build, and a run is
        mostly waiting: the SSOT is unchanged on the overwhelming majority of polls. `generated_at`
        is a fresh wall-clock stamp every time, so it must stay out of the identity of the payload —
        otherwise every poll looks like a change and the client re-renders the whole page over
        state that did not move. It stays *in* the body (the page shows "updated Ns ago").

        No browser cache is involved (`Cache-Control: no-store` stands): the client keeps the ETag
        itself and sends it back as `If-None-Match`, which is why this works at all.
        """
        status: dict[str, object]
        try:
            status = status_api.collect_status(
                self.server.active_root(), events_scanner=_scan_cached, readiness_probe=_readiness_cached
            )
        except Exception as exc:  # the dashboard must stay up even over a broken SSOT
            status = {"error": f"{type(exc).__name__}: {exc}"}
        etag = _status_etag(status)
        if self.headers.get("If-None-Match") == etag:
            self.send_response(HTTPStatus.NOT_MODIFIED)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()  # a 304 carries no body, and therefore no Content-Length
            return
        body = json.dumps(status, ensure_ascii=False).encode("utf-8")
        self._send(HTTPStatus.OK, body, "application/json; charset=utf-8", etag=etag)

    def _send_events(self) -> None:
        """The tail of events.ndjson, newest first — the Activity feed watching a headless build.

        `?limit=N` (default 50, capped at 200) bounds the payload; escalation-kind events carry
        `open` so the feed can offer resolve on the ones still pending.
        """
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        try:
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "limit must be an integer"})
            return
        limit = max(1, min(limit, 200))
        root = self.server.active_root()
        events = _load_events_cached(root / ".rein" / "events.ndjson")
        open_ids = {e.id for e in events if e.event in events_mod.ATTENTION_EVENTS}
        self._send_json(
            HTTPStatus.OK,
            {
                "events": [
                    {
                        "id": e.id,
                        "seq": e.seq,
                        "date": e.ts[:10],
                        "ts": e.ts,
                        "event": e.event,
                        "actor": e.actor,
                        "subject_ids": list(e.subject_ids),
                        "detail": e.detail,
                        "needs_decision": e.id in open_ids,
                    }
                    for e in reversed(events[-limit:])
                ],
                "total": len(events),
                # The chain root the feed was rendered from: a viewer can check it against the
                # root a gate receipt bound, which is how a regenerated log stops looking normal.
                "chain_root": event_chain.chain_root(events),
            },
        )

    def _send_review_get(self, suffix: str) -> None:
        """Route a /api/review/ GET: the Challenge-first session, one stage, a reveal, or a gate.

        The Challenge-first paths are matched first and by exact shape; anything else is handed to
        review_api as a gate *name*, so a traversal-shaped suffix is just an unknown gate (404). The
        broad `except` keeps a damaged SSOT from 500-ing the pane, the same posture as /api/status.
        """
        root = self.server.active_root()
        try:
            if suffix == "session":
                self._send_json(HTTPStatus.OK, review_api.review_session(root))
                return
            if suffix.startswith("stage/"):
                self._send_json(HTTPStatus.OK, review_api.stage_data(root, suffix[len("stage/") :]))
                return
            if suffix.startswith("challenge/") and suffix.endswith("/reveal"):
                cid = suffix[len("challenge/") : -len("/reveal")]
                self._send_json(HTTPStatus.OK, review_api.challenge_reveal(root, cid))
                return
            self._send_json(HTTPStatus.OK, review_api.collect_review(root, suffix))
        except review_api.ReviewError as exc:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        except Exception as exc:  # a broken repo must not take the pane down
            self._send_json(HTTPStatus.OK, {"error": f"{type(exc).__name__}: {exc}"})

    def _post_body(self) -> dict[str, object]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "request body must be JSON") from None
        if not isinstance(raw, dict):
            raise UiActionError(HTTPStatus.BAD_REQUEST, "request body must be a JSON object")
        return raw

    def do_POST(self) -> None:
        if self.server.read_only:
            self._send_json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "server is running with --read-only"})
            return
        if self.headers.get("X-Rein-Token") != self.server.token:
            self._send_json(HTTPStatus.FORBIDDEN, {"error": "missing or invalid X-Rein-Token"})
            return
        try:
            if self.path == "/api/gate/approve":
                self._approve_gate(self._post_body())
            elif self.path == "/api/run":
                self._run_action(self._post_body())
            elif self.path == "/api/project/select":
                self._select_project(self._post_body())
            elif self.path.startswith("/api/review/"):
                self._review_post(self.path[len("/api/review/") :], self._post_body())
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
        except UiActionError as exc:
            self._send_json(exc.status, {"error": exc.message})

    def _approve_gate(self, body: dict[str, object]) -> None:
        # A button cannot open a gate. Clicking in a localhost UI is not authentication — the
        # browser proves nothing about who is at the keyboard — so this endpoint reports
        # readiness, shows what an approval would cover, and hands back the command the human
        # runs in their own terminal (plan §7.1).
        gate = str(body.get("gate") or "")
        root = self.server.active_root()
        try:
            repo = repo_mod.Repo(root)
            blockers = approve.readiness(repo, gate)
            subject = approve.approval_subject(repo, gate) if not blockers else None
        except (approve.ApprovalError, models.DocumentError) as exc:
            raise UiActionError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        self._send_json(
            HTTPStatus.OK,
            {
                "ok": not blockers,
                "gate": gate,
                "blockers": blockers,
                "covers": subject,
                "next": [f"rein approve {gate}", "(then type the gate name at the prompt)"],
            },
        )

    # -- Challenge-first human-review writes (plan §21.1) --
    #
    # Every write is one Store transaction that refuses a stale machine digest (409) and records an
    # audit event beside the change — a human answering a challenge cannot make the machine review
    # stale (E2E-09), and a machine review regenerated under the reviewer's feet refuses their next
    # write (E2E-08). The machine half is copied through byte-for-byte, so only `human` moves.

    def _review_post(self, action: str, body: dict[str, object]) -> None:
        handlers = {
            "challenge": self._review_challenge,
            "decision": self._review_decision,
            "counterfactual": self._review_counterfactual,
            "expertise": self._review_expertise,
            "disposition": self._review_disposition,
            "expert": self._review_expert,
            "complete": self._review_complete,
        }
        handler = handlers.get(action)
        if handler is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": f"unknown review action '{action}'"})
            return
        handler(body)

    def _review_mutate(
        self,
        body: dict[str, object],
        event: str,
        apply: Callable[[models.Review, dict[str, object]], dict[str, object]],
    ) -> None:
        """Persist one human-half change through the Store, guarding staleness and recording why."""
        # Required, not optional: `assert_machine_current` used to wave through an empty digest,
        # so a client that simply omitted the field — a stale tab, a script, a retried request —
        # got its answers merged into whatever machine review happened to be on disk. That is the
        # exact E2E-08 failure the guard exists to stop, reachable by leaving a field out.
        expected = str(body.get("machine_digest") or "")
        if not expected:
            raise UiActionError(
                HTTPStatus.BAD_REQUEST,
                "machine_digest is required — an answer has to name the machine review it is about",
            )
        store = store_mod.Store(repo_mod.Repo(self.server.active_root()))
        try:
            with store.transaction() as tx:
                review = tx.store.read_review()
                if review is None or not review.is_generated:
                    raise UiActionError(HTTPStatus.BAD_REQUEST, "no machine review to answer")
                human_review.assert_machine_current(review, expected)
                new_human = apply(review, dict(review.human))
                state = tx.store.read_state()
                tx.write(
                    "review",
                    {**review.raw, "human": new_human},
                    expect_digest=store_mod.read_digest(review),
                )
                tx.append(event, cycle_id=state.cycle_id if state else "", subject_ids=_review_subjects(body))
            fresh = store.read_review()
            self._send_json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "machine_digest": fresh.machine_digest() if fresh else "",
                    "session": review_api.review_session(self.server.active_root()),
                },
            )
        except human_review.StaleReview as exc:
            raise UiActionError(HTTPStatus.CONFLICT, str(exc)) from None
        except (store_mod.StoreError, models.DocumentError, ValueError) as exc:
            raise UiActionError(HTTPStatus.BAD_REQUEST, str(exc)) from None

    def _review_challenge(self, body: dict[str, object]) -> None:
        # `confidence` used to default to "low" here while the pane sent a hardcoded "medium", so the
        # recorded number was whichever of two fabrications won. How sure the reviewer was is theirs
        # to state; a server that guesses it is writing a human judgement nobody made.
        cid, choice = str(body.get("challenge_id") or ""), str(body.get("choice") or "")
        confidence, rationale = str(body.get("confidence") or ""), str(body.get("rationale") or "")
        if not confidence:
            raise UiActionError(HTTPStatus.BAD_REQUEST, "confidence is required (low, medium or high)")
        self._review_mutate(
            body,
            "challenge_answered",
            lambda review, human: human_review.record_challenge_answer(
                review, human, cid, choice, confidence=confidence, rationale=rationale
            ),
        )

    def _review_decision(self, body: dict[str, object]) -> None:
        # Confidence is required, not defaulted. A default would be this server inventing a human
        # judgement — the same overclaim `epistemic_status` exists to prevent, one layer up.
        cid, choice = str(body.get("card_id") or ""), str(body.get("choice") or "")
        confidence, reason = str(body.get("confidence") or ""), str(body.get("reason") or "")
        if not confidence:
            raise UiActionError(HTTPStatus.BAD_REQUEST, "confidence is required (low, medium or high)")
        self._review_mutate(
            body,
            "decision_recorded",
            lambda review, human: human_review.record_decision(
                review, human, cid, choice, confidence=confidence, reason=reason
            ),
        )

    def _review_counterfactual(self, body: dict[str, object]) -> None:
        cid, model = str(body.get("challenge_id") or ""), str(body.get("corrected_model") or "")
        answer = str(body.get("answer") or "")
        self._review_mutate(
            body,
            "counterfactual_answered",
            lambda _review, human: human_review.record_counterfactual(human, cid, model, answer=answer),
        )

    def _review_expertise(self, body: dict[str, object]) -> None:
        domain, level = str(body.get("domain") or ""), str(body.get("level") or "")
        self._review_mutate(
            body, "expertise_declared", lambda _review, human: human_review.record_expertise(human, domain, level)
        )

    def _review_disposition(self, body: dict[str, object]) -> None:
        subject, action = str(body.get("subject_id") or ""), str(body.get("action") or "")
        note = str(body.get("note") or "")
        self._review_mutate(
            body,
            "disposition_recorded",
            lambda _review, human: human_review.record_disposition(human, subject, action, note=note),
        )

    def _review_expert(self, body: dict[str, object]) -> None:
        domain, reason = str(body.get("domain") or ""), str(body.get("reason") or "")
        subjects = body.get("subject_ids")
        subject_ids = [str(s) for s in subjects] if isinstance(subjects, list) else []
        self._review_mutate(
            body,
            "expert_requested",
            lambda _review, human: human_review.request_expert(human, domain, subject_ids, reason=reason),
        )

    def _review_complete(self, body: dict[str, object]) -> None:
        self._review_mutate(body, "human_review_frozen", lambda review, human: human_review.freeze(review, human))

    def _select_project(self, body: dict[str, object]) -> None:
        # The client sends only a registered *name*; the server maps it to a root through the
        # registry, never a browser-supplied path — so switching cannot widen the dashboard's reach.
        if self.server.registry_path is None:
            raise UiActionError(
                HTTPStatus.CONFLICT, "this dashboard is pinned to a single repository — no project registry"
            )
        name = str(body.get("name") or "")
        reg = registry_mod.load(self.server.registry_path)
        try:
            reg.set_active(name)
        except registry_mod.RegistryError as exc:
            raise UiActionError(HTTPStatus.BAD_REQUEST, str(exc)) from None
        registry_mod.save(reg, self.server.registry_path)
        self._send_json(HTTPStatus.OK, {"ok": True, "active": name, "root": str(reg.projects[name])})

    def _run_action(self, body: dict[str, object]) -> None:
        action = str(body.get("action") or "")
        params = body.get("params")
        argv = action_argv(action, params if isinstance(params, dict) else {})
        try:
            proc = subprocess.run(
                argv, cwd=self.server.active_root(), capture_output=True, text=True, timeout=ACTION_TIMEOUT_SEC
            )
        except subprocess.TimeoutExpired:
            raise UiActionError(
                HTTPStatus.GATEWAY_TIMEOUT, f"'{' '.join(argv)}' timed out after {ACTION_TIMEOUT_SEC}s"
            ) from None
        except OSError as exc:
            raise UiActionError(HTTPStatus.INTERNAL_SERVER_ERROR, f"cannot launch '{argv[0]}': {exc}") from None
        self._send_json(
            HTTPStatus.OK,
            {
                "action": action,
                "argv": argv,
                "exit_code": proc.returncode,
                "stdout": _tail(proc.stdout),
                "stderr": _tail(proc.stderr),
            },
        )


def open_mode(no_open: bool, term_program: str | None) -> str:
    """Decide how to surface the URL at startup (kept pure so the choice is unit-testable).

    Inside VS Code's integrated terminal (`TERM_PROGRAM=vscode`) opening the system browser is the
    wrong target — under WSL it launches the Windows browser, away from the editor — so we point the
    user at the built-in Simple Browser / Ports preview instead. `--no-open` overrides both.
    """
    if no_open:
        return "none"
    if term_program == "vscode":
        return "vscode"
    return "browser"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="local dashboard for the Loose Rein SSOT")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="port (default 8765; 0 = ephemeral)")
    parser.add_argument("--root", "--repo", dest="root", default="", help="repository root (default: discovered)")
    parser.add_argument(
        "--no-open", action="store_true", help="do not open the browser automatically (VS Code: use Simple Browser)"
    )
    parser.add_argument("--read-only", action="store_true", help="disable the action endpoints (view only)")
    parser.add_argument("--once", action="store_true", help="print the status JSON and exit (no server)")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="required to bind a non-loopback host while the write endpoints are enabled",
    )
    args = parser.parse_args(argv)
    common.configure_logging()
    reg_path = registry_mod.registry_path()
    try:
        root = repo_mod.get(args.root or None).root
        discovered = True
    except repo_mod.RepoNotFoundError as exc:
        # Not inside a repo — but a populated registry still lets us serve a picked project, so the
        # dashboard can be launched from anywhere once projects have been registered.
        reg = registry_mod.load(reg_path)
        picked = reg.active or next(iter(reg.projects), None)
        if picked is None:
            logger.error(str(exc))
            return 1
        root = reg.projects[picked]
        discovered = False

    if args.once:  # a scripting/inspection path — never mutate the registry
        print(json.dumps(status_api.collect_status(root), ensure_ascii=False, indent=2))
        return 0

    if args.host not in _LOOPBACK_HOSTS and not args.read_only and not args.allow_remote:
        logger.error(
            f"refusing to bind {args.host}: the write endpoints (gate approval, actions) would be"
            " exposed beyond this machine. Add --read-only, or --allow-remote if that is intended."
        )
        return 2

    if discovered:
        # Record the launched-from repo and make it the active target (least surprise: `rein ui`
        # inside repo B shows repo B), while keeping every other registered project in the switcher.
        reg = registry_mod.load(reg_path)
        reg.active = registry_mod.record_use(reg, root)
        registry_mod.save(reg, reg_path)

    server = DashboardServer((args.host, args.port), root=root, read_only=args.read_only, registry_path=reg_path)
    url = f"http://{args.host}:{server.server_address[1]}/"
    mode = " (read-only)" if args.read_only else ""
    print(f"Loose Rein dashboard{mode}: {url}  — Ctrl+C to stop")
    if open_mode(args.no_open, os.environ.get("TERM_PROGRAM")) == "vscode":
        print("  VS Code detected — open it inside the editor: Ctrl+Shift+P → 'Simple Browser: Show'")
        print("  and paste the URL above (or use the PORTS panel's 'Preview in Editor').")
    elif open_mode(args.no_open, os.environ.get("TERM_PROGRAM")) == "browser":
        webbrowser.open(url)  # best-effort; under WSL the printed URL is the fallback
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
