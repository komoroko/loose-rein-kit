"""Read-only aggregation of what a human must read before opening a gate — the review pane's data.

status_api.py answers "where does the lifecycle stand"; this module answers the companion question
"what do I read to approve the gate in front of me". `collect_review(root, gate)` returns one JSON
object per gate: the phase deliverables rendered through mdlite (escape-first — see its threat
model), each deliverable's Self-assessment section split out so the pane can pin it, and for gate
④ the work-branch diff plus the generated review's freshness.

Reach is fixed server-side, the same way ui.action_argv fixes command lines: the client sends only
a gate name; which files are read comes from the `_GATE_SPEC` constant plus a template-excluding
glob inside two fixed directories, every path is containment-checked after `resolve()` (a symlinked
deliverable pointing outside the repo is reported missing, never followed), and a single file is
capped at `_MAX_DELIVERABLE` bytes. Git use is read-only subprocesses with a timeout; a non-git or
detached repo degrades the diff block to an error/log field, never an exception.

Reads are tolerant like status_api: a missing deliverable renders as `exists: false` (the reviewer
should *see* that a gate's document is absent), and only an unknown gate raises (`ReviewError` →
the HTTP layer's 404).
"""

from __future__ import annotations

import html
import re
import subprocess
from datetime import datetime
from pathlib import Path

from rein import event_chain, human_review, mdlite, models, status_api, strict_yaml
from rein import events as events_mod

_MAX_DELIVERABLE = 300_000  # bytes of one deliverable the pane will render
_MAX_PATCH = 200_000  # bytes of unified diff for gate ④
_GIT_TIMEOUT_SEC = 10
_GLOB_NAME_RE = re.compile(r"^(T|ADR)-[A-Za-z0-9_.-]+\.md$")
_TEMPLATE_NAMES = frozenset({"T-template.md", "ADR-template.md"})
# The *labelled* confidence line ("- **Confidence**: …"), not any prose mentioning the word: the
# label must be what precedes the colon, so a sentence like "we have high confidence in X" is not
# mistaken for the assessment. The value is everything after that colon.
_CONFIDENCE_LINE_RE = re.compile(r"^[^:\n]*\bconfidence\b[^:\n]*:(?P<value>.*)$", re.IGNORECASE | re.MULTILINE)
_LEVEL_RE = re.compile(r"\b(high|medium|low)\b", re.IGNORECASE)
# The scaffold's unfilled placeholder is the three levels as a slash run ("high / medium / low",
# optionally "per area …"). A genuinely filled per-area line separates them differently
# ("architecture=high / choices=medium"), so this run is a precise "nobody answered" signal.
_PLACEHOLDER_RE = re.compile(r"\bhigh\s*/\s*medium\s*/\s*low\b", re.IGNORECASE)
_LEVEL_RANK = {"low": 0, "medium": 1, "high": 2}

# Gate -> what the human reads to open it. "main" is the deliverable under approval, "context" the
# upstream document it is judged against. ("glob", dir, pattern) expands inside that fixed
# directory only, excluding the scaffold templates; ("code", path) renders verbatim, not as
# markdown (tasks.yaml is machine truth — reviewers must see it exactly).
_SpecItem = str | tuple[str, str] | tuple[str, str, str]
_GATE_SPEC: dict[str, dict[str, list[_SpecItem]]] = {
    "requirements": {"main": ["docs/10-requirements.md"], "context": ["docs/00-product-brief.md"]},
    "design": {
        "main": ["docs/20-design.md", ("glob", "docs/decisions", "ADR-*.md")],
        "context": ["docs/10-requirements.md"],
    },
    "tasks": {"main": [("glob", "docs/tasks", "T-*.md"), ("code", ".rein/plan.yaml")], "context": []},
    # Gate 4 reviews the generated review, not a security-review markdown file: green tests
    # plus an AI's summary was never the evidence this gate is supposed to weigh.
    "build": {"main": [("code", ".rein/review.yaml")], "context": []},
    "release": {"main": ["docs/test/test-plan.md", "docs/retrospective.md"], "context": []},
}


class ReviewError(Exception):
    """An unknown gate name — the only input error this module can be handed."""


def _confidence(section_md: str) -> str | None:
    """The confidence level the pane badges, or None when the author never stated one.

    Two rules, both in service of "the badge must never look better than the document":

    - Read only the value of a **labelled** `Confidence:` line. The word also occurs in the section
      heading and in prose ("we have high confidence the runner exists"), and taking the first
      level found anywhere would let that prose badge a `low` self-assessment as `high`.
    - AGENTS.md asks for confidence *by area*, so one line legitimately carries several levels
      ("high (API surface), low (integration)"). Report the **weakest** — the low spot is the part
      the human must not miss. The unfilled scaffold placeholder is recognised separately and
      reads as unset rather than as a real `low`.
    """
    for line in _CONFIDENCE_LINE_RE.finditer(section_md):
        value = line.group("value")
        if _PLACEHOLDER_RE.search(value):
            return None
        levels = {m.group(1).lower() for m in _LEVEL_RE.finditer(value)}
        if levels:
            return min(levels, key=lambda lv: _LEVEL_RANK[lv])
    return None


def _within(root: Path, path: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def _deliverable(root: Path, rel: str | Path, *, kind: str = "markdown") -> dict[str, object]:
    """One deliverable entry: rendered body, split-out self-assessment, and honest absence."""
    rel = Path(rel)
    path = root / rel
    entry: dict[str, object] = {
        "id": rel.name,
        "label": str(rel),
        "kind": kind,
        "exists": False,
        "html": "",
        "self_assessment": None,
        "truncated": False,
        "mtime": None,
    }
    if not _within(root, path):
        return entry  # a symlink pointing out of the repo reads as absent, never followed
    try:
        raw = path.read_bytes()
        stat = path.stat()
    except OSError:
        return entry
    entry["exists"] = True
    entry["mtime"] = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
    if len(raw) > _MAX_DELIVERABLE:
        raw = raw[:_MAX_DELIVERABLE]
        entry["truncated"] = True
    text = raw.decode("utf-8", errors="replace")
    if kind == "code":
        entry["html"] = "<pre><code>" + html.escape(text, quote=True) + "</code></pre>"
        return entry
    section, rest = mdlite.extract_section(text, "Self-assessment")
    if section is not None:
        entry["self_assessment"] = {"html": mdlite.render(section), "confidence": _confidence(section)}
        text = rest
    entry["html"] = mdlite.render(text)
    return entry


def _expand(root: Path, spec: list[_SpecItem]) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    for item in spec:
        if isinstance(item, str):
            out.append(_deliverable(root, item))
        elif len(item) == 2:  # ("code", path)
            out.append(_deliverable(root, item[1], kind="code"))
        else:  # ("glob", dir, pattern) — fixed directory, template-free, name-validated, sorted
            _, rel_dir, pattern = item
            base = root / rel_dir
            names = sorted(
                p.name for p in base.glob(pattern) if p.name not in _TEMPLATE_NAMES and _GLOB_NAME_RE.match(p.name)
            )
            out.extend(_deliverable(root, Path(rel_dir) / n) for n in names)
    return out


# -- gate ④: the work-branch diff and the generated-review freshness --


def _git(root: Path, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=_GIT_TIMEOUT_SEC)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, proc.stdout if proc.returncode == 0 else (proc.stderr or proc.stdout)


def _default_branch(root: Path) -> str | None:
    rc, out = _git(root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and out.strip():
        return out.strip()
    for candidate in ("main", "master"):
        rc, _ = _git(root, "rev-parse", "--verify", "--quiet", candidate)
        if rc == 0:
            return candidate
    return None


def _diff_block(root: Path) -> dict[str, object]:
    """The gate-④ change set: merge-base(HEAD, default branch) diff, or an honest fallback.

    Same base definition as the build loop's security-review prompt. When no base exists (no
    default branch, HEAD *is* the base, single-branch repo) the block degrades to the last 20
    commits so the reviewer still sees what the branch contains.
    """
    rc, out = _git(root, "rev-parse", "HEAD")
    if rc != 0:
        return {"error": "not a git repository (or it has no commits)"}
    head = out.strip()
    base_ref = _default_branch(root)
    base = None
    if base_ref:
        rc, out = _git(root, "merge-base", "HEAD", base_ref)
        base = out.strip() if rc == 0 and out.strip() else None
    if base is None or base == head:
        rc, out = _git(root, "log", "--oneline", "-20")
        return {
            "head": head,
            "log": out.strip().splitlines() if rc == 0 else [],
            "note": "no merge-base diff (HEAD is at the base or no default branch); showing recent commits",
        }
    _, stat = _git(root, "diff", "--stat", f"{base}..HEAD")
    _, names = _git(root, "diff", "--name-status", f"{base}..HEAD")
    _, patch = _git(root, "diff", f"{base}..HEAD")
    truncated = len(patch.encode("utf-8", errors="replace")) > _MAX_PATCH
    if truncated:
        patch = patch.encode("utf-8", errors="replace")[:_MAX_PATCH].decode("utf-8", errors="replace")
    return {
        "head": head,
        "base": base,
        "base_ref": base_ref,
        "stat": stat.rstrip(),
        "name_status": [ln.split("\t", 1) for ln in names.strip().splitlines() if "\t" in ln],
        "patch": patch,  # raw text — the client renders each line as a JSX text child, never as HTML
        "truncated": truncated,
    }


def _review_meta(root: Path, head: str | None) -> dict[str, object]:
    """Whether the generated machine review speaks for the commit actually under review.

    Gate ④ approves the generated *review.yaml*, whose machine binding records the
    `subject_head_sha` it was produced against. Freshness is that sha against the current HEAD —
    a commit made after the review was generated leaves the review stale (plan §17.5, E2E-08),
    and the pane must show it rather than imply currency.
    """
    try:
        raw = strict_yaml.load_mapping((root / ".rein" / "review.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return {"reviewed_head": None, "head": head, "fresh": False}
    machine = raw.get("machine")
    binding = machine.get("binding") if isinstance(machine, dict) else None
    reviewed = str(binding.get("subject_head_sha", "")) if isinstance(binding, dict) else ""
    reviewed_or_none = reviewed or None
    return {"reviewed_head": reviewed_or_none, "head": head, "fresh": bool(reviewed and head and reviewed == head)}


def _gate_statuses(root: Path) -> dict[str, str]:
    """Gate statuses from state.yaml; {} when it cannot be read.

    A broken SSOT must not take the review pane down — but an unreadable gate reads as
    `pending`, never as approved, so the pane can only ever understate what has been decided.
    """
    try:
        raw = strict_yaml.load_mapping((root / ".rein" / "state.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return {}
    state = models.State(raw)
    return {gate: state.gate_status(gate) for gate in models.GATE_ORDER}


def collect_review(root: str | Path, gate: str) -> dict[str, object]:
    """Everything the review pane shows for `gate`. Raises ReviewError only for an unknown gate."""
    if gate not in _GATE_SPEC:
        raise ReviewError(f"unknown gate '{gate}' (expected one of {', '.join(models.GATE_ORDER)})")
    root = Path(root)

    gates = _gate_statuses(root)
    awaiting = next((g for g in models.GATE_ORDER if gates.get(g) != "approved"), None)

    result: dict[str, object] = {
        "gate": gate,
        "index": models.GATE_ORDER.index(gate) + 1,
        "status": gates.get(gate, "pending"),
        "awaiting": awaiting,
        "is_awaiting": gate == awaiting,
        "deliverables": _expand(root, _GATE_SPEC[gate]["main"]),
        "context": _expand(root, _GATE_SPEC[gate]["context"]),
        "diff": None,
        "review_meta": None,
        "open_escalations": None,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if gate == "build":
        diff = _diff_block(root)
        result["diff"] = diff
        head_value = diff.get("head")
        result["review_meta"] = _review_meta(root, head_value if isinstance(head_value, str) else None)
    if gate == "release":
        events, _ = event_chain.scan(root / ".rein" / "events.ndjson")
        result["open_escalations"] = len(events_mod.open_attention(events, status_api.task_status_of(root)))
    return result


# -- gate ④ human review session (plan §14.1, §21.1, §21.2) --

# The deliverable review above answers "what do I read"; this session answers the harder question
# gate ④ asks — "what do *you* decide". The stages run scope (what this approval covers) → orient
# (what was actually built, and under which conditions) → decision (the answers) → diff → freeze.
# The two reading stages before the questions are the load-bearing part: a reviewer who has to
# reconstruct the change from a diff before every card spends their attention on reconstruction.
# The rules live in human_review and the orient content in brief; this layer only shapes them into
# JSON. Every payload is machine-review content plus the reviewer's own progress — never raw agent
# HTML (the client renders via textContent).


def _load_review(root: Path) -> models.Review | None:
    """review.yaml as a Review, or None when absent/unreadable — tolerant like the rest of the pane."""
    try:
        raw = strict_yaml.load_mapping((root / ".rein" / "review.yaml").read_text(encoding="utf-8"))
    except (OSError, strict_yaml.StrictParseError):
        return None
    return models.Review(raw)


def _not_generated(stage: str | None = None) -> dict[str, object]:
    payload: dict[str, object] = {"generated": False, "reason": "no machine review has been generated"}
    if stage is not None:
        payload["stage"] = stage
    return payload


def _coverage_totals(review: models.Review) -> dict[str, object]:
    """What the detector read, and — the part that matters — what it could not.

    `analyzed_files` is a count; the manifest never lists the paths it *did* read. So the honest
    summary is a number for the covered side and a path list for the uncovered one, which is also
    the asymmetry a reviewer needs: nobody can act on "eleven files were fine", and everybody can
    act on "ui.min.js was never parsed".
    """
    manifest = review.coverage
    if not manifest:
        return {
            "analyzed_files": 0,
            "analyzed_hunks": 0,
            "analyzed_bytes": 0,
            "coverage_status": "unknown",
            "unsupported_files": [],
            "generated_files": [],
        }
    unsupported = [
        {
            "path": str(item.get("path", "")),
            "reason": str(item.get("reason", "")),
            "detail": str(item.get("detail", "")),
        }
        for item in manifest.get("unsupported_files", []) or []
    ]
    generated = [str(item.get("path", "")) for item in manifest.get("generated_files", []) or []]
    return {
        "analyzed_files": int(manifest.get("analyzed_files", 0) or 0),
        "analyzed_hunks": int(manifest.get("analyzed_hunks", 0) or 0),
        # No default: `analyzed_bytes` is required by the schema, and 0 for an unmeasured manifest
        # is what let a byte budget pass a change it never measured (see human_review._diff_bytes).
        "analyzed_bytes": int(manifest["analyzed_bytes"]),
        "coverage_status": str(manifest.get("coverage_status", "unknown")),
        "unsupported_files": unsupported,
        "generated_files": sorted(set(generated)),
    }


def scope_block(root: Path, review: models.Review) -> dict[str, object]:
    """What this review speaks for — stated before the first question is asked.

    Everything here is derived from what the machine review already recorded: the binding says which
    two commits bound the change, the coverage manifest says how much of it could be read, and the
    budget says whether that is a reviewable amount in one sitting.

    The reason it is a stage rather than a footnote: an approval covers a boundary, and a reviewer
    who does not know the boundary cannot know what they approved. Gate ④ binds a human's judgement to
    `trusted_base_sha..subject_head_sha`; that range should be the first thing they read, not
    something reconstructible afterwards from review.yaml.
    """
    binding = review.machine.get("binding")
    binding = binding if isinstance(binding, dict) else {}
    head_rc, head_out = _git(root, "rev-parse", "HEAD")
    head = head_out.strip() if head_rc == 0 else None
    reviewed_head = str(binding.get("subject_head_sha", "")) or None
    machine = review.machine
    return {
        "base": str(binding.get("trusted_base_sha", "")) or None,
        "head": reviewed_head,
        "repo_head": head,
        # A commit made after the review was generated leaves it stale, and the scope card is the
        # first place that can say so — before the reviewer has spent any attention.
        "fresh": bool(reviewed_head and head and reviewed_head == head),
        "generated_at": str(binding.get("generated_at", "")) or None,
        "effective_risk": review.effective_risk,
        "independence": binding.get("independence") or {},
        "coverage": _coverage_totals(review),
        "counts": {
            "claims": len(review.claim_results),
            "gaps": len(machine.get("gaps", []) or []),
            "extra_behaviors": len(review.extra_behaviors),
            "scenarios": len(machine.get("scenarios", []) or []),
            "decision_cards": len(machine.get("decision_cards", []) or []),
            "security_findings": len(review.security_findings),
            "statements": len(machine.get("statements", []) or []),
        },
        # How much of a judgement this session will actually ask for. Only high/critical cards
        # block the freeze, so the card total is not what a reviewer is about to be answerable for.
        "decisions_required": len(human_review.unanswered_decisions(review, review.human)),
        "budget": human_review.budget_report(review, review.human),
        "scope_split_required": human_review.scope_split_required(review, review.human),
    }


def review_session(root: str | Path) -> dict[str, object]:
    """The whole state of the human review: stage progress, what is still unanswered, every blocker.

    This is the one call the review pane polls: it carries the outstanding decisions, the expertise
    and budget verdicts, and the machine digest a subsequent write must echo back (a stale one is
    refused — plan §17.5).
    """
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        return _not_generated()
    human = dict(review.human)
    # `settled` is None for the reading stages: they record nothing, so neither "done" nor "skipped"
    # is a claim this payload is entitled to make (human_review.stage_settled).
    stages = [
        {
            "name": stage,
            "settled": human_review.stage_settled(review, human, stage),
        }
        for stage in models.REVIEW_STAGE_ORDER
    ]
    return {
        "generated": True,
        "human_status": review.human_status,
        "machine_digest": review.machine_digest(),
        "scope": scope_block(root, review),
        "unanswered_decisions": human_review.unanswered_decisions(review, human),
        "expertise_gaps": human_review.expertise_gaps(review, human),
        "budget": human_review.budget_report(review, human),
        "scope_split_required": human_review.scope_split_required(review, human),
        "completion_blockers": human_review.completion_blockers(review, human),
        "can_freeze": human_review.can_freeze(review, human),
        "stages": stages,
    }


#: Ceiling on an as-built body served to the pane. Past it the response says the file is too large
#: and gives its size — a silently truncated schema is a schema somebody would read as complete.
AS_BUILT_MAX_BYTES = 256 * 1024


def _declared_as_built(review: models.Review) -> set[str]:
    """Every path the stored brief named as an as-built view of a declared surface.

    This is the whole access rule. The route reads a blob out of the repository at a commit, so
    what it may read must come from the review itself rather than from the request: a path the
    frozen plan declared, that `brief.derive` then resolved against the reviewed tree. Anything
    else — including a perfectly ordinary source file — is not part of what this gate published.
    """
    brief = review.machine.get("brief")
    section = brief.get("requirements_on_people") if isinstance(brief, dict) else None
    if not isinstance(section, dict):
        return set()
    entries = list(section.get("unobserved") or [])
    declared = section.get("as_declared")
    if isinstance(declared, dict):
        entries += list(declared.get("entries") or [])
    paths = set()
    for entry in entries:
        for built in (entry.get("as_built") or []) if isinstance(entry, dict) else []:
            if isinstance(built, dict) and built.get("path"):
                paths.add(str(built["path"]))
    return paths


def as_built(root: str | Path, path: str) -> dict[str, object]:
    """One declared surface as it *ends up*, read at the commit the review is bound to.

    A diff shows what changed; this shows what somebody now has to operate — the schema after the
    migration, the settings module after the key was added. It is a fetch rather than a section of
    the brief because a body belongs in review.yaml even less than a diff does: the document would
    grow a copy of the repository, and the copy would be the thing that goes stale.

    Read at `binding.subject_head_sha`, never from the working tree. The rest of gate ④ describes
    one commit, and a file from a different one shown beside it is the mistake `stage_data` refuses
    to make when it declines to recompute the brief.
    """
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        raise ReviewError("no machine review has been generated — there is nothing bound to a commit to read")
    if path not in _declared_as_built(review):
        raise ReviewError(f"{path!r} is not an as-built path of any surface this review declared")
    binding = review.machine.get("binding")
    head = str(binding.get("subject_head_sha", "")) if isinstance(binding, dict) else ""
    if not head:
        raise ReviewError("the review records no subject head to read the file at")

    rc, blob = _git(root, "rev-parse", f"{head}:{path}")
    blob = blob.strip()
    if rc != 0 or not blob:
        raise ReviewError(f"{path} does not exist at {head[:12]}")
    rc, size = _git(root, "cat-file", "-s", blob)
    if rc != 0 or not size.strip().isdigit():
        # Refuse rather than read: an unmeasured blob is one the ceiling below never applied to.
        raise ReviewError(f"{path}@{head[:12]}: its size could not be measured, so it is not served")
    measured = int(size.strip())
    if measured > AS_BUILT_MAX_BYTES:
        return {"path": path, "commit": head, "bytes": measured, "too_large": True, "limit": AS_BUILT_MAX_BYTES}
    rc, content = _git(root, "show", f"{head}:{path}")
    if rc != 0:
        raise ReviewError(f"{path}@{head[:12]} could not be read")
    return {"path": path, "commit": head, "bytes": measured, "content": content}


def stage_data(root: str | Path, stage: str) -> dict[str, object]:
    """The content of one review stage.

    Raises ReviewError for an unknown stage (the HTTP layer's 404). No stage withholds anything any
    more: a card's evidence used to be stripped until the reviewer had recorded an unprimed guess
    about it, which made the screen a quiz standing in front of the decision. What lowers the cost
    of deciding is the opposite — `scope` and `orient` hand over the boundary and the change before
    the first card is asked.
    """
    if stage not in models.REVIEW_STAGE_VALUES:
        raise ReviewError(f"unknown review stage '{stage}' (one of {', '.join(models.REVIEW_STAGE_ORDER)})")
    root = Path(root)
    review = _load_review(root)
    if review is None or not review.is_generated:
        return _not_generated(stage)
    human = dict(review.human)
    machine = review.machine
    payload: dict[str, object] = {"stage": stage, "generated": True}
    if stage == "scope":
        payload["scope"] = scope_block(root, review)
    elif stage == "orient":
        # What was built, and under what conditions — derived at generation time and carried in the
        # machine half (brief.derive). Served as it was recorded: this layer must not recompute it,
        # or the reviewer would be reading a brief about a different tree than the one the rest of
        # the review is bound to.
        payload["brief"] = machine.get("brief", {})
        payload["residual_findings"] = list(machine.get("residual_findings", []) or [])
        # The claim comparison belongs here rather than on the decision screen: only the claims the
        # comparator could *not* settle become cards, so a reviewer who saw cards alone would never
        # see what the review did establish. It asks for nothing, which is why it can sit in a
        # reading stage — and the three axes stay separate lanes, because integrity is a fact,
        # semantic support is somebody's judgement, and conformance is an observation.
        payload["claims"] = list(review.claim_results)
        payload["actual_extraction"] = list(machine.get("actual_extraction", []) or [])
    elif stage == "decision":
        # The one screen that asks for anything. The cards *with* their evidence, the sentences
        # their options mean, the security findings (cards too — severity-ordered alongside the
        # rest), the gaps and extra behaviours that raised them, and what this reviewer has already
        # answered: a decision screen that could not show its own previous answer would ask for it
        # twice.
        payload["decision_cards"] = [dict(c) for c in machine.get("decision_cards", []) or []]
        payload["statements"] = list(machine.get("statements", []) or [])
        payload["gaps"] = list(machine.get("gaps", []) or [])
        payload["extra_behaviors"] = list(review.extra_behaviors)
        payload["security_findings"] = list(review.security_findings)
        payload["summary"] = machine.get("summary", {})
        payload["decisions"] = [d for d in human.get("decisions", []) or [] if isinstance(d, dict)]
        payload["unanswered"] = human_review.unanswered_decisions(review, human)
    elif stage == "diff":
        payload["diff"] = _diff_block(root)
    elif stage == "freeze":
        payload["can_freeze"] = human_review.can_freeze(review, human)
        payload["completion_blockers"] = human_review.completion_blockers(review, human)
    return payload
