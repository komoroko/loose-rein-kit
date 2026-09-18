"""`rein lens` — read the library, see what this cycle would apply, and count what it found.

Three questions, and the third is the one that keeps the library from rotting. `--list` is what
exists. `--select <stage>` is what would be pointed at this cycle's deliverable and what a human is
being asked about. `--stats` is how often each lens was applied and how often it found something,
read out of the audit chain across every cycle this repository has archived.

The stats have no threshold attached and never will. A ceiling on how many lenses may exist gets
answered by deleting whichever is cheapest to delete, not whichever has stopped earning its place;
what a person needs is the name of the lens that has been applied eleven times and found nothing,
so they can look at it and decide whether the condition is wrong or the cause is gone.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
from collections import defaultdict
from collections.abc import Mapping, Sequence

from rein import common, event_chain, lens_judge, lenses, models, review_reading
from rein import events as events_mod
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)


def stats(events: Sequence[models.Event]) -> dict[str, dict[str, int]]:
    """Per lens: how often it was selected into a plan, applied, and found something.

    `selected` counts `lens_selected`, which names every lens the resolution wrote into the plan —
    the `proposed` ones as much as the `applied` ones. It is here because the retirement rule could
    not see a whole class of lens without it. A lens a human drops at the gate is deleted from the
    plan before the freeze, so it leaves no `lens_applied` behind and reads exactly like a lens
    whose condition never held: both are simply absent. The difference was always on record — the
    `lens_selected` event still names it — and this tally was reading the other event.
    """
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"selected": 0, "applied": 0, "found": 0})
    for event in events:
        if event.event == "lens_selected":
            for lens_id in event.subject_ids:
                counts[str(lens_id)]["selected"] += 1
            continue
        if event.event != "lens_applied":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        lens_id = str(detail.get("lens") or "")
        if not lens_id:
            continue
        counts[lens_id]["applied"] += 1
        if detail.get("found") is True:
            counts[lens_id]["found"] += 1
    return dict(counts)


#: Said wherever the output invites an edit to the library. The counts come from this repository's
#: audit chain and its archives; the library they point at is user-global. `--stats` used to end
#: "Narrow it in <user-global path>, or drop it" on the strength of one repository's numbers, so
#: the reading it directed was wider than the reading it had. Which range the statistics should
#: cover is a separate question (`20-open.md` item 9); saying which one they *do* cover is not.
_SCOPE_NOTE = (
    "These counts are this repository's chain and archives only. The library is shared across "
    "every repository you use, so check the others before narrowing, dropping or adding anything "
    "in it."
)

#: The other half. Everything above is a reason to remove a lens: a tally over lenses that exist
#: cannot see the one nobody wrote, so a library read through this output alone only ever shrinks —
#: with no threshold anywhere, which is what makes the drift quiet. Entry is a human's judgement at
#: a named occasion rather than a verb here, because the material for it (what went wrong this
#: cycle, against what was being watched for) is what the retrospective already puts side by side.
_ENTRY_NOTE = (
    "This tally can only ever argue for removal — it counts the lenses you have, never the one "
    "that was missing. Entry is section 2 of docs/retrospective.md, held against the root causes "
    "in section 1: rework whose cause no applied lens was watching for is a lens to write, by "
    "hand, into {path}."
)


def render_stats(counts: Mapping[str, Mapping[str, int]], library: Sequence[lenses.Lens]) -> str:
    known = {lens.id: lens for lens in library}
    rows = sorted(counts.items(), key=lambda kv: (-kv[1]["applied"], kv[0]))
    if not rows:
        return "no lens has been selected or applied yet — nothing to say about which ones earn their place"
    lines = [f"{'lens':<32} {'selected':>8} {'applied':>8} {'found':>6}  condition"]
    for lens_id, count in rows:
        lens = known.get(lens_id)
        where = lens.applies_when if lens is not None else "(no longer in the library)"
        lines.append(f"{lens_id:<32} {count['selected']:>8} {count['applied']:>8} {count['found']:>6}  {where}")
    silent = [lens_id for lens_id, c in rows if c["applied"] >= 2 and c["found"] == 0]
    if silent:
        lines.append("")
        lines.append(
            f"{len(silent)} lens(es) applied and never found anything: {', '.join(silent)}. "
            "Either the condition is wider than the failure, or the cause is gone. Narrow it in "
            f"{lenses.library_path()}, or drop it."
        )
    # Selected and never recorded as applied. Three things produce it and the tally cannot tell
    # them apart, so it names the count and not a cause: a human dropped it at the gate, the
    # reviewer never ran `--record`, or the cycle is still open. Naming it is what makes the first
    # one visible at all — a lens whose condition is wide enough to keep being proposed and keep
    # being dropped costs a judgement every cycle and reads, today, as a lens that never came up.
    unapplied = [lens_id for lens_id, c in rows if c["selected"] > 0 and c["applied"] == 0]
    if unapplied:
        lines.append("")
        lines.append(
            f"{len(unapplied)} lens(es) selected into a plan and never recorded as applied: "
            f"{', '.join(unapplied)}. Dropped at the gate, not recorded by the reviewer, or still "
            "in an open cycle — this tally cannot tell which, only that it was not simply absent."
        )
    lines.append("")
    lines.append(_ENTRY_NOTE.format(path=lenses.library_path()))
    lines.append("")
    lines.append(_SCOPE_NOTE)
    return "\n".join(lines)


# --- the conditions that take reading --------------------------------------------


def _text(path: pathlib.Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _deliverable(repo: repo_mod.Repo, plan: models.Plan, state: models.State | None, stage: str, task_id: str) -> str:
    """What a `conditional` lens's prose condition is decided against, for one stage.

    Each of the six packaged ones names its own source and says the plan is not it: the
    requirements, the design document, `docs/decisions/`, the tickets, the diff. So this is not a
    second reading of `plan.yaml` — everything decidable from the plan is a `standard` lens's
    `when:` block, already settled before anyone gets here.

    An empty string is the honest answer for a deliverable that is missing, unreadable, or (at the
    code stage) a diff git could not produce, and it ends in `unavailable` rather than in a verdict.
    """
    root = repo.root
    if stage == "requirements":
        return _text(root / "docs" / "10-requirements.md")
    if stage == "design":
        # The ADR lens asks whether a rejected option's downside is underplayed, and says the plan
        # does not index `docs/decisions/`. Nothing else would carry that.
        parts = [_text(root / "docs" / "20-design.md")]
        parts += [_text(adr) for adr in sorted((root / "docs" / "decisions").glob("ADR-*.md"))]
        return "\n\n".join(part for part in parts if part.strip())
    if stage == "tasks":
        tickets = sorted((root / "docs" / "tasks").glob("T-*.md"))
        if task_id:
            tickets = [ticket for ticket in tickets if ticket.stem == task_id]
        return "\n\n".join(part for part in (_text(ticket) for ticket in tickets) if part.strip())
    if stage != "code":
        return ""
    base = plan.base_commit
    if not base or set(base) == {"0"}:
        return ""  # a cycle that never recorded what it branched from has no diff to read
    include: tuple[str, ...] = ()
    if task_id:
        task = next((t for t in plan.tasks if t.id == task_id), None)
        include = tuple(task.scope_include) if task is not None else ()
    try:
        return review_reading.diff_of(repo, base, "HEAD", review_reading.not_the_product(repo, state), include=include)
    except Exception as exc:  # noqa: BLE001 - every failure here is "review more than necessary"
        logger.warning(f"the diff this stage's conditions are decided against could not be read: {exc}")
        return ""


def _recorded(events: Sequence[models.Event], stage: str, task_id: str) -> list[lens_judge.Verdict]:
    """The verdicts already in the chain for this hand-off, newest first, or `[]`.

    Asked once. The selection is resolved once against the plan and read back from it for the rest
    of the cycle, and a judgement re-run on every call would put the review's inputs back where the
    freeze took them from — a reviewer and the person who approved the gate could be looking at two
    different answers to the same question, with nothing saying so.
    """
    for event in reversed(events):
        if event.event != "lens_judged":
            continue
        detail = event.detail if isinstance(event.detail, Mapping) else {}
        if str(detail.get("stage") or "") != stage or str(detail.get("task") or "") != task_id:
            continue
        raw = detail.get("verdicts")
        if not isinstance(raw, list):
            return []
        found = [lens_judge.Verdict.from_dict(row) for row in raw if isinstance(row, Mapping)]
        return [verdict for verdict in found if verdict is not None]
    return []


def render_verdicts(verdicts: Sequence[lens_judge.Verdict], settings: lens_judge.Settings) -> str:
    """What was asked, what came back, and — when it changed nothing — that it changed nothing."""
    lines: list[str] = []
    for verdict in verdicts:
        at = "" if verdict.probability is None else f" p={verdict.probability:.2f}"
        note = f" ({verdict.reason})" if verdict.reason else ""
        lines.append(f"  {verdict.lens_id}: {verdict.outcome}{at}{note}")
    if not settings.may_drop and any(v.outcome == lens_judge.DOES_NOT_HOLD for v in verdicts):
        lines.append(
            "  (recorded only — `review_policy.lens_judgement.may_drop` is off, so none of these "
            "was removed from the hand-off)"
        )
    return "\n".join(lines)


def _judge_handoff(
    repo: repo_mod.Repo,
    store: store_mod.Store,
    plan: models.Plan,
    state: models.State | None,
    stage: str,
    task_id: str,
    proposed: Sequence[lenses.Lens],
    settings: lens_judge.Settings,
) -> tuple[list[lens_judge.Verdict], list[lenses.Lens]]:
    """`(verdicts, what still goes to the reviewer)` for the `conditional` half of a hand-off.

    This is the place because it is where the narrowing already happens. The selection is resolved
    once, against the whole plan, and that unit is right — `min_claims` counts what the plan states
    and no single task has a value for it. What was wrong was handing one list to readers with
    different reach, and the fix put the narrowing at the hand-off. A condition that takes reading
    the deliverable belongs at the same point, and for the same reason: **this is the first moment
    the deliverable exists.**

    Asked once per stage and task. A judgement re-run on every call would let a reviewer and the
    person who approved the gate hold two different answers to one question, which is the property
    the freeze exists to remove.
    """
    questions = {lens.id: lens.applies_when for lens in proposed if lens.applies_when}
    if not questions:
        return [], list(proposed)
    live, _ = event_chain.scan(repo.events)
    verdicts = _recorded(live, stage, task_id)
    if not verdicts:
        if not settings.configured:
            # Nothing is asked and nothing is recorded. The lenses stay candidates, which is what
            # this repository does today, and an event per call saying so would fill the chain with
            # the absence of a feature.
            return [], list(proposed)
        verdicts = lens_judge.judge(
            settings,
            state=_deliverable(repo, plan, state, stage, task_id),
            questions=questions,
        )
        with store.transaction() as tx:
            tx.append(
                "lens_judged",
                cycle_id=state.cycle_id if state is not None else "",
                subject_ids=sorted(questions),
                detail={
                    "stage": stage,
                    "task": task_id,
                    # Recorded beside the verdicts rather than looked up later: `config.yaml` is
                    # frozen at the mandate, but a reading of this chain years from now should not
                    # have to reconstruct which value was in force to know what the probabilities
                    # were compared against.
                    "threshold": settings.threshold,
                    "may_drop": settings.may_drop,
                    "verdicts": [verdict.as_dict() for verdict in verdicts],
                },
            )
    removed = lens_judge.dropped(verdicts, settings)
    return verdicts, [lens for lens in proposed if lens.id not in removed]


def _select(repo: repo_mod.Repo, library: Sequence[lenses.Lens], stage: str, task_id: str = "") -> int:
    """Print the selection for `stage`, freezing it into the plan the first time it is resolved.

    The library is user-global and a person edits it between cycles. If the reviewer at the code
    stage re-derived its own list, a change to one line of an overlay would change what this cycle
    was reviewed for, with nothing in the audit chain saying so — and the same repository would
    answer differently on another laptop. So the selection is resolved once, against the plan, and
    written into the plan, where the mandate freezes it with everything else.

    Before the freeze that write is re-done on every call, because the facts it is resolved against
    grow: `/req` runs with no tasks in the plan yet and `/tasks` runs with all of them. The last
    write before the mandate is the one that gets frozen. After the freeze nothing is re-derived.
    """
    store = store_mod.Store(repo)
    try:
        plan = store.read_plan()
    except models.DocumentError as exc:
        logger.error(str(exc))
        return 2
    if plan is None:
        logger.error("no .rein/plan.yaml — the selection is resolved against the plan")
        return 2
    state = store.read_state()
    frozen_plan = state is not None and "mandate" in state.approved_gates

    # What the scope entries in this plan actually cover. `None` is git failing to answer, which is
    # said out loud rather than passed on as an empty listing: a path condition decided against no
    # files at all holds nowhere, and a selection that quietly lost its path lenses is the failure
    # this whole hand-off exists to stop.
    tracked: tuple[str, ...] = ()
    if not frozen_plan or task_id:  # the only two readings that resolve a `paths` condition
        listing = repo.tracked_paths()
        if listing is None:
            logger.warning(
                "git could not list this repository's files, so a lens condition on `paths` is decided "
                "against the scope entries alone and may hold for fewer tasks than it should."
            )
        tracked = listing or ()

    if not frozen_plan:
        resolved = lenses.resolve(library, lenses.Facts.of(plan, tracked=tracked))
        lens_changed = resolved != [dict(entry.raw) for entry in plan.lenses]
        # The reaches this draft carries right now, against the last ones rein saw. This verb is
        # where the snapshot lives because it is the one plan-reading command every drafting
        # command already runs (`/req`, `/design`, `/tasks`), and it is the only "before" the
        # freeze can be compared against: a human moving a decision from `mandate` to `local` edits
        # the file, and nothing else in the harness is watching when they do.
        reaches = {d.id: d.reach for d in plan.decisions}
        live, _ = event_chain.scan(repo.events)
        reach_changed = reaches != event_chain.derived_reaches(live)
        if lens_changed or reach_changed:
            cycle_id = state.cycle_id if state is not None else ""
            with store.transaction() as tx:
                # Only when the selection actually moved. A snapshot of the reaches is a record
                # *about* the draft and changes nothing in it, so pairing it with a plan write
                # would re-write the document every time a decision was added.
                if lens_changed:
                    raw = json.loads(json.dumps(dict(plan.raw)))
                    raw["lenses"] = resolved
                    tx.write("plan", raw, expect_digest=store_mod.read_digest(plan))
                    tx.append(
                        "lens_selected",
                        cycle_id=cycle_id,
                        subject_ids=sorted({entry["id"] for entry in resolved}),
                        detail={"count": len(resolved), "stages": sorted({e["stage"] for e in resolved})},
                    )
                if reach_changed:
                    tx.append(
                        "decisions_derived",
                        cycle_id=cycle_id,
                        subject_ids=sorted(reaches),
                        detail={"reaches": reaches},
                    )
            if lens_changed:
                plan = store.read_plan()
                if plan is None:  # written and then unreadable: a defect, not a selection
                    logger.error("the plan could not be read back after writing the lens selection")
                    return 2

    applied_ids, proposed_ids = lenses.frozen(plan, stage=stage)
    applied, missing_applied = lenses.by_id(library, applied_ids)
    proposed, missing_proposed = lenses.by_id(library, proposed_ids)

    where = "frozen with the mandate" if frozen_plan else "resolved against this plan and written into it"
    narrowed = ""
    if task_id:
        task = next((t for t in plan.tasks if t.id == task_id), None)
        if task is None:
            # Refused rather than falling back to the whole list: a reviewer that asked for one
            # task's lenses and silently got every task's would be reviewing against a wider
            # selection than it thinks, with nothing on screen saying so.
            logger.error(f"no task {task_id!r} in this plan — `rein dag` names the ones it holds")
            return 2
        before = len(applied) + len(proposed)
        applied = lenses.for_task(applied, task, tracked=tracked)
        proposed = lenses.for_task(proposed, task, tracked=tracked)
        dropped = before - len(applied) - len(proposed)
        narrowed = f", narrowed to {task_id}"
        if dropped:
            narrowed += f" — {dropped} dropped, their paths are outside this task's scope"

    # The `conditional` half, decided against the thing its condition names. Only after the freeze:
    # before it there is no design document, no ticket and no diff to read, which is the whole
    # reason these lenses could not be settled from the plan in the first place.
    verdicts: list[lens_judge.Verdict] = []
    settings = lens_judge.Settings.of(store.read_config())
    if frozen_plan and proposed:
        verdicts, proposed = _judge_handoff(repo, store, plan, state, stage, task_id, proposed, settings)

    print(f"{stage}: {len(applied)} applied, {len(proposed)} proposed ({where}{narrowed})")
    if applied:
        print("\napplied (the condition is decidable, so nobody is asked):")
        print(lenses.render(applied))
    if proposed:
        print("\nproposed (deciding the condition takes judgement — keep or drop at the mandate):")
        print(lenses.render(proposed))
    if verdicts:
        print("\ndecided against this stage's deliverable:")
        print(render_verdicts(verdicts, settings))
    off = [lens.id for lens in library if lens.stage == stage and lens.lens_class == lenses.CLASS_UNCLASSIFIED]
    if off:
        print(f"\noff, no condition written down yet: {', '.join(off)}")
    if frozen_plan and not plan.lenses:
        logger.warning(
            "this mandate froze no lens selection, so there is nothing to read back. The review "
            "runs without lenses rather than against whatever the library says today; `rein revise "
            "--to mandate` reopens the plan if this cycle should have had them."
        )
    for lens_id in missing_applied + missing_proposed:
        # Named, never skipped. The frozen list is the record of what this cycle was reviewed for,
        # and an id the library no longer holds is precisely the drift the freeze exists to expose.
        logger.warning(f"{lens_id} was frozen into this plan and is no longer in the library — it cannot be applied")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="the review lens library: what exists, what applies, what it found")
    parser.add_argument("--list", action="store_true", help="every lens in the library, packaged and your own")
    parser.add_argument("--select", metavar="STAGE", help=f"what would apply at a stage ({', '.join(lenses.STAGES)})")
    parser.add_argument(
        "--task",
        metavar="T-NNN",
        default="",
        help="narrow --select to one task: lenses whose paths are outside its scope are dropped",
    )
    parser.add_argument("--stats", action="store_true", help="applied/found counts per lens, across archived cycles")
    parser.add_argument("--record", metavar="LENS", help="record that this lens was applied (with --found)")
    parser.add_argument("--found", choices=("yes", "no"), help="whether --record's lens found anything")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    library = lenses.library()
    if args.list:
        print(f"{len(library)} lens(es) — packaged: {lenses.packaged_path()}, yours: {lenses.library_path()}")
        for lens_class in (lenses.CLASS_STANDARD, lenses.CLASS_CONDITIONAL, lenses.CLASS_UNCLASSIFIED):
            chosen = [lens for lens in library if lens.lens_class == lens_class]
            if chosen:
                print(f"\n{lens_class} ({len(chosen)}):")
                print(lenses.render(chosen))
        return 0

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1

    if args.task and not args.select:
        logger.error("--task narrows --select; on its own it names a task with nothing to narrow")
        return 2

    if args.select:
        if args.select not in lenses.STAGE_VALUES:
            logger.error(f"unknown stage {args.select!r} — one of {', '.join(lenses.STAGES)}")
            return 2
        return _select(repo, library, args.select, args.task)

    if args.record:
        if args.found is None:
            logger.error("--record needs --found yes|no — an application nobody scored says nothing about the lens")
            return 2
        known = {lens.id for lens in library}
        if args.record not in known:
            # Refused rather than recorded: a count against an id no library holds can never be
            # read back beside a condition, which is the only thing the count is for.
            logger.error(f"unknown lens {args.record!r} — `rein lens --list` names the ones that exist")
            return 2
        store = store_mod.Store(repo)
        state = store.read_state()
        if state is None:
            logger.error("no .rein/state.yaml — run `rein init` first")
            return 2
        with store.transaction() as tx:
            tx.append(
                "lens_applied",
                cycle_id=state.cycle_id,
                subject_ids=[args.record],
                detail={"lens": args.record, "found": args.found == "yes"},
            )
        print(f"recorded: {args.record} applied, found={args.found}")
        return 0

    if args.stats:
        # Across archived cycles too. A lens that has stopped earning its place stopped doing so
        # over several cycles, and `cycle-close` moves the chain that would show it into the
        # archive — reading only the live one makes the answer go blank exactly when it matters.
        live, _ = event_chain.scan(repo.events)
        sources, unreadable = events_mod.cycle_sources(repo, live)
        every = [event for source in sources for event in source.events]
        print(render_stats(stats(every), library))
        for rel in unreadable:
            logger.warning(f"{rel} could not be verified, so its lens counts are not included")
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
