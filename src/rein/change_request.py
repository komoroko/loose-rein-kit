"""`rein changes` — "not yet, and here is what is wrong", recorded where it survives.

The lifecycle could record **yes** and could roll one back (`rein revise`, for gates already
approved). The answer in between — *I have read this, I am not approving it, change these things*
— lived in a chat message, so a gate could sit "ready" while the human had already said no.

A request is **anchored**: `--target` names a place (`docs/10-requirements.md#R-3`, `T-004`), not
a mood, so the agent answering it edits the slice the anchor names instead of re-running the phase
over the whole deliverable.

Three states, and which one blocks is the whole design:

  ``open``       a human asked. **Gate readiness refuses while any of these stand** — this is
                 what makes declining mean something rather than being a note.
  ``addressed``  the agent says it answered it, and names how. No longer blocking, but listed on
                 the approval screen, so the human reads what changed before deciding again.
  ``resolved``   closed by the gate approval that covered it.

The agent may move `open → addressed` because the alternative is a terminal round trip per fix,
and that is not a loophole: `addressed` approves nothing. It hands the decision back with a claim
attached, which the human reads beside the digests before answering `[y/N]`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from rein import common, event_chain, models, observations
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)


class ChangeRequestError(RuntimeError):
    """The request cannot be recorded, or does not exist."""


def new_id() -> str:
    """An identity, and nothing else.

    It used to carry the gate name — `CR-{gate.upper()}-{hex}` — which made the id's own schema
    pattern a second, weaker copy of the gate vocabulary. The copy went stale the moment a cycle
    could grow a gate named `T-001`: every request against a crossing died on
    `'CR-T-001-…' does not match '^CR-[A-Z]+-[0-9A-F]+$'`, so the one gate kind a human could not
    refuse was the irreversible one. Which gate a request stands against is the record's `gate`
    field; an id that restates it is a grammar to keep in step with for nothing.
    """
    return f"CR-{event_chain.new_id()[:8].upper()}"


def _find(state: models.State, request_id: str) -> Mapping[str, object] | None:
    return next((cr for cr in state.change_requests if cr.get("id") == request_id), None)


def render(requests: Sequence[Mapping[str, object]]) -> str:
    """The queue as lines. Anchors first — they are what a reader acts on."""
    if not requests:
        return "no change requests"
    width = max(len(str(cr.get("id", ""))) for cr in requests)
    lines = []
    for cr in requests:
        head = f"  {str(cr.get('id', '')).ljust(width)}  [{cr.get('status')}] {cr.get('gate')}: {cr.get('target')}"
        lines.append(f"{head}\n      {cr.get('reason')}")
        if cr.get("note"):
            lines.append(f"      → {cr.get('note')}")
    return "\n".join(lines)


# --- writes (one Store transaction each, with the audit event beside the change) ------


def add(repo: repo_mod.Repo, gate: str, target: str, reason: str) -> str:
    """Record a change request against `gate`. Returns its id.

    Deliberately possible at any time, including on a gate that is otherwise ready — that is the
    case it exists for. It needs no authority of any kind, because it can only ever *narrow* what
    happens next.
    """
    if not models.gate_name_ok(gate):
        raise ChangeRequestError(f"unknown gate {gate!r} ({models.gate_names()})")
    if not target.strip():
        raise ChangeRequestError(
            "a change request needs a --target: the file#anchor or id it is about "
            "(e.g. docs/10-requirements.md#R-3, or T-004). Without one, answering it means "
            "re-reading the whole deliverable."
        )
    if not reason.strip():
        raise ChangeRequestError("a change request needs a --reason: what is wrong, in the human's own words")

    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None:
        raise ChangeRequestError("no .rein/state.yaml — run `rein init` first")
    # Spelling was checked above; this is the other question, and the one a caller holding a state
    # owes. A request filed against a gate this cycle does not have holds nothing shut and appears
    # in no `rein changes list --gate <one it has>`, which reads as "recorded" and is not.
    if absent := state.gate_absence_reason(gate):
        raise ChangeRequestError(absent)
    request_id = new_id()

    with store.transaction() as tx:
        current = tx.store.read_state()
        if current is None:
            raise ChangeRequestError("no .rein/state.yaml to record the request in")
        raw = json.loads(json.dumps(current.raw))
        raw.setdefault("change_requests", []).append(
            {
                "id": request_id,
                "gate": gate,
                "target": target.strip(),
                "reason": reason.strip(),
                "status": "open",
                "opened_at": event_chain.now_iso(),
            }
        )
        raw["updated_at"] = event_chain.now_iso()
        tx.write("state", raw, expect_digest=store_mod.read_digest(current))
        tx.append(
            "changes_requested",
            cycle_id=current.cycle_id,
            subject_ids=[gate, request_id, target.strip()],
            detail={"reason": reason.strip()},
        )
    # After the transaction, and only for a `local` decision that actually exists in this plan.
    # A change request aimed at such a decision is the human saying the loop's reading of the reach
    # was wrong: it called the decision cheap to undo, and the person who would pay disagreed. That
    # is what falsifies selection-by-reach.
    #
    # A request aimed at a `mandate` decision is the opposite reading — the loop routed it to a
    # human and the human is using the route — and counting it here would put the claim's successes
    # into the figure that exists to falsify it. Nor is a bare `D-` prefix enough: `target` is free
    # text, so `docs/D-spec.md` would be counted as an overruled decision that no plan contains.
    if gate == "mandate":
        overruled = _overruled_local_decision(store, target.strip())
        if overruled is not None:
            observations.record(
                "reach_overruled",
                project=repo.root.name,
                cycle_id=state.cycle_id,
                subject=overruled,
                arm=observations.ARM_TOO_LOCAL,
            )
    return request_id


def _overruled_local_decision(store: store_mod.Store, target: str) -> str | None:
    """The id of the `local` decision `target` names, or None when it names something else.

    Resolved against `plan.decisions` rather than pattern-matched on the string: the quantity is
    only meaningful if every reading of it is a decision the loop really did settle on its own.
    An unreadable plan yields None — an observation is never worth failing a change request over,
    and a guess would be worse than a gap.
    """
    try:
        plan = store.read_plan()
    except models.DocumentError:
        return None
    if plan is None:
        return None
    decision = next((d for d in plan.decisions if d.id == target), None)
    return decision.id if decision is not None and decision.is_local else None


def address(repo: repo_mod.Repo, request_id: str, note: str) -> str:
    """Mark a request answered, with a note saying how. Returns the gate it belongs to.

    The note is required. "Addressed" with nothing behind it is the shape of a status field being
    cleared to make a board green, and this one stops a gate from being blocked — so it has to say
    what was done, and the human reads it beside the digests before approving.
    """
    if not note.strip():
        raise ChangeRequestError(
            "--note is required: say what was changed. This is what the human reads next to the "
            "digests before they decide, so 'done' is not an answer."
        )
    store = store_mod.Store(repo)
    with store.transaction() as tx:
        current = tx.store.read_state()
        if current is None:
            raise ChangeRequestError("no .rein/state.yaml")
        existing = _find(current, request_id)
        if existing is None:
            raise ChangeRequestError(f"no change request {request_id!r} — `rein changes list` shows the open ones")
        if existing.get("status") != "open":
            raise ChangeRequestError(f"{request_id} is already {existing.get('status')}")

        raw = json.loads(json.dumps(current.raw))
        for entry in raw.get("change_requests", []):
            if entry.get("id") == request_id:
                entry["status"] = "addressed"
                entry["addressed_at"] = event_chain.now_iso()
                entry["note"] = note.strip()
        raw["updated_at"] = event_chain.now_iso()
        tx.write("state", raw, expect_digest=store_mod.read_digest(current))
        tx.append(
            "changes_addressed",
            cycle_id=current.cycle_id,
            subject_ids=[str(existing.get("gate")), request_id],
            detail={"note": note.strip()},
        )
    return str(existing.get("gate"))


def resolve_addressed(raw: dict[str, object], gates: Iterable[str]) -> list[str]:
    """Close the addressed requests standing against any of `gates`, in place. Returns their ids.

    Called from inside `approve.record_approval`'s transaction: the approval is what closes them,
    because the approval is the human reading the notes and deciding they are answered. Open
    requests are untouched — readiness refused before this point, so there should be none.

    **Several gates, because one approval can end more than one gate's life.** Approving the
    mandate re-derives the cycle's crossing gates from the plan it freezes, so a crossing the new
    cut no longer declares irreversible stops existing in that same write. A request standing
    against it used to be left behind pointing at a gate `State.gate_ids` no longer lists: it
    blocked nothing, because readiness only ever asks about a gate it has, and it appeared in no
    `rein changes list --gate <one this cycle has>`. Recorded, and holding nothing shut — the exact
    state :func:`add` refuses to create. The gate it stood against is gone, so the approval that
    removed it closes it, and the receipt of that approval is what the record points back to.
    """
    wanted = set(gates)
    closed: list[str] = []
    entries = raw.get("change_requests")
    if not isinstance(entries, list):
        return closed
    for entry in entries:
        if isinstance(entry, dict) and entry.get("gate") in wanted and entry.get("status") == "addressed":
            entry["status"] = "resolved"
            closed.append(str(entry.get("id")))
    return closed


def open_against(state: models.State, gates: Iterable[str]) -> list[Mapping[str, Any]]:
    """Every still-open request standing against one of `gates`, grouped by gate in that order.

    Built on `State.change_requests_for` rather than filtering `change_requests` again, so "what
    is open against a gate" keeps one definition: the readiness check that refuses a gate and this
    one, which refuses to let a gate be removed out from under a request, must not be able to
    disagree about which requests count.
    """
    return [cr for gate in gates for cr in state.change_requests_for(gate, "open")]


# --- CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rein changes", description="ask for changes instead of approving, and answer them"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    add_p = sub.add_parser("add", help="record a change request against a gate (holds it shut)")
    add_p.add_argument("gate", help=models.gate_names())
    add_p.add_argument("--target", required=True, help="what it is about: docs/10-requirements.md#R-3, T-004, C-001")
    add_p.add_argument("--reason", required=True, help="what is wrong")

    list_p = sub.add_parser("list", help="show the change requests (open ones by default)")
    list_p.add_argument("--gate", default="", help="only this gate")
    list_p.add_argument("--all", action="store_true", help="include addressed and resolved")
    list_p.add_argument("--json", action="store_true", help="machine-readable (what a phase command reads)")

    addr_p = sub.add_parser("address", help="mark a request answered, naming how")
    addr_p.add_argument("id", help="the change request id (CR-...)")
    addr_p.add_argument("--note", required=True, help="what was changed")

    for name in ("add", "list", "address"):
        sub.choices[name].add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")

    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    try:
        return _dispatch(repo, args)
    except (ChangeRequestError, models.DocumentError, store_mod.StoreError) as exc:
        logger.error(str(exc))
        return 1


def _dispatch(repo: repo_mod.Repo, args: argparse.Namespace) -> int:
    if args.command == "add":
        request_id = add(repo, args.gate, args.target, args.reason)
        print(f"{request_id} recorded — gate '{args.gate}' stays shut until it is addressed")
        print(f"  the agent answers it with: rein changes address {request_id} --note <what changed>")
        return 0

    if args.command == "address":
        gate = address(repo, args.id, args.note)
        print(f"{args.id} addressed — it no longer blocks gate '{gate}', and the approval screen will show it")
        return 0

    state = store_mod.Store(repo).read_state()
    if state is None:
        logger.error("no .rein/state.yaml")
        return 1
    requests = state.change_requests
    if args.gate:
        requests = [cr for cr in requests if cr.get("gate") == args.gate]
    if not args.all:
        requests = [cr for cr in requests if cr.get("status") != "resolved"]
    if args.json:
        print(json.dumps(requests, ensure_ascii=False, indent=2))
    else:
        print(render(requests))
    return 0


if __name__ == "__main__":
    sys.exit(main())
