"""review_api: gate → fixed deliverable set, split-out self-assessment, and the gate-④ diff.

The reach-safety class matters most: the module decides *server-side* which files the review pane
may read, so these tests pin the template exclusion, the containment check on symlinks, the size
caps, and that agent-written markup never reaches the payload as live HTML.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from rein import review_api
from tests._support import make_state

MakeRepo = Callable[..., Path]


def _review(root: Path, gate: str) -> dict[str, Any]:
    """collect_review with assertion-friendly typing (the payload is JSON-shaped by contract)."""
    return review_api.collect_review(root, gate)


REQ_DOC = (
    "# Requirements\n\n## Summary\nvalue\n\n### R-1: thing\n- [ ] criterion\n\n"
    "## Self-assessment (assumptions, confidence)\n- **Confidence**: low (unverified integration)\n"
    "- **Assumptions made**: none\n"
)


def _write(root: Path, rel: str, text: str) -> Path:
    dest = root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding="utf-8")
    return dest


def _git(root: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


class TestGateMapping:
    def test_unknown_gate_raises(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        with pytest.raises(review_api.ReviewError):
            _review(root, "nope")

    def test_requirements_gate_deliverable_and_context(self, make_repo: MakeRepo) -> None:
        root = make_repo(
            state=make_state(phase="requirements", gates={g: "pending" for g in ("requirements", "design", "tasks")})
        )
        _write(root, "docs/10-requirements.md", REQ_DOC)
        _write(root, "docs/00-product-brief.md", "# Brief\ngoal")
        out = _review(root, "requirements")
        assert out["is_awaiting"] is True and out["awaiting"] == "requirements"
        (main,) = out["deliverables"]
        assert main["exists"] is True
        assert "<h2>Summary</h2>" in main["html"]
        assert main["self_assessment"]["confidence"] == "low"
        assert "Confidence" not in main["html"]  # the section is split out, not duplicated
        (ctx,) = out["context"]
        assert ctx["label"] == "docs/00-product-brief.md" and "<h1>Brief</h1>" in ctx["html"]

    def test_unfilled_confidence_placeholder_reads_unset(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "## Self-assessment\n- **Confidence**: high / medium / low\n")
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] is None

    def test_prose_mentioning_a_level_does_not_become_the_confidence(self, make_repo: MakeRepo) -> None:
        # The badge must never look better than the document: only the *labelled* Confidence line
        # counts, or an assumption written as "we have high confidence …" would badge a low
        # self-assessment as high.
        root = make_repo()
        _write(
            root,
            "docs/10-requirements.md",
            "## Self-assessment\n"
            "- **Assumptions made**: we have high confidence the runner exists\n"
            "- **Confidence**: low (integration unverified)\n",
        )
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] == "low"

    def test_per_area_confidence_reports_the_weakest_area(self, make_repo: MakeRepo) -> None:
        # AGENTS.md asks for confidence by area, so several levels on one line is the filled-in
        # form, not the placeholder — and the low spot is the part the human must not miss.
        root = make_repo()
        _write(
            root,
            "docs/10-requirements.md",
            "## Self-assessment\n- **Confidence**: high (API surface), low (integration with CI)\n",
        )
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["self_assessment"]["confidence"] == "low"

    def test_per_area_placeholder_prose_still_reads_unset(self, make_repo: MakeRepo) -> None:
        # docs/20-design.md's scaffold line — "per area high / medium / low (e.g. …)".
        root = make_repo()
        _write(
            root,
            "docs/20-design.md",
            "## Self-assessment\n- **Confidence**: per area high / medium / low (e.g. architecture=high)\n",
        )
        main = _review(root, "design")["deliverables"][0]
        assert main["self_assessment"]["confidence"] is None

    def test_design_glob_excludes_template_and_sorts(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        for name in ("ADR-002.md", "ADR-001.md", "ADR-template.md"):
            _write(root, f"docs/decisions/{name}", f"# {name}")
        labels = [d["label"] for d in _review(root, "design")["deliverables"]]
        assert labels == ["docs/20-design.md", "docs/decisions/ADR-001.md", "docs/decisions/ADR-002.md"]

    def test_tasks_gate_renders_tickets_and_verbatim_yaml(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, ".rein/plan.yaml", "claims: []  # <script>alert(1)</script>\n")
        _write(root, "docs/tasks/T-001.md", "# T-001\n\n## Self-assessment\n- **Confidence**: medium\n")
        _write(root, "docs/tasks/T-template.md", "# template")
        out = _review(root, "tasks")
        labels = [d["label"] for d in out["deliverables"]]
        assert labels == ["docs/tasks/T-001.md", ".rein/plan.yaml"]
        yaml_entry = out["deliverables"][1]
        assert yaml_entry["kind"] == "code"
        assert yaml_entry["html"].startswith("<pre><code>")
        assert "<script>" not in yaml_entry["html"]

    def test_missing_deliverable_is_reported_absent(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["exists"] is False and main["html"] == ""

    def test_release_gate_counts_open_escalations(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        from rein import event_chain
        from tests._support import chain

        event_chain.append_lines(root / ".rein" / "events.ndjson", chain("task_completed", "task_failed"))
        # Only the event awaiting a human decision counts; a completed task does not.
        assert _review(root, "release")["open_escalations"] == 1


class TestReachSafety:
    def test_symlink_escaping_the_repo_reads_absent(
        self, make_repo: MakeRepo, tmp_path_factory: pytest.TempPathFactory
    ) -> None:
        root = make_repo()
        outside = tmp_path_factory.mktemp("outside") / "secret.md"
        outside.write_text("# secret", encoding="utf-8")
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "10-requirements.md").symlink_to(outside)
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["exists"] is False and "secret" not in main["html"]

    def test_oversize_deliverable_is_truncated(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "x" * (review_api._MAX_DELIVERABLE + 100))
        (main,) = _review(root, "requirements")["deliverables"]
        assert main["truncated"] is True

    def test_agent_markup_never_survives_rendering(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, "docs/10-requirements.md", "# R\n<script>fetch('/api/gate/approve')</script>")
        (main,) = _review(root, "requirements")["deliverables"]
        assert "<script" not in main["html"]


class TestBuildGateDiff:
    def test_non_git_repo_degrades_to_error(self, make_repo: MakeRepo) -> None:
        out = _review(make_repo(), "build")
        assert "error" in out["diff"]
        assert out["review_meta"]["fresh"] is False

    def test_branch_diff_against_merge_base(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "base\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "base")
        _git(root, "checkout", "-qb", "build/demo")
        _write(root, "a.txt", "base\nnew line\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "T-001: change")
        out = _review(root, "build")
        diff = out["diff"]
        assert diff["base_ref"] == "main" and diff["base"] != diff["head"]
        assert "+new line" in diff["patch"]
        assert ["M", "a.txt"] in diff["name_status"]
        assert diff["truncated"] is False

    def test_head_at_base_falls_back_to_log(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "base\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "only commit")
        diff = _review(root, "build")["diff"]
        assert "patch" not in diff
        assert diff["log"] and "only commit" in diff["log"][0]

    def test_machine_review_freshness(self, make_repo: MakeRepo) -> None:
        # There is no security-review.md: freshness is the review.yaml machine binding's
        # subject_head_sha against the current HEAD (a later commit leaves it stale, E2E-08).
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "x\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "c")
        head = _git(root, "rev-parse", "HEAD")
        _write(root, ".rein/review.yaml", f"machine:\n  binding:\n    subject_head_sha: {head}\n")
        assert _review(root, "build")["review_meta"]["fresh"] is True
        _write(root, ".rein/review.yaml", "machine:\n  binding:\n    subject_head_sha: '0000000'\n")
        meta = _review(root, "build")["review_meta"]
        assert meta["fresh"] is False and meta["reviewed_head"] == "0000000"


class TestScopeStage:
    """The scope stage says what this review speaks for — and what it does not.

    It comes before the unprimed challenge and is deliberately not a priming stage: a commit range,
    a file count and a coverage gap reveal no expected answer, and withholding them would ask a
    reviewer to answer the first question without knowing what their approval will cover.
    """

    def _generated(self, root: Path, *, extra_coverage: str = "", head: str = "e" * 40) -> None:
        _write(
            root,
            ".rein/review.yaml",
            "machine:\n"
            "  status: generated\n"
            "  effective_risk: high\n"
            "  binding:\n"
            "    change_digest: sha256:" + "a" * 64 + "\n"
            "    plan_digest: sha256:" + "b" * 64 + "\n"
            "    toolchain_digest: sha256:" + "c" * 64 + "\n"
            "    trusted_base_sha: " + "f" * 40 + "\n"
            "    subject_head_sha: " + head + "\n"
            "  coverage:\n"
            "    - diff_digest: sha256:" + "d" * 64 + "\n"
            "      analyzed_files: 11\n"
            "      analyzed_hunks: 87\n"
            "      analyzed_bytes: 421888\n"
            "      truncated: false\n"
            "      coverage_status: sufficient\n" + extra_coverage + "human:\n  status: not_started\n",
        )

    def test_the_scope_stage_comes_first_and_reveals_no_expected_answer(self, make_repo: MakeRepo) -> None:
        from rein import models

        assert models.REVIEW_STAGE_ORDER[0] == "scope"

        root = make_repo()
        self._generated(root)
        session_stages = review_api.review_session(root)["stages"]
        assert isinstance(session_stages, list)
        stages = {s["name"]: s for s in session_stages}
        assert "scope" in stages
        # A reading stage records nothing, so it claims neither "done" nor "skipped".
        assert stages["scope"]["settled"] is None

    def test_the_scope_names_the_range_the_size_and_the_questions_coming(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        self._generated(root)
        scope = review_api.stage_data(root, "scope")["scope"]
        assert isinstance(scope, dict)
        assert scope["base"] == "f" * 40 and scope["head"] == "e" * 40
        assert scope["effective_risk"] == "high"
        coverage = scope["coverage"]
        assert coverage["analyzed_files"] == 11 and coverage["analyzed_bytes"] == 421888
        assert scope["challenges_asked"] == 0  # no challenges recorded, so none will be asked
        names = {row["name"] for row in scope["budget"]}
        assert "max_diff_bytes_per_partition" in names

    def test_what_the_review_could_not_read_is_named_by_path(self, make_repo: MakeRepo) -> None:
        """A count cannot be acted on; "ui.min.js was never parsed" can."""
        root = make_repo()
        self._generated(
            root,
            extra_coverage="      unsupported_files:\n        - path: web/ui.min.js\n          reason: generated\n",
        )
        coverage = review_api.stage_data(root, "scope")["scope"]["coverage"]  # type: ignore[index]
        assert coverage["unsupported_files"] == [{"path": "web/ui.min.js", "reason": "generated", "detail": ""}]

    def test_the_scope_says_up_front_when_the_review_no_longer_speaks_for_head(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _git(root, "init", "-q", "-b", "main")
        _write(root, "a.txt", "x\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "c")
        head = _git(root, "rev-parse", "HEAD")

        self._generated(root, head=head)
        assert review_api.stage_data(root, "scope")["scope"]["fresh"] is True  # type: ignore[index]

        _write(root, "b.txt", "y\n")
        _git(root, "add", ".")
        _git(root, "commit", "-qm", "later")
        scope = review_api.stage_data(root, "scope")["scope"]
        assert isinstance(scope, dict) and scope["fresh"] is False
        assert scope["repo_head"] != scope["head"]

    def test_an_ungenerated_review_has_no_scope_to_state(self, make_repo: MakeRepo) -> None:
        root = make_repo()
        _write(root, ".rein/review.yaml", "machine:\n  status: not_generated\nhuman:\n  status: not_started\n")
        assert review_api.stage_data(root, "scope")["generated"] is False
