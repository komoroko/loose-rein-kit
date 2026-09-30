"""`rein reviews` — which reviews run, the one terminal path that changes them, and the record they are held to.

  rein reviews show [--json]
  rein reviews apply <file> --reason "why"
  rein reviews restore

`.rein/reviews.yaml` is outside the mandate's freeze (`models.Reviews`): which tools raise the
quality of the work is inside what the mandate delegates, so changing one rewinds nothing. What
the freeze used to guarantee is kept another way, in two halves.

**Who writes it.** `rein reviews apply` insists on a terminal and a `[y/N]` exactly as `rein approve`
does; the dashboard's write session is minted only by the launch link `rein ui` prints. `rein guard`
refuses an agent's edit of the file (rule 1).

**What it is checked against.** The edit hook never sees a shell write, so who may write it
guarantees nothing by itself. Every write appends a `reviews_changed` event carrying the whole
document it wrote and its digest (`store.Transaction` refuses a write of the file without one), and
every reader — the build, the commit-stage guard, `doctor`, `cycle-close`, this verb — compares the
file on disk with the last such record (:func:`binding_problem`). A file that differs was changed by
something that recorded nothing, and nothing runs on it until `restore` writes the recorded one back.
`cycle_initialized` carries the record into the next cycle's chain.

`apply` takes a whole document rather than one knob at a time: the dashboard edits the same shape,
so the terminal and the page cannot disagree about what a change is. It is applied against the
version its author read (`expect`), because a change is relative to what somebody saw.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rein import common, data, digests, event_chain, models, strict_yaml
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

_REASON_MAX = 500

#: The packaged document `rein init` and `rein sync` seed.
PACKAGED = "scaffold/rein/reviews.yaml"


class ReviewsError(common.ReinError):
    """A refused change, or a reviews.yaml nothing may run on, with a message that names the next step."""


@dataclass(frozen=True)
class Record:
    """One point in the chain that gave reviews.yaml its content: what, by whom, when, and why."""

    document: Mapping[str, Any]
    digest: str
    actor: str
    ts: str
    reason: str
    #: Position in the chain it was read from.
    index: int

    def adversarial(self, stage: str) -> bool:
        return models.Reviews(self.document).adversarial(stage)


def records(events: Sequence[models.Event]) -> list[Record]:
    """Every record of reviews.yaml's content in `events`, in chain order."""
    out: list[Record] = []
    for index, event in enumerate(events):
        if event.event == "reviews_changed":
            detail: Mapping[str, Any] = event.detail
            reason = str(detail.get("reason", ""))
        elif event.event == "cycle_initialized" and isinstance(event.detail.get("reviews"), Mapping):
            detail = event.detail["reviews"]
            reason = f"carried over from cycle {event.detail.get('previous_cycle', '')}"
        else:
            continue
        document = detail.get("document")
        if not isinstance(document, Mapping) or not detail.get("digest"):
            raise ReviewsError(f"the {event.event} event at chain position {index} records no reviews document")
        out.append(
            Record(
                document=document,
                digest=str(detail["digest"]),
                actor=event.actor,
                ts=event.ts,
                reason=reason,
                index=index,
            )
        )
    return out


def recorded(repo: repo_mod.Repo) -> Record | None:
    """The content the chain last gave reviews.yaml, or None when it has given it none."""
    events, _ = event_chain.scan(repo.events)
    found = records(events)
    return found[-1] if found else None


def adversarial_switched_off(events: Sequence[models.Event], *, since: int) -> list[dict[str, str]]:
    """The drafting stages whose adversarial review was off at any point from `since` on, with when and why.

    `since` is where the drafting round began in the chain (after the last mandate approval, or its
    start). A stage counts when it was already off then, or was switched off afterwards, even if it
    is on again now: the review that did not run while the stage was drafted is not brought back by
    switching it on before the approval. Rows are `{id: stage, change: what happened}`.
    """
    found = records(events)
    at_start = next((r for r in reversed(found) if r.index < since), None)
    during = [r for r in found if r.index >= since]
    rows: list[dict[str, str]] = []
    for stage in models.ADVERSARIAL_STAGES:
        notes: list[str] = []
        was = at_start.adversarial(stage) if at_start is not None else None
        if at_start is not None and not was:
            notes.append(f"off when this round began (set {at_start.ts} by {at_start.actor}: {at_start.reason})")
        for record in during:
            now = record.adversarial(stage)
            if now == was or (was is None and now):
                was = now
                continue
            notes.append(
                f"{'switched back on' if now else 'switched off'} {record.ts} by {record.actor}: {record.reason}"
            )
            was = now
        if notes:
            rows.append({"id": stage, "change": "; ".join(notes)})
    return rows


def binding_problem(repo: repo_mod.Repo) -> str:
    """Why reviews.yaml may not be run on as it sits on disk, or "" when it is what the chain records."""
    last = recorded(repo)
    live = store_mod.Store(repo).document_digest("reviews")
    if last is None:
        if not live:
            return f"no {repo.reviews} — `rein sync` seeds the packaged one"
        return (
            f"{repo.reviews} is recorded nowhere in the audit chain, so nothing says a person chose it. "
            "Apply it at your terminal: `rein reviews apply .rein/reviews.yaml --reason ...`"
        )
    if live == last.digest:
        return ""
    where = f"last recorded {last.ts} by {last.actor}"
    if not live:
        return f"{repo.reviews} is missing, and the audit chain records one ({where}) — `rein reviews restore`"
    return (
        f"{repo.reviews} is not what the audit chain records ({where}): something changed it without "
        "recording it. `rein reviews restore` writes the recorded one back; a change a person means to make "
        "goes through `rein reviews apply`."
    )


def require_bound(repo: repo_mod.Repo) -> models.Reviews:
    """reviews.yaml, refused unless it is what the chain records. What every reader runs on."""
    problem = binding_problem(repo)
    if problem:
        raise ReviewsError(problem)
    reviews = store_mod.Store(repo).read_reviews()
    if reviews is None:
        raise ReviewsError(f"{repo.reviews} vanished while it was being read")
    return reviews


def _record_write(
    tx: store_mod.Transaction,
    document: Mapping[str, Any],
    *,
    expect: str | None,
    cycle_id: str,
    actor: str,
    reason: str,
    changes: Sequence[str],
) -> None:
    tx.write("reviews", document, expect_digest=expect)
    tx.append(
        "reviews_changed",
        cycle_id=cycle_id,
        actor=actor,
        detail={
            "reason": reason[:_REASON_MAX],
            "changes": list(changes),
            "document": dict(document),
            "digest": digests.of(document),
        },
    )


def describe(before: Mapping[str, Any] | None, after: Mapping[str, Any]) -> list[str]:
    """What a change does, one line per thing a reader would call a change.

    Both sides are normalized documents (`models.Reviews.normalized`), and every difference
    between two of them is a line here — order included, because the steps run in it and a
    reviewer is asked in it. :func:`prepare` refuses a difference this finds no line for.
    """
    old = models.Reviews(before or {})
    new = models.Reviews(after)
    lines: list[str] = []
    for stage in models.ADVERSARIAL_STAGES:
        now = new.adversarial(stage)
        if before is None or old.adversarial(stage) != now:
            lines.append(f"adversarial review at {stage}: {'on' if now else 'OFF'}")
    old_steps = {step.name: step for step in old.steps}
    new_steps = {step.name: step for step in new.steps}
    for step in old.steps:
        if step.name not in new_steps:
            lines.append(f"step {step.name}: removed (was {', '.join(step.reviews)})")
    for step in new.steps:
        if step.name not in old_steps:
            lines.append(f"step {step.name}: added, reads for {', '.join(step.reviews)}")
    kept_before = [s.name for s in old.steps if s.name in new_steps]
    kept_after = [s.name for s in new.steps if s.name in old_steps]
    if kept_before != kept_after:
        lines.append(f"steps run in the order {', '.join(kept_after)} (was {', '.join(kept_before)})")
    for name in kept_after:
        was, now_step = old_steps[name], new_steps[name]
        if dropped := [r for r in was.reviews if r not in now_step.reviews]:
            lines.append(f"step {name}: no longer reads for {', '.join(dropped)}")
        if added := [r for r in now_step.reviews if r not in was.reviews]:
            lines.append(f"step {name}: now reads for {', '.join(added)}")
        order_before = [r for r in was.reviews if r in now_step.reviews]
        order_after = [r for r in now_step.reviews if r in was.reviews]
        if order_before != order_after:
            lines.append(f"step {name}: asks in the order {', '.join(order_after)} (was {', '.join(order_before)})")
        for key, was_value, now_value in (
            ("retries", was.retries, now_step.retries),
            ("stage", was.stage, now_step.stage),
            ("paths", list(was.paths), list(now_step.paths)),
        ):
            if was_value != now_value:
                lines.append(f"step {name}: {key} {was_value} → {now_value}")
    old_questions, new_questions = old.questions, new.questions
    for name in sorted(old_questions.keys() - new_questions.keys()):
        lines.append(f"custom review {name}: removed")
    for name in sorted(new_questions.keys() - old_questions.keys()):
        lines.append(f"custom review {name}: added, asks: {new_questions[name]}")
    for name in sorted(old_questions.keys() & new_questions.keys()):
        if old_questions[name] != new_questions[name]:
            lines.append(f"custom review {name}: now asks: {new_questions[name]} (was: {old_questions[name]})")
    return lines


@dataclass(frozen=True)
class Proposal:
    """A validated next reviews.yaml, against the record it would replace."""

    current: Record | None
    document: dict[str, Any]
    changes: list[str]

    @property
    def expect(self) -> str:
        """The digest of the version this proposal was made against ("" when none is recorded)."""
        return self.current.digest if self.current is not None else ""


def prepare(repo: repo_mod.Repo, document: Mapping[str, Any]) -> Proposal:
    """Validate `document` as the next reviews.yaml and say what it changes.

    Refused while the file on disk is not what the chain records: "what changes" would be measured
    from a version nobody chose, and writing over it would erase the evidence that one was made.
    """
    errors = models.schema_errors(document, "reviews") or models.Reviews(document).problems()
    if errors:
        raise ReviewsError("the reviews document is not valid:\n  " + "\n  ".join(errors))
    current = recorded(repo)
    if current is not None and (problem := binding_problem(repo)):
        raise ReviewsError(problem)
    proposed = models.Reviews(document).normalized()
    before = models.Reviews(current.document).normalized() if current is not None else None
    if proposed == before:
        return Proposal(current=current, document=proposed, changes=[])
    changes = describe(before, proposed)
    if not changes:
        raise AssertionError(f"describe() has no line for a change it was handed: {before!r} → {proposed!r}")
    return Proposal(current=current, document=proposed, changes=changes)


def apply(repo: repo_mod.Repo, document: Mapping[str, Any], reason: str, *, actor: str, expect: str) -> list[str]:
    """Write `document` as reviews.yaml and record it. Returns what changed ([] = nothing).

    `expect` is the digest of the version the change was made against (`Proposal.expect`, or the
    `digest` the dashboard was served). A change is relative to what its author saw; applied over
    anything else it would silently undo somebody else's.

    The caller is the one who established that a person is doing this — a terminal confirmation,
    or the dashboard's write session — and says which in `actor`.
    """
    if not reason.strip():
        raise ReviewsError("a change to which reviews run needs a reason — it is recorded, and acceptance reads it")
    store = store_mod.Store(repo)
    state = store.read_state()
    if state is None or not state.cycle_id:
        raise ReviewsError("no cycle to record the change under — run `rein init` first")
    with store.transaction() as tx:
        # Under the store lock: every write of the file is a transaction, so nothing can land
        # between this reading of the record and the write below.
        proposal = prepare(repo, document)
        if proposal.expect != expect:
            raise store_mod.StaleWriteError(
                "reviews.yaml changed since it was read — nothing was applied. Read it again and redo the change."
            )
        if not proposal.changes:
            return []
        _record_write(
            tx,
            proposal.document,
            expect=proposal.expect or store.document_digest("reviews") or None,
            cycle_id=state.cycle_id,
            actor=actor,
            reason=reason,
            changes=proposal.changes,
        )
    return proposal.changes


def seed(repo: repo_mod.Repo, *, actor: str) -> bool:
    """Write and record the packaged reviews.yaml when the chain has never recorded one. True when written.

    Only then. Once a record exists a missing file is restored from it, never re-seeded: seeding
    over a record would reset a person's choice to the default for anyone who deletes the file.
    """
    store = store_mod.Store(repo)
    if repo.reviews.exists() or recorded(repo) is not None:
        return False
    state = store.read_state()
    if state is None or not state.cycle_id:
        raise ReviewsError("no cycle to record the packaged reviews under — run `rein init` first")
    document = models.Reviews.parse(data.read_text(PACKAGED), what=PACKAGED).normalized()
    with store.transaction() as tx:
        _record_write(
            tx,
            document,
            expect=None,
            cycle_id=state.cycle_id,
            actor=actor,
            reason="the packaged reviews, seeded",
            changes=describe(None, document),
        )
    return True


def restore(repo: repo_mod.Repo) -> bool:
    """Write back the reviews.yaml the chain records. True when the file had to change.

    Anyone may run this, an agent included: it can only return the file to what a person last
    chose, and it records that the file had been changed without a record.
    """
    last = recorded(repo)
    if last is None:
        raise ReviewsError("the audit chain records no reviews.yaml to restore — `rein sync` seeds the packaged one")
    store = store_mod.Store(repo)
    live = store.document_digest("reviews")
    if live == last.digest:
        return False
    state = store.read_state()
    if state is None or not state.cycle_id:
        raise ReviewsError("no cycle to record the restore under")
    with store.transaction() as tx:
        tx.write("reviews", last.document, expect_digest=live or None)
        tx.append(
            "reviews_restored",
            cycle_id=state.cycle_id,
            actor="rein reviews restore",
            detail={"digest": last.digest, "replaced": live},
        )
    return True


def render(reviews: models.Reviews) -> str:
    lines = ["adversarial review before the mandate:"]
    lines += [f"  {stage}: {'on' if reviews.adversarial(stage) else 'OFF'}" for stage in models.ADVERSARIAL_STAGES]
    lines.append("reviewer steps:")
    lines += [
        f"  {step.name} ({step.stage}, retries {step.retries}): {', '.join(step.reviews)}" for step in reviews.steps
    ] or ["  (none — no reviewer reads the code before acceptance)"]
    if reviews.custom:
        lines.append("custom reviews:")
        lines += [f"  {name}: {question}" for name, question in reviews.questions.items()]
    lines.append("acceptance: actual extraction, comparison, security review (not configurable)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein reviews", description="which reviews run, and changing them")
    sub = parser.add_subparsers(dest="action", required=True)
    show = sub.add_parser("show", help="print which reviews run")
    show.add_argument("--json", action="store_true", help="the document itself, for a script or a prompt")
    show.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    change = sub.add_parser("apply", help="replace reviews.yaml with <file>, confirmed at this terminal")
    change.add_argument("file", help="the reviews document to apply (YAML, the shape `show --json` prints)")
    change.add_argument("--reason", required=True, help="why — recorded in the audit chain, shown at acceptance")
    change.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    back = sub.add_parser("restore", help="write back the reviews.yaml the audit chain records")
    back.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    try:
        if args.action == "show":
            reviews = require_bound(repo)
            text = json.dumps(dict(reviews.raw), ensure_ascii=False, indent=2) if args.json else render(reviews)
            print(common.terminal_text(text))
            return 0
        if args.action == "restore":
            print(
                "restored — reviews.yaml is what the audit chain records again"
                if restore(repo)
                else "nothing to restore: reviews.yaml is what the audit chain records"
            )
            return 0
        if not common.stdin_is_terminal():
            logger.error(
                "changing which reviews run needs a confirmation typed at a terminal, and stdin is not one. "
                "Run this in your shell — there is deliberately no flag that skips it."
            )
            return 2
        if not args.reason.strip():
            logger.error("--reason is empty — a change to which reviews run is recorded with why")
            return 2
        document = strict_yaml.load_mapping(Path(args.file).read_text(encoding="utf-8"), what=args.file)
        proposal = prepare(repo, document)
        if not proposal.changes:
            print("nothing changes: the document says what reviews.yaml already says")
            return 0
        print(common.terminal_text("This changes which reviews run:\n" + "\n".join(f"  {c}" for c in proposal.changes)))
        if not common.ask_yes_no("Apply it?"):
            print("nothing was changed")
            return 1
        # Against the version just shown: a change landing while the question was open is refused,
        # not written over.
        apply(repo, document, args.reason, actor="local-confirmation", expect=proposal.expect)
        print("applied — the next reading of each task uses it; tasks already read keep what they were read for")
        return 0
    except (ReviewsError, models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError, OSError) as exc:
        logger.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
