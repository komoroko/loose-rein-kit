"""Verify ui.py: the action whitelist and the HTTP surface (deterministic, offline).

The gate-approval rewrite itself lives in approve.py (the single sanctioned write path) and is
unit-tested in test_approve.py; here only the endpoint's delegation behavior is asserted."""

from __future__ import annotations

import http.client
import json
import logging
import re
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from rein import models, registry, store, ui
from tests._support import SANDBOXED_PROFILES, chain, make_config, make_state, seed_repo

# The frontend's source. `ui_assets/app.js` is a built bundle — minified React plus this code — so a
# canary about what the page *says* reads the sources it was built from, and only the canaries about
# what the server *serves* read the bundle. `make check` rebuilds and compares the two, so a source
# these canaries pass over is the source the shipped bundle came from.
UI_SRC = Path(__file__).resolve().parents[1] / "ui"


def ui_sources() -> dict[str, str]:
    return {
        str(p.relative_to(UI_SRC)): p.read_text(encoding="utf-8")
        for p in sorted(UI_SRC.rglob("*"))
        if p.suffix in (".js", ".jsx")
    }


# --- action_argv: the fixed whitelist ------------------------------------------


def test_action_argv_whitelist() -> None:
    assert ui.action_argv("doctor", {}) == ["make", "doctor"]
    assert ui.action_argv("tests", {}) == ["make", "test"]  # parameterless: zero injection surface
    argv = ui.action_argv("revise", {"phase": "design", "reason": "rethink auth"})
    assert argv[:2] == ["make", "revise"] and "--to design" in argv[2]
    assert "'rethink auth'" in argv[2]  # free text is shell-quoted server-side
    assert ui.action_argv("cycle_close", {"slug": "payment-refactor"}) == [
        "make",
        "cycle-close",
        "NAME=payment-refactor",
    ]


@pytest.mark.parametrize(
    ("action", "params"),
    [
        ("rm_rf", {}),  # not on the whitelist
        # There is no `events --resolve`; a dashboard button that outlived such a verb would post an
        # action that could only ever fail. The action is gone, so it is now simply not on the
        # whitelist — a log an operator can close by hand is not evidence of anything.
        ("events_resolve", {"id": 3, "note": "fixed"}),
        ("revise", {"phase": "verify", "reason": "x"}),  # not a roll-back target
        ("revise", {"phase": "design", "reason": "  "}),  # empty reason
        ("cycle_close", {"slug": "Bad Slug!"}),  # invalid slug characters
        ("cycle_close", {"slug": "x; rm -rf /"}),  # injection attempt
    ],
)
def test_action_argv_rejects_invalid(action: str, params: dict[str, object]) -> None:
    with pytest.raises(ui.UiActionError) as exc:
        ui.action_argv(action, params)
    assert exc.value.status == 400


# --- HTTP surface ---------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolate_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every ui.main / registry write off the developer's real ~/.config during tests."""
    monkeypatch.setenv("REIN_CONFIG_HOME", str(tmp_path / "cfg"))


def _seed_repo(base: Path, project: str) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    seed_repo(
        base,
        state=make_state(
            project=project,
            gates=dict.fromkeys(models.GATE_ORDER, "pending"),
            phase="requirements",
            plan_status="draft",
        ),
        config=make_config(profiles=SANDBOXED_PROFILES),
    )
    return base


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _seed_repo(tmp_path, "demo")


@pytest.fixture
def server(repo: Path) -> Iterator[ui.DashboardServer]:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def session_for(srv: ui.DashboardServer) -> str:
    """Redeem this server's launch secret, exactly as opening the printed link does.

    Cached per server because the secret is single-use: a test making two writes is one browser
    holding one session, not two redemptions.
    """
    cached = getattr(srv, "_test_session", None)
    if cached is None:
        cached = srv.redeem(srv.launch_secret)
        assert cached, "the launch secret must be redeemable once"
        srv._test_session = cached  # type: ignore[attr-defined]
    return cached


def write(srv: ui.DashboardServer, path: str, body: dict[str, object] | None = None) -> tuple[int, bytes]:
    """An authorized POST: the write session plus the CSRF token, as the real page sends them."""
    return _request(srv, "POST", path, body, token=srv.token, session=session_for(srv))


def _request(
    srv: ui.DashboardServer,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    token: str | None = None,
    session: str | None = None,
) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Rein-Token"] = token
    if session:
        headers["Cookie"] = f"{ui.SESSION_COOKIE}={session}"
    conn.request(method, path, json.dumps(body) if body is not None else None, headers)
    res = conn.getresponse()
    data = res.read()
    conn.close()
    return res.status, data


class _Stream:
    """An open `/api/stream`, read one event at a time.

    SSE has no request/response pairing to assert on: what a test needs is "the next thing the
    server said", under a deadline, so a stream that correctly says nothing fails as a timeout
    instead of hanging the suite. `timeout` is the socket's, so it is also how long silence is
    allowed to last before :meth:`next_event` gives up.
    """

    def __init__(self, srv: ui.DashboardServer, timeout: float = 20.0) -> None:
        self.conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=timeout)
        self.conn.request("GET", "/api/stream")
        self.res = self.conn.getresponse()
        assert self.res.status == 200
        assert (self.res.getheader("Content-Type") or "").startswith("text/event-stream")

    def next_event(self, name: str | None = None) -> tuple[str, dict[str, Any]]:
        """The next event, or the next one called `name`. Comments and `retry:` are not events."""
        current: str | None = None
        while True:
            raw = self.res.readline()
            if not raw:
                raise AssertionError(f"the stream ended before a {name or 'named'} event arrived")
            line = raw.decode("utf-8").rstrip("\r\n")
            if line.startswith("event: "):
                current = line[len("event: ") :]
            elif line.startswith("data: ") and current is not None:
                got, current = (current, json.loads(line[len("data: ") :])), None
                if name is None or got[0] == name:
                    return got

    def opening(self) -> dict[str, Any]:
        """The `status` the server sends on connect, skipping the `record` that accompanies it."""
        return self.next_event("status")[1]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_the_stream_opens_by_saying_what_is_true_now(server: ui.DashboardServer) -> None:
    with _Stream(server) as stream:
        payload = stream.opening()
    # This fixture stands at gate ① with nothing mechanical in the way, so the recommendation is
    # the human's decision — not "/req" again. The dashboard's waiting-state signals key off it.
    assert payload["next"]["command"] == "rein approve requirements"
    assert payload["decision"]["waiting_on_human"] is True
    assert payload["project"] == "demo"


def test_get_page_is_offline_self_contained(server: ui.DashboardServer) -> None:
    status, data = _request(server, "GET", "/", session=session_for(server))
    page = data.decode("utf-8")
    assert status == 200 and "Loose Rein" in page
    assert server.token in page  # the POST token reaches a page that holds a write session
    assets = {name: _request(server, "GET", f"/assets/{name}")[1].decode("utf-8") for name in ui._ASSET_TYPES}

    # Nothing this repository writes may name an absolute URL at all.
    for name, text in {"index.html": page, **ui_sources()}.items():
        assert "http://" not in text and "https://" not in text, name
        assert "//cdn" not in text and "@import" not in text, name

    # The bundle carries React's own strings, and this is the exact set of absolute URLs in it —
    # an exact set, so a sixth one fails here rather than being waved through by a prefix rule.
    # None is a request: an XML namespace URI is an identifier that is never dereferenced, and the
    # react.dev link is text inside a minified error message.
    INERT = {
        "http://www.w3.org/1998/Math/MathML",
        "http://www.w3.org/1999/xlink",
        "http://www.w3.org/2000/svg",
        "http://www.w3.org/XML/1998/namespace",
        "https://react.dev/errors/",
    }
    assert set(re.findall(r"https?://[^\"' )]*", assets["app.js"])) == INERT
    assert "http" not in assets["app.css"]

    # Every URL the page names is same-origin: an attribute in the page, a literal in the sources.
    # The sources are where this is asked rather than the bundle, because minification leaves
    # fragments of React's own regexes looking like paths, and a canary that has to allow "/$" is
    # not checking anything. What the bundle answers for is the exact set above.
    for url in re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', page):
        assert url.startswith(("/assets/", "#")), f"index.html references {url}"
    for name, text in ui_sources().items():
        for url in re.findall(r'"(/[^"\s]*)"', text):
            assert url.startswith(("/assets/", "/api/")), f"{name} names {url}"

    # The page is an empty root: everything below is rendered by the bundle, so the markers that
    # prove the screens exist belong to the sources it was built from.
    assert '<div id="root"></div>' in page
    sources = "".join(ui_sources().values())
    for marker in ('id="stepper"', 'id="trace"', 'id="rvMain"', 'id="tabs"', 'id="toasts"', "data-theme"):
        assert marker in sources, marker


def test_the_page_puts_no_repository_string_in_the_dom_as_html() -> None:
    """The page holds the approval token, so an XSS here is a self-approval.

    Task ids, claim ids, gap ids and reviewer prose are all agent-written and none is pattern-
    validated on load (dag.py takes `str(raw["id"])` as-is). The old page built HTML by string
    concatenation and escaped each interpolation by hand — 103 calls, any one of which could be
    forgotten. JSX escapes every interpolation by construction, so what is left to police is the
    deliberate opt-out, and there are exactly two: the deliverable body and the phase agent's
    self-assessment, both rendered server-side by mdlite, which escapes at the source.
    """
    raw = [
        (name, line.strip())
        for name, text in ui_sources().items()
        for line in text.splitlines()
        if ("dangerouslySetInnerHTML" in line or ".innerHTML" in line) and not line.lstrip().startswith("//")
    ]
    assert [name for name, _ in raw] == ["gate/Deliverables.jsx", "gate/Deliverables.jsx"], raw
    assert all("__html: sa.html" in line or "__html: entry.html" in line for _, line in raw), raw


def test_every_task_status_is_styled_by_the_name_the_page_emits() -> None:
    """The status string reaches the DOM verbatim (`esc(tk.status)`), so a stylesheet spelling it
    any other way styles nothing.

    This happened: the DAG rules said `.nd.in_progress` — Mermaid's spelling, where `-` cannot
    appear in an identifier (`dag_render._node_key`) — while the class emitted was `in-progress`.
    A running task's node matched no `fill` rule and fell through to the SVG default, black, on
    the one status a person watching a build is looking for.
    """
    css = (ui.ASSETS_DIR / "app.css").read_text(encoding="utf-8")
    for status in models.TASK_STATUS_ORDER:
        for selector in (f"svg.dag .nd.{status}", f".seg.{status}", f".chip.{status}"):
            if status == "todo":
                continue  # todo is the unqualified base rule of all three, by design
            assert selector in css, f"{selector} is not styled — a {status} task would render unstyled"
    assert "in_progress" not in css, "the underscored spelling is Mermaid's, and matches nothing in the DOM"


def test_the_graph_says_what_its_lines_mean() -> None:
    """An edge drawn without a head or a key is a line between two boxes: the reader cannot tell
    which end must finish first, nor that a column is an execution layer."""
    js = (UI_SRC / "Board.jsx").read_text(encoding="utf-8")
    assert "markerEnd" in js and "<marker" in js
    assert "blocked_by" in js and "execution layers" in js and "critical path" in js


def test_shipped_assets_match_the_allowlist_exactly() -> None:
    # _ASSET_TYPES is a hand-maintained allowlist (auditability over convenience); this catches a
    # file added to ui_assets/ but forgotten in the dict — which would 404 at runtime — and vice versa.
    on_disk = {p.name for p in ui.ASSETS_DIR.iterdir() if p.is_file()}
    assert on_disk == set(ui._ASSET_TYPES) | {"index.html"}


def test_the_as_built_route_reaches_review_api_rather_than_the_gate_list(server: ui.DashboardServer) -> None:
    """Wiring, and the reason the wiring matters.

    `/api/review/<anything>` is otherwise read as a *gate name*, so without the prefix match an
    as-built request would come back as a 200-shaped "unknown gate" and the pane would render an
    empty document instead of saying it could not read the file. The refusal is the point: this
    route reads blobs out of the repository, and it may only read what the stored brief published.
    """
    status, data = _request(server, "GET", "/api/review/as-built/db%2Fschema.sql")
    assert status == 404
    assert b"nothing bound to a commit" in data


# --- the payload contract between the modules and the server --------------------
#
# The drift this catches actually happened: `_tasks_block` renamed its task list to `rows` and
# `trace` collapsed to a verdict, while the Board kept reading `tasks.tasks` and
# `trace.requirements`. Nothing failed — no test asserted that a field a module reads exists — so
# the Tasks tab threw a TypeError inside `refresh()`, whose catch reports "disconnected". A pane
# that silently stops rendering is worse than one that 500s, because the dashboard still looks alive.


def _repo_with_tasks(tmp_path: Path) -> Path:
    from tests._support import make_claim, make_plan, make_task

    base = tmp_path / "threaded"
    base.mkdir(parents=True, exist_ok=True)
    seed_repo(
        base,
        state=make_state(
            project="demo",
            gates=dict.fromkeys(models.GATE_ORDER, "pending"),
            phase="build",
            plan_status="frozen",
            tasks={"T-001": "done", "T-002": "in-progress"},
        ),
        config=make_config(profiles=SANDBOXED_PROFILES),
        plan=make_plan(
            claims=[make_claim("C-001", requirement_ids=["R-1"])],
            tasks=[make_task("T-001", claim_ids=["C-001"]), make_task("T-002", claim_ids=["C-001"])],
        ),
    )
    return base


def test_status_payload_carries_every_field_the_modules_read(tmp_path: Path) -> None:
    """Each key below is read by name in ui_assets/*.js; a rename on either side must fail here."""
    from rein import status_api

    payload = status_api.collect_status(_repo_with_tasks(tmp_path))

    tasks = payload["tasks"]
    assert isinstance(tasks, dict)
    # Board.jsx: Dag/Layers/pills; Now.jsx: InTheWay
    for key in ("rows", "counts", "total", "layers", "critical_path", "frontier"):
        assert key in tasks, f"status.tasks.{key}"
    for key in ("id", "title", "kind", "status", "risk", "blocked_by", "claim_ids", "handoff", "commit"):
        assert key in tasks["rows"][0], f"status.tasks.rows[].{key}"
    # the pills and the layer bar index counts by the status vocabulary itself
    assert set(tasks["counts"]) == set(models.TASK_STATUS_ORDER)

    trace = payload["trace"]
    assert isinstance(trace, dict)
    for key in ("ok", "errors", "warnings", "requirements"):
        assert key in trace, f"status.trace.{key}"
    for key in ("id", "nfr", "claims", "tasks"):
        assert key in trace["requirements"][0], f"status.trace.requirements[].{key}"

    # Now.jsx InTheWay / notify.js snapshot
    assert isinstance(payload["attention"], list)
    for key in ("gates", "warnings", "next", "project", "current_phase", "phase_order", "decision"):
        assert key in payload, f"status.{key}"
    decision = payload["decision"]
    assert isinstance(decision, dict)
    for key in ("id", "waiting_on_human", "kind", "headline", "action"):
        assert key in decision, f"status.decision.{key}"


def test_the_pending_decision_is_one_identity_not_a_stream_of_events() -> None:
    """The notifier interrupts on `decision.id` changing; a decision the loop re-derives is silent."""
    from rein import status_api

    approve = status_api.Recommendation(command="rein approve build", kind="approve_gate", reason="ready")
    first = status_api.pending_decision(approve, "build")
    assert first["waiting_on_human"] is True and first["id"]
    # the same decision, re-derived on the next poll, is the same identity
    assert status_api.pending_decision(approve, "build")["id"] == first["id"]
    # a different gate is a different call to make
    assert status_api.pending_decision(approve, "release")["id"] != first["id"]
    # work for the agent is not an interruption for the human
    run = status_api.Recommendation(command="/build", kind="run_phase", reason="phase in progress")
    assert status_api.pending_decision(run, "build") == {
        "id": "",
        "waiting_on_human": False,
        "kind": "run_phase",
        "headline": "phase in progress",
        "action": "/build",
        "blocking": 0,
        "open": 0,
    }


def test_the_queue_counts_the_decision_without_choosing_it() -> None:
    """The queue may not pick what interrupts: that would jitter the ping and split the advice.

    `rein next` and the dashboard must recommend the same command, so the decision stays
    derived from the decision table. What the queue adds is how much stands behind it.
    """
    from rein import status_api

    run = status_api.Recommendation(command="/build", kind="run_phase", reason="phase in progress")
    queue = [
        {"id": "a", "severity": "blocking", "kind": "gate_blocker", "subject": "build", "headline": "x", "action": ""},
        {"id": "b", "severity": "attention", "kind": "escalation", "subject": "T-1", "headline": "y", "action": ""},
        {"id": "c", "severity": "info", "kind": "ungrounded", "subject": "C-1", "headline": "z", "action": ""},
    ]
    decision = status_api.pending_decision(run, "build", pending=queue)
    assert decision["action"] == "/build"  # still the decision table's answer, not queue[0]'s
    assert decision["blocking"] == 1
    assert decision["open"] == 2  # info is context, not something waiting on a person


def test_event_feed_payload_carries_every_field_the_feed_reads(server: ui.DashboardServer, repo: Path) -> None:
    _seed_events(repo, count=3)
    status, data = _request(server, "GET", "/api/events")
    assert status == 200
    payload = json.loads(data)
    for key in ("events", "total", "chain_root"):
        assert key in payload, key
    assert payload["events"], "events were just seeded, so the feed must not be empty"
    for key in ("seq", "date", "event", "actor", "subject_ids", "detail", "needs_decision"):
        assert key in payload["events"][0], f"events[].{key}"


def test_no_module_reads_a_field_the_payload_stopped_carrying() -> None:
    """Spelling canaries for the renames that already bit, so they cannot come back quietly."""
    sources = ui_sources()
    forbidden = {
        "tasks.tasks": "the task list is `tasks.rows`",
        "in_progress": "the status is spelled `in-progress` (models.TASK_STATUS_ORDER)",
        ".escalations": "events awaiting a human arrive as `attention`",
        "d.logs": "there is no `logs` block; those logs live in docs/",
        "e.open": "the feed's open flag is `needs_decision`",
    }
    for name, text in sources.items():
        for token, why in forbidden.items():
            assert token not in text, f"{name} reads `{token}` — {why}"


def test_the_page_does_not_tell_the_human_to_go_and_do_what_it_just_did() -> None:
    """`/api/gate/approve` records the approval; the page must say so.

    It did not. The endpoint began life as a readiness check, and `post()` in api.js kept the
    message that belonged to that contract — so a human clicked Approve, the gate actually opened,
    and the toast said "is ready — run the command shown to approve". The board refreshed to a ✓ in
    the same breath, leaving the two halves of the page disagreeing about the one judgement in this
    product that widens what happens next.

    A refusal never reaches that branch: an unready gate and a moved repository are both 409s
    carrying `error`. So `approval_id` in the body means it happened, and the only honest toast
    names it.
    """
    api = (UI_SRC / "api.js").read_text(encoding="utf-8")
    assert "run the command shown to approve" not in api
    assert "approval_id" in api, "the toast has to read the field that proves the approval was recorded"
    assert "(data.blockers" not in api, "a POST never carries blockers — an unready gate is a 409"


def test_every_api_route_the_server_dispatches_is_reached_from_the_page() -> None:
    """A route with no caller is dead weight; a caller with no route is a 404 the human meets.

    Both directions, read out of the two sources rather than a hand-kept list: `do_GET`/`do_POST`
    match `self.path` against literals, and the modules build their URLs from literals too.
    """
    source = Path(ui.__file__).read_text(encoding="utf-8")
    routes = {m.rstrip("?") for m in re.findall(r'self\.path(?:\s*==\s*|\.startswith\()\s*"(/api/[^"]*)"', source)}
    assert routes, "no /api/ route literals found — this canary would pass on anything"
    bundle = "".join(ui_sources().values()) + (ui.ASSETS_DIR / "index.html").read_text(encoding="utf-8")
    for route in sorted(routes):
        assert route in bundle, f"{route} is dispatched by ui.py but nothing on the page calls it"
    for called in sorted(set(re.findall(r'"(/api/[^"?]*)"', bundle))):
        assert any(called.startswith(r) for r in routes), f"the page calls {called}, which ui.py does not dispatch"


def test_assets_are_served_with_their_types_and_nothing_else(server: ui.DashboardServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", "/assets/app.css")
    res = conn.getresponse()
    assert res.status == 200 and res.getheader("Content-Type", "").startswith("text/css")
    res.read()
    conn.close()
    # anything off the exact-name allowlist is a 404 (including traversal shapes)
    for path in ("/assets/nope.js", "/assets/../ui.py", "/assets/app.js.bak"):
        assert _request(server, "GET", path)[0] == 404, path


def test_get_unknown_path_is_404(server: ui.DashboardServer) -> None:
    assert _request(server, "GET", "/nope")[0] == 404


def test_post_without_token_is_403(server: ui.DashboardServer) -> None:
    status, _ = _request(server, "POST", "/api/run", {"action": "doctor", "params": {}})
    assert status == 403


def test_post_unknown_action_is_400(server: ui.DashboardServer) -> None:
    status, _ = write(server, "/api/run", {"action": "nope", "params": {}})
    assert status == 400


# --- gate approval, and where the authority to record it comes from ------------------
#
# The doctrine this replaces said "a localhost click is not authentication" while embedding the
# CSRF token in the served page — so anything able to `curl` that page could write, including the
# gate-④ human-review writes the gate requires. The correction is not about proving a human,
# which nothing here can do. It is about the channel the capability travels over: the launch link
# is printed to the terminal `rein ui` runs in, and a captured subprocess cannot read that.


def _readiness(srv: ui.DashboardServer, gate: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(_request(srv, "GET", f"/api/gate/{gate}/readiness")[1])
    return payload


def test_a_gate_is_approved_from_the_pane_that_showed_it(server: ui.DashboardServer, repo: Path) -> None:
    from rein import repo as repo_mod
    from rein import store as store_mod

    ready = _readiness(server, "requirements")
    assert ready["ok"] and ready["covers"]

    status, body = write(server, "/api/gate/approve", {"gate": "requirements", "covers": ready["covers"]})
    assert status == 200, body
    approval_id = json.loads(body)["approval_id"]

    state = store_mod.Store(repo_mod.Repo(repo)).read_state()
    assert state is not None and state.gate_status("requirements") == "approved"
    receipt = state.gate_receipt("requirements") or {}
    assert receipt["approval_id"] == approval_id
    # The receipt says which channel carried the confirmation rather than flattening both into an
    # unqualified "approved" — neither is proof of a human, and a later reader must see which.
    assert receipt["confirmed_via"] == "ui-session"


def test_an_approval_that_does_not_name_what_it_covers_is_refused(server: ui.DashboardServer) -> None:
    assert write(server, "/api/gate/approve", {"gate": "requirements"})[0] == 400


def test_an_approval_is_refused_when_the_repository_moved_under_it(server: ui.DashboardServer, repo: Path) -> None:
    """The digests on screen are what the approval binds. If they moved while the human was
    reading them, recording would cover bytes nobody saw."""
    ready = _readiness(server, "requirements")
    stale = {**ready["covers"], "plan_digest": "sha256:" + "0" * 64}
    status, body = write(server, "/api/gate/approve", {"gate": "requirements", "covers": stale})
    assert status == 409
    assert "moved while this gate was on screen" in json.loads(body)["error"]


def test_a_blocked_gate_is_refused_rather_than_recorded(server: ui.DashboardServer, repo: Path) -> None:
    from rein import repo as repo_mod
    from rein import store as store_mod

    ready = _readiness(server, "build")
    assert ready["ok"] is False and ready["blockers"] and ready["covers"] is None

    status, _ = write(server, "/api/gate/approve", {"gate": "build", "covers": {}})
    assert status == 409
    state = store_mod.Store(repo_mod.Repo(repo)).read_state()
    assert state is not None and state.gate_status("build") == "pending"


def test_fetching_the_page_yields_no_way_to_write(server: ui.DashboardServer, repo: Path) -> None:
    """The two-curl attack, and the whole reason the in-page token was never authority: any local
    process could fetch `/`, read the token out of it, and post."""
    from rein import repo as repo_mod
    from rein import store as store_mod

    page = _request(server, "GET", "/")[1].decode("utf-8")
    assert server.token not in page
    assert "window.READ_ONLY = true" in page

    ready = _readiness(server, "requirements")
    status, _ = _request(
        server, "POST", "/api/gate/approve", {"gate": "requirements", "covers": ready["covers"]}, token=server.token
    )
    assert status == 403
    state = store_mod.Store(repo_mod.Repo(repo)).read_state()
    assert state is not None and state.gate_status("requirements") == "pending"


def test_the_session_and_the_token_are_both_required(server: ui.DashboardServer) -> None:
    body: dict[str, object] = {"gate": "requirements", "covers": {}}
    assert _request(server, "POST", "/api/gate/approve", body, session=session_for(server))[0] == 403
    assert _request(server, "POST", "/api/gate/approve", body, token=server.token)[0] == 403


def test_the_launch_secret_works_once(server: ui.DashboardServer) -> None:
    """Single use is the detection property: if something else redeems it first, the human's
    browser lands on a read-only page instead of their dashboard."""
    assert server.redeem(server.launch_secret)
    assert server.redeem(server.launch_secret) is None


def test_a_wrong_launch_secret_grants_nothing(server: ui.DashboardServer) -> None:
    assert server.redeem("not-the-secret") is None
    assert server.has_session("invented") is False


def test_the_launch_link_is_what_puts_the_token_in_the_page(server: ui.DashboardServer) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    conn.request("GET", f"/?k={server.launch_secret}")
    res = conn.getresponse()
    page = res.read().decode("utf-8")
    cookie = res.getheader("Set-Cookie") or ""
    conn.close()
    assert server.token in page and "window.READ_ONLY = false" in page
    # HttpOnly so page scripts cannot read it back out; SameSite=Strict so no other origin rides it.
    assert ui.SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie


def test_reloading_the_launch_url_keeps_the_session_and_raises_no_alarm(
    server: ui.DashboardServer, caplog: pytest.LogCaptureFixture
) -> None:
    """The launch URL stays in the address bar, so every reload re-presents a spent secret.

    Two things have to hold, and the second is what the warning is *for*: the reload must still be
    writable (the cookie is the authority, not the query string), and it must stay quiet. A theft
    alarm that fires on F5 is one the human stops reading, which costs the detection property the
    single-use secret exists to provide.
    """
    session = session_for(server)  # the browser's first visit, redeeming the link
    with caplog.at_level(logging.WARNING, logger="rein.ui"):
        page = _request(server, "GET", f"/?k={server.launch_secret}", session=session)[1].decode("utf-8")
    assert server.token in page and "window.READ_ONLY = false" in page
    assert "opened the dashboard first" not in caplog.text

    # Without the session it is the real anomaly — someone holding a spent secret and nothing else.
    with caplog.at_level(logging.WARNING, logger="rein.ui"):
        page = _request(server, "GET", f"/?k={server.launch_secret}")[1].decode("utf-8")
    assert server.token not in page and "window.READ_ONLY = true" in page
    assert "opened the dashboard first" in caplog.text


def test_a_read_only_server_hands_out_no_session(repo: Path) -> None:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        page = _request(srv, "GET", f"/?k={srv.launch_secret}")[1].decode("utf-8")
        assert srv.token not in page
        assert "window.READ_ONLY = true" in page
    finally:
        srv.shutdown()
        srv.server_close()


def test_read_only_server_refuses_posts(repo: Path) -> None:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _ = write(srv, "/api/gate/approve", {"gate": "requirements"})
        assert status == 405
        with _Stream(srv) as stream:
            assert stream.opening()["project"] == "demo"  # reads still work
    finally:
        srv.shutdown()
        srv.server_close()


def test_main_once_prints_parseable_json(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ui.main(["--once", "--root", str(repo)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["next"]["command"] == "rein approve requirements"


def test_main_refuses_non_loopback_bind_with_writes_enabled(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ui.main(["--host", "0.0.0.0", "--root", str(repo)]) == 2
    assert "refusing to bind" in capsys.readouterr().err
    # --once starts no server, so the guard does not apply to it
    assert ui.main(["--host", "0.0.0.0", "--once", "--root", str(repo)]) == 0


def test_open_mode_targets_vscode_over_external_browser() -> None:
    assert ui.open_mode(no_open=False, term_program="vscode") == "vscode"
    assert ui.open_mode(no_open=False, term_program=None) == "browser"
    assert ui.open_mode(no_open=False, term_program="Apple_Terminal") == "browser"
    assert ui.open_mode(no_open=True, term_program="vscode") == "none"  # --no-open overrides detection


# --- events endpoint ------------------------------------------------------------


def _seed_events(repo: Path, count: int) -> None:
    """A chained log: `count - 1` completed tasks, then one event awaiting a human decision."""
    from rein import event_chain

    ui._events_cache = None
    names = ["task_completed"] * (count - 1) + ["task_failed"]
    event_chain.append_lines(repo / ".rein" / "events.ndjson", chain(*names))


def test_get_events_returns_tail_newest_first_with_open_flag(server: ui.DashboardServer, repo: Path) -> None:
    _seed_events(repo, count=5)
    status, data = _request(server, "GET", "/api/events?limit=3")
    assert status == 200
    payload = json.loads(data)
    assert payload["total"] == 5 and len(payload["events"]) == 3
    newest = payload["events"][0]
    assert newest["event"] == "task_failed" and newest["needs_decision"] is True
    assert payload["events"][1]["needs_decision"] is False  # a completed task needs no decision
    # The chain root the feed was rendered from, so a viewer can check it against a receipt.
    assert payload["chain_root"].startswith("sha256:")


def test_get_events_defaults_and_rejects_bad_limit(server: ui.DashboardServer, repo: Path) -> None:
    empty = json.loads(_request(server, "GET", "/api/events")[1])
    assert empty["events"] == [] and empty["total"] == 0
    assert _request(server, "GET", "/api/events?limit=abc")[0] == 400


def test_events_are_parsed_once_per_version_of_the_log(server: ui.DashboardServer, repo: Path) -> None:
    # The Activity feed polls every 3s and answering it means parsing the *whole* log (an
    # escalation's open state depends on a resolve that may sit anywhere in it). Cache on the
    # file's identity — but an append must still be visible on the very next request.
    _seed_events(repo, count=5)
    log = repo / ".rein" / "events.ndjson"
    first = ui._load_events_cached(log)
    assert ui._load_events_cached(log) is first  # unchanged file: the same parsed list, not a re-parse

    from rein import event_chain

    event_chain.append_lines(log, [event_chain.link(first[-1], event_chain.make("task_completed", "demo-cycle"))])
    payload = json.loads(_request(server, "GET", "/api/events")[1])
    assert payload["total"] == 6 and payload["events"][0]["seq"] == 6

    missing = repo / ".rein" / "nope.ndjson"
    assert ui._load_events_cached(missing) == []  # an absent log is empty


# --- /api/stream ------------------------------------------------------------------


def _advance_to_design(repo: Path) -> None:
    (repo / ".rein" / "state.yaml").write_bytes(
        store.dump_yaml(
            make_state(
                gates={
                    "requirements": "approved",
                    "design": "pending",
                    "tasks": "pending",
                    "build": "pending",
                    "release": "pending",
                },
                phase="design",
                plan_status="draft",
            )
        )
    )


def test_the_stream_says_nothing_while_the_repository_does_not_move(server: ui.DashboardServer) -> None:
    """The property the whole design rests on.

    `generated_at` is a fresh wall-clock stamp on every read, so if it counted towards the payload's
    identity an idle repository would look like it changed on every tick, and the page would
    re-render over state that did not move — losing a half-typed field, a task's open detail, the
    scroll inside a long patch. Silence here is the assertion.
    """
    with _Stream(server, timeout=3.0) as stream:
        payload = stream.opening()
        assert payload["generated_at"] is not None  # still in the body: it is a fact about the read
        with pytest.raises(TimeoutError):
            stream.next_event("status")


def test_the_stream_pushes_a_status_when_the_ssot_moves(server: ui.DashboardServer, repo: Path) -> None:
    with _Stream(server) as stream:
        assert stream.opening()["gates"][0]["status"] == "pending"
        _advance_to_design(repo)
        assert stream.next_event("status")[1]["gates"][0]["status"] == "approved"


def test_the_stream_pushes_a_record_event_when_the_log_grows(server: ui.DashboardServer, repo: Path) -> None:
    """The audit log gets its own signal because an appended event need not move the status payload
    at all — and the Record feed still has to know it happened."""
    with _Stream(server) as stream:
        first = stream.next_event("record")[1]
        _seed_events(repo, count=5)
        assert stream.next_event("record")[1]["revision"] != first["revision"]


def test_the_stream_follows_a_project_switch_with_no_client_coordination(
    multi_server: tuple[ui.DashboardServer, dict[str, Path]],
) -> None:
    """The active project is re-read every tick rather than captured at connect, so switching
    targets needs nothing from the page: the next push is already the other repository's."""
    srv, _ = multi_server
    with _Stream(srv) as stream:
        assert stream.opening()["project"] == "alpha"
        assert write(srv, "/api/project/select", {"name": "beta"})[0] == 200
        assert stream.next_event("status")[1]["project"] == "beta"


def test_the_stream_ends_when_the_server_closes(repo: Path) -> None:
    """A stream thread holds a socket for the life of a run; it must not outlive the server."""
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    stream = _Stream(srv)
    stream.opening()
    srv.shutdown()
    srv.server_close()
    assert srv.closing.is_set()
    stream.close()


def test_status_identity_ignores_when_the_payload_was_generated(repo: Path) -> None:
    a, b = ui._collect_status(repo), ui._collect_status(repo)
    assert a["generated_at"] != b["generated_at"] or a["generated_at"] is not None
    assert ui._status_identity(a) == ui._status_identity(b)
    _advance_to_design(repo)
    assert ui._status_identity(ui._collect_status(repo)) != ui._status_identity(a)


def test_the_fingerprint_moves_when_the_ssot_does_and_costs_no_parse(repo: Path) -> None:
    """The two-stage check: a tick stats, and only a moved fingerprint buys a status read."""
    before = ui._ssot_fingerprint(repo)
    assert before, "the fingerprint must actually watch something"
    assert ui._ssot_fingerprint(repo) == before  # stable while nothing moves
    _advance_to_design(repo)
    assert ui._ssot_fingerprint(repo) != before


def test_the_fingerprint_is_a_fixed_handful_of_stats_and_never_a_walk() -> None:
    """The property that makes the two-stage check worth having, and it was got wrong once.

    The first version globbed `.rein/**` and `.git/refs/**`. That is ~100 paths in this repository
    and cost 271ms per tick on a WSL mount — more than the status read it exists to avoid, turning
    the optimisation into the slowest thing in the loop. A fingerprint has to be cheap on the worst
    filesystem a dashboard runs on, so it is a fixed list and there is no directory walk in it.
    """
    assert len(ui._WATCHED) <= 10, "a fingerprint this long is no longer obviously cheaper than a read"
    assert not any("*" in rel for rel in ui._WATCHED), "a glob here is a directory walk on every tick"
    assert ".rein/state.yaml" in ui._WATCHED and ".git/HEAD" in ui._WATCHED


def test_the_frontend_fixture_still_looks_like_a_real_status_payload(repo: Path) -> None:
    """`tests/ui/fixtures/status.json` is what the dashboard's own suite renders against.

    It is a snapshot, so it can drift from what the server sends — and a frontend suite that is
    green against a payload shape the server retired is worse than no suite, because it reports
    confidence about a page that would break. This is the seam: the keys the fixture carries must
    still be the keys a live read produces.
    """
    fixture: dict[str, Any] = json.loads(
        (Path(__file__).parent / "ui" / "fixtures" / "status.json").read_text(encoding="utf-8")
    )
    live: dict[str, Any] = ui._collect_status(repo)
    assert set(fixture) == set(live), "the fixture's top-level keys are not the payload's any more"
    # The `repo` fixture has no plan, so its `tasks` is None; the frontend fixture's has one, and
    # the block's own shape is pinned by test_status_payload_carries_every_field_the_modules_read.
    assert set(fixture["tasks"]["counts"]) == set(models.TASK_STATUS_ORDER)
    for gate in fixture["gates"]:
        assert set(gate) == {"name", "status", "index", "phase", "approval_id"}


def test_status_reads_the_event_log_through_the_cache(repo: Path) -> None:
    # The stream reads status on every fingerprint move; it used to re-parse the whole
    # events.ndjson each time while the cache served only the feed. Same file version → same list.
    _seed_events(repo, count=5)
    log = repo / ".rein" / "events.ndjson"
    ui._events_cache = None  # empty the one slot, so what fills it can only be the read below
    status: dict[str, Any] = ui._collect_status(repo)
    assert len(status["attention"]) == 1
    assert ui._events_cache is not None and ui._events_cache[0][0] == str(log)


# --- review endpoint ------------------------------------------------------------


def test_get_review_serves_rendered_deliverable(server: ui.DashboardServer, repo: Path) -> None:
    doc = repo / "docs" / "10-requirements.md"
    doc.parent.mkdir(parents=True)
    doc.write_text(
        "# Requirements\n<script>steal(TOKEN)</script>\n\n## Self-assessment\n- **Confidence**: low\n",
        encoding="utf-8",
    )
    status, data = _request(server, "GET", "/api/review/requirements")
    assert status == 200
    payload = json.loads(data)
    assert payload["is_awaiting"] is True and payload["index"] == 1
    (main,) = payload["deliverables"]
    assert "<h1>Requirements</h1>" in main["html"]
    assert "<script" not in main["html"]  # XSS regression: agent markup arrives inert
    assert main["self_assessment"]["confidence"] == "low"


@pytest.mark.parametrize("path", ["/api/review/nope", "/api/review/../state", "/api/review/", "/api/review/Build"])
def test_get_review_unknown_gate_is_404(server: ui.DashboardServer, path: str) -> None:
    assert _request(server, "GET", path)[0] == 404


# --- the gate ④ human review (plan §21.1, §21.2) --------------------------------


def _generated_review_with_card() -> dict[str, object]:
    """A minimal generated machine review carrying one high-risk Decision Card (DC-001).

    Being high-risk it blocks the freeze until answered (human_review.unanswered_decisions), and its
    `evidence` is served with it from the first request — the withholding that used to gate it on an
    unprimed guess is gone.
    """
    return {
        "machine": {
            "status": "generated",
            "binding": {
                "change_digest": "sha256:" + "a" * 64,
                "plan_digest": "sha256:" + "b" * 64,
                "environment_digest": "sha256:" + "c" * 64,
            },
            "coverage": {
                "diff_digest": "sha256:" + "d" * 64,
                "analyzed_files": 1,
                "analyzed_bytes": 1024,
                "coverage_status": "sufficient",
            },
            "actual_extraction": [],
            "claims": [],
            "brief": {
                "delivered": [{"task_id": "T-001", "title": "the retry path", "status": "done"}],
                "execution_boundary": [{"step": "test", "profile": "quality", "sandbox": "oci", "network": "none"}],
            },
            "residual_findings": [
                {
                    "task_id": "T-001",
                    "severity": "consider",
                    "statement": "the retry key could be threaded through instead of rebuilt",
                    "observed_commit": "1" * 40,
                }
            ],
            "statements": [
                {"id": "STMT-001", "text": "Return it to the implementer.", "epistemic_status": "machine_inferred"},
                {"id": "STMT-002", "text": "Change the expectation instead.", "epistemic_status": "machine_inferred"},
            ],
            "decision_cards": [
                {
                    "id": "DC-001",
                    "question": "C-001 diverged. What happens to it?",
                    "risk": "high",
                    "options": [{"id": "A", "statement_id": "STMT-001"}, {"id": "B", "statement_id": "STMT-002"}],
                    "evidence": {"expected": {"statement": "a lost response never double-commits"}},
                }
            ],
        },
        "human": {"status": "not_started"},
    }


@pytest.fixture
def review_server(tmp_path: Path) -> Iterator[ui.DashboardServer]:
    root = tmp_path / "rv"
    root.mkdir()
    seed_repo(
        root,
        state=make_state(project="rv", gates=dict.fromkeys(models.GATE_ORDER, "pending"), phase="build"),
        config=make_config(profiles=SANDBOXED_PROFILES),
        review=_generated_review_with_card(),
    )
    srv = ui.DashboardServer(("127.0.0.1", 0), root=root, read_only=False)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()


def _digest(server: ui.DashboardServer) -> str:
    return str(json.loads(_request(server, "GET", "/api/review/session")[1])["machine_digest"])


def test_the_session_names_what_is_still_owed(review_server: ui.DashboardServer) -> None:
    status, data = _request(review_server, "GET", "/api/review/session")
    assert status == 200
    session = json.loads(data)
    assert session["generated"] is True
    assert session["unanswered_decisions"] == ["DC-001"]
    assert session["machine_digest"].startswith("sha256:")


def _card(stage_payload: dict[str, Any], card_id: str) -> dict[str, Any]:
    cards = stage_payload["decision_cards"]
    assert isinstance(cards, list)
    return next(c for c in cards if c["id"] == card_id)


def test_the_decision_stage_serves_a_card_with_its_evidence(review_server: ui.DashboardServer) -> None:
    """The evidence used to be stripped until the reviewer had guessed at the card first.

    That withheld the material for the judgement on the one screen that asks for it. Pinning the
    opposite is what stops the sequence being reintroduced.
    """
    stage = json.loads(_request(review_server, "GET", "/api/review/stage/decision")[1])
    evidence = _card(stage, "DC-001")["evidence"]
    assert evidence["expected"]["statement"] == "a lost response never double-commits"


def test_the_orient_stage_carries_the_brief_and_the_unresolved_findings(
    review_server: ui.DashboardServer,
) -> None:
    """The stage that exists so the decision stage can ask for less.

    The residual findings are the ones the per-task reviewer marked `consider`: they stop nothing,
    they were written to the task handoff, and until this stage existed nothing ever read them back.
    """
    stage = json.loads(_request(review_server, "GET", "/api/review/stage/orient")[1])
    assert stage["brief"]["delivered"][0]["task_id"] == "T-001"
    assert stage["brief"]["execution_boundary"][0]["network"] == "none"
    finding = stage["residual_findings"][0]
    assert finding["task_id"] == "T-001" and finding["severity"] == "consider"
    # The finding is stamped with the tree it was observed against, not the reviewed HEAD.
    assert finding["observed_commit"] == "1" * 40


def test_a_human_answer_leaves_the_machine_digest_unchanged(review_server: ui.DashboardServer) -> None:
    # E2E-09: recording a decision moves the human half, never the machine half.
    before = _digest(review_server)
    body: dict[str, object] = {"card_id": "DC-001", "choice": "B", "confidence": "low", "machine_digest": before}
    data = json.loads(write(review_server, "/api/review/decision", body)[1])
    assert data["machine_digest"] == before


def test_a_stale_machine_digest_is_refused_with_409(review_server: ui.DashboardServer) -> None:
    # E2E-08: an answer written against a machine review that has since changed is a conflict.
    stale = "sha256:" + "0" * 64
    body: dict[str, object] = {"card_id": "DC-001", "choice": "B", "confidence": "low", "machine_digest": stale}
    status, _ = write(review_server, "/api/review/decision", body)
    assert status == 409


def test_an_answer_naming_no_machine_review_is_refused(review_server: ui.DashboardServer) -> None:
    """The guard used to be opt-in from the client: `assert_machine_current` short-circuited on
    an empty string, so simply omitting the field merged the answer into whatever machine review
    happened to be on disk — the E2E-08 failure, reachable by leaving a field out."""
    body: dict[str, object] = {"card_id": "DC-001", "choice": "B", "confidence": "low"}
    status, data = write(review_server, "/api/review/decision", body)
    assert status == 400 and b"machine_digest is required" in data


def test_a_decision_card_can_be_answered_from_the_pane(review_server: ui.DashboardServer) -> None:
    """The judgement gate ④ asks for had a schema slot, an id validator and no endpoint at all."""
    body: dict[str, object] = {
        "card_id": "DC-001",
        "choice": "A",
        "confidence": "high",
        "reason": "the retry key is not threaded through",
        "machine_digest": _digest(review_server),
    }
    status, data = write(review_server, "/api/review/decision", body)
    assert status == 200, data
    stage = json.loads(_request(review_server, "GET", "/api/review/stage/decision")[1])
    assert stage["decisions"] == [
        {"card_id": "DC-001", "choice": "A", "confidence": "high", "reason": "the retry key is not threaded through"}
    ]
    assert stage["unanswered"] == []


def test_an_unanswered_high_risk_card_blocks_the_freeze(review_server: ui.DashboardServer) -> None:
    session = json.loads(_request(review_server, "GET", "/api/review/session")[1])
    assert session["unanswered_decisions"] == ["DC-001"]
    assert any("decision cards" in b for b in session["completion_blockers"])
    assert session["can_freeze"] is False


def test_a_decision_without_a_confidence_is_refused(review_server: ui.DashboardServer) -> None:
    """The pane hardcoded "medium" and the server defaulted to "low", so the recorded number was
    whichever fabrication won. How sure the reviewer was is theirs to state or not state at all."""
    body: dict[str, object] = {"card_id": "DC-001", "choice": "A", "machine_digest": _digest(review_server)}
    status, data = write(review_server, "/api/review/decision", body)
    assert status == 400 and b"confidence is required" in data


def test_the_challenge_endpoints_are_gone(review_server: ui.DashboardServer) -> None:
    """A removed screen must not leave a live write behind it.

    An endpoint nothing renders is still an endpoint: left in place it would keep accepting answers
    to a question the pane no longer asks, and move `human_digest` for a judgement nobody made.
    """
    assert _request(review_server, "GET", "/api/review/challenge/DC-001/reveal")[0] == 404
    for action in ("challenge", "counterfactual"):
        body: dict[str, object] = {"challenge_id": "DC-001", "choice": "B", "machine_digest": _digest(review_server)}
        assert write(review_server, f"/api/review/{action}", body)[0] == 404


def test_a_stage_tick_means_a_recorded_judgement_not_a_visit(review_server: ui.DashboardServer) -> None:
    """`settled` is None where a stage records nothing — neither "done" nor "skipped" is a claim
    this payload is entitled to make about a stage the reviewer merely scrolled past."""
    session = json.loads(_request(review_server, "GET", "/api/review/session")[1])
    settled = {s["name"]: s["settled"] for s in session["stages"]}
    assert settled["decision"] is False  # DC-001 is not decided yet
    assert settled["freeze"] is False  # not frozen
    # The reading stages record nothing — including orient, which exists to lower what the reviewer
    # has to reconstruct, so ticking it on open would be the "a mouse moved" claim exactly.
    assert settled["scope"] is None and settled["orient"] is None and settled["diff"] is None


def test_review_post_without_token_is_403(review_server: ui.DashboardServer) -> None:
    status, _ = _request(review_server, "POST", "/api/review/decision", {"card_id": "DC-001", "choice": "B"})
    assert status == 403


def test_review_is_readable_on_a_read_only_server(repo: Path) -> None:
    srv = ui.DashboardServer(("127.0.0.1", 0), root=repo, read_only=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert _request(srv, "GET", "/api/review/requirements")[0] == 200  # reviewing is view-only
    finally:
        srv.shutdown()
        srv.server_close()


# --- project switcher -----------------------------------------------------------


@pytest.fixture
def multi_server(tmp_path: Path) -> Iterator[tuple[ui.DashboardServer, dict[str, Path]]]:
    """A server backed by a real registry with two projects (alpha active, beta second)."""
    alpha = _seed_repo(tmp_path / "alpha", "alpha")
    beta = _seed_repo(tmp_path / "beta", "beta")
    reg_path = registry.registry_path()
    reg = registry.Registry()
    reg.add("alpha", alpha)
    reg.add("beta", beta)
    reg.set_active("alpha")
    registry.save(reg, reg_path)

    srv = ui.DashboardServer(("127.0.0.1", 0), root=alpha, read_only=False, registry_path=reg_path)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv, {"alpha": alpha, "beta": beta}
    finally:
        srv.shutdown()
        srv.server_close()


def test_get_projects_lists_registry_with_active(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, data = _request(srv, "GET", "/api/projects")
    assert status == 200
    payload = json.loads(data)
    assert payload["active"] == "alpha"
    by_name = {p["name"]: p for p in payload["projects"]}
    assert set(by_name) == {"alpha", "beta"}
    assert by_name["alpha"]["active"] is True and by_name["alpha"]["exists"] is True


def test_a_project_switch_persists(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    assert srv.active_root().name == "alpha"
    status, _ = write(srv, "/api/project/select", {"name": "beta"})
    assert status == 200
    assert srv.active_root().name == "beta"
    assert json.loads(_request(srv, "GET", "/api/projects")[1])["active"] == "beta"


def test_select_unknown_project_is_400(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, _ = write(srv, "/api/project/select", {"name": "ghost"})
    assert status == 400


def test_select_without_token_is_403(multi_server: tuple[ui.DashboardServer, dict[str, Path]]) -> None:
    srv, _ = multi_server
    status, _ = _request(srv, "POST", "/api/project/select", {"name": "beta"})
    assert status == 403


def test_pinned_server_reports_single_project_and_refuses_select(server: ui.DashboardServer) -> None:
    # The `server` fixture builds a registry_path-less (pinned) server from a single repo.
    payload = json.loads(_request(server, "GET", "/api/projects")[1])
    assert len(payload["projects"]) == 1 and payload["projects"][0]["active"] is True
    status, _ = write(server, "/api/project/select", {"name": "whatever"})
    assert status == 409  # no registry backs a pinned server


def test_main_once_does_not_touch_the_registry(repo: Path) -> None:
    # --once is a scripting/inspection path: it prints status and must not mutate user-global state.
    assert ui.main(["--once", "--root", str(repo)]) == 0
    assert not registry.registry_path().exists()


# --- requesting changes: the other direction of the same footer ---------------------


def test_the_pane_can_record_a_change_request(server: ui.DashboardServer, repo: Path) -> None:
    """The line this dashboard draws is about direction, not authentication: a change request only
    ever narrows what happens next, so it rides the same session every write here carries — while
    approving, which widens, is what the launch-link handover exists to protect."""
    from rein import approve
    from rein import repo as repo_mod

    status, body = write(
        server,
        "/api/changes",
        {"gate": "requirements", "target": "docs/10-requirements.md#R-3", "reason": "unmeasurable"},
    )
    assert status == 200
    assert json.loads(body)["id"].startswith("CR-REQUIREMENTS-")

    blockers = approve.readiness(repo_mod.Repo(repo), "requirements")
    assert any("open change request" in b for b in blockers)


def test_a_change_request_still_needs_the_session(server: ui.DashboardServer) -> None:
    body: dict[str, object] = {"gate": "requirements", "target": "R-3", "reason": "x"}
    assert _request(server, "POST", "/api/changes", body, token=server.token)[0] == 403


def test_an_unanchored_change_request_is_refused(server: ui.DashboardServer) -> None:
    status, body = write(server, "/api/changes", {"gate": "requirements", "target": "", "reason": "vague"})
    assert status == 400
    assert "needs a --target" in json.loads(body)["error"]
