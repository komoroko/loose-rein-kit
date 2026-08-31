"""Verify init_cmd.py: seeding from package data, brownfield detection, and idempotence."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import yaml

from rein import init_cmd
from rein import lock as lock_mod

_STATE = """---
project: "<enter the product name>"
branch: "<enter the work branch name>"  # e.g. build/<product>. Implement on this branch.
current_phase: brief
gates:
  requirements: pending
updated_at: "<YYYY-MM-DD>"
---
# board
"""

_CONFIG = """build:
  headless:
    cmd: ["claude", "-p"]
gates:
  enforce_hook: true
  template_mode: true
"""

_BRIEF = """# Product Brief

## What do you want to build? (1-3 lines)
<!-- e.g. A CLI tool. -->


## For whom / what problem to solve
-
"""


# --- pure text surgery ---------------------------------------------------------------


def test_fill_state_fills_placeholders_and_keeps_comments() -> None:
    scaffold = (
        "# a comment that must survive\n"
        'project: "product"\n'
        "cycle_id: cycle-1                      # lowercase slug\n"
        "current_phase: brief\n"
        'updated_at: ""\n'
    )
    filled = init_cmd.fill_state(scaffold, "demo", "demo-cycle", "2026-07-23")
    assert 'project: "demo"' in filled
    assert "cycle_id: demo-cycle" in filled
    assert 'updated_at: "2026-07-23"' in filled
    assert "# a comment that must survive" in filled
    assert "# lowercase slug" in filled  # line surgery, never a YAML round-trip


def test_fill_plan_and_config_fill_their_own_placeholders() -> None:
    plan = init_cmd.fill_plan("cycle:\n  id: cycle-1\n  branch: build/product\n", "demo-cycle", "build/demo")
    assert "id: demo-cycle" in plan and "branch: build/demo" in plan
    config = init_cmd.fill_config("project:\n  name: product\n  work_branch: build/product\n", "demo", "build/demo")
    assert "name: demo" in config and "work_branch: build/demo" in config


def test_the_cycle_slug_is_schema_safe() -> None:
    assert init_cmd._cycle_slug("My Product!") == "my-product"
    assert init_cmd._cycle_slug("!!!") == "cycle-1"


def test_disable_template_mode_flips_only_that_flag() -> None:
    out = init_cmd.disable_template_mode(_CONFIG)
    assert "template_mode: false" in out
    assert "enforce_hook: true" in out


def test_transforms_are_idempotent() -> None:
    once = init_cmd.fill_state(_STATE, "demo", "build/demo", "2026-07-02")
    assert init_cmd.fill_state(once, "demo", "build/demo", "2026-07-02") == once
    once = init_cmd.disable_template_mode(_CONFIG)
    assert init_cmd.disable_template_mode(once) == once


def test_fill_brief_inserts_once_and_never_overwrites() -> None:
    out = init_cmd.fill_brief(_BRIEF, "A todo CLI.")
    assert "A todo CLI." in out
    assert out.index("<!--") < out.index("A todo CLI.")  # after the scaffold's example comment
    assert init_cmd.fill_brief(out, "Something else.") == out  # existing words are never replaced
    assert init_cmd.fill_brief("# no heading\n", "X") == "# no heading\n"


_GUARD_CONFIG = """gates:
  enforce_hook: true
  template_mode: true
  guard_paths:
    docs/20-design.md: requirements
    docs/tasks/: design
    src/: tasks
    backend/: tasks
    frontend/: tasks
    scripts/: tasks        # product scripts
build:
  quality_gate:
    steps:
      - name: test
        kind: cmd
        run: "make test"
      - name: check
        kind: cmd
        run: "make check"
"""


def test_brownfield_config_scopes_guard_to_docs_and_sets_cmds() -> None:
    """A pending gate must not freeze normal development on code that already exists, so the
    guard is scoped to the docs deliverables and the code prefixes are commented out."""
    from rein import data as data_mod

    out = init_cmd.brownfield_config(data_mod.read_text("scaffold/rein/config.yaml"), "npm test", "npm run lint")
    assert "#     - { path: src/, requires_gate: tasks }" in out
    assert "- { path: docs/20-design.md, requires_gate: requirements }" in out  # docs stay guarded
    assert 'command: ["npm", "test"]' in out
    assert 'command: ["npm", "run", "lint"]' in out
    assert "template_mode: false" in out


def test_brownfield_config_keeps_make_cmds_when_flags_absent() -> None:
    out = init_cmd.brownfield_config(_GUARD_CONFIG, "", "")
    assert 'run: "make test"' in out
    assert 'run: "make check"' in out


def test_detect_commands_recognizes_the_common_stacks() -> None:
    node = init_cmd.detect_commands({"package.json": '{"scripts": {"test": "vitest", "lint": "eslint ."}}'})
    assert node["test"] == ["npm test"] and node["check"] == ["npm run lint"]
    py = init_cmd.detect_commands({"pyproject.toml": "[tool.pytest]\n[tool.ruff]\n", "uv.lock": ""})
    assert py["test"] == ["uv run pytest"] and py["check"] == ["ruff check ."]
    mk = init_cmd.detect_commands({"makefile": "test:\n\ttrue\ncheck:\n\ttrue\n"})
    assert mk["test"] == ["make test"] and mk["check"] == ["make check"]
    assert init_cmd.detect_commands({}) == {"test": [], "check": []}


def test_source_from_direct_url_reconstructs_git_source() -> None:
    vcs = '{"url": "https://example.com/rein", "vcs_info": {"vcs": "git", "commit_id": "abc123"}}'
    assert init_cmd.source_from_direct_url(vcs) == "git+https://example.com/rein@abc123"
    # requested_revision wins over commit_id; an already-prefixed url is kept as-is.
    rev = (
        '{"url": "git+ssh://git@host/rein",'
        ' "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "v1.0"}}'
    )
    assert init_cmd.source_from_direct_url(rev) == "git+ssh://git@host/rein@v1.0"
    bare = '{"url": "https://example.com/rein", "vcs_info": {"vcs": "git"}}'
    assert init_cmd.source_from_direct_url(bare) == "git+https://example.com/rein"


def test_source_from_direct_url_returns_empty_without_vcs_coordinates() -> None:
    # An editable / local install (dir_info) has no VCS coordinates → nothing to record.
    assert init_cmd.source_from_direct_url('{"url": "file:///repo", "dir_info": {"editable": true}}') == ""
    assert init_cmd.source_from_direct_url("not json") == ""
    assert init_cmd.source_from_direct_url('{"vcs_info": {"vcs": "git"}}') == ""  # no url


def test_detect_source_returns_empty_when_metadata_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib.metadata as md

    def _raise(_name: str) -> object:
        raise md.PackageNotFoundError("rein")

    monkeypatch.setattr(md, "distribution", _raise)
    assert init_cmd.detect_source() == ""


def test_is_brownfield_detects_code_markers(tmp_path: Path) -> None:
    assert init_cmd.is_brownfield(tmp_path) is False
    (tmp_path / "docs").mkdir()  # the tool's own dirs never count
    assert init_cmd.is_brownfield(tmp_path) is False
    (tmp_path / "src").mkdir()
    assert init_cmd.is_brownfield(tmp_path) is True


# --- run_init (greenfield) --------------------------------------------------------------


def test_run_init_seeds_a_bare_directory(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "git+https://example.com/rein") == 0
    # The SSOT trio, placeholder-filled, gate guard live.
    state = (tmp_path / ".rein" / "state.yaml").read_text(encoding="utf-8")
    assert 'project: "demo"' in state
    config = (tmp_path / ".rein" / "config.yaml").read_text(encoding="utf-8")
    assert "template_mode: false" in config
    # The four SSOT documents, each valid against its own schema (seeded from the scaffold).
    for name in ("plan", "state", "review", "config"):
        assert (tmp_path / ".rein" / f"{name}.yaml").exists()
    # Docs scaffolds + the pristine snapshot cycle-close restores from.
    assert (tmp_path / "docs" / "00-product-brief.md").is_file()
    assert (tmp_path / "docs" / "10-requirements.md").is_file()
    assert (tmp_path / ".rein" / "scaffold" / "docs" / "10-requirements.md").is_file()
    # Materialized artifacts (repo-relative — the wrappers' @-imports depend on these paths).
    assert (tmp_path / ".rein" / "prompts" / "commands" / "req.md").is_file()
    assert (tmp_path / ".rein" / "schema" / "config.schema.json").is_file()
    assert (tmp_path / ".rein" / "AGENTS.rein.md").is_file()
    # The agent-neutral pointer, and NO agent surfaces (those are opt-in).
    assert "rein-rules" in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert not (tmp_path / ".claude").exists()
    assert not (tmp_path / ".github").exists()
    # The lock records the source, the seeds, and the materialized files.
    data = lock_mod.read(tmp_path / ".rein" / "rein.lock")
    assert data is not None
    assert data["rein"]["source"] == "git+https://example.com/rein"
    assert ".rein/state.yaml" in data["seeded"]
    assert "prompts/commands/req.md" in data["prompts"]["files"]


def test_run_init_writes_the_runtime_artifact_gitignore_block(tmp_path: Path) -> None:
    from rein import gitignore

    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.SECTION_HEADER in text
    for pattern in (".worktrees/", ".rein/work/", ".rein/pr-draft.md", ".rein/pr-stack/"):
        assert pattern in text
    # The SSOT and docs/** are never listed — they are committed and reviewed at each gate.
    assert ".rein/state.yaml" not in text
    assert "\ndocs/\n" not in text


def test_run_init_does_not_append_the_agents_pointer_in_a_template_repo(tmp_path: Path) -> None:
    """A repo whose own config already says `template_mode: true` (re-running init in the
    template itself) must not get a pointer to a rules body its AGENTS.md already *is*."""
    from rein import store
    from tests._support import make_config

    (tmp_path / ".rein").mkdir()
    (tmp_path / ".rein" / "config.yaml").write_bytes(store.dump_yaml(make_config(template_mode=True)))
    (tmp_path / "AGENTS.md").write_text("# Repository rules\n\nthe rules themselves.\n", encoding="utf-8")

    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    assert "rein-rules" not in (tmp_path / "AGENTS.md").read_text(encoding="utf-8")


def test_run_init_appends_its_block_to_an_existing_gitignore_once(tmp_path: Path) -> None:
    from rein import gitignore

    (tmp_path / ".gitignore").write_text(".venv/\n__pycache__/\n", encoding="utf-8")
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    first = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert first.startswith(".venv/\n__pycache__/\n")
    assert first.count(gitignore.SECTION_HEADER) == 1
    # A second init (idempotent re-run) does not duplicate the block.
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8").count(gitignore.SECTION_HEADER) == 1


def test_switch_branch_recognizes_a_repo_before_its_first_commit(tmp_path: Path) -> None:
    """A fresh `git init` has no HEAD commit to resolve yet, but it is very much a repo —
    the most common state `rein init` actually runs in (plan's own greenfield walkthrough)."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    result = init_cmd._switch_branch(tmp_path, "build/demo")
    assert "not a repository" not in result
    assert "created and switched to build/demo" in result


def test_run_init_falls_back_to_detected_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(init_cmd, "detect_source", lambda: "git+https://example.com/rein@v9")
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0  # no explicit source
    assert "detected      source: git+https://example.com/rein@v9" in capsys.readouterr().out
    data = lock_mod.read(tmp_path / ".rein" / "rein.lock")
    assert data is not None
    assert data["rein"]["source"] == "git+https://example.com/rein@v9"


def test_run_init_explicit_source_is_not_overridden_by_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_cmd, "detect_source", lambda: "git+https://detected/rein")
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "git+https://explicit/rein") == 0
    data = lock_mod.read(tmp_path / ".rein" / "rein.lock")
    assert data is not None
    assert data["rein"]["source"] == "git+https://explicit/rein"


def test_run_init_rerun_never_overwrites(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    state_path = tmp_path / ".rein" / "state.yaml"
    state_path.write_text(state_path.read_text(encoding="utf-8").replace("brief", "design"), encoding="utf-8")
    (tmp_path / "docs" / "10-requirements.md").write_text("FILLED\n", encoding="utf-8")
    capsys.readouterr()
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "skip" in out
    assert "design" in state_path.read_text(encoding="utf-8")  # the human's edit survives
    assert (tmp_path / "docs" / "10-requirements.md").read_text(encoding="utf-8") == "FILLED\n"


def test_run_init_brownfield_adapts_config_and_brief(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('existing')\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest", "lint": "eslint ."}}', encoding="utf-8")
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "brownfield" in out and "/onboard" in out
    config = (tmp_path / ".rein" / "config.yaml").read_text(encoding="utf-8")
    assert "#     - { path: src/, requires_gate: tasks }" in config  # code paths unguarded until re-enabled
    assert 'command: ["npm", "test"]' in config and 'command: ["npm", "run", "lint"]' in config
    brief = (tmp_path / "docs" / "00-product-brief.md").read_text(encoding="utf-8")
    assert "Adopted into an existing codebase" in brief
    # The guard config still parses and validates as YAML.
    parsed = yaml.safe_load(config)
    guarded = {entry["path"]: entry["requires_gate"] for entry in parsed["guard"]["paths"]}
    assert guarded.get("docs/tasks/") == "design"
    assert "src/" not in guarded  # commented out: existing code keeps flowing


def test_main_requires_a_name_without_a_tty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert init_cmd.main(["--repo", str(tmp_path)]) == 2
    assert "--name" in capsys.readouterr().err


def test_main_greenfield_flag_overrides_detection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "src").mkdir()  # would auto-detect brownfield
    assert init_cmd.main(["--name", "demo", "--greenfield", "--repo", str(tmp_path)]) == 0
    assert "greenfield" in capsys.readouterr().out
    config = (tmp_path / ".rein" / "config.yaml").read_text(encoding="utf-8")
    assert "- { path: src/, requires_gate: tasks }" in config  # code paths stay guarded (greenfield semantics)


def test_wizard_asks_only_name_and_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    prompts: list[str] = []

    def fake_input(prompt: str = "") -> str:
        prompts.append(prompt)
        return ""  # accept every default / skip the brief

    monkeypatch.setattr("builtins.input", fake_input)
    monkeypatch.setattr(init_cmd, "detect_source", lambda: "git+https://example.com/rein@vX")
    monkeypatch.setattr(init_cmd, "detect_agent", lambda _root: None)  # no ambient host to default to
    proj = tmp_path / "myproduct"
    proj.mkdir()
    assert init_cmd.wizard(proj) == 0
    # Only two questions are posed up front: the product name (defaulting to the folder) and the
    # brief. The surface and the sandbox are asked during the run, each with a default — which is
    # why neither prompt carries an "n of N": whether those two are asked at all depends on what
    # the seeded config turns out to say, so any count printed here would be a guess.
    assert any("product name" in p for p in prompts)
    assert not any("work branch" in p or "source URL" in p or "headless" in p for p in prompts)
    assert not any(re.search(r"\d/\d", p) for p in prompts)
    out = capsys.readouterr().out
    assert "What do you want to build?" in out
    # Name defaults to the folder, branch to build/<name>, source is the detected one.
    state = (proj / ".rein" / "state.yaml").read_text(encoding="utf-8")
    assert 'project: "myproduct"' in state
    data = lock_mod.read(proj / ".rein" / "rein.lock")
    assert data is not None and data["rein"]["source"] == "git+https://example.com/rein@vX"


# --- sandboxing is part of initialization -----------------------------------------
#
# It is the one precondition `rein init` never mentioned: a fresh config ships `kind: host`,
# which is not policy-compliant, and the human learned about it later from a `doctor` FAIL that
# pointed at a command sandboxing one profile of three.


def test_a_fresh_repo_is_told_what_it_still_owes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "sandbox: not built yet" in out
    assert "rein oci build --all --write-config" in out


def test_a_missing_runtime_is_named_as_the_thing_to_install_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rein import executors

    monkeypatch.setattr(executors, "container_runtime", lambda: None)
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    line = init_cmd.sandbox_step(tmp_path, offer=False)
    assert "docker/podman not found" in line
    assert "rein oci build --all --write-config" in line


def test_the_wizard_offers_to_build_and_takes_no_for_an_answer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Default no: it is a multi-minute build, and a setup step that runs one by default is a
    setup step people learn to Ctrl-C."""
    from rein import executors, oci_cli

    monkeypatch.setattr(executors, "container_runtime", lambda: "docker")
    monkeypatch.setattr(oci_cli, "main", lambda *a, **k: pytest.fail("must not build on a bare Enter"))  # noqa: ARG005
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    assert "skipped" in init_cmd.sandbox_step(tmp_path, offer=True, ask=lambda _q: "")


def test_the_wizard_builds_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rein import executors, oci_cli

    calls: list[list[str]] = []

    def record(argv: list[str]) -> int:
        calls.append(argv)
        return 0

    monkeypatch.setattr(executors, "container_runtime", lambda: "docker")
    monkeypatch.setattr(oci_cli, "main", record)
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    assert "built and pinned" in init_cmd.sandbox_step(tmp_path, offer=True, ask=lambda _q: "y")
    assert calls == [["build", "--all", "--write-config", "--repo", str(tmp_path)]]


def test_an_already_sandboxed_repo_is_not_asked_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests._support import SANDBOXED_PROFILES, make_config, seed_repo

    seed_repo(tmp_path, config=make_config(profiles=SANDBOXED_PROFILES))
    monkeypatch.setattr("rein.executors.container_runtime", lambda: "docker")
    assert "already sandboxed" in init_cmd.sandbox_step(tmp_path, offer=True, ask=lambda _q: "y")


def test_the_step_command_substitution_is_anchored_on_the_step_name() -> None:
    """It used to match the literal `command: [make, test]`.

    The day that default changed, the substitution would have silently done nothing and every
    brownfield repo would have been initialized with a DoD that ignored its own detected commands.
    """
    from rein import data as data_mod

    out = init_cmd.brownfield_config(data_mod.read_text("scaffold/rein/config.yaml"), "cargo test", "cargo clippy")

    assert 'command: ["cargo", "test"]' in out
    assert 'command: ["cargo", "clippy"]' in out
    assert "compileall" not in out.split("- name: check")[1].split("- name: review")[0]


def test_a_scaffold_whose_shape_moved_fails_instead_of_doing_nothing() -> None:
    with pytest.raises(ValueError, match="no quality-gate step named"):
        init_cmd.brownfield_config("quality_gate: []\n", "npm test", "npm run lint")


def _answer(reply: str) -> Callable[[str, str], str]:
    """An `ask` stub that always gives `reply`, ignoring the prompt and its default."""

    def ask(_prompt: str, _default: str = "") -> str:
        return reply

    return ask


def _refuse(why: str) -> Callable[..., int]:
    """A stub that fails the test if it is ever called."""

    def never(*_a: object, **_k: object) -> int:
        pytest.fail(why)

    return never


# --- the agent surface is part of initialization ------------------------------------
#
# The wizard's own closing line says "start with /req" — a command that does not exist until an
# integration is installed. Printing a suggestion instead of asking made an "opt-in" step
# mandatory in practice, and the tool paid for it at runtime: every `/`-recommendation had to
# carry a "no agent surface is installed" sentence, computed on the fly.


def test_a_fresh_repo_is_told_the_surface_is_still_owed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "") == 0
    out = capsys.readouterr().out
    assert "agent surface: none yet" in out
    assert "rein install <agent>" in out


def test_the_wizard_offers_the_surface_and_takes_none_for_an_answer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from rein import install as install_mod

    monkeypatch.setattr(
        install_mod,
        "install_integration",
        _refuse("must not install on 'none'"),
    )
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    line = init_cmd.surface_step(tmp_path, offer=True, ask=_answer("none"))
    assert "skipped" in line and "rein install <agent>" in line


def test_the_wizard_installs_the_surface_when_asked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    assert "installed claude" in init_cmd.surface_step(tmp_path, offer=True, ask=_answer("claude"))
    assert (tmp_path / ".claude" / "commands" / "req.md").is_file()


def test_a_repo_that_already_has_a_surface_is_not_asked_again(tmp_path: Path) -> None:
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    init_cmd.surface_step(tmp_path, offer=True, ask=_answer("claude"))

    def refuse(_prompt: str, _default: str = "") -> str:
        pytest.fail("an installed surface must not be asked about again")

    line = init_cmd.surface_step(tmp_path, offer=True, ask=refuse)
    assert "already present (claude)" in line


def test_an_unrecognised_answer_is_asked_again_not_read_as_a_decline(tmp_path: Path) -> None:
    """`cluade` is a typo. Filing it as "no thanks" answers the question on the human's behalf."""
    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    answers = iter(["cluade", "claude"])

    def ask(_prompt: str, _default: str = "") -> str:
        return next(answers)

    assert "installed claude" in init_cmd.surface_step(tmp_path, offer=True, ask=ask)


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, EOFError])
def test_interrupting_an_add_on_skips_it_rather_than_crashing(tmp_path: Path, interrupt: type[BaseException]) -> None:
    """These two questions are asked *after* the repository is written, and cannot be asked before.

    The surface question is about the config that was just seeded and the sandbox question about
    the profiles in it, so the wizard's "ask everything first, then write" contract stops at the
    brief. Ctrl+C here used to come out as a traceback over a repository that had in fact been
    initialized — a completed setup reported as a crash.
    """

    def interrupted(*_args: str) -> str:
        raise interrupt()

    init_cmd.run_init(tmp_path, "demo", "build/demo", "")
    assert "skipped" in init_cmd.surface_step(tmp_path, offer=True, ask=interrupted)
    assert "skipped" in init_cmd.sandbox_step(tmp_path, offer=True, ask=interrupted)


def test_the_detected_default_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDECODE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)
    monkeypatch.setattr("rein.init_cmd.shutil.which", lambda _cmd: None)
    assert init_cmd.detect_agent(tmp_path) is None
    (tmp_path / ".codex").mkdir()
    assert init_cmd.detect_agent(tmp_path) == "codex"
    (tmp_path / "CLAUDE.md").write_text("x", encoding="utf-8")
    assert init_cmd.detect_agent(tmp_path) == "claude"  # the more specific signal wins
