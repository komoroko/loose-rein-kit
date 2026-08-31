"""Verify scripts/template_lint.py's drift canaries — and run them against the live repo.

`template_lint` lives in `scripts/`, not in the installed `rein` package (it is the template
repository's own maintenance tool, not something a product needs from every release), so it is
loaded from its file path rather than imported as `rein.template_lint`.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from rein import models, store
from tests._support import make_config

_REPO_ROOT = Path(__file__).resolve().parents[1]

_spec = importlib.util.spec_from_file_location("template_lint", _REPO_ROOT / "scripts" / "template_lint.py")
assert _spec is not None and _spec.loader is not None
template_lint = importlib.util.module_from_spec(_spec)
sys.modules["template_lint"] = template_lint
_spec.loader.exec_module(template_lint)


_CONFIG = store.dump_yaml(
    make_config(
        quality_gate=[
            {"name": "test", "kind": "command", "command": ["make", "test"], "executor_profile": "quality"},
            {"name": "review", "kind": "agent", "agent_role": "code_reviewer"},
        ]
    )
).decode()

_AGENTS = (
    "kinds: foundation / parallel / integration. "
    "gates: requirements, design, tasks, build, release. steps: test, review.\n"
)
_TASKS_CMD = (
    "kind: foundation | parallel | integration. "
    "status: todo in-progress blocked needs-revision awaiting-evidence done.\n"
)
_DOD_PROSE = "the pipeline runs test then review.\n"  # every prose copy of the DoD must echo the step names


def _files(**overrides: str) -> dict[str, str]:
    files = {
        template_lint.AGENTS_MD: _AGENTS,
        template_lint.TASKS_CMD: _TASKS_CMD,
        template_lint.BUILD_CMD: _DOD_PROSE,
        "README.md": _DOD_PROSE,
        "README.ja.md": _DOD_PROSE,
        template_lint.CONFIG_PATH: _CONFIG,
    }
    files.update(overrides)
    return files


# --- vocabulary ------------------------------------------------------------------


def test_gate_names_come_from_the_vocabulary_not_a_scraped_file() -> None:
    """Read from a document's front matter these would drift. A constant cannot drift from the
    code that acts on it, which is the whole point of a canary."""
    assert template_lint.gate_names() == sorted(models.GATE_ORDER)


def test_quality_gate_steps_reads_the_dod_names() -> None:
    assert template_lint.quality_gate_steps(_CONFIG) == ["test", "review"]


def test_check_vocabulary_is_green_when_everything_is_echoed() -> None:
    assert template_lint.check_vocabulary(_files()) == []


def test_check_vocabulary_trips_on_a_missing_kind() -> None:
    files = _files(**{template_lint.TASKS_CMD: _TASKS_CMD.replace("integration", "join")})
    failures = template_lint.check_vocabulary(files)
    assert any("tasks.md" in f and "`integration`" in f for f in failures)


def test_check_vocabulary_trips_on_a_missing_quality_gate_step() -> None:
    files = _files(**{template_lint.AGENTS_MD: _AGENTS.replace("review", "critique")})
    failures = template_lint.check_vocabulary(files)
    assert any("AGENTS.md" in f and "`review`" in f for f in failures)


def test_check_vocabulary_trips_on_a_dod_copy_gone_stale() -> None:
    """The README/build.md prose copies of the DoD must echo the step names too."""
    files = _files(**{"README.ja.md": "the pipeline runs test then critique.\n"})
    failures = template_lint.check_vocabulary(files)
    assert any("README.ja.md" in f and "`review`" in f for f in failures)


# --- wrapper parity ----------------------------------------------------------------


def _wrapper_tree(root: Path) -> None:
    """A minimal healthy body+wrapper layout (one command, one agent role)."""
    (root / ".rein" / "prompts" / "commands").mkdir(parents=True)
    (root / ".rein" / "prompts" / "agents").mkdir(parents=True)
    (root / ".claude" / "commands").mkdir(parents=True)
    (root / ".claude" / "agents").mkdir(parents=True)
    (root / ".github" / "prompts").mkdir(parents=True)
    (root / ".github" / "agents").mkdir(parents=True)
    (root / ".rein" / "prompts" / "commands" / "req.md").write_text("# /req\n", encoding="utf-8")
    (root / ".rein" / "prompts" / "agents" / "architect.md").write_text("# Role\n", encoding="utf-8")
    (root / ".claude" / "commands" / "req.md").write_text(
        "---\ndescription: Phase 1.\n---\n@.rein/prompts/commands/req.md\n", encoding="utf-8"
    )
    (root / ".github" / "prompts" / "req.prompt.md").write_text(
        "---\ndescription: Phase 1.\n---\nRead `.rein/prompts/commands/req.md`.\n", encoding="utf-8"
    )
    (root / ".claude" / "agents" / "architect.md").write_text(
        "---\nname: architect\ndescription: Designs.\n---\nRead `.rein/prompts/agents/architect.md`.\n",
        encoding="utf-8",
    )
    (root / ".github" / "agents" / "architect.agent.md").write_text(
        "---\ndescription: Designs.\n---\nRead `.rein/prompts/agents/architect.md`.\n", encoding="utf-8"
    )
    # Codex: a skill is a directory, and a subagent is TOML — the same two facts the parity
    # check has to handle to cover a third host at all.
    (root / ".agents" / "skills" / "req").mkdir(parents=True)
    (root / ".codex" / "agents").mkdir(parents=True)
    (root / ".agents" / "skills" / "req" / "SKILL.md").write_text(
        "---\nname: req\ndescription: Phase 1.\n---\nRead `.rein/prompts/commands/req.md`.\n", encoding="utf-8"
    )
    (root / ".codex" / "agents" / "architect.toml").write_text(
        'name = "architect"\ndescription = "Designs."\n'
        'developer_instructions = "See `.rein/prompts/agents/architect.md`."\n',
        encoding="utf-8",
    )


def test_check_wrapper_parity_green(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    assert template_lint.check_wrapper_parity(tmp_path) == []


def test_check_wrapper_parity_trips_on_missing_wrapper(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".github" / "prompts" / "req.prompt.md").unlink()
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("missing wrapper req.prompt.md" in f for f in failures)


def test_check_wrapper_parity_trips_on_orphan_wrapper_and_stale_reference(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".claude" / "commands" / "extra.md").write_text("---\ndescription: X.\n---\n", encoding="utf-8")
    (tmp_path / ".claude" / "agents" / "architect.md").write_text(
        "---\nname: architect\ndescription: Designs.\n---\nno reference here\n", encoding="utf-8"
    )
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("extra.md: no shared body" in f for f in failures)
    assert any("does not reference .rein/prompts/agents/architect.md" in f for f in failures)


def test_check_wrapper_parity_covers_the_codex_surfaces(tmp_path: Path) -> None:
    """A host left outside this check is exactly how it stays a phase
    behind the other two hosts without anything going red."""
    _wrapper_tree(tmp_path)
    (tmp_path / ".agents" / "skills" / "req" / "SKILL.md").unlink()
    (tmp_path / ".codex" / "agents" / "architect.toml").unlink()
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("missing wrapper req/SKILL.md" in f for f in failures)
    assert any("missing wrapper architect.toml" in f for f in failures)


def test_check_wrapper_parity_reads_a_toml_description_as_the_same_sentence(tmp_path: Path) -> None:
    """A TOML basic string carries quotes and escapes; a description with a quotation mark in it
    must not read as drift against the YAML dialects that write it bare."""
    _wrapper_tree(tmp_path)
    for path, text in (
        (Path(".claude/agents/architect.md"), '---\nname: architect\ndescription: Presents "options".\n---\n'),
        (Path(".github/agents/architect.agent.md"), '---\ndescription: Presents "options".\n---\n'),
        (Path(".codex/agents/architect.toml"), 'description = "Presents \\"options\\"."\n'),
    ):
        body = "Read `.rein/prompts/agents/architect.md`.\n"
        (tmp_path / path).write_text(text + body, encoding="utf-8")
    assert template_lint.check_wrapper_parity(tmp_path) == []


def test_check_wrapper_parity_trips_on_description_drift(tmp_path: Path) -> None:
    _wrapper_tree(tmp_path)
    (tmp_path / ".github" / "prompts" / "req.prompt.md").write_text(
        "---\ndescription: Phase one, reworded.\n---\nRead `.rein/prompts/commands/req.md`.\n", encoding="utf-8"
    )
    failures = template_lint.check_wrapper_parity(tmp_path)
    assert any("descriptions for `req` differ" in f for f in failures)


# --- capability mapping --------------------------------------------------------------

_CLAUDE_MAP = "| `structured-question` | AskUserQuestion |\n| `notify-and-wait` | PushNotification |\n"
_COPILOT_MAP = "| `structured-question` | numbered options in chat |\n| `notify-and-wait` | end the turn |\n"
_CODEX_MAP = "| `structured-question` | numbered options in chat |\n| `notify-and-wait` | end the turn |\n"
_AGENTS_VOCAB = "vocabulary: `structured-question`, `notify-and-wait`.\n"


def _maps(claude: str = _CLAUDE_MAP, copilot: str = _COPILOT_MAP, codex: str = _CODEX_MAP) -> dict[str, str]:
    return {
        template_lint.CLAUDE_MAPPING: claude,
        template_lint.COPILOT_MAPPING: copilot,
        template_lint.CODEX_MAPPING: codex,
    }


def test_check_capability_mapping_green() -> None:
    assert template_lint.check_capability_mapping(_maps(), _AGENTS_VOCAB) == []


def test_check_capability_mapping_trips_on_one_sided_token() -> None:
    failures = template_lint.check_capability_mapping(
        _maps(claude=_CLAUDE_MAP + "| `session-compaction` | /compact |\n"), _AGENTS_VOCAB
    )
    assert any("missing capability `session-compaction`" in f and "instructions" in f for f in failures)


def test_check_capability_mapping_covers_the_codex_mapping() -> None:
    """A token only Codex is missing has to be reported — a newly added host is checked by
    nothing, which is the drift `check_wrapper_parity` was widened to stop one file over."""
    failures = template_lint.check_capability_mapping(
        _maps(
            claude=_CLAUDE_MAP + "| `session-compaction` | /compact |\n",
            copilot=_COPILOT_MAP + "| `session-compaction` | /compact |\n",
        ),
        _AGENTS_VOCAB + " `session-compaction`\n",
    )
    assert [f for f in failures if template_lint.CODEX_MAPPING in f and "session-compaction" in f]


def test_check_capability_mapping_trips_on_undefined_token() -> None:
    failures = template_lint.check_capability_mapping(_maps(), "vocabulary: `notify-and-wait`.\n")
    assert any("`structured-question` is mapped but never defined" in f for f in failures)


def test_check_neutral_vocabulary_trips_on_dialect_leak() -> None:
    texts = {"AGENTS.md": "clean\n", ".rein/prompts/commands/req.md": "ask via AskUserQuestion\n"}
    failures = template_lint.check_neutral_vocabulary(texts)
    assert failures == [
        ".rein/prompts/commands/req.md: Claude-only mechanism `AskUserQuestion` leaked into a neutral file"
    ]


def test_neutral_texts_scans_docs_scaffolds_but_not_records(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("rules\n", encoding="utf-8")
    (tmp_path / ".rein" / "prompts" / "commands").mkdir(parents=True)
    (tmp_path / ".rein" / "prompts" / "commands" / "req.md").write_text("body\n", encoding="utf-8")
    (tmp_path / "docs" / "decisions").mkdir(parents=True)
    (tmp_path / "docs" / "decisions" / "ADR-template.md").write_text("via AskUserQuestion\n", encoding="utf-8")
    (tmp_path / "docs" / "notes").mkdir()
    (tmp_path / "docs" / "notes" / "comparison.md").write_text("Claude Code's AskUserQuestion\n", encoding="utf-8")
    (tmp_path / "docs" / "archive").mkdir()
    (tmp_path / "docs" / "archive" / "old.md").write_text("AskUserQuestion transcript\n", encoding="utf-8")

    texts = template_lint.neutral_texts(tmp_path)
    assert set(texts) == {"AGENTS.md", ".rein/prompts/commands/req.md", "docs/decisions/ADR-template.md"}
    failures = template_lint.check_neutral_vocabulary(texts)
    assert failures == [
        "docs/decisions/ADR-template.md: Claude-only mechanism `AskUserQuestion` leaked into a neutral file"
    ]


# --- rules-module wiring -----------------------------------------------------------


def _rules_tree(root: Path) -> None:
    (root / ".rein" / "prompts" / "rules").mkdir(parents=True)
    (root / ".rein" / "prompts" / "rules" / "gate-workflow.md").write_text("# Rules\n", encoding="utf-8")


_WIRED = {".rein/prompts/commands/req.md": "read `.rein/prompts/rules/gate-workflow.md` first\n"}


def test_check_rules_wiring_green(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    assert template_lint.check_rules_wiring(tmp_path, dict(_WIRED)) == []


def test_check_rules_wiring_trips_on_an_orphan_module(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    # A reference from AGENTS.md alone doesn't count — only a command body loads a module.
    texts = {"AGENTS.md": "see `.rein/prompts/rules/gate-workflow.md`\n"}
    failures = template_lint.check_rules_wiring(tmp_path, texts)
    assert failures == [".rein/prompts/rules/gate-workflow.md: not read by any command body (orphan module)"]


def test_check_rules_wiring_trips_on_a_stale_reference(tmp_path: Path) -> None:
    _rules_tree(tmp_path)
    texts = dict(_WIRED)
    texts["docs/10-requirements.md"] = "spec: `.rein/prompts/rules/renamed.md`\n"
    failures = template_lint.check_rules_wiring(tmp_path, texts)
    assert failures == ["docs/10-requirements.md: references .rein/prompts/rules/renamed.md which does not exist"]


def test_check_rules_wiring_green_without_a_rules_dir(tmp_path: Path) -> None:
    """No rules/ directory and no references (a product repo that trimmed the modules) is healthy."""
    assert template_lint.check_rules_wiring(tmp_path, {"AGENTS.md": "rules\n"}) == []


# --- README parity ---------------------------------------------------------------

_EN = "## A\n## B\nRun `make init` then `make -f rein.mk rein-upgrade`.\nSee src/rein/dag.py.\n"
_JA = "## あ\n## い\n`make init` の後 `make -f rein.mk rein-upgrade`。\nsrc/rein/dag.py を参照。\n"


def test_check_readme_parity_is_green_for_matching_structure() -> None:
    assert template_lint.check_readme_parity(_EN, _JA) == []


def test_check_readme_parity_trips_on_section_count() -> None:
    assert "sections" in template_lint.check_readme_parity(_EN, _JA + "## う\n")[0]


def test_check_readme_parity_trips_on_a_one_sided_make_target() -> None:
    failures = template_lint.check_readme_parity(_EN + "Also `make feedback`.\n", _JA)
    assert failures == ["README.ja.md: missing make-target mention `feedback` (present in README.md)"]


def test_check_readme_parity_trips_on_a_one_sided_script() -> None:
    failures = template_lint.check_readme_parity(_EN, _JA + "src/rein/adopt.py も。\n")
    assert failures == ["README.md: missing script mention `adopt.py` (present in README.ja.md)"]


def test_check_readme_parity_ignores_prose_make_mentions() -> None:
    # "make tasks visible" is prose, not a target — only backticked mentions count.
    assert template_lint.check_readme_parity(_EN + "We make tasks visible.\n", _JA) == []


# --- version ↔ changelog -----------------------------------------------------------


def _config_with_guard(paths: dict[str, str]) -> str:
    entries = [{"path": path, "requires_gate": gate} for path, gate in paths.items()]
    return store.dump_yaml(make_config(guard_paths=entries)).decode()


def test_check_guard_defaults_green_and_drifts() -> None:
    """The block exists in two hand-maintained places on purpose — the code default applies
    when the key is omitted, the shipped config spells it out for the human editing it — so a
    rule added to only one of them is the drift this canary exists to catch."""
    from rein import gate_guard

    green = _config_with_guard(dict(gate_guard.DEFAULT_GUARD_PATHS))
    assert template_lint.check_guard_defaults(green) == []

    missing = template_lint.check_guard_defaults(_config_with_guard({"src/": "tasks"}))
    assert any("guard.paths is missing" in f for f in missing)

    extra = template_lint.check_guard_defaults(
        _config_with_guard({**gate_guard.DEFAULT_GUARD_PATHS, "extra/": "tasks"})
    )
    assert any("DEFAULT_GUARD_PATHS is missing `extra/`" in f for f in extra)

    mismatch = template_lint.check_guard_defaults(
        _config_with_guard({**gate_guard.DEFAULT_GUARD_PATHS, "src/": "design"})
    )
    assert any("`src/`" in f and "design" in f for f in mismatch)

    assert "guard.paths block is missing" in template_lint.check_guard_defaults(_config_with_guard({}))[0]


def _gitignore(*entries: str) -> str:
    """A .gitignore with the Loose Rein section between two sections that are none of its business."""
    body = "\n".join(entries)
    return (
        f"# ---- mkdocs ----\nsite/\n\n{template_lint.IGNORE_SECTION_HEADER}\n"
        f"# a comment\n{body}\n\n# ---- OS ----\n.DS_Store\n"
    )


def test_runtime_artifacts_are_derived_from_the_code_that_writes_them() -> None:
    """Not a hand-kept list: the worktree dir comes from config, the PR draft from pr_draft."""
    from rein import pr_draft

    artifacts = template_lint.runtime_artifacts(_CONFIG)
    assert pr_draft.OUT_PATH in artifacts
    assert models.Config.parse(_CONFIG).worktree_dir.rstrip("/") + "/" in artifacts


def test_check_runtime_artifacts_is_green_when_the_section_matches() -> None:
    text = _gitignore(*sorted(template_lint.runtime_artifacts(_CONFIG)))
    assert template_lint.check_runtime_artifacts(text, _CONFIG) == []


def test_check_runtime_artifacts_trips_on_an_entry_nothing_writes() -> None:
    """The drift that went unnoticed for two releases: .gitignore named three `build-loop.*`
    files after the loop's locks and journal moved to $XDG_RUNTIME_DIR, outside the repo."""
    text = _gitignore(*sorted(template_lint.runtime_artifacts(_CONFIG)), ".rein/build-loop.log")
    problems = template_lint.check_runtime_artifacts(text, _CONFIG)
    assert any("build-loop.log" in p and "nothing writes" in p for p in problems)


def test_check_runtime_artifacts_trips_on_a_generated_file_left_untracked_by_the_section() -> None:
    from rein import pr_draft

    text = _gitignore(models.Config.parse(_CONFIG).worktree_dir.rstrip("/") + "/")
    problems = template_lint.check_runtime_artifacts(text, _CONFIG)
    assert any(pr_draft.OUT_PATH in p and "the tool writes it" in p for p in problems)


def test_check_runtime_artifacts_reports_a_missing_section() -> None:
    assert "section is missing" in template_lint.check_runtime_artifacts("# ---- Python ----\n.venv/\n", _CONFIG)[0]


def test_the_section_reader_stops_at_the_next_header() -> None:
    """Otherwise the canary would claim authority over .venv/ and every editor file below it."""
    text = _gitignore(".worktrees/", ".rein/pr-draft.md")
    assert template_lint._ignore_section(text) == [".worktrees/", ".rein/pr-draft.md"]


def test_check_tracked_artifacts_flags_a_committed_generated_file(tmp_path: Path) -> None:
    """.gitignore does not apply to a path git already tracks, so the ignore rule above a
    committed artifact reads as protection that is not there."""
    from rein import common, pr_draft

    root = tmp_path / "repo"
    (root / ".rein").mkdir(parents=True)
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        assert common.run(["git", "-C", str(root), *args])[0] == 0
    (root / pr_draft.OUT_PATH).write_text("draft\n", encoding="utf-8")
    assert template_lint.check_tracked_artifacts(root, _CONFIG) == []  # untracked: nothing to say

    assert common.run(["git", "-C", str(root), "add", "-f", pr_draft.OUT_PATH])[0] == 0
    problems = template_lint.check_tracked_artifacts(root, _CONFIG)
    assert any(pr_draft.OUT_PATH in p and "tracked by git" in p for p in problems)


def test_check_tracked_artifacts_is_silent_outside_a_git_checkout(tmp_path: Path) -> None:
    assert template_lint.check_tracked_artifacts(tmp_path, _CONFIG) == []


def test_check_ignored_payload_flags_a_shipped_file_git_cannot_see(tmp_path: Path) -> None:
    """The bug this exists for: an unanchored `build/` in .gitignore matched the Codex skill
    `.agents/skills/build/SKILL.md`, so seven of the eight phase entry points reached a clone
    while every filesystem-level parity check stayed green."""
    from rein import common

    root = tmp_path / "repo"
    skill = root / ".agents/skills/build"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: build\n---\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        assert common.run(["git", "-C", str(root), *args])[0] == 0
    assert template_lint.check_ignored_payload(root) == []  # nothing ignored yet

    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    problems = template_lint.check_ignored_payload(root)
    assert any(".agents/skills/build/SKILL.md" in p and "reaches no clone" in p for p in problems)

    # Anchoring it to the root is the fix, and the check has to agree that it is one.
    (root / ".gitignore").write_text("/build/\n", encoding="utf-8")
    assert template_lint.check_ignored_payload(root) == []


def test_check_ignored_payload_accepts_a_tracked_file_whatever_the_patterns_say(tmp_path: Path) -> None:
    """`check-ignore` consults the index, and a tracked file does reach a clone — reporting it
    would send someone chasing a pattern that is not costing them anything."""
    from rein import common

    root = tmp_path / "repo"
    skill = root / ".agents/skills/build"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: build\n---\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n", encoding="utf-8")
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        assert common.run(["git", "-C", str(root), *args])[0] == 0
    assert common.run(["git", "-C", str(root), "add", "-f", ".agents/skills/build/SKILL.md"])[0] == 0
    assert template_lint.check_ignored_payload(root) == []


def test_check_ignored_payload_is_silent_outside_a_git_checkout(tmp_path: Path) -> None:
    assert template_lint.check_ignored_payload(tmp_path) == []


def test_check_oci_profile_mentions_catches_a_profile_name_used_as_a_containerfile(tmp_path: Path) -> None:
    """The exact bug: `quality` is an executor profile, `python` is the Containerfile it builds
    from, and seven sites told people to run `--profile quality`, which exits 1."""
    doc = tmp_path / "README.md"
    doc.write_text("Run `rein oci build --profile quality` first.\n", encoding="utf-8")
    problems = template_lint.check_oci_profile_mentions(tmp_path)
    assert any("--profile quality" in p and "no packaged Containerfile" in p for p in problems)

    doc.write_text("Run `rein oci build --profile python` first.\n", encoding="utf-8")
    assert template_lint.check_oci_profile_mentions(tmp_path) == []


def test_check_oci_profile_mentions_reads_the_containerfile_headers_too(tmp_path: Path) -> None:
    """Both copies of the python Containerfile carried the bad name in their own header, and
    _DATA_PARITY kept them identically wrong rather than catching it."""
    target = tmp_path / ".rein/oci/python"
    target.mkdir(parents=True)
    (target / "Containerfile").write_text("# Built with `rein oci build --profile quality`.\n", encoding="utf-8")
    assert any("--profile quality" in p for p in template_lint.check_oci_profile_mentions(tmp_path))


def test_check_scaffold_config_parity_allows_the_identity_and_nothing_else(tmp_path: Path) -> None:
    """The two config.yaml copies are not byte-identical, so _DATA_PARITY cannot hold them —
    which is how the same wrong instruction came to sit in both."""
    live = tmp_path / template_lint.CONFIG_PATH
    scaffold = tmp_path / template_lint._SCAFFOLD_CONFIG
    for path in (live, scaffold):
        path.parent.mkdir(parents=True, exist_ok=True)
    body = "project:\n  name: {}\n  work_branch: build/{}\nexecution:\n  max_parallel: 3\n"
    live.write_text(body.format("loose-rein-kit", "loose-rein-kit"), encoding="utf-8")
    scaffold.write_text(body.format("product", "product"), encoding="utf-8")
    assert template_lint.check_scaffold_config_parity(tmp_path) == []

    scaffold.write_text(body.format("product", "product").replace("max_parallel: 3", "max_parallel: 9"), "utf-8")
    problems = template_lint.check_scaffold_config_parity(tmp_path)
    assert any("outside the project identity" in p for p in problems)


def test_check_ssot_validates_catches_a_schema_change_that_missed_the_live_documents(tmp_path: Path) -> None:
    """`make check` never ran `rein doctor`, so a schema change could ship a template whose very
    first `rein doctor` fails — as collapsing `machine.coverage` to one manifest nearly did."""
    review = tmp_path / ".rein" / "review.yaml"
    review.parent.mkdir(parents=True, exist_ok=True)
    review.write_text("machine:\n  status: not_generated\nhuman:\n  status: not_started\n", encoding="utf-8")
    assert template_lint.check_ssot_validates(tmp_path) == []

    review.write_text(
        "machine:\n  status: not_generated\n  coverage: []\nhuman:\n  status: not_started\n", encoding="utf-8"
    )
    problems = template_lint.check_ssot_validates(tmp_path)
    assert any("review.yaml" in p and "coverage" in p for p in problems)


def test_check_version_lock_green_and_drifts() -> None:
    """`uv sync --frozen` refuses a lock that disagrees with pyproject, so this drift kills CI at
    dependency install — while `uv run --frozen` keeps the whole local gate green."""
    lock = '[[package]]\nname = "loose-rein-kit"\nversion = "0.1.2"\nsource = { editable = "." }\n'
    assert template_lint.check_version_lock("0.1.2", lock) == []
    assert "uv.lock says 0.1.2" in template_lint.check_version_lock("0.1.3", lock)[0]
    assert "no `loose-rein-kit` package entry" in template_lint.check_version_lock("0.1.2", "[[package]]\n")[0]
    # An absent version is check_version_changelog's finding to report, not this one's.
    assert template_lint.check_version_lock("", lock) == []


def test_check_version_changelog_green_and_drifts() -> None:
    log = "# Changelog\n\n## [0.2.0] - 2026-07-08\n\n## [0.1.0] - 2026-07-01\n"
    assert template_lint.check_version_changelog("0.2.0", log) == []
    assert "0.1.0" in template_lint.check_version_changelog("0.1.0", log)[0]
    assert "missing or empty" in template_lint.check_version_changelog("", log)[0]
    assert "no `## [x.y.z]`" in template_lint.check_version_changelog("0.2.0", "# Changelog\n")[0]


# --- against the live repo (the actual CI gate) ------------------------------------


def _live_template_mode() -> bool:
    config = yaml.safe_load((_REPO_ROOT / template_lint.CONFIG_PATH).read_text(encoding="utf-8")) or {}
    return bool((config.get("guard") or {}).get("template_mode") is True)


@pytest.mark.skipif(not _live_template_mode(), reason="not the template repo (gates.template_mode is false)")
def test_live_repo_has_no_drift() -> None:
    files = {
        path: (_REPO_ROOT / path).read_text(encoding="utf-8")
        for path in (
            template_lint.AGENTS_MD,
            template_lint.TASKS_CMD,
            template_lint.BUILD_CMD,
            template_lint.CONFIG_PATH,
            "README.md",
            "README.ja.md",
        )
    }
    assert template_lint.check_vocabulary(files) == []


def test_main_skips_in_a_product_repo(make_repo: Callable[..., Path], capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(config=make_config(template_mode=False))
    assert template_lint.main([]) == 0
    assert "skipped" in capsys.readouterr().out


def test_main_reports_an_invalid_config_rather_than_skipping(
    make_repo: Callable[..., Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """A config it cannot parse is not "not the template repo" — treating it as one would turn
    every drift canary off silently."""
    root = make_repo()
    (root / ".rein" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    assert template_lint.main([]) == 1
    assert "is not valid" in capsys.readouterr().err


def test_check_banned_absence_flags_gate_weakening_terms() -> None:
    clean = {"AGENTS.md": "gate approvals live in `.rein/state.yaml`"}
    assert template_lint.check_banned_absence(clean) == []
    dirty = {"rules/gate.md": "pass `approve --force` to override, or set `enforce_hook`"}
    problems = template_lint.check_banned_absence(dirty)
    assert any("approve --force" in p for p in problems)
    assert any("enforce_hook" in p for p in problems)


def test_the_packaged_scaffold_is_scanned(tmp_path: Path) -> None:
    """The scaffold is what a *product* repo is seeded from — the one place stale vocabulary
    does the most damage, and the one place the canary was not looking."""
    from pathlib import Path as _Path

    root = _Path(__file__).resolve().parents[1]
    scanned = template_lint.neutral_texts(root)
    assert any(p.startswith("src/rein/data/scaffold/") for p in scanned)
    assert any(p.startswith("src/rein/data/integrations/") for p in scanned)


def test_no_file_but_upstream_spells_an_upgrade_command() -> None:
    """`uv tool upgrade` is a no-op for a tag-pinned install, and five places used to print it."""
    assert template_lint.check_upgrade_command(_REPO_ROOT) == []


def test_the_distribution_name_matches_pyproject() -> None:
    assert template_lint.check_distribution_name(_REPO_ROOT) == []


def test_the_upgrade_canary_catches_a_reintroduced_literal(tmp_path: Path) -> None:
    from unittest.mock import patch

    texts = {"docs/x.md": f"run `{template_lint._DEAD_UPGRADE_COMMAND}`"}
    with patch.object(template_lint, "_tracked_texts", lambda _root: texts):
        failures = template_lint.check_upgrade_command(tmp_path)
    assert len(failures) == 1 and "docs/x.md" in failures[0]


def test_the_upgrade_canary_refuses_to_pass_when_it_could_not_look(tmp_path: Path) -> None:
    """`Repo._git` returns "" for every failure, and an empty listing would be a silent pass."""
    template_lint._tracked_texts.cache_clear()
    with pytest.raises(OSError, match="could not list git-tracked files"):
        template_lint._tracked_texts(tmp_path)  # not a git checkout
