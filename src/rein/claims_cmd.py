"""`rein claims` — what each cycle committed to and what it was allowed to touch, oldest first.

`00-concept.md` names three things a human has to keep hold of for delegation to be real: the
claims, the scope, and the judgement history. `rein decisions` reads the third one back across
cycles, and its own note said the third was the one that went out of reach when a cycle closed.
**That was one write site short of the truth.** `cycle.CYCLE_STATE` archives `plan.yaml`, which
is where both the frozen claims and the frozen scope live, and `cycle.CYCLE_DOCS` archives
`10-requirements.md` with it. All three vanish from the working tree at the same moment; only one
of them had a way back.

So this is the same read, one axis over: `events.cycle_sources` enumerates the cycles, each
cycle's `plan.yaml` says what was promised and what the promise was allowed to reach, and that
cycle's `review.yaml` says what the acceptance review made of each claim.

**The three axes are never collapsed into one word.** `review.schema.json` says why: integrity is
a fact, semantic support is somebody's judgement, conformance is an observation, and a single
`verified` is how an AI's opinion comes to be read as a check. A claim from a cycle with no
generated review is *unreviewed*, which is not the same as a claim whose verdict is `unverified`
— the first is an absence, the second is a finding.

**Read-only, and no schema is imposed on the past.** An archived `plan.yaml` was written by
whatever release closed that cycle, so it is parsed as a document and the fields that answer the
question are taken; a cycle whose records cannot be read is named rather than folded in. Nothing
here is an input to any gate: it answers a person asking what this repository has promised.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from rein import common, event_chain, strict_yaml
from rein import events as events_mod
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: What one claim's review result is read out of, in the order a reader needs them: the verdict
#: first, then the three axes it was derived from, each keeping its own vocabulary.
_AXES: tuple[tuple[str, str], ...] = (
    ("integrity", "integrity"),
    ("semantic_support", "semantics"),
    ("conformance", "conformance"),
)

#: Printed for a claim in a cycle whose review was never generated. Not a verdict: a review that
#: did not run and a review that could not decide must never render the same (plan §2.4).
UNREVIEWED = "unreviewed"


@dataclass(frozen=True)
class Claim:
    """One promise, and what the review of that cycle made of it."""

    ident: str
    statement: str
    risk: str = ""
    #: `aligned` / `diverged` / `missing` / `unverified` / `unknown`, or :data:`UNREVIEWED`.
    verdict: str = UNREVIEWED
    #: `(label, status)` per axis, empty when no review result covers this claim.
    axes: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CycleClaims:
    """What one cycle promised and how far it could reach. `label` is the archive path, or `""`."""

    label: str
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    claims: list[Claim] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)


def _document(path: Path) -> tuple[Mapping[str, object] | None, str | None]:
    """`(document, why not)` — a YAML mapping read leniently, or the reason it could not be."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, None
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{path.name}: {exc}"
    try:
        return strict_yaml.load_mapping(raw, what=path.name), None
    except strict_yaml.StrictParseError as exc:
        return None, f"{path.name}: {exc}"


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(str(entry).strip() for entry in value if str(entry).strip())


def _verdicts(rein_dir: Path) -> tuple[dict[str, Mapping[str, object]], str | None]:
    """Each claim's review result by claim id, and why `review.yaml` could not be read.

    An ungenerated review is not an error and not an empty result: it is the absence
    :data:`UNREVIEWED` names, so the mapping comes back empty and every claim says so itself.
    """
    document, why = _document(rein_dir / "review.yaml")
    if document is None:
        return {}, why
    machine = document.get("machine")
    rows = machine.get("claims") if isinstance(machine, Mapping) else None
    if not isinstance(rows, Sequence) or isinstance(rows, str):
        return {}, None
    found: dict[str, Mapping[str, object]] = {}
    for row in rows:
        if isinstance(row, Mapping) and str(row.get("claim_id") or "").strip():
            found[str(row["claim_id"]).strip()] = row
    return found, None


def _claim(row: Mapping[str, object], result: Mapping[str, object] | None) -> Claim | None:
    ident = str(row.get("id") or "").strip()
    statement = str(row.get("statement") or "").strip()
    if not ident or not statement:
        return None
    if result is None:
        return Claim(ident=ident, statement=statement, risk=str(row.get("risk") or "").strip())
    axes: list[tuple[str, str]] = []
    for key, label in _AXES:
        axis = result.get(key)
        status = str(axis.get("status") or "").strip() if isinstance(axis, Mapping) else ""
        if status:
            axes.append((label, status))
    return Claim(
        ident=ident,
        statement=statement,
        risk=str(row.get("risk") or "").strip(),
        verdict=str(result.get("verdict") or "").strip() or "unknown",
        axes=tuple(axes),
    )


def _cycle(label: str, rein_dir: Path) -> CycleClaims:
    plan, why_plan = _document(rein_dir / "plan.yaml")
    results, why_review = _verdicts(rein_dir)
    unreadable = [why for why in (why_plan, why_review) if why]
    if plan is None:
        return CycleClaims(label, unreadable=unreadable)
    scope = plan.get("scope")
    include = _strings(scope.get("include")) if isinstance(scope, Mapping) else ()
    exclude = _strings(scope.get("exclude")) if isinstance(scope, Mapping) else ()
    rows = plan.get("claims")
    claims: list[Claim] = []
    if isinstance(rows, Sequence) and not isinstance(rows, str):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            claim = _claim(row, results.get(str(row.get("id") or "").strip()))
            if claim is not None:
                claims.append(claim)
    return CycleClaims(label, include=include, exclude=exclude, claims=claims, unreadable=unreadable)


def history(repo: repo_mod.Repo) -> tuple[list[CycleClaims], list[str]]:
    """Every cycle's claims and scope, oldest first, plus the archives that could not be verified."""
    live, defects = event_chain.scan(repo.events)
    if defects:
        logger.warning(f"{repo.events} has {len(defects)} chain defect(s); the open cycle is read anyway")
    sources, unverified = events_mod.cycle_sources(repo, live)
    return [_cycle(source.label, source.rein_dir) for source in sources], unverified


def _scope_line(entry: CycleClaims) -> str:
    """What the mandate could reach. An empty `include` is unbounded, never "nothing"."""
    include = ", ".join(entry.include) if entry.include else "(unbounded — every guarded path)"
    line = f"  scope     include: {include}"
    if entry.exclude:
        line += f"\n            exclude: {', '.join(entry.exclude)}"
    return line


def render(cycles: Sequence[CycleClaims], unverified: Sequence[str] = ()) -> str:
    """The promises as a person reads them: one block per cycle, oldest first."""
    lines: list[str] = []
    total = 0
    for entry in cycles:
        lines.append(entry.label or "this cycle (still open)")
        for problem in entry.unreadable:
            lines.append(f"  ! {problem}")
        lines.append(_scope_line(entry))
        for claim in entry.claims:
            total += 1
            risk = f"  risk {claim.risk}" if claim.risk else ""
            lines.append(f"  {claim.ident:<10} {claim.verdict}{risk}")
            lines.append(f"    {claim.statement}")
            if claim.axes:
                lines.append("    " + "  ".join(f"{label}: {status}" for label, status in claim.axes))
        if not entry.claims and not entry.unreadable:
            lines.append("  no claim was frozen")
        lines.append("")
    for rel in unverified:
        lines.append(f"! {rel} did not verify, so that cycle's claims are not shown")
    if not total:
        lines.append(
            "No claim is on record yet. A claim is frozen by the mandate — `plan.yaml` carries it "
            "with the scope it may reach, and both are archived with the cycle that closed."
        )
    else:
        lines.append(
            f"{total} claim(s) across {len(cycles)} cycle(s). The verdict and its three axes are "
            "printed apart on purpose: integrity is a fact, semantics is a judgement, conformance "
            f"is an observation, and `{UNREVIEWED}` means no review was generated for that cycle."
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="what each cycle committed to and what it could touch, oldest cycle first (read-only)"
    )
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
