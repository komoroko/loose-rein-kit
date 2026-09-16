"""The review lenses that get applied, and the rule for which ones do.

`adversarial-reviewer.md` carried three fixed lists and told the reviewer to work through every
lens in the set, reporting each as a finding or as "attacked — no finding". A concurrency lens over
a change with no concurrency, an injection lens over a change that touches no store: every one of
those costs a pass over the deliverable and returns "attacked — nothing", and the cost is not only
the time. Findings compete for attention with each other, and a reviewer sent to look for failures
that cannot occur here brings back the ones that can *plus* noise. Over-reviewing is not thorough.

So a lens is not a paragraph in a prompt. It is a record with a **condition**, and the condition is
what decides whether it is applied at all:

* **`standard`** — the condition is decidable from the plan: the paths its tasks declare, the
  claims it answers, the risk they carry. Applied automatically when it holds, and nobody is asked.
* **`conditional`** — there is a condition, and deciding it takes reading the deliverable. Proposed
  at the mandate gate with the reason, where a human keeps or drops it in the pass they already
  make.
* **`unclassified`** — nothing has been written down about when this applies. **Off**, until
  somebody says otherwise for one cycle.

**Every lens carries a condition, and `standard` carries a machine-decidable one.** A `standard`
lens whose `when:` block is empty is not a lens that always applies — it is a lens whose condition
nobody finished writing, and it is loaded as `unclassified` rather than run on everything. What
makes that check bite is that the condition vocabulary reaches the early stages too: `min_claims`
and `min_tasks` and the named facts below are what a requirements-stage lens has instead of paths.
A handful of conditions *are* satisfied by any cycle that reaches the gate at all — `min_claims: 1`
is "this cycle states something", and ambiguity is worth attacking in any requirement ever
written. Those are conditions, written down and checked, not omissions; what the class system
refuses is the lens that never said.

**A cross-project library stays honest because every lens carries its condition.** The risk of
sharing lenses between repositories is a lens that fires where its failure cannot happen;
`standard` cannot, since its condition names what must be in the change, `conditional` states its
case to a human, and `unclassified` is off. So the library is user-global
(`$XDG_CONFIG_HOME/rein/lenses.yaml`, overlaying the packaged defaults) while the *selection* is
resolved once against the plan and written into `.rein/plan.yaml`, where the mandate freezes it —
the same shape `.rein/prompts/` already has. After the freeze `rein lens --select` reads that list
back and never re-derives it. A review's inputs must not depend on machine-local state, or the same
repository reviewed on another laptop, or after one line of a user overlay changed, answers
differently with nothing in the audit chain to show it.

**Qualification, and retirement.** A lens earns its place by the same rule a bug earns a
regression test: not the first time, but when the same cause comes back. Once is an incident; twice
is what tells you the condition. That is why a one-off finding goes to `unclassified` — there is
nothing yet to write in `when:`. And the reverse rule is what keeps the library from rotting:
`rein lens --stats` counts, per lens, how often it was applied and how often it found something. A
lens that keeps applying and never finds has the wrong condition or has outlived its cause.
Counted, never capped: a ceiling on how many lenses may exist would be answered by deleting
whichever is cheapest to delete.
"""

from __future__ import annotations

import fnmatch
import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from rein import models
from rein import store as store_mod

logger = logging.getLogger(__name__)

#: What a lens can be pointed at. Ordered earliest-first, which is also the order of preference:
#: a lens belongs at the earliest stage whose deliverable could carry the failure it looks for.
#: Attacking an ambiguous requirement after the code is written finds the same defect at the price
#: of the implementation that was built on it.
#:
#: There is no `acceptance` stage. Acceptance reads the change through the grounded pipeline — a
#: blind extractor, Expected against Actual, decision cards — and what it attacks is fixed by that
#: machinery rather than chosen from a library. A stage in this tuple that no command selects for
#: and no lens belongs to is the same unfinished record the class system exists to refuse.
STAGES: tuple[str, ...] = models.LENS_STAGE_ORDER
STAGE_VALUES = models.LENS_STAGE_VALUES

#: How confidently the condition can be decided, which is what sets the behaviour.
CLASS_STANDARD = "standard"
CLASS_CONDITIONAL = "conditional"
CLASS_UNCLASSIFIED = "unclassified"
LENS_CLASS_VALUES = frozenset({CLASS_STANDARD, CLASS_CONDITIONAL, CLASS_UNCLASSIFIED})

#: What a frozen selection entry says happened to a lens. `proposed` is the human's at the gate;
#: it is recorded because dropping one is a decision, and a decision nothing records is one the
#: next cycle silently re-makes.
SELECTION_APPLIED = "applied"
SELECTION_PROPOSED = "proposed"
SELECTION_STATUS_VALUES = models.LENS_SELECTION_STATUS_VALUES

#: The closed set of plan facts a condition may name. Closed for the same reason the event
#: vocabulary is: a predicate anybody can invent is one nobody can check, and a condition that
#: cannot be checked is the unfinished lens this file refuses to ship.
FACT_NFR_CLAIMS = "nfr_claims"
FACT_PARALLEL_TASKS = "parallel_tasks"
FACT_TASK_DEPENDENCIES = "task_dependencies"
FACT_VALUES = frozenset({FACT_NFR_CLAIMS, FACT_PARALLEL_TASKS, FACT_TASK_DEPENDENCIES})

LIBRARY_NAME = "lenses.yaml"


@dataclass(frozen=True)
class Facts:
    """What a condition is decided against: this cycle's plan, reduced to what a lens can ask.

    Counts and classes, never text. A condition that could read a requirement's wording would be a
    second reviewer with no evidence behind it; what a lens gets to know is the shape of the cycle.
    """

    changed: tuple[str, ...] = ()
    risks: tuple[str, ...] = ()
    claims: int = 0
    tasks: int = 0
    present: frozenset[str] = frozenset()

    @classmethod
    def of(cls, plan: Any) -> Facts:
        """Read off a `models.Plan`. An absent plan yields facts nothing conditional holds against."""
        if plan is None:
            return cls()
        claims = list(plan.claims)
        tasks = list(plan.tasks)
        present: set[str] = set()

        def is_nfr(claim: Any) -> bool:
            return claim.id.startswith("NFR-") or any(r.startswith("NFR-") for r in claim.requirement_ids)

        if any(is_nfr(claim) for claim in claims):
            present.add(FACT_NFR_CLAIMS)
        if sum(1 for task in tasks if task.kind == "parallel") >= 2:
            present.add(FACT_PARALLEL_TASKS)
        if any(task.blocked_by for task in tasks):
            present.add(FACT_TASK_DEPENDENCIES)
        return cls(
            changed=tuple(sorted({path for task in tasks for path in task.scope_include})),
            risks=tuple(claim.risk for claim in claims if claim.risk),
            claims=len(claims),
            tasks=len(tasks),
            present=frozenset(present),
        )


@dataclass(frozen=True)
class Condition:
    """When a lens is worth applying, in terms a machine can settle against :class:`Facts`.

    Every axis left out is an axis this condition does not turn on — never "matches nothing". What
    is *not* allowed is every axis being left out at once, which is what `Condition.stated` is for.
    """

    paths: tuple[str, ...] = ()
    claim_risk: str = ""
    min_claims: int = 0
    min_tasks: int = 0
    requires: tuple[str, ...] = ()

    @property
    def stated(self) -> bool:
        return bool(self.paths or self.claim_risk or self.min_claims or self.min_tasks or self.requires)

    def holds(self, facts: Facts) -> bool:
        if self.min_claims and facts.claims < self.min_claims:
            return False
        if self.min_tasks and facts.tasks < self.min_tasks:
            return False
        if self.requires and not set(self.requires) <= facts.present:
            return False
        if not self._paths_hold(facts.changed):
            return False
        return self._risk_holds(facts.risks)

    def _paths_hold(self, changed: Sequence[str]) -> bool:
        if not self.paths:
            return True
        return any(fnmatch.fnmatch(path, pattern) for path in changed for pattern in self.paths)

    def _risk_holds(self, risks: Iterable[str]) -> bool:
        if not self.claim_risk:
            return True
        return any(models.risk_at_least(risk, self.claim_risk) for risk in risks)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.paths:
            out["paths"] = list(self.paths)
        if self.claim_risk:
            out["claim_risk"] = self.claim_risk
        if self.min_claims:
            out["min_claims"] = self.min_claims
        if self.min_tasks:
            out["min_tasks"] = self.min_tasks
        if self.requires:
            out["requires"] = list(self.requires)
        return out

    @classmethod
    def parse(cls, raw: Any, *, lens_id: str) -> Condition:
        if not isinstance(raw, Mapping):
            return cls()
        requires = raw.get("requires")
        named = tuple(str(f) for f in requires) if isinstance(requires, list) else ()
        unknown = [f for f in named if f not in FACT_VALUES]
        if unknown:
            # Dropped rather than treated as false: a condition holding on a fact nothing computes
            # would silently never apply, which reads as "this lens never finds anything".
            logger.warning(f"lens {lens_id}: unknown fact(s) {', '.join(unknown)} in `when.requires` — ignored")
            named = tuple(f for f in named if f in FACT_VALUES)
        paths = raw.get("paths")
        return cls(
            paths=tuple(str(p) for p in paths) if isinstance(paths, list) else (),
            claim_risk=str(raw.get("claim_risk", "")),
            min_claims=int(raw["min_claims"]) if isinstance(raw.get("min_claims"), int) else 0,
            min_tasks=int(raw["min_tasks"]) if isinstance(raw.get("min_tasks"), int) else 0,
            requires=named,
        )


@dataclass(frozen=True)
class Lens:
    """One thing a reviewer is sent to try, and when it is worth trying."""

    id: str
    stage: str
    attack: str
    lens_class: str = CLASS_UNCLASSIFIED
    when: Condition = Condition()
    applies_when: str = ""
    origin: str = ""

    def holds(self, facts: Facts) -> bool:
        return self.when.holds(facts)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "stage": self.stage, "attack": self.attack, "class": self.lens_class}
        condition = self.when.as_dict()
        if condition:
            out["when"] = condition
        if self.applies_when:
            out["applies_when"] = self.applies_when
        if self.origin:
            out["origin"] = self.origin
        return out


def _lens(raw: Mapping[str, Any]) -> Lens | None:
    lens_id, stage = str(raw.get("id", "")), str(raw.get("stage", ""))
    attack = str(raw.get("attack", ""))
    if not lens_id or stage not in STAGE_VALUES or not attack:
        logger.warning(f"skipping a lens with no id, no attack, or an unknown stage: {raw!r}")
        return None
    lens_class = str(raw.get("class", CLASS_UNCLASSIFIED))
    if lens_class not in LENS_CLASS_VALUES:
        logger.warning(f"lens {lens_id}: unknown class {lens_class!r} — treating it as unclassified")
        lens_class = CLASS_UNCLASSIFIED
    when = Condition.parse(raw.get("when"), lens_id=lens_id)
    if lens_class == CLASS_STANDARD and not when.stated:
        # The check the whole class system rests on. `standard` means "applied without asking
        # anybody", and the licence for that is a condition a machine can settle. With no `when:`
        # there is nothing to settle, so what would actually ship is a lens applied to every change
        # — the always-on list this file replaced, re-entering through an empty field.
        logger.warning(
            f"lens {lens_id}: class `standard` with no `when:` condition — loaded as `unclassified`. "
            "A standard lens is applied without asking anybody, and that needs a condition a machine "
            "can decide; write one, or classify it `conditional` and let a human decide at the gate."
        )
        lens_class = CLASS_UNCLASSIFIED
    return Lens(
        id=lens_id,
        stage=stage,
        attack=attack,
        lens_class=lens_class,
        when=when,
        applies_when=str(raw.get("applies_when", "")),
        origin=str(raw.get("origin", "")),
    )


def library_path() -> Path:
    """The user's own library — beside the project registry, for the same reason it is."""
    return store_mod.config_home() / "rein" / LIBRARY_NAME


def packaged_path() -> Path:
    return Path(__file__).resolve().parent / "data" / LIBRARY_NAME


def _read(path: Path) -> list[Lens]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(f"{path} could not be read, so none of its lenses are available: {exc}")
        return []
    entries = raw.get("lenses") if isinstance(raw, Mapping) else None
    if not isinstance(entries, list):
        return []
    return [lens for lens in (_lens(e) for e in entries if isinstance(e, Mapping)) if lens is not None]


def library() -> list[Lens]:
    """The packaged defaults, overlaid by the user's own file. Later wins, by id.

    Overlaid rather than replaced so that turning one packaged lens off — or narrowing its
    condition after it kept applying and never finding — does not mean copying the whole set and
    inheriting responsibility for keeping it current.
    """
    merged: dict[str, Lens] = {lens.id: lens for lens in _read(packaged_path())}
    for lens in _read(library_path()):
        merged[lens.id] = lens
    return sorted(merged.values(), key=lambda lens: (STAGES.index(lens.stage), lens.id))


def select(lenses: Sequence[Lens], *, stage: str, facts: Facts) -> tuple[list[Lens], list[Lens]]:
    """`(applied, proposed)` for one stage — what runs, and what a human is asked about.

    `unclassified` appears in neither: off is what "we have not written down when this applies"
    means. It is in the library so that the next time its cause comes back there is something to
    attach a condition to, which is the whole reason a one-off finding is kept at all.
    """
    applied: list[Lens] = []
    proposed: list[Lens] = []
    for lens in lenses:
        if lens.stage != stage or not lens.holds(facts):
            continue
        if lens.lens_class == CLASS_STANDARD:
            applied.append(lens)
        elif lens.lens_class == CLASS_CONDITIONAL:
            proposed.append(lens)
    return applied, proposed


# -- the frozen selection ------------------------------------------------------


def resolve(lenses: Sequence[Lens], facts: Facts) -> list[dict[str, str]]:
    """The whole selection, every stage at once, in the shape `plan.lenses` holds.

    Every stage together because it is one function of one plan, and the plan freezes whole. The
    alternative — resolve each stage when its command runs — would freeze `requirements` against a
    plan with no tasks in it yet and `code` against the finished one, so two lenses with the same
    condition would get different answers depending on what time of day they were asked.
    """
    out: list[dict[str, str]] = []
    for stage in STAGES:
        applied, proposed = select(lenses, stage=stage, facts=facts)
        out += [{"id": lens.id, "stage": stage, "status": SELECTION_APPLIED} for lens in applied]
        out += [{"id": lens.id, "stage": stage, "status": SELECTION_PROPOSED} for lens in proposed]
    return out


def frozen(plan: Any, *, stage: str) -> tuple[list[str], list[str]]:
    """`(applied_ids, proposed_ids)` as `plan.lenses` recorded them for `stage`."""
    if plan is None:
        return [], []
    entries = [entry for entry in plan.lenses if entry.stage == stage]
    return (
        [e.id for e in entries if e.status == SELECTION_APPLIED],
        [e.id for e in entries if e.status == SELECTION_PROPOSED],
    )


def by_id(lenses: Sequence[Lens], ids: Sequence[str]) -> tuple[list[Lens], list[str]]:
    """`(found, missing)` — the library entries for `ids`, and the ids the library no longer holds.

    Missing ones are named rather than skipped. A frozen selection naming a lens that has since
    been deleted from the user's overlay is exactly the machine-local drift the freeze exists to
    make visible, and silently reviewing without it would hide it.
    """
    known = {lens.id: lens for lens in lenses}
    found = [known[lens_id] for lens_id in ids if lens_id in known]
    missing = [lens_id for lens_id in ids if lens_id not in known]
    return found, missing


def render(lenses: Sequence[Lens]) -> str:
    lines: list[str] = []
    for lens in lenses:
        lines.append(f"  {lens.id} [{lens.stage}] {lens.attack}")
        if lens.applies_when:
            lines.append(f"      applies when: {lens.applies_when}")
    return "\n".join(lines)
