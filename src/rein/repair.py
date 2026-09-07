"""Which of gate ④'s findings the loop repairs, and which a human decides.

Inside a task, judging and repairing are already separated *and both automated*: the reviewer
writes findings, the implementer resolves the `must_fix` ones, the reviewer looks again
(`build_loop._run_agent_step`). At gate ④ only the judging half was automated. `review.assemble`
produced findings and the loop printed three commands for somebody to type.

The route back into the code was `rein revise --to build --from-review`, which marks the task
**and its whole dependent closure** `needs-revision` — the status reserved for a defect in the
*specification*. So `status_api` then demanded a `/tasks` reconcile and a re-approval of gate ③,
for a repair that changes no requirement, no claim and no plan. Reset, salvage, re-approve, and
round again: that loop is what this module ends.

**A finding is routed by what repairing it would change, not by who found it.**

* **code** — the repair lands inside one task's declared scope and touches no claim, no
  acceptance criterion and no requirement. The loop repairs it, and no gate moves. That the
  repair cannot become a plan change is mechanical rather than promised:
  `gate_guard.FROZEN_AFTER_GATE_THREE` denies a write to `plan.yaml` or `config.yaml` while the
  plan is frozen, which it is from gate ③ onward.
* **plan** — the repair needs a claim, an acceptance criterion or a requirement to change. A
  human, through `/revise`. Not this loop's, at any budget.
* **judgement** — deciding which of those two it *is*. A claim that came back `diverged` may mean
  the code is wrong or the plan is wrong, and nothing mechanical separates them; an extra
  behaviour nobody asked for may be unwanted or may be the plan's omission. These are the Decision
  Cards, and they are what a human is actually for.

The third class is where the loop stops being autonomous, and it is deliberately the smallest one
that is honest. It also feeds back: `decision_cards.OPTION_DISPOSITION` already names the repair
each option means, and an answer of `revise_implementation` says the code is the thing that is
wrong — which makes the subject a **code** repair on the next round. The human decides *whether*;
the loop does the work.

Nothing here reads a model's opinion. Attribution is `findings.attribute`, which matches a
finding's own code anchors against the task scopes the plan declares, and a finding no scope owns
is never guessed at — it goes to the human, because "no task covers this path" means the plan does
not say.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from rein import findings as findings_mod
from rein import models

#: The disposition that says "the code is what is wrong". A human answering a Decision Card with
#: it hands the subject back to the loop; every other answer keeps it.
REPAIRS_THE_CODE = "revise_implementation"


@dataclass(frozen=True)
class Repair:
    """One task, and the findings of gate ④ it has to answer."""

    task_id: str
    items: tuple[findings_mod.Attribution, ...]

    def render(self) -> str:
        return "\n".join(f"- {a.finding_id} ({a.kind}) at {a.basis or 'no anchor'}" for a in self.items)


@dataclass(frozen=True)
class Routing:
    """Gate ④'s blocking findings, split by who can act on each."""

    #: Grouped by task, in plan order, so one launch answers everything about one scope.
    code: tuple[Repair, ...] = ()
    #: A human's, through a Decision Card. The loop has nothing to do with these.
    judgement: tuple[findings_mod.Attribution, ...] = ()
    #: A human's, and nobody's scope owns them — the plan does not say who they belong to.
    unowned: tuple[findings_mod.Attribution, ...] = field(default=())

    @property
    def repairable(self) -> bool:
        return bool(self.code)

    def render(self) -> str:
        lines: list[str] = []
        for repair in self.code:
            lines.append(f"  {repair.task_id}: {len(repair.items)} finding(s) the loop can repair")
            lines += [f"    {line}" for line in repair.render().splitlines()]
        if self.judgement:
            lines.append(f"  {len(self.judgement)} finding(s) waiting on your decision:")
            lines += [f"    - {a.finding_id} ({a.kind})" for a in self.judgement]
        if self.unowned:
            lines.append(f"  {len(self.unowned)} finding(s) no task's declared scope owns:")
            lines += [f"    - {a.finding_id} ({a.kind}) at {a.basis or 'no anchor'}" for a in self.unowned]
        return "\n".join(lines) or "  nothing blocking."


def answered_to_repair(human: Mapping[str, object] | None) -> set[str]:
    """Subjects a human answered with `revise_implementation` — "the code is the thing that is wrong".

    Read from the human half rather than derived, because it is the one input here that is not
    mechanical: it is a person saying which side of a `diverged` claim is the mistaken one. Until
    this was read, every option on every Decision Card was recorded and none of them did anything.
    """
    entries = (human or {}).get("dispositions")
    if not isinstance(entries, list):
        return set()
    return {
        str(entry.get("subject_id", ""))
        for entry in entries
        if isinstance(entry, Mapping) and str(entry.get("action", "")) == REPAIRS_THE_CODE
    }


def route(plan: models.Plan, review: models.Review | None, human: Mapping[str, object] | None = None) -> Routing:
    """Split gate ④'s blocking findings into what the loop repairs and what a human decides.

    A security finding is a **code** repair as soon as a task's scope owns its anchor. Nothing
    about it is a question: the reviewer read the code, named the lines, and the plan says whose
    they are. Everything else starts as a **judgement** and becomes a code repair only when a
    human has said so on the record (:func:`answered_to_repair`).
    """
    if review is None or not review.is_generated:
        return Routing()
    decided = answered_to_repair(human if human is not None else review.human)

    by_task: dict[str, list[findings_mod.Attribution]] = {}
    judgement: list[findings_mod.Attribution] = []
    unowned: list[findings_mod.Attribution] = []
    for attribution in findings_mod.attribute(plan, review):
        if not attribution.owned:
            unowned.append(attribution)
            continue
        if attribution.kind == "security" or attribution.finding_id in decided:
            by_task.setdefault(attribution.task_id, []).append(attribution)
        else:
            judgement.append(attribution)

    order = [task.id for task in plan.tasks]
    code = tuple(
        Repair(task_id, tuple(by_task[task_id]))
        for task_id in sorted(by_task, key=lambda tid: order.index(tid) if tid in order else len(order))
    )
    return Routing(code=code, judgement=tuple(judgement), unowned=tuple(unowned))


def next_step(routing: Routing, rounds_left: int) -> str:
    """What a human should be told is happening, or has to happen. "" when nothing is blocking."""
    if routing.repairable and rounds_left > 0:
        subjects = sum(len(r.items) for r in routing.code)
        return f"repairing {subjects} finding(s) across {len(routing.code)} task(s); {rounds_left} round(s) left"
    if routing.repairable:
        return "the repair rounds are spent and findings still stand — read them in `rein ui`"
    if routing.judgement or routing.unowned:
        return "what is left is a decision, not a repair — answer the cards in `rein ui`"
    return ""
