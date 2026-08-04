"""The traceability thread: requirement → claim → task.

The thread follows the plan's structure, never a matching number in a free-text field, and asks:

  **Coverage**   does every requirement have a claim, every claim a task, every requirement a
                 design section, and — with ``--test-plan`` — a row in the test plan?
  **Anchoring**  does every requirement a claim cites actually exist in the requirements
                 document, rather than being one an agent invented?

Requirement ids are **declared** by the `R-N` / `NFR-N` headings of `docs/10-requirements.md`
and **claimed** by the plan. The document supplies the checklist; it never supplies the meaning.
A heading with no claim behind it is a break in the thread, not a requirement the trace may
quietly accept — and a claim citing an id no heading declares is a dangling reference, not
evidence that the requirement exists.

**A check with nothing to check is not a pass.** When neither side carries a requirement id the
thread reports `unknown` and exits 2. An earlier version printed "the thread is whole" against an
empty plan, which is exactly the self-consistent green this tool exists to refuse.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from rein import dag, models
from rein import repo as repo_mod

logger = logging.getLogger(__name__)

#: The requirements document declares an id by giving it a heading. Anything else in that file
#: is prose *about* a requirement, which is why a bare mention does not declare one.
#:
#: A heading whose title is still the scaffold's `<placeholder>` declares nothing: the file
#: `rein init` writes ships `### R-1: <title>`, and reading those as real requirements would
#: greet every fresh repository with two blocking trace errors about requirements nobody wrote.
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+((?:NFR|R)-\d+)\b[ \t]*:?[ \t]*(.*)$", re.MULTILINE)

REQUIREMENTS_DOC = "docs/10-requirements.md"
DESIGN_DOC = "docs/20-design.md"


def _read(path: Path) -> str | None:
    """The file's text, or None when it is absent or unreadable."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def is_nfr(requirement_id: str) -> bool:
    """True for a non-functional requirement (NFR-N).

    NFRs trace with softer *coverage* rules: many are cross-cutting and are demonstrated at
    `/verify` rather than owned by one task or one design section, so a gap is a warning. Their
    presence in the test plan is not softened — that is the only place a cross-cutting NFR is
    ever actually checked.
    """
    return requirement_id.startswith("NFR-")


def _order_key(requirement_id: str) -> tuple[int, int, str]:
    """Sort R-2 before R-10, and every R-N before every NFR-N."""
    head, _, number = requirement_id.partition("-")
    return (1 if head == "NFR" else 0, int(number) if number.isdigit() else 0, requirement_id)


def declared_requirements(text: str) -> list[str]:
    """The requirement ids `text` declares as headings, in document order, deduplicated."""
    found: dict[str, None] = {}
    for match in _HEADING_RE.finditer(text):
        if match.group(2).lstrip().startswith("<"):
            continue  # still the scaffold's placeholder title
        found.setdefault(match.group(1), None)
    return list(found)


def mentions(text: str, requirement_id: str) -> bool:
    """True when `text` refers to `requirement_id` (word-bounded, so R-1 never matches R-10)."""
    return re.search(rf"(?<![\w-]){re.escape(requirement_id)}(?![\w-])", text) is not None


@dataclass
class TraceReport:
    """Everything the thread found. `checked` says whether it had anything to look at."""

    requirements: list[str] = field(default_factory=list)
    claims_by_requirement: dict[str, list[str]] = field(default_factory=dict)
    tasks_by_claim: dict[str, list[str]] = field(default_factory=dict)

    requirements_without_claims: list[str] = field(default_factory=list)
    claims_without_tasks: list[str] = field(default_factory=list)
    orphan_tasks: list[str] = field(default_factory=list)
    dangling: list[tuple[str, str]] = field(default_factory=list)  # (claim id, cited requirement)
    undesigned: list[str] = field(default_factory=list)
    untested: list[str] = field(default_factory=list)

    checked: bool = True  # False when neither the document nor the plan named a requirement
    checked_design: bool = False
    checked_test_plan: bool = False

    @property
    def errors(self) -> list[str]:
        """Findings that block a gate."""
        problems: list[str] = [
            f"{rid}: declared in {REQUIREMENTS_DOC} but no claim states what it means"
            for rid in self.requirements_without_claims
            if not is_nfr(rid)
        ]
        problems += [
            f"{cid}: cites {rid}, which {REQUIREMENTS_DOC} does not declare — a requirement id "
            "an agent invented is not a requirement"
            for cid, rid in self.dangling
        ]
        problems += [f"{rid}: no section in {DESIGN_DOC} covers it" for rid in self.undesigned if not is_nfr(rid)]
        problems += [f"{rid}: never appears in the test plan — it would ship unverified" for rid in self.untested]
        problems += [f"{tid}: task answers for no claim" for tid in self.orphan_tasks]
        return problems

    @property
    def warnings(self) -> list[str]:
        problems = [
            f"{rid}: no claim yet (NFR — often demonstrated at /verify rather than owned by one task)"
            for rid in self.requirements_without_claims
            if is_nfr(rid)
        ]
        problems += [f"{cid}: no task is answerable for this claim" for cid in self.claims_without_tasks]
        problems += [
            f"{rid}: no section in {DESIGN_DOC} (NFR — cross-cutting NFRs are verified at /verify)"
            for rid in self.undesigned
            if is_nfr(rid)
        ]
        if not self.checked_design:
            problems.append(f"{DESIGN_DOC} is absent — the design dimension was not checked")
        return problems

    @property
    def ok(self) -> bool:
        return self.checked and not self.errors


def trace(
    plan: models.Plan,
    graph: dag.Graph | None = None,
    *,
    declared: list[str] | tuple[str, ...] = (),
    design_text: str | None = None,
    test_plan_text: str | None = None,
) -> TraceReport:
    """Follow the thread through `plan`.

    `graph` supplies the task side (None traces the plan alone); `declared` is the requirements
    document's checklist; `design_text` and `test_plan_text` add the design and test-plan
    dimensions when those documents are available.
    """
    report = TraceReport(checked_design=design_text is not None, checked_test_plan=test_plan_text is not None)
    tasks = graph.tasks if graph is not None else ()
    task_claims: dict[str, list[str]] = {}
    for task in tasks:
        for cid in task.claim_ids:
            task_claims.setdefault(cid, []).append(task.id)
    if graph is not None:
        report.orphan_tasks = sorted(t.id for t in tasks if not t.claim_ids)

    declared_set = set(declared)
    for claim in plan.claims:
        for rid in claim.requirement_ids:
            report.claims_by_requirement.setdefault(rid, []).append(claim.id)
            if declared_set and rid not in declared_set:
                report.dangling.append((claim.id, rid))
        report.tasks_by_claim[claim.id] = sorted(task_claims.get(claim.id, []))
        if graph is not None and not report.tasks_by_claim[claim.id]:
            report.claims_without_tasks.append(claim.id)

    report.requirements = sorted(declared_set | set(report.claims_by_requirement), key=_order_key)
    report.checked = bool(report.requirements)
    report.requirements_without_claims = [rid for rid in declared if not report.claims_by_requirement.get(rid)]
    if design_text is not None:
        report.undesigned = [rid for rid in declared if not mentions(design_text, rid)]
    if test_plan_text is not None:
        report.untested = [rid for rid in declared if not mentions(test_plan_text, rid)]
    return report


def trace_repo(
    repo: repo_mod.Repo,
    plan: models.Plan,
    graph: dag.Graph | None = None,
    *,
    test_plan_text: str | None = None,
) -> TraceReport:
    """:func:`trace` with the checklist and the design document read from `repo`.

    Every caller that has a repository goes through here, so `approve`, `doctor`, `status` and
    the CLI all answer the coverage question the same way — a gate cannot be readier than the
    board says it is.
    """
    return trace(
        plan,
        graph,
        declared=declared_requirements(_read(repo.path(REQUIREMENTS_DOC)) or ""),
        design_text=_read(repo.path(DESIGN_DOC)),
        test_plan_text=test_plan_text,
    )


def render_trace(report: TraceReport) -> str:
    """The thread as a human-facing report."""
    lines = ["### Traceability thread (requirement → claim → task)", ""]
    if not report.checked:
        lines.append(
            f"**unknown** — neither {REQUIREMENTS_DOC} nor the plan names a requirement id, so there "
            "is no thread to follow yet. This is not a pass: `/req` declares `R-N` / `NFR-N` headings "
            "and writes the matching claims into `.rein/plan.yaml`."
        )
        return "\n".join(lines)

    lines.append("| Requirement | Claims | Tasks |")
    lines.append("|-------------|--------|-------|")
    for rid in report.requirements:
        claims = report.claims_by_requirement.get(rid, [])
        tasks = sorted({t for cid in claims for t in report.tasks_by_claim.get(cid, [])})
        lines.append(f"| {rid} | {', '.join(claims) or '-'} | {', '.join(tasks) or '-'} |")
    lines.append("")

    errors, warnings = report.errors, report.warnings
    if errors:
        lines.append(f"### Blocking ({len(errors)})")
        lines += [f"- {e}" for e in errors]
        lines.append("")
    if warnings:
        lines.append(f"### Warnings ({len(warnings)})")
        lines += [f"- {w}" for w in warnings]
        lines.append("")
    if not errors and not warnings:
        scope = "requirement → claim → task → design"
        if report.checked_test_plan:
            scope += " → test plan"
        lines.append(f"The thread is whole across {len(report.requirements)} requirements ({scope}).")
    return "\n".join(lines).rstrip()


def run(repo: repo_mod.Repo, test_plan: str | None = None) -> int:
    """`rein dag --trace`: 0 when whole, 1 on a blocking break, 2 when there is nothing to check."""
    from rein import store as store_mod

    store = store_mod.Store(repo)
    try:
        plan = store.read_plan()
    except models.DocumentError as exc:
        logger.error(str(exc))
        return 1
    if plan is None:
        logger.warning(f"no plan at {repo.plan} yet — nothing to trace")
        return 2

    requirements_text = _read(repo.path(REQUIREMENTS_DOC))
    if requirements_text is None:
        logger.warning(f"{REQUIREMENTS_DOC} is absent — the requirement checklist could not be read")
    declared = declared_requirements(requirements_text or "")

    test_plan_text: str | None = None
    if test_plan is not None:
        # `Path / "/abs"` yields the absolute path, so this accepts either form.
        test_plan_text = _read(repo.path(test_plan))
        if test_plan_text is None:
            logger.error(f"{test_plan}: cannot read the test plan — nothing to check the requirements against")
            return 2

    graph: dag.Graph | None
    try:
        graph = dag.join(plan, store.read_state())
    except (dag.DagError, models.DocumentError):
        graph = None  # trace the plan side even when the state cannot be joined

    report = trace(
        plan,
        graph,
        declared=declared,
        design_text=_read(repo.path(DESIGN_DOC)),
        test_plan_text=test_plan_text,
    )
    print(render_trace(report))
    if not report.checked:
        return 2
    return 0 if report.ok else 1
