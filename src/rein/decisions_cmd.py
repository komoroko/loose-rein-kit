"""`rein decisions` — every decision this repository has recorded, oldest cycle first.

**The write sites stay where they are; this is the read side.** A judgement lands in one of four
places, each for its own good reason: `plan.yaml`'s `decisions` holds what the drafting phase met
and how far each one reached, an ADR holds the ones that needed options weighed, `## Clarifications`
holds an ambiguity somebody closed, and `## Open questions` holds one that was passed through on a
stated assumption. Collapsing them into one file would mean a fifth write path and four migrations,
and the reason they are separate — different authors, different moments, different shapes — would
not go away.

What was missing is that **all four are per-cycle**. `cycle-close` archives `plan.yaml`,
`10-requirements.md` *and* `docs/decisions/`, then restores the last two from the pristine
snapshot. So the working tree only ever shows the cycle now open: a decision settled last cycle is
not merely harder to find, it is not in `docs/` at all. A person asking "what did I already decide
about this" had no way to be answered.

`00-concept.md` names the judgement history as one of the three things a human must keep hold of
for delegation to be real. This reads it back along the axis it was written on — the cycle — using
`events.cycle_sources`, the same enumeration `--cost`, `rein lens --stats` and `rein observe` ask.

**Read-only, and no schema is imposed on the past.** An archived `plan.yaml` was written by
whatever release closed that cycle, and validating it against today's schema would make the
history go blank on the next schema change — the exact failure the cross-cycle read exists to
prevent. So the documents are parsed as YAML and markdown, the four fields that answer the
question are taken, and a cycle whose records cannot be read is named rather than folded in.
"""

from __future__ import annotations

import argparse
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from rein import common, data, event_chain, mdlite, strict_yaml
from rein import events as events_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: `- Q: … → A: … (2026-09-03)` is what the scaffold asks for, but the arrow is the only part a
#: human reliably keeps. A bullet without one is still a record of something being closed, so it
#: is kept whole rather than dropped for not matching a shape nobody validates.
_BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<text>.+?)\s*$")

#: The scaffold document these bullets are read out of. Held by name because the placeholders are
#: read from it rather than guessed at — see :func:`_template_bullets`.
_REQUIREMENTS_DOC = "10-requirements.md"
_SCAFFOLD_REQUIREMENTS = f"scaffold/docs/{_REQUIREMENTS_DOC}"

_ADR_FIELD_RE = re.compile(r"^\s*[-*]\s+\*\*(?P<field>Status|Date)\*\*\s*:\s*(?P<value>.+?)\s*$", re.MULTILINE)
_ADR_TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class Decision:
    """One row of the history. `where` is the write site it came from, kept because the four are
    not interchangeable: a `## Open questions` entry is an assumption still standing, and reading
    it as a settled decision is the misreading this whole record exists to prevent."""

    where: str
    ident: str
    summary: str
    status: str = ""


@dataclass(frozen=True)
class CycleHistory:
    """What one cycle decided. `label` is the archive path, or `""` for the cycle still open."""

    label: str
    decisions: list[Decision] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"{path} could not be read: {exc}")
        return None


@lru_cache(maxsize=1)
def _template_bullets() -> frozenset[str]:
    """Every bullet the shipped `10-requirements.md` already contains.

    **The template's own text is not a record of anything.** A repository that has clarified
    nothing still carries `- Q: <question> → A: <the human's answer> (YYYY-MM-DD)`, and printing
    that back as somebody's judgement is worse than printing nothing. Which lines are placeholders
    is read from the scaffold rather than matched against a guessed shape: a pattern for "looks
    like a slot" has to decide whether `- must handle <= 3 retries` is one, and it will be wrong
    in whichever direction it is written. `template_lint` holds documents against the same
    payload for the same reason.
    """
    try:
        shipped = data.read_text(_SCAFFOLD_REQUIREMENTS)
    except (OSError, FileNotFoundError) as exc:
        # Not fatal, and not silent: the history is still readable, it just stops filtering. A
        # missing payload means a broken install, which `rein doctor` is the place to hear about.
        logger.warning(f"the packaged {_SCAFFOLD_REQUIREMENTS} could not be read ({exc}); placeholders are shown")
        return frozenset()
    return frozenset(_raw_bullets(shipped))


def _raw_bullets(body: str) -> list[str]:
    """Every bullet of `body`, comments stripped, in order."""
    found: list[str] = []
    for line in mdlite.strip_comments(body).splitlines():
        match = _BULLET_RE.match(line)
        if match is not None and (text := match.group("text").strip()):
            found.append(text)
    return found


def _bullets(body: str) -> list[str]:
    """The bullets of one section, minus the ones the scaffold already shipped."""
    shipped = _template_bullets()
    return [text for text in _raw_bullets(body) if text not in shipped]


def _plan_decisions(rein_dir: Path) -> tuple[list[Decision], str | None]:
    """`plan.yaml`'s own `decisions`, and why it could not be read when it could not.

    Parsed as a document rather than as a `models.Plan`: see the module docstring. What is taken
    is what the question needs — which decision, how far it reached, whether anybody settled it —
    and a row missing those is skipped rather than guessed at.
    """
    path = rein_dir / "plan.yaml"
    raw = _text(path)
    if raw is None:
        return [], None
    try:
        document = strict_yaml.load_mapping(raw, what=path.name)
    except strict_yaml.StrictParseError as exc:
        return [], f"{path.name}: {exc}"
    rows = document.get("decisions")
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        return [], None
    found: list[Decision] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ident = str(row.get("id") or "").strip()
        subject = str(row.get("subject") or "").strip()
        if not ident or not subject:
            continue
        answer = str(row.get("answer") or "").strip()
        settled_by = str(row.get("settled_by") or "").strip()
        reach = str(row.get("reach") or "?").strip()
        status = str(row.get("status") or "?").strip()
        by = f" [{settled_by}]" if settled_by else ""
        found.append(
            Decision(
                where="plan",
                ident=ident,
                summary=f"{subject}{f' → {answer}' if answer else ''}{by}",
                status=f"{reach}/{status}",
            )
        )
    return found, None


def _adrs(docs_dir: Path) -> list[Decision]:
    """The ADRs of one cycle. The template itself is not one — it is the shape, not a decision."""
    found: list[Decision] = []
    for path in sorted((docs_dir / "decisions").glob("ADR-*.md")):
        if path.name == "ADR-template.md":
            continue
        raw = _text(path)
        if raw is None:
            continue
        title = _ADR_TITLE_RE.search(raw)
        fields = {m.group("field"): m.group("value") for m in _ADR_FIELD_RE.finditer(raw)}
        status = fields.get("Status", "").strip()
        date = fields.get("Date", "").strip()
        found.append(
            Decision(
                where="adr",
                ident=path.stem,
                summary=title.group("title").strip() if title else path.name,
                status=" ".join(part for part in (status, date) if part),
            )
        )
    return found


def _requirement_notes(docs_dir: Path) -> list[Decision]:
    """`## Clarifications` and `## Open questions` — what was closed, and what was passed through.

    Both come off `10-requirements.md`, and both are kept even though only the first is a closed
    question: an assumption somebody shipped on is a judgement, and it is the one most likely to
    be the answer to "why is it like this".
    """
    raw = _text(docs_dir / _REQUIREMENTS_DOC)
    if raw is None:
        return []
    found: list[Decision] = []
    for heading, where in (("Clarifications", "clarified"), ("Open questions", "open")):
        section, _ = mdlite.extract_section(raw, heading)
        if section is None:
            continue
        for n, text in enumerate(_bullets(section), 1):
            found.append(Decision(where=where, ident=f"{where[:1].upper()}{n}", summary=text))
    return found


def history(repo: repo_mod.Repo) -> tuple[list[CycleHistory], list[str]]:
    """Every cycle's decisions, oldest first, plus the archives that could not be verified."""
    live, defects = event_chain.scan(repo.events)
    if defects:
        logger.warning(f"{repo.events} has {len(defects)} chain defect(s); the open cycle is read anyway")
    sources, unverified = events_mod.cycle_sources(repo, live)
    out: list[CycleHistory] = []
    for source in sources:
        decisions, why = _plan_decisions(source.rein_dir)
        decisions += _adrs(source.docs_dir)
        decisions += _requirement_notes(source.docs_dir)
        out.append(CycleHistory(source.label, decisions, [why] if why else []))
    return out, unverified


def render(cycles: Sequence[CycleHistory], unverified: Sequence[str] = ()) -> str:
    """The history as a person reads it: one block per cycle, oldest first."""
    lines: list[str] = []
    total = 0
    for entry in cycles:
        heading = entry.label or "this cycle (still open)"
        lines.append(heading)
        for problem in entry.unreadable:
            lines.append(f"  ! {problem}")
        for decision in entry.decisions:
            total += 1
            status = f"  {decision.status}" if decision.status else ""
            lines.append(f"  {decision.where:<9} {decision.ident:<14}{status}".rstrip())
            lines.append(f"    {decision.summary}")
        if not entry.decisions and not entry.unreadable:
            lines.append("  nothing recorded")
        lines.append("")
    for rel in unverified:
        lines.append(f"! {rel} did not verify, so that cycle's decisions are not shown")
    if not total:
        lines.append(
            "No decision is on record yet. They accumulate as cycles close: `plan.yaml` keeps what "
            "the drafting phase met, an ADR keeps what needed options weighed, and "
            "`10-requirements.md` keeps what was clarified or passed through on an assumption."
        )
    else:
        lines.append(
            f"{total} record(s) across {len(cycles)} cycle(s). Four write sites, read as one — "
            "`plan` is a decision the drafting phase met, `adr` one that needed options weighed, "
            "`clarified` an ambiguity somebody closed, `open` an assumption the work went ahead on."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="every decision on record, oldest cycle first (read-only)")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    cycles, unverified = history(repo)
    print(render(cycles, unverified))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
