"""One-way-mirror the plan's issues and sub-issues to GitHub Issues (human-facing visibility, opt-in).

Treating `.rein/plan.yaml` (the task graph's SSOT) as **truth**, idempotently projects each issue I-NNN to one
GitHub Issue and each of its sub-issues T-NNN to a GitHub sub-issue of it. **One-way only** — Issues-side
edits are never read back (preserving deterministic, offline operation).
Not part of the orchestrator's (build_loop.py) control flow; it is called as a side effect from slash commands.

Behavior:
  - Off by default. Enable with `github.enabled: true` in `.rein/config.yaml`.
  - If `gh` is absent / remote is absent / disabled, **print an explicit message and exit 0** (auto-skip).
  - The issue number is not written to the plan. Match `gh issue list` results by the hidden body marker
    `<!-- rein:I-NNN -->` / `<!-- rein:T-NNN -->` (uniqueness is already validated by the plan), falling back to
    the title prefix for issues created before the marker existed. This prevents drift without polluting the
    SSOT and survives a human editing the issue title (title-only matching would create a duplicate).
  - Create a missing issue, update on content diff, close on `status==done` (`close_on_done`). Never delete.
    An issue closes once every sub-issue it holds is done.
  - A sub-issue is created under its issue (`gh issue create --parent`) and moved back under it
    (`gh issue edit --parent`) when the two disagree. A gh without `--parent` is refused, not worked
    around: a mirror with the hierarchy missing would show the work flat and say nothing about it.

`--dry-run` calls `gh` not at all and outputs the planned creations from tasks.yaml (for offline/testing).
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rein import common, dag, models
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

CONFIG_PATH = ".rein/config.yaml"


class IssueSyncError(RuntimeError):
    """A gh integration failure."""


@dataclass(frozen=True)
class GithubConfig:
    enabled: bool
    label: str
    close_on_done: bool
    repo: str
    # Where gh/git run (repo-remote inference is cwd-sensitive). Not a YAML knob — load() sets
    # it to the config file's repository so calls behave the same from any launch directory.
    root: str = "."

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> GithubConfig:
        try:
            data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            data = {}
        gh = (data.get("github") if isinstance(data, dict) else None) or {}
        return cls(
            enabled=bool(gh.get("enabled", False)),
            label=str(gh.get("label", "rein")),
            close_on_done=bool(gh.get("close_on_done", True)),
            repo=str(gh.get("repo", "") or ""),
            root=str(Path(path).resolve().parent.parent),
        )


@dataclass(frozen=True)
class DesiredIssue:
    title: str
    body: str
    labels: tuple[str, ...]
    closed: bool


@dataclass(frozen=True)
class ExistingIssue:
    number: int
    title: str
    state: str  # "OPEN" | "CLOSED"
    labels: tuple[str, ...]
    body: str
    #: The number of the issue GitHub has this one under, or None.
    parent: int | None = None


@dataclass(frozen=True)
class Action:
    op: str  # create | update | close | reopen | reparent
    task_id: str  # the plan id it mirrors: an issue's I-NNN or a sub-issue's T-NNN
    number: int | None
    desired: DesiredIssue
    add_labels: tuple[str, ...] = field(default=())
    remove_labels: tuple[str, ...] = field(default=())
    #: The plan id of the issue this one belongs under ("" for an issue itself).
    parent: str = ""


# --- pure logic (under test) -----------------------------------------------


#: Label families this tool owns. Anything else on an issue belongs to a human and is left
#: alone. Keeping this in step with `desired_issue` is load-bearing: a family that is emitted
#: but not listed here reads as "missing" on every run, so each sync re-adds the same labels
#: forever.
MANAGED_PREFIXES: tuple[str, ...] = ("kind:", "status:", "risk:", "claim:")


def _managed(label: str, base_label: str) -> bool:
    """Whether this is a label this tool manages. Everyone else's labels are untouched."""
    return label == base_label or label.startswith(MANAGED_PREFIXES)


# The invisible task marker embedded in each mirror issue's body. Matching by it (not the title)
# keeps the issue↔task link intact when a human edits the title.
_MARKER_RE = re.compile(r"<!--\s*rein:([IT]-\d+)\s*-->")


def task_id_of(title: str, body: str) -> str:
    """The task an existing issue mirrors: the hidden body marker wins; fall back to the title prefix."""
    match = _MARKER_RE.search(body)
    return match.group(1) if match else title.split(":", 1)[0].strip()


def _issue_body(task: dag.Task) -> str:
    deps = ", ".join(task.blocked_by) if task.blocked_by else "none"
    return "\n".join(
        [
            f"`{task.id}` — Loose Rein sub-issue of `{task.issue_id}` (SSOT: `.rein/plan.yaml` / details: "
            f"`docs/tasks/{task.id}.md`)",
            "",
            f"- kind: {task.kind}",
            f"- risk: {task.risk}",
            f"- claims: {', '.join(task.claim_ids) or '(none)'}",
            f"- blockedBy: {deps}",
            "",
            "> This issue is a **one-way mirror** from plan.yaml. Editing it here changes nothing:",
            "> the plan is frozen by the mandate and only `rein revise` can move it.",
            "",
            f"<!-- rein:{task.id} -->",
        ]
    )


def desired_issue(task: dag.Task, *, base_label: str, close_on_done: bool) -> DesiredIssue:
    labels = [base_label, f"kind:{task.kind}", f"status:{task.status}", f"risk:{task.risk}"]
    labels += [f"claim:{cid}" for cid in task.claim_ids]
    return DesiredIssue(
        title=f"{task.id}: {task.title}",
        body=_issue_body(task),
        labels=tuple(labels),
        closed=task.is_done and close_on_done,
    )


def desired_parent(
    issue: models.Issue, members: Sequence[dag.Task], *, base_label: str, close_on_done: bool
) -> DesiredIssue:
    """The mirror of one issue: what it is for, and the sub-issues GitHub holds under it."""
    status = dag.rollup_status([t.status for t in members])
    deps = ", ".join(issue.blocked_by) if issue.blocked_by else "none"
    body = "\n".join(
        [
            f"`{issue.id}` — Loose Rein issue (SSOT: `.rein/plan.yaml`). The most one launch carries; "
            "what was approved is its sub-issues.",
            "",
            f"- kind: {issue.kind}",
            f"- sub-issues: {', '.join(t.id for t in members) or '(none)'}",
            f"- blockedBy: {deps}",
            "",
            "> This issue is a **one-way mirror** from plan.yaml. Editing it here changes nothing:",
            "> the plan is frozen by the mandate and only `rein revise` can move it.",
            "",
            f"<!-- rein:{issue.id} -->",
        ]
    )
    return DesiredIssue(
        title=f"{issue.id}: {issue.title}",
        body=body,
        labels=(base_label, f"kind:{issue.kind}", f"status:{status}"),
        closed=status == "done" and close_on_done,
    )


def _content_differs(ex: ExistingIssue, desired: DesiredIssue, base_label: str) -> bool:
    ex_managed = {label for label in ex.labels if _managed(label, base_label)}
    return ex.title != desired.title or ex.body != desired.body or ex_managed != set(desired.labels)


def plan_actions(
    tasks: tuple[dag.Task, ...],
    existing_by_id: dict[str, ExistingIssue],
    *,
    base_label: str,
    close_on_done: bool,
    issues: Sequence[models.Issue] = (),
) -> list[Action]:
    """Derive the deterministic diff-action list from the plan and existing issues.

    Every issue first (ascending id), then every sub-issue (ascending id): a sub-issue is created
    under its issue, so its issue has to exist by then.
    """
    actions: list[Action] = []
    by_issue: dict[str, list[dag.Task]] = {}
    for task in tasks:
        by_issue.setdefault(task.issue_id, []).append(task)
    wanted: list[tuple[str, DesiredIssue, str]] = [
        (
            issue.id,
            desired_parent(issue, by_issue.get(issue.id, []), base_label=base_label, close_on_done=close_on_done),
            "",
        )
        for issue in sorted(issues, key=lambda i: i.id)
    ]
    mirrored = {issue.id for issue in issues}
    wanted += [
        (
            task.id,
            desired_issue(task, base_label=base_label, close_on_done=close_on_done),
            task.issue_id if task.issue_id in mirrored else "",
        )
        for task in sorted(tasks, key=lambda t: t.id)
    ]
    for element_id, desired, parent in wanted:
        ex = existing_by_id.get(element_id)
        if ex is None:
            actions.append(Action("create", element_id, None, desired, add_labels=desired.labels, parent=parent))
            continue
        task_id = element_id
        if _content_differs(ex, desired, base_label):
            ex_managed = {label for label in ex.labels if _managed(label, base_label)}
            add = tuple(sorted(set(desired.labels) - ex_managed))
            remove = tuple(sorted(ex_managed - set(desired.labels)))
            actions.append(Action("update", task_id, ex.number, desired, add_labels=add, remove_labels=remove))
        if parent:
            known = existing_by_id.get(parent)
            if known is None or ex.parent != known.number:
                actions.append(Action("reparent", task_id, ex.number, desired, parent=parent))
        if desired.closed and ex.state == "OPEN":
            actions.append(Action("close", task_id, ex.number, desired))
        elif not desired.closed and ex.state == "CLOSED":
            actions.append(Action("reopen", task_id, ex.number, desired))
    return actions


def format_plan(actions: list[Action]) -> str:
    if not actions:
        return "(no mirror diff: Issues match plan.yaml)"
    return "\n".join(
        f"- {a.op:<8} {a.task_id} :: {a.desired.title}" + (f"  (under {a.parent})" if a.parent else "") for a in actions
    )


# --- label provisioning (gh issue create fails if the target label is absent in the repo, so create idempotently) ----

_DEFAULT_COLOR = "ededed"
_BASE_COLOR = "cccccc"
_REQ_COLOR = "0a3069"
_KIND_COLORS = {"foundation": "8250df", "parallel": "0969da", "integration": "1a7f37"}
_STATUS_COLORS = {
    "todo": "eeeeee",
    "in_progress": "bf8700",
    "blocked": "cf222e",
    "needs-revision": "e16f24",
    "awaiting-evidence": "7057c4",
    "done": "1a7f37",
}
_PHASE_COLORS = {"requirements": "8250df", "design": "0969da", "build": "1a7f37", "verify": "cf222e"}


@dataclass(frozen=True)
class LabelSpec:
    name: str
    color: str
    description: str


def label_specs(graph: dag.Graph, base_label: str) -> list[LabelSpec]:
    """Every label this tool uses: the fixed kind/status/risk sets plus one per claim in the plan."""
    specs: list[LabelSpec] = [LabelSpec(base_label, _BASE_COLOR, "mirror issue for a Loose Rein task")]
    for kind in sorted(dag.KIND_VALUES):
        specs.append(LabelSpec(f"kind:{kind}", _KIND_COLORS.get(kind, _DEFAULT_COLOR), f"kind: {kind}"))
    for status in sorted(dag.STATUS_VALUES):
        specs.append(LabelSpec(f"status:{status}", _STATUS_COLORS.get(status, _DEFAULT_COLOR), f"status: {status}"))
    for risk in models.RISK_ORDER:
        specs.append(LabelSpec(f"risk:{risk}", _PHASE_COLORS.get(risk, _DEFAULT_COLOR), f"risk: {risk}"))
    claims: set[str] = set()
    for task in graph.tasks:
        claims.update(task.claim_ids)
    for claim_id in sorted(claims):
        specs.append(LabelSpec(f"claim:{claim_id}", _REQ_COLOR, f"answers claim {claim_id}"))
    return specs


def _issues(repo: repo_mod.Repo) -> tuple[models.Issue, ...]:
    plan = store_mod.Store(repo).read_plan()
    return plan.issues if plan is not None else ()


# --- gh execution ----------------------------------------------------------


# common.run, aliased so the tests can fake the gh/git calls at this seam.
_run = common.run


def preflight(cfg: GithubConfig) -> tuple[bool, str]:
    """Decide whether integration is possible. If not, return (False, reason); the caller exits 0 and skips."""
    if not cfg.enabled:
        return False, "Skipped Issues mirror because github.enabled=false."
    if shutil.which("gh") is None:
        return False, "Skipped Issues mirror because the gh CLI was not found."
    if not cfg.repo:
        rc, out = _run(["git", "remote"], cwd=cfg.root)
        if rc != 0 or not out.strip():
            return False, "Skipped Issues mirror: no git remote (you can also set github.repo in config)."
    return True, ""


FETCH_LIMIT = 1000  # gh issue list page cap; hitting it means the mirror snapshot may be incomplete


def fetch_existing(cfg: GithubConfig) -> dict[str, ExistingIssue]:
    args = [
        "gh",
        "issue",
        "list",
        "--label",
        cfg.label,
        "--state",
        "all",
        "--json",
        "number,title,state,labels,body,parent",
        "--limit",
        str(FETCH_LIMIT),
    ]
    if cfg.repo:
        args += ["--repo", cfg.repo]
    rc, out = _run(args, cwd=cfg.root)
    if rc != 0:
        raise IssueSyncError(f"gh issue list failed:\n{out[-500:]}")
    try:
        data: Any = json.loads(out or "[]")
    except json.JSONDecodeError as exc:
        raise IssueSyncError(f"cannot parse gh issue list output: {exc}") from exc
    if len(data) >= FETCH_LIMIT:
        # A truncated snapshot would make plan_actions re-create issues it failed to see. Stop
        # safe-side rather than risk duplicating the mirror (never plan against a partial view).
        raise IssueSyncError(
            f"gh issue list returned {FETCH_LIMIT} issues (the fetch limit) — the snapshot may be"
            " truncated and syncing could create duplicates. Prune old mirror issues or raise FETCH_LIMIT."
        )
    result: dict[str, ExistingIssue] = {}
    for item in data:
        title = str(item.get("title", ""))
        body = str(item.get("body", ""))
        labels = tuple(str(label.get("name", "")) for label in (item.get("labels") or []))
        parent = item.get("parent")
        result[task_id_of(title, body)] = ExistingIssue(
            number=int(item["number"]),
            title=title,
            state=str(item.get("state", "")).upper(),
            labels=labels,
            body=body,
            parent=int(parent["number"]) if isinstance(parent, dict) and parent.get("number") else None,
        )
    return result


def _gh(args: list[str], cfg: GithubConfig) -> tuple[int, str]:
    cmd = ["gh", *args]
    if cfg.repo:
        cmd += ["--repo", cfg.repo]
    return _run(cmd, cwd=cfg.root)


def ensure_labels(graph: dag.Graph, cfg: GithubConfig) -> None:
    """Idempotently provision the labels used (--force creates/updates). Best-effort (does not raise on failure)."""
    for spec in label_specs(graph, cfg.label):
        _gh(["label", "create", spec.name, "--color", spec.color, "--description", spec.description, "--force"], cfg)


#: The gh release that added `--parent` to `gh issue create` and `gh issue edit`.
PARENT_GH = "2.103"


def supports_parent(cfg: GithubConfig) -> bool:
    """Whether this gh can put an issue under another (`gh issue create --parent`)."""
    rc, out = _gh_plain(["issue", "create", "--help"], cfg)
    return rc == 0 and "--parent" in out


def _gh_plain(args: list[str], cfg: GithubConfig) -> tuple[int, str]:
    """gh without `--repo`: for questions about gh itself, which no repository answers."""
    return _run(["gh", *args], cwd=cfg.root)


def _parent_number(action: Action, numbers: dict[str, int]) -> str:
    number = numbers.get(action.parent)
    if number is None:
        raise IssueSyncError(f"{action.task_id}: its issue {action.parent} has no GitHub issue to go under")
    return str(number)


def _apply_one(action: Action, cfg: GithubConfig, numbers: dict[str, int] | None = None) -> None:
    """Carry out one action. `numbers` maps plan ids to issue numbers, and learns each one created."""
    numbers = numbers if numbers is not None else {}
    if action.op == "create":
        args = ["issue", "create", "--title", action.desired.title, "--body", action.desired.body]
        for label in action.desired.labels:
            args += ["--label", label]
        if action.parent:
            args += ["--parent", _parent_number(action, numbers)]
        rc, out = _gh(args, cfg)
        if rc != 0:
            raise IssueSyncError(f"{action.task_id}: issue creation failed:\n{out[-500:]}")
        # gh issue create outputs a URL. To reliably get the number even if warnings are mixed into stdout+stderr,
        # extract /issues/<n> with a regex (splitting on the tail would break with extra output).
        match = re.search(r"/issues/(\d+)", out)
        if match:
            numbers[action.task_id] = int(match.group(1))
        if action.desired.closed:
            if match:
                _gh(["issue", "close", match.group(1)], cfg)
            else:
                # Not fatal: the next sync sees the issue OPEN with desired closed and plans a
                # "close" op (plan_actions), so the mirror converges — just disclose the miss.
                logger.info(
                    f"note: {action.task_id}: could not extract the new issue's number from gh output;"
                    " it stays open until the next sync closes it."
                )
    elif action.op == "update":
        args = ["issue", "edit", str(action.number), "--title", action.desired.title, "--body", action.desired.body]
        if action.add_labels:
            args += ["--add-label", ",".join(action.add_labels)]
        if action.remove_labels:
            args += ["--remove-label", ",".join(action.remove_labels)]
        rc, out = _gh(args, cfg)
        if rc != 0:
            raise IssueSyncError(f"{action.task_id}: issue update failed:\n{out[-500:]}")
    elif action.op == "reparent":
        rc, out = _gh(["issue", "edit", str(action.number), "--parent", _parent_number(action, numbers)], cfg)
        if rc != 0:
            raise IssueSyncError(f"{action.task_id}: could not move it under {action.parent}:\n{out[-500:]}")
    elif action.op == "close":
        _gh(["issue", "close", str(action.number)], cfg)
    elif action.op == "reopen":
        _gh(["issue", "reopen", str(action.number)], cfg)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="one-way-mirror the plan's issues and sub-issues to GitHub Issues")
    parser.add_argument("--dry-run", action="store_true", help="output planned creations without calling gh (offline)")
    parser.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()

    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    cfg = GithubConfig.load(str(repo.config))

    if args.dry_run:
        graph = dag.load(repo)
        issues = _issues(repo)
        print("[dry-run] labels to create/ensure:")
        print(", ".join(spec.name for spec in label_specs(graph, cfg.label)))
        actions = plan_actions(graph.tasks, {}, base_label=cfg.label, close_on_done=cfg.close_on_done, issues=issues)
        print("[dry-run] planned Issues mirror (existing issues not fetched = all assumed create):")
        print(format_plan(actions))
        return 0

    ready, reason = preflight(cfg)
    if not ready:
        print(reason)
        return 0

    try:
        if not supports_parent(cfg):
            raise IssueSyncError(
                f"this gh cannot put an issue under another (`gh issue create --parent`, gh {PARENT_GH} or later). "
                "Each sub-issue is mirrored under its issue or not at all — upgrade gh and run this again."
            )
        graph = dag.load(repo)
        issues = _issues(repo)
        ensure_labels(graph, cfg)  # provision labels before creating issues (create fails if they are absent)
        existing = fetch_existing(cfg)
        actions = plan_actions(
            graph.tasks, existing, base_label=cfg.label, close_on_done=cfg.close_on_done, issues=issues
        )
        numbers = {element_id: ex.number for element_id, ex in existing.items()}
        for action in actions:
            _apply_one(action, cfg, numbers)
    except (OSError, dag.DagError, yaml.YAMLError, IssueSyncError) as exc:
        logger.error(f"Issues mirror failed (does not affect the SSOT): {exc}")
        return 1
    print(f"Issues mirror complete: {len(actions)} operation(s)." if actions else "Issues match plan.yaml (no ops).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
