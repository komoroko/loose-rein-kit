"""The review lenses that get applied, and the rule for which ones do.

`adversarial-reviewer.md` carried three fixed lists and told the reviewer to work through every
lens in the set, reporting each as a finding or as "attacked — no finding". A concurrency lens over
a change with no concurrency, an injection lens over a change that touches no store: every one of
those costs a pass over the deliverable and returns "attacked — nothing", and the cost is not only
the time. Findings compete for attention with each other, and a reviewer sent to look for failures
that cannot occur here brings back the ones that can *plus* noise. Over-reviewing is not thorough.

So a lens is not a paragraph in a prompt. It is a record with a **condition**, and the condition is
what decides whether it is applied at all:

* **`standard`** — the condition is decidable from the mandate: the paths a task declares, the
  claims it answers, the risk it carries. Applied automatically when it holds, and nobody is asked.
* **`conditional`** — there is a condition but deciding it takes judgement. Proposed at the mandate
  gate with the reason, where a human keeps or drops it in the same pass they already make.
* **`unclassified`** — nothing has been written down about when this applies. **Off**, until
  somebody says otherwise for one cycle.

There is no "always on" class, because a lens with no condition is one nobody can tell apart from a
lens whose condition is "always". The first is unfinished and the second is rare.

**A cross-project library stays honest because every lens carries its condition.** The risk of
sharing lenses between repositories is a lens that fires where its failure cannot happen;
`standard` cannot, since its condition names what must be in the change, `conditional` states its
case to a human, and `unclassified` is off. So the library is user-global
(`$XDG_CONFIG_HOME/rein/lenses.yaml`, overlaying the packaged defaults) while the *selection* is
copied into `.rein/plan.yaml` and frozen with the mandate — the same shape `.rein/prompts/` already
has. A review's inputs must not depend on machine-local state, or the same repository reviewed
elsewhere answers differently.

**Qualification, and retirement.** A lens earns its place by the same rule a bug earns a
regression test: not the first time, but when the same cause comes back. Once is an incident; twice
is what tells you the condition. That is why a one-off finding goes to `unclassified` — there is
nothing yet to write in `applies_when`. And the reverse rule is what keeps the library from
rotting: `rein lens --stats` counts, per lens, how often it was applied and how often it found
something. A lens that keeps applying and never finds has the wrong condition or has outlived its
cause. Counted, never capped: a ceiling on how many lenses may exist would be answered by deleting
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

from rein import store as store_mod

logger = logging.getLogger(__name__)

#: What a lens can be pointed at. Ordered earliest-first, which is also the order of preference:
#: a lens belongs at the earliest stage whose deliverable could carry the failure it looks for.
#: Attacking an ambiguous requirement after the code is written finds the same defect at the price
#: of the implementation that was built on it.
STAGES: tuple[str, ...] = ("requirements", "design", "tasks", "code", "acceptance")
STAGE_VALUES = frozenset(STAGES)

#: How confidently the condition can be decided, which is what sets the behaviour.
CLASS_STANDARD = "standard"
CLASS_CONDITIONAL = "conditional"
CLASS_UNCLASSIFIED = "unclassified"
LENS_CLASS_VALUES = frozenset({CLASS_STANDARD, CLASS_CONDITIONAL, CLASS_UNCLASSIFIED})

LIBRARY_NAME = "lenses.yaml"


@dataclass(frozen=True)
class Lens:
    """One thing a reviewer is sent to try, and when it is worth trying."""

    id: str
    stage: str
    attack: str
    lens_class: str = CLASS_UNCLASSIFIED
    paths: tuple[str, ...] = ()
    claim_risk: str = ""
    applies_when: str = ""
    origin: str = ""

    def matches_paths(self, changed: Sequence[str]) -> bool:
        """No patterns means the condition does not turn on paths, not that it matches nothing."""
        if not self.paths:
            return True
        return any(fnmatch.fnmatch(path, pattern) for path in changed for pattern in self.paths)

    def matches_risk(self, risks: Iterable[str]) -> bool:
        if not self.claim_risk:
            return True
        from rein import models

        return any(models.risk_at_least(risk, self.claim_risk) for risk in risks)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"id": self.id, "stage": self.stage, "attack": self.attack, "class": self.lens_class}
        if self.paths:
            out["paths"] = list(self.paths)
        if self.claim_risk:
            out["claim_risk"] = self.claim_risk
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
    paths = raw.get("paths")
    return Lens(
        id=lens_id,
        stage=stage,
        attack=attack,
        lens_class=lens_class,
        paths=tuple(str(p) for p in paths) if isinstance(paths, list) else (),
        claim_risk=str(raw.get("claim_risk", "")),
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


def select(
    lenses: Sequence[Lens], *, stage: str, changed: Sequence[str] = (), risks: Sequence[str] = ()
) -> tuple[list[Lens], list[Lens]]:
    """`(applied, proposed)` for one stage — what runs, and what a human is asked about.

    `unclassified` appears in neither: off is what "we have not written down when this applies"
    means. It is in the library so that the next time its cause comes back there is something to
    attach a condition to, which is the whole reason a one-off finding is kept at all.
    """
    applied: list[Lens] = []
    proposed: list[Lens] = []
    for lens in lenses:
        if lens.stage != stage:
            continue
        if not (lens.matches_paths(changed) and lens.matches_risk(risks)):
            continue
        if lens.lens_class == CLASS_STANDARD:
            applied.append(lens)
        elif lens.lens_class == CLASS_CONDITIONAL:
            proposed.append(lens)
    return applied, proposed


def render(lenses: Sequence[Lens]) -> str:
    lines: list[str] = []
    for lens in lenses:
        lines.append(f"  {lens.id} [{lens.stage}] {lens.attack}")
        if lens.applies_when:
            lines.append(f"      applies when: {lens.applies_when}")
    return "\n".join(lines)
