"""The per-task dossier: what the loop already knows, written down once instead of re-derived.

Every agent the build launches used to be handed a pointer and sent to find things out. The
implementer got `docs/tasks/T-004.md` and `docs/20-design.md` as *paths*, and read them cold on
every launch — and on every retry, for any CLI that cannot resume a session. The reviewer got a
list of changed paths and re-surveyed the diff from scratch. The integration fixer got task ids
and a failure summary. Each of them re-established, from the repository, facts the orchestrator
had already computed in code and then dropped on the floor:

  * which claims this task answers, and what each one actually says (it is in the frozen plan)
  * the scope the plan gave the task (`plan.yaml` has carried `scope.include/exclude` with
    nothing reading it)
  * which changed paths are source, which are tests, and which are 800 lines of lockfile
    (`diff_facts` classifies exactly this, for the coverage manifest, and told nobody else)
  * what the previous attempts tried and what went wrong (the handoff kept only the last one)

So the dossier is not a new source of truth — it is the existing ones, resolved and handed over.
The consequences are both directions of the same coin: fewer tokens, because nothing is read
twice, and better answers, because what the loop knows is no longer something the model has to
guess at.

**It is written into the worktree, not passed as an argument.** A prompt is one argv element and
a large one hits `E2BIG` (`review.py` already had to move its request to stdin for this). The
file goes under `.rein/work/`, which every task commit already excludes and which dies with the
worktree — the canonical record of anything decided here goes through the control plane.

**The blind extractor never gets one.** Gate ④'s actual-behaviour extraction is the one
participant that is *supposed* to re-derive everything, having never seen the plan
(`actual_extraction.FORBIDDEN_KEYS`). The duplicated read there is the point of the exercise.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rein import common, dag, diff_facts, digests, models

#: Where a leaf finds its dossier, relative to its own working directory.
RELATIVE_PATH = ".rein/work"

#: Where the reviewer writes its findings, beside the dossier it read. A file rather than stdout:
#: an agent CLI interleaves its own chatter with whatever it means to say, and the one thing that
#: must not be ambiguous is the boundary between "the reviewer's verdict" and "the reviewer
#: thinking aloud". `review.py`'s gate-④ transport had to move its payload off argv for the same
#: family of reason.
FINDINGS_SUFFIX = ".findings.json"


def findings_path(cwd: str, task_id: str) -> Path:
    return Path(cwd) / RELATIVE_PATH / f"{task_id}{FINDINGS_SUFFIX}"


#: How many past attempts to carry. Enough to see a loop forming, few enough to stay one screen.
MAX_HISTORY = 5

#: Cap on the changed-path list. A task touching more files than this has a scope problem the
#: list will not fix, and an uncapped list is how a prompt grows without anyone deciding to.
MAX_PATHS = 200


def path_for(cwd: str, task_id: str) -> Path:
    return Path(cwd) / RELATIVE_PATH / f"{task_id}.json"


def classify_paths(paths: Sequence[str]) -> dict[str, Any]:
    """Split a changed-path list into what a reader needs to read and what they only need to know.

    A lockfile's 800 changed lines say one thing — "the dependencies moved" — and saying it in one
    line rather than in the middle of the code review is the difference between a reviewer reading
    the change and a reviewer scrolling past it.
    """
    kinds: dict[str, list[str]] = {}
    for path in list(paths)[:MAX_PATHS]:
        kinds.setdefault(diff_facts.classify_path(path), []).append(path)
    mechanical = [
        {"path": path, "kind": kind} for kind in sorted(diff_facts.MECHANICAL_KINDS) for path in kinds.get(kind, [])
    ]
    result: dict[str, Any] = {
        "source": kinds.get("source", []),
        "tests": kinds.get("test", []),
        "migrations": kinds.get("migration", []),
        "mechanical": mechanical,
    }
    if len(paths) > MAX_PATHS:
        result["omitted"] = len(paths) - MAX_PATHS
    return {key: value for key, value in result.items() if value}


def scope_violations(task: dag.Task, changed: Sequence[str]) -> list[str]:
    """Changed paths the task's declared scope does not cover.

    `plan.yaml` has carried `scope.include` / `scope.exclude` since the schema was written and
    nothing ever read them — so "do not reach into other tasks' territory" was an instruction in a
    prompt, checked by nobody. A task with no declared scope is unbounded, which is what an empty
    `include` has always meant; it is not read as "nothing is allowed".

    A pattern covers the path it names and everything beneath it, trailing slash or not
    (:func:`rein.common.path_covered`). The same rule `guard.paths` uses, so an operator learns it
    once — and the same rule whichever way they spell a directory, which is what it did not use
    to be.
    """
    include, exclude = task.scope_include, task.scope_exclude
    if not include and not exclude:
        return []
    return sorted(
        path for path in changed if _matches_any(path, exclude) or (include and not _matches_any(path, include))
    )


def _matches_any(path: str, patterns: Sequence[str]) -> bool:
    return any(common.path_covered(path, p) for p in patterns)


def build(
    task: dag.Task,
    *,
    plan: models.Plan | None,
    repo_path: Any,
    changed: Sequence[str] = (),
    diff_cmd: str = "",
    base: str = "",
    history: Sequence[Mapping[str, Any]] = (),
    handoff: Mapping[str, Any] | None = None,
    env: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble one task's dossier. Pure apart from `repo_path`, which reads the pinned sources.

    `repo_path` is a callable `(rel) -> Path` — `Repo.path`. Only used to digest the documents the
    agent is being sent to read, so that what it read is answerable later rather than assumed.
    """
    claims = _claims_for(task, plan)
    document: dict[str, Any] = {
        "task": {
            "id": task.id,
            "title": task.title,
            "kind": task.kind,
            "risk": task.risk,
            "scope": {"include": list(task.scope_include), "exclude": list(task.scope_exclude)},
        },
        "claims": claims,
        "acceptance": _acceptance(task),
        "sources": _sources(task, claims, repo_path),
    }
    diff = classify_paths(changed)
    if diff or base or diff_cmd:
        document["diff"] = {
            **({"base": base} if base else {}),
            **({"command": diff_cmd} if diff_cmd else {}),
            **diff,
        }
    if history:
        document["history"] = [dict(entry) for entry in list(history)[-MAX_HISTORY:]]
    note = _handoff_note(handoff or {})
    if note:
        document["previous_attempt"] = note
    review = (handoff or {}).get("review")
    if isinstance(review, Mapping) and review.get("findings"):
        # So the reviewer does not re-derive the objection it already made, and so the implementer
        # can see what was said about the work it is continuing.
        document["review"] = {"findings": list(review["findings"])}
    if env:
        document["env"] = dict(env)
    return document


def write(cwd: str, document: Mapping[str, Any]) -> Path:
    """Write a dossier into a worktree and return its path. Creates `.rein/work/` if needed."""
    target = path_for(cwd, str(document.get("task", {}).get("id", "task")))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return target


def handover_bytes(document: Mapping[str, Any], written: Path, repo_path: Any) -> int:
    """How much this launch was told to read: the dossier itself plus the documents it names.

    Distinct from the prompt bytes the loop sends, and much larger. The prompt is a few kilobytes
    of instruction; the reading list is this file plus the ticket, the design slice and the
    baseline — which is where a build's input actually goes, and the number that decides whether
    handing the same documents to every launch is worth caching.

    It is a ceiling on the reading list, not a measurement of what was read: an agent may skim the
    design document or open half the repository beside it, and neither is visible from this side of
    the process boundary. A source that has since disappeared contributes nothing rather than
    raising — a measurement is not a thing to fail a build on.
    """
    total = written.stat().st_size if written.is_file() else 0
    sources = document.get("sources")
    for entry in (sources if isinstance(sources, Mapping) else {}).values():
        rel = entry.get("path") if isinstance(entry, Mapping) else None
        if not isinstance(rel, str):
            continue
        candidate = repo_path(rel)
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


#: What a reviewer may say about a change (`models.FINDING_SEVERITY_VALUES`).
FINDING_SEVERITIES = models.FINDING_SEVERITY_VALUES

#: Caps on what a reviewer can hand back. Its output is untrusted like every other reviewer's
#: (`review_policy`'s whole premise), so a payload past these is refused rather than truncated.
MAX_FINDINGS = 50
_FINDING_TEXT_MAX = 2000


def parse_findings(raw: str) -> list[dict[str, Any]]:
    """A reviewer's findings, validated. Raises :class:`FindingsError` on anything unusable.

    Strict about shape and silent about opinion: what a finding *says* is the reviewer's business
    and this module never second-guesses it. What it may not do is arrive in a form the loop would
    have to interpret — an unknown severity, an unparseable body, fifty thousand entries — because
    then "the reviewer found nothing" and "the reviewer said something nobody could read" would
    reach the run as the same answer.
    """
    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise FindingsError(f"the reviewer's findings are not valid JSON ({exc})") from None
    if not isinstance(document, dict) or not isinstance(document.get("findings"), list):
        raise FindingsError("the reviewer's findings must be an object with a `findings` list")
    entries = document["findings"]
    if len(entries) > MAX_FINDINGS:
        raise FindingsError(f"{len(entries)} findings is past the cap of {MAX_FINDINGS} — split the review")
    findings: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise FindingsError(f"findings[{index}] is not an object")
        severity = str(entry.get("severity", ""))
        if severity not in FINDING_SEVERITIES:
            raise FindingsError(f"findings[{index}]: severity {severity!r} is not one of {sorted(FINDING_SEVERITIES)}")
        statement = str(entry.get("statement", "")).strip()
        if not statement:
            raise FindingsError(f"findings[{index}]: a finding has to say something")
        findings.append(
            {
                "severity": severity,
                "statement": statement[:_FINDING_TEXT_MAX],
                "anchor": str(entry.get("anchor", ""))[:512],
            }
        )
    return findings


def must_fix(findings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [f for f in findings if f.get("severity") == "must_fix"]


def render_findings(findings: Sequence[Mapping[str, Any]]) -> str:
    """The findings as the next implementer is told them — one line each, anchor first."""
    return "\n".join(f"  - {f.get('anchor') or '(no anchor)'}: {f.get('statement')}" for f in findings)


class FindingsError(RuntimeError):
    """The reviewer's output could not be read as findings."""


def _claims_for(task: dag.Task, plan: models.Plan | None) -> list[dict[str, Any]]:
    """The claims this task answers, with what each one says.

    The task ticket names claim ids; the statements live in the frozen plan. An agent handed only
    the ids either goes and reads the plan or, more often, works from the ticket's prose and never
    learns what the claim it is answering actually asserts.
    """
    if plan is None:
        return []
    wanted = set(task.claim_ids)
    return [
        {
            "id": claim.id,
            "statement": str(claim.raw.get("statement", "")),
            "risk": claim.risk,
            "requirement_ids": [str(r) for r in claim.raw.get("requirement_ids", [])],
        }
        for claim in plan.claims
        if claim.id in wanted
    ]


def _acceptance(task: dag.Task) -> list[dict[str, Any]]:
    """This task's own bar, and how each criterion will be judged.

    Saying *how* matters as much as saying what. A criterion the loop will run as a command is a
    thing to make pass; one that is prose only is a thing to make true and explain at gate ④; one
    that is `external` is a thing to make ready for somebody to look at. Handing all three over as
    an undifferentiated checklist — which is what a markdown ticket does — loses that.
    """
    rows: list[dict[str, Any]] = []
    for entry in task.acceptance:
        spec = entry.get("evidence")
        row: dict[str, Any] = {"id": str(entry.get("id", "?")), "statement": str(entry.get("statement", ""))}
        if isinstance(spec, dict):
            row["evidence"] = {k: v for k, v in spec.items() if k in {"kind", "command", "paths"}}
        else:
            row["evidence"] = {"kind": "prose"}
        rows.append(row)
    return rows


def _sources(task: dag.Task, claims: Sequence[Mapping[str, Any]], repo_path: Any) -> dict[str, Any]:
    """The documents this task is sent to read, each with the digest of what it will find there.

    Recording the digest is what makes "which text was this built from" answerable after the fact
    — the question that had no answer when a ticket was edited between the approval and the build.
    """
    found: dict[str, Any] = {}
    for name, rel in (
        ("ticket", f"docs/tasks/{task.id}.md"),
        ("design", "docs/20-design.md"),
        ("baseline", "docs/05-current-state.md"),
    ):
        candidate = repo_path(rel)
        if candidate.is_file():
            found[name] = {"path": rel, "digest": digests.of_file(candidate)}
    if "design" in found and claims:
        found["design"]["read"] = "the section(s) covering " + ", ".join(str(c["id"]) for c in claims)
    return found


def _handoff_note(handoff: Mapping[str, Any]) -> dict[str, Any]:
    """What an interrupted attempt left, as data rather than as a sentence to parse."""
    note: dict[str, Any] = {}
    for key in ("failed_step", "salvage_branch", "salvage_state"):
        if handoff.get(key):
            note[key] = handoff[key]
    report = handoff.get("report")
    if isinstance(report, Mapping) and report.get("outcome"):
        note["reported"] = {"outcome": report["outcome"], "summary": str(report.get("summary", ""))}
    return note
