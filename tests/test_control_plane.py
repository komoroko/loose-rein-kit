"""Tests for control_plane.py — how a leaf worktree records something that outlives it (plan §30.7).

The bug this module closes is easy to state and easy to miss: without it an implementer in a
worktree runs `rein decision add`, the command prints success, and the record goes into a
directory that is deleted minutes later. So the first test here is the end-to-end one — the
decision survives the worktree — and everything after it is about why handing an LLM agent a
socket into the Store is safe.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from rein import control_plane, models
from rein import repo as repo_mod
from rein import store as store_mod
from tests._support import make_plan, make_state, make_task, seed_repo

CENTRAL_ONLY = sorted(models.CENTRAL_ONLY_CAPABILITIES)


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    seed_repo(
        tmp_path,
        plan=make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]),
        state=make_state(phase="build"),
    )
    return repo_mod.Repo(tmp_path)


@pytest.fixture
def server(repo: repo_mod.Repo) -> Iterator[control_plane.ControlServer]:
    with control_plane.serving(repo) as running:
        yield running


def leaf_token(server: control_plane.ControlServer, task: str = "T-001") -> str:
    return control_plane.mint(
        server.secret, run_id="RUN-1", task_id=task, capabilities=sorted(control_plane.LEAF_CAPABILITIES)
    )


def ask(server: control_plane.ControlServer, capability: str, token: str, **args: object) -> dict[str, object]:
    return control_plane.call(server.socket_path, control_plane.Request(capability, token, dict(args)))


# --- the bug this module exists to close --------------------------------------


@pytest.mark.integration
def test_a_leaf_s_decision_survives_the_worktree(tmp_path: Path) -> None:
    """The failure this closes, end to end: record a decision from inside a worktree, delete the
    worktree, and the decision is still in the canonical chain."""
    canonical = tmp_path / "main"
    canonical.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=canonical, check=True)
    for name, value in (("user.email", "t@e.x"), ("user.name", "T")):
        subprocess.run(["git", "config", name, value], cwd=canonical, check=True)
    seed_repo(canonical, plan=make_plan(tasks=[make_task("T-001", claim_ids=["C-001"])]), state=make_state())
    subprocess.run(["git", "add", "-A"], cwd=canonical, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=canonical, check=True)

    leaf = tmp_path / "leaf"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "work-T-001", str(leaf)], cwd=canonical, check=True)

    repo = repo_mod.Repo(canonical)
    leaf_repo = repo_mod.Repo(leaf)
    assert repo.is_canonical_checkout and not leaf_repo.is_canonical_checkout
    assert store_mod.runtime_dir(repo) == store_mod.runtime_dir(leaf_repo)  # one Store, one lock

    with control_plane.serving(repo) as running:
        result = control_plane.call(
            running.socket_path,
            control_plane.Request("decision.declare", leaf_token(running), {"statement": "timeout 30", "risk": "low"}),
        )
    assert result["recorded"] == "decision.declare"

    shutil.rmtree(leaf)
    subprocess.run(["git", "worktree", "prune"], cwd=canonical, check=True)

    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["decision_declared"]
    assert events[0].detail["statement"] == "timeout 30"
    assert events[0].actor == "leaf:T-001"


def test_a_leaf_with_no_control_plane_refuses_rather_than_losing_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_repo(tmp_path)
    leaf = repo_mod.Repo(tmp_path)
    leaf._cache["git_common_dir"] = tmp_path / "elsewhere" / ".git"  # pretend to be a worktree
    monkeypatch.delenv(control_plane.SOCKET_ENV, raising=False)
    monkeypatch.delenv(control_plane.TOKEN_ENV, raising=False)

    with pytest.raises(control_plane.ControlPlaneError, match="refusing rather than losing it"):
        control_plane.route(leaf, "decision.declare", {"statement": "x"})


def test_the_canonical_checkout_writes_directly(repo: repo_mod.Repo) -> None:
    result = control_plane.route(repo, "decision.declare", {"task": "T-001", "statement": "use one key"})
    assert result["recorded"] == "decision.declare"
    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["decision_declared"]
    # The actor column is read by a human deciding who did what, so "a leaf recorded this" and
    # "the orchestrator recorded this" must not print the same.
    assert events[0].actor == "canonical-checkout"


# --- a leaf cannot mint authority ---------------------------------------------


@pytest.mark.parametrize("capability", CENTRAL_ONLY)
def test_minting_refuses_every_central_only_capability(capability: str) -> None:
    secret = control_plane.new_secret()
    with pytest.raises(control_plane.TokenError, match="central-only"):
        control_plane.mint(secret, run_id="R", task_id="T-001", capabilities=[capability])


def test_minting_refuses_an_unknown_capability() -> None:
    with pytest.raises(control_plane.TokenError, match="unknown capability"):
        control_plane.mint(control_plane.new_secret(), run_id="R", task_id="T-001", capabilities=["do.anything"])


def test_leaf_capabilities_and_central_only_partition_the_vocabulary() -> None:
    assert control_plane.LEAF_CAPABILITIES | models.CENTRAL_ONLY_CAPABILITIES == models.CAPABILITY_VALUES
    assert not (control_plane.LEAF_CAPABILITIES & models.CENTRAL_ONLY_CAPABILITIES)
    assert "gate.approve" in models.CENTRAL_ONLY_CAPABILITIES


def test_the_secret_never_lands_in_the_working_tree(repo: repo_mod.Repo) -> None:
    """A secret in the repository is a secret in the diff, and the implementer mounts that."""
    control_plane.read_or_create_secret(store_mod.runtime_dir(repo))
    assert not list(repo.rein_dir.rglob(control_plane.SECRET_NAME))
    secret_path = store_mod.runtime_dir(repo) / control_plane.SECRET_NAME
    assert secret_path.exists()
    assert (secret_path.stat().st_mode & 0o077) == 0  # not group- or world-readable


def test_the_secret_is_stable_across_reads(repo: repo_mod.Repo) -> None:
    runtime = store_mod.runtime_dir(repo)
    assert control_plane.read_or_create_secret(runtime) == control_plane.read_or_create_secret(runtime)


# --- token verification --------------------------------------------------------


def test_a_valid_token_round_trips() -> None:
    secret = control_plane.new_secret()
    raw = control_plane.mint(secret, run_id="RUN-1", task_id="T-002", capabilities=["decision.declare"])
    token = control_plane.verify(secret, raw)
    assert token.run_id == "RUN-1" and token.task_id == "T-002"
    assert token.allows("decision.declare")
    assert not token.allows("gate.approve")


def test_a_token_signed_with_another_secret_is_refused() -> None:
    raw = control_plane.mint(control_plane.new_secret(), run_id="R", task_id="T", capabilities=["decision.declare"])
    with pytest.raises(control_plane.TokenError, match="not issued by this orchestrator"):
        control_plane.verify(control_plane.new_secret(), raw)


def test_a_tampered_payload_is_refused() -> None:
    import base64

    secret = control_plane.new_secret()
    raw = control_plane.mint(secret, run_id="R", task_id="T", capabilities=["decision.declare"])
    encoded, _, signature = raw.partition(".")
    body = base64.urlsafe_b64decode(encoded).replace(b'"decision.declare"', b'"gate.approve"    ')
    forged = f"{base64.urlsafe_b64encode(body).decode()}.{signature}"
    with pytest.raises(control_plane.TokenError, match="does not verify"):
        control_plane.verify(secret, forged)


def test_an_expired_token_is_refused() -> None:
    secret = control_plane.new_secret()
    raw = control_plane.mint(secret, run_id="R", task_id="T", capabilities=["decision.declare"], ttl_sec=1)
    later = datetime.now().astimezone() + timedelta(seconds=5)
    with pytest.raises(control_plane.TokenError, match="expired"):
        control_plane.verify(secret, raw, now=later)


@pytest.mark.parametrize("raw", ["", "nodot", ".", "!!!.abc"])
def test_a_malformed_token_is_refused(raw: str) -> None:
    with pytest.raises(control_plane.TokenError):
        control_plane.verify(control_plane.new_secret(), raw)


# --- the server's own refusals -------------------------------------------------


@pytest.mark.parametrize("capability", CENTRAL_ONLY)
def test_the_server_refuses_a_central_only_capability_whatever_the_token_says(
    server: control_plane.ControlServer, capability: str
) -> None:
    """Not redundant with mint(): this is the check that still holds if the secret leaks. An
    agent that can approve its own work has not been reviewed by anyone."""
    forged = control_plane.mint(
        server.secret, run_id="R", task_id="T-001", capabilities=sorted(control_plane.LEAF_CAPABILITIES)
    )
    with pytest.raises(control_plane.ControlPlaneError, match="central-only"):
        ask(server, capability, forged, statement="approve me")


def test_a_token_that_does_not_grant_the_capability_is_refused(server: control_plane.ControlServer) -> None:
    narrow = control_plane.mint(server.secret, run_id="R", task_id="T-001", capabilities=["knowledge_gap.create"])
    with pytest.raises(control_plane.ControlPlaneError, match="does not grant decision.declare"):
        ask(server, "decision.declare", narrow, statement="x")


def test_a_replayed_request_is_refused(server: control_plane.ControlServer) -> None:
    """The same message sent twice — a capture off a log — is accepted once."""
    token = leaf_token(server)
    request = control_plane.Request("decision.declare", token, {"statement": "first"})
    control_plane.call(server.socket_path, request)
    with pytest.raises(control_plane.ControlPlaneError, match="replayed"):
        control_plane.call(server.socket_path, request)


def test_one_token_serves_a_leaf_s_whole_task(server: control_plane.ControlServer) -> None:
    """The orchestrator mints one token per task, so consuming it on the first call would let an
    implementer record a decision and then be locked out of recording its knowledge gap."""
    token = leaf_token(server)
    ask(server, "decision.declare", token, statement="first")
    ask(server, "knowledge_gap.create", token, statement="second")
    ask(server, "task.status", token, task="T-001", status="done")


def test_a_request_without_a_nonce_is_refused(server: control_plane.ControlServer) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(server.socket_path))
    body = {"capability": "decision.declare", "token": leaf_token(server), "args": {"statement": "x"}}
    client.sendall((json.dumps(body) + "\n").encode("utf-8"))
    answer = json.loads(client.recv(65536).decode("utf-8"))
    client.close()
    assert answer["ok"] is False and "nonce" in answer["error"]


def test_an_unknown_capability_is_refused(server: control_plane.ControlServer) -> None:
    with pytest.raises(control_plane.ControlPlaneError, match="unknown capability"):
        ask(server, "rm.rf", leaf_token(server))


def test_a_malformed_request_is_refused_without_a_traceback(server: control_plane.ControlServer) -> None:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(5)
    client.connect(str(server.socket_path))
    client.sendall(b"{not json\n")
    answer = json.loads(client.recv(65536).decode())
    client.close()
    assert answer["ok"] is False
    assert "Traceback" not in answer["error"]


def test_a_statement_is_required(server: control_plane.ControlServer) -> None:
    with pytest.raises(control_plane.ControlPlaneError, match="needs a --statement"):
        ask(server, "decision.declare", leaf_token(server), statement="   ")


def test_an_unknown_risk_is_refused(server: control_plane.ControlServer) -> None:
    with pytest.raises(control_plane.ControlPlaneError, match="unknown risk"):
        ask(server, "decision.declare", leaf_token(server), statement="x", risk="spicy")


# --- escalation ----------------------------------------------------------------


def test_a_low_risk_decision_is_recorded_and_the_task_keeps_going(
    server: control_plane.ControlServer, repo: repo_mod.Repo
) -> None:
    result = ask(server, "decision.declare", leaf_token(server), statement="name the variable x", risk="low")
    assert result["escalated"] is False
    state = store_mod.Store(repo).read_state()
    assert state is not None and state.task_status.get("T-001") in (None, "todo")


@pytest.mark.parametrize("risk", ["medium", "high", "critical"])
def test_a_decision_at_or_above_the_floor_parks_the_task(
    server: control_plane.ControlServer, repo: repo_mod.Repo, risk: str
) -> None:
    """A default the plan never described is not the implementer's to choose. The task waits
    for a human rather than the agent proceeding on its own invention (plan §16.5)."""
    result = ask(server, "decision.declare", leaf_token(server), statement="timeout 30", risk=risk)
    assert result["escalated"] is True
    assert "Stop work on it" in str(result["note"])

    state = store_mod.Store(repo).read_state()
    assert state is not None and state.task_status["T-001"] == "needs-revision"


def test_the_escalation_floor_is_medium() -> None:
    assert control_plane.ESCALATION_FLOOR == "medium"
    assert not models.risk_at_least("low", control_plane.ESCALATION_FLOOR)


def test_a_knowledge_gap_is_recorded_without_parking_the_task(
    server: control_plane.ControlServer, repo: repo_mod.Repo
) -> None:
    result = ask(server, "knowledge_gap.create", leaf_token(server), statement="cannot find the retry spec")
    assert result["escalated"] is False
    events = store_mod.Store(repo).read_events()
    assert [e.event for e in events] == ["knowledge_gap"]


# --- the record itself ---------------------------------------------------------


def test_a_leaf_s_record_is_an_ordinary_chained_event(server: control_plane.ControlServer, repo: repo_mod.Repo) -> None:
    """The whole point of routing through the Store: a leaf's decision lands under the same
    rules as anything the orchestrator does itself."""
    ask(server, "decision.declare", leaf_token(server), statement="one", risk="low")
    ask(server, "decision.declare", leaf_token(server), statement="two", risk="low")

    store = store_mod.Store(repo)
    events = store.read_events()  # would raise if the chain were broken
    assert [e.seq for e in events] == [1, 2]
    assert events[1].prev_event_digest == events[0].event_digest
    assert all(e.detail["run_id"] == "RUN-1" for e in events)


def test_concurrent_leaves_produce_one_intact_chain(server: control_plane.ControlServer, repo: repo_mod.Repo) -> None:
    import threading

    errors: list[Exception] = []

    def declare(index: int) -> None:
        try:
            ask(server, "decision.declare", leaf_token(server, f"T-00{index}"), statement=f"d{index}", risk="low")
        except Exception as exc:  # noqa: BLE001 - re-raised below with context
            errors.append(exc)

    threads = [threading.Thread(target=declare, args=(i,)) for i in range(1, 6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert not errors, errors
    events = store_mod.Store(repo).read_events()
    assert [e.seq for e in events] == [1, 2, 3, 4, 5]


# --- lifecycle -----------------------------------------------------------------


def test_the_socket_is_private_and_removed_afterwards(repo: repo_mod.Repo) -> None:
    with control_plane.serving(repo) as running:
        path = running.socket_path
        assert (path.stat().st_mode & 0o077) == 0
    assert not path.exists()


def test_a_stale_socket_does_not_block_the_next_run(repo: repo_mod.Repo) -> None:
    """A socket left by a killed run holds no state, so removing it is safe in a way that
    removing a lock file would not be."""
    runtime = store_mod.runtime_dir(repo)
    store_mod.ensure_private_dir(runtime)
    (runtime / control_plane.SOCKET_NAME).write_text("stale", encoding="utf-8")
    with control_plane.serving(repo) as running:
        assert running.socket_path.exists()


def test_calling_an_absent_socket_says_so(tmp_path: Path) -> None:
    with pytest.raises(control_plane.ControlPlaneError, match="cannot reach the control plane"):
        control_plane.call(tmp_path / "nope.sock", control_plane.Request("decision.declare", "t", {}))


# --- the CLI -------------------------------------------------------------------


def test_the_cli_records_from_the_canonical_checkout(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    rc = control_plane.main(
        ["add", "--task", "T-001", "--statement", "keep one key", "--risk", "low", "--repo", str(repo.root)]
    )
    assert rc == 0
    assert "recorded in the central audit chain" in capsys.readouterr().out
    assert [e.event for e in store_mod.Store(repo).read_events()] == ["decision_declared"]


def test_the_cli_exits_2_when_the_record_parks_the_task(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 is the signal an autonomous loop needs: this task is not yours to finish."""
    rc = control_plane.main(
        ["add", "--task", "T-001", "--statement", "timeout 30", "--risk", "high", "--repo", str(repo.root)]
    )
    assert rc == 2
    assert "Stop work on it" in capsys.readouterr().out


def test_the_knowledge_gap_cli_uses_its_own_capability(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert control_plane.knowledge_gap_main(["add", "--statement", "no spec found", "--repo", str(repo.root)]) == 0
    assert [e.event for e in store_mod.Store(repo).read_events()] == ["knowledge_gap"]


def test_the_cli_offers_no_verb_that_edits_or_removes_a_record() -> None:
    """A record is added, never edited or removed. The argparse choice list is the whole
    surface, so asserting on it is asserting on what a human (or an agent) can ask for."""
    parser = control_plane._build_parser("rein decision", "decision.declare")
    action = next(a for a in parser._actions if a.dest == "action")
    assert action.choices == ["add"]
