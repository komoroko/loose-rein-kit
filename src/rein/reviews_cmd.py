"""`rein reviews` — which reviews run, and the one terminal path that changes them.

  rein reviews show [--json]
  rein reviews apply <file> --reason "why"

`.rein/reviews.yaml` is outside the mandate's freeze (`models.Reviews`): which tools raise the
quality of the work is inside what the mandate delegates, so changing one rewinds nothing. What
the freeze used to guarantee is kept another way. `rein guard` refuses every edit of the file (rule
1: it is machine-written), and the two writers are both a person's: this verb, which insists on a
terminal and a `[y/N]` exactly as `rein approve` does, and the dashboard's write session, minted
only by the launch link `rein ui` prints. Each change lands in the audit chain with its reason
(`reviews_changed`), and acceptance lists which reviews each task was actually read for.

`apply` takes a whole document rather than one knob at a time: the dashboard edits the same shape,
so the terminal and the page cannot disagree about what a change is.
"""

from __future__ import annotations

import argparse
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rein import build_loop, common, event_chain, models, strict_yaml
from rein import repo as repo_mod
from rein import store as store_mod

logger = logging.getLogger(__name__)

_REASON_MAX = 500


class ReviewsError(Exception):
    """A refused change, with a message that already names the next step."""


def describe(before: models.Reviews | None, after: models.Reviews) -> list[str]:
    """What a change does, one line per thing a reader would call a change."""
    lines: list[str] = []
    for stage in models.ADVERSARIAL_STAGES:
        was = before.adversarial(stage) if before is not None else None
        now = after.adversarial(stage)
        if was != now:
            lines.append(f"adversarial review at {stage}: {'on' if now else 'OFF'}")
    old_steps = {step.name: step for step in before.steps} if before is not None else {}
    new_steps = {step.name: step for step in after.steps}
    for name in sorted(old_steps.keys() - new_steps.keys()):
        lines.append(f"step {name}: removed (was {', '.join(old_steps[name].reviews)})")
    for name in sorted(new_steps.keys() - old_steps.keys()):
        lines.append(f"step {name}: added, reads for {', '.join(new_steps[name].reviews)}")
    for name in sorted(old_steps.keys() & new_steps.keys()):
        old, new = old_steps[name], new_steps[name]
        if dropped := [r for r in old.reviews if r not in new.reviews]:
            lines.append(f"step {name}: no longer reads for {', '.join(dropped)}")
        if added := [r for r in new.reviews if r not in old.reviews]:
            lines.append(f"step {name}: now reads for {', '.join(added)}")
        for key, was_value, now_value in (
            ("retries", old.retries, new.retries),
            ("stage", old.stage, new.stage),
            ("paths", list(old.paths), list(new.paths)),
        ):
            if was_value != now_value:
                lines.append(f"step {name}: {key} {was_value} → {now_value}")
    old_custom = {str(c.get("name")): str(c.get("prompt")) for c in before.custom} if before is not None else {}
    new_custom = {str(c.get("name")): str(c.get("prompt")) for c in after.custom}
    for name in sorted(old_custom.keys() - new_custom.keys()):
        lines.append(f"custom review {name}: removed")
    for name in sorted(new_custom.keys() - old_custom.keys()):
        lines.append(f"custom review {name}: added, asks {new_custom[name]}")
    for name in sorted(old_custom.keys() & new_custom.keys()):
        if old_custom[name] != new_custom[name]:
            lines.append(f"custom review {name}: asks {new_custom[name]} (was {old_custom[name]})")
    return lines


def prepare(
    repo: repo_mod.Repo, document: Mapping[str, Any]
) -> tuple[models.Reviews | None, models.Reviews, list[str]]:
    """Validate `document` as the next reviews.yaml: `(current, proposed, what changes)`.

    Everything a build would refuse is refused here, before anything is written — a custom review
    whose question file is missing included (`build_loop.review_questions`).
    """
    body = {k: v for k, v in document.items() if k != "updated_at"}
    errors = models.schema_errors(body, "reviews") or models.Reviews(body).problems()
    if errors:
        raise ReviewsError("the reviews document is not valid:\n  " + "\n  ".join(errors))
    proposed = models.Reviews(body)
    try:
        build_loop.review_questions(repo, proposed)
    except ValueError as exc:
        raise ReviewsError(str(exc)) from None
    current = store_mod.Store(repo).read_reviews()
    return current, proposed, describe(current, proposed)


def apply(repo: repo_mod.Repo, document: Mapping[str, Any], reason: str, *, actor: str) -> list[str]:
    """Write `document` as reviews.yaml and record the change. Returns what changed ([] = nothing).

    The caller is the one who established that a person is doing this — a terminal confirmation,
    or the dashboard's write session — and says which in `actor`.
    """
    if not reason.strip():
        raise ReviewsError("a change to which reviews run needs a reason — it is recorded, and acceptance reads it")
    store = store_mod.Store(repo)
    _, proposed, changes = prepare(repo, document)
    if not changes:
        return []
    state = store.read_state()
    if state is None or not state.cycle_id:
        raise ReviewsError("no cycle to record the change under — run `rein init` first")
    seen = store.document_digest("reviews")
    raw = {**dict(proposed.raw), "updated_at": event_chain.now_iso()}
    with store.transaction() as tx:
        tx.write("reviews", raw, expect_digest=seen or None)
        tx.append(
            "reviews_changed",
            cycle_id=state.cycle_id,
            actor=actor,
            detail={"reason": reason[:_REASON_MAX], "changes": changes},
        )
    return changes


def render(reviews: models.Reviews) -> str:
    lines = ["adversarial review before the mandate:"]
    lines += [f"  {stage}: {'on' if reviews.adversarial(stage) else 'OFF'}" for stage in models.ADVERSARIAL_STAGES]
    lines.append("reviewer steps:")
    lines += [
        f"  {step.name} ({step.stage}, retries {step.retries}): {', '.join(step.reviews)}" for step in reviews.steps
    ] or ["  (none — no reviewer reads the code before acceptance)"]
    if reviews.custom:
        lines.append("custom reviews:")
        lines += [f"  {c.get('name')}: {c.get('prompt')}" for c in reviews.custom]
    lines.append("acceptance: actual extraction, comparison, security review (not configurable)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rein reviews", description="which reviews run, and changing them")
    sub = parser.add_subparsers(dest="action", required=True)
    show = sub.add_parser("show", help="print which reviews run")
    show.add_argument("--json", action="store_true", help="the document itself, for a script or a prompt")
    show.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    change = sub.add_parser("apply", help="replace reviews.yaml with <file>, confirmed at this terminal")
    change.add_argument("file", help="the reviews document to apply (YAML, the shape `show --json` prints)")
    change.add_argument("--reason", required=True, help="why — recorded in the audit chain, shown at acceptance")
    change.add_argument("--repo", default=None, help="repository root (default: discovered from cwd)")
    args = parser.parse_args(argv)
    common.configure_logging()
    try:
        repo = repo_mod.get(args.repo)
    except repo_mod.RepoNotFoundError as exc:
        logger.error(str(exc))
        return 1
    store = store_mod.Store(repo)
    try:
        if args.action == "show":
            reviews = store.read_reviews()
            if reviews is None:
                logger.error(f"no {repo.reviews} — `rein sync` writes the packaged one")
                return 1
            print(json.dumps(dict(reviews.raw), ensure_ascii=False, indent=2) if args.json else render(reviews))
            return 0
        document = strict_yaml.load_mapping(Path(args.file).read_text(encoding="utf-8"), what=args.file)
        if not common.stdin_is_terminal():
            logger.error(
                "changing which reviews run needs a confirmation typed at a terminal, and stdin is not one. "
                "Run this in your shell — there is deliberately no flag that skips it."
            )
            return 2
        _, _, changes = prepare(repo, document)
        if not changes:
            print("nothing changes: the document says what reviews.yaml already says")
            return 0
        print("This changes which reviews run:\n" + "\n".join(f"  {line}" for line in changes))
        if not common.ask_yes_no("Apply it?"):
            print("nothing was changed")
            return 1
        apply(repo, document, args.reason, actor="local-confirmation")
        print("applied — the next reading of each task uses it; tasks already read keep what they were read for")
        return 0
    except (ReviewsError, models.DocumentError, strict_yaml.StrictParseError, store_mod.StoreError, OSError) as exc:
        logger.error(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
