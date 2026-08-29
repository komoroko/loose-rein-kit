"""Verify install.py: settings merge/unmerge (ported from adopt), sync's pristine rules,
and the install→guard→uninstall end-to-end path."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rein import gate_guard, init_cmd, install, models, store
from rein import lock as lock_mod
from rein import repo as repo_mod
from tests._support import make_config

# --- pure settings logic (semantics preserved from adopt.py) ---------------------------


def test_merge_settings_appends_missing_only_and_records_added() -> None:
    existing = {
        "permissions": {"allow": ["Read", "Bash(npm test:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "./my-hook.sh"}]}]},
    }
    template = {
        "permissions": {"allow": ["Read", "Bash(rein build:*)"]},
        "hooks": {
            "PreToolUse": [{"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "rein guard"}]}],
            "SessionStart": [{"hooks": [{"type": "command", "command": "cat state.md"}]}],
        },
    }
    merged, notes, added = install.merge_settings(existing, template)
    assert merged["permissions"]["allow"] == ["Read", "Bash(npm test:*)", "Bash(rein build:*)"]
    assert merged["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "./my-hook.sh"  # existing kept first
    assert merged["hooks"]["PreToolUse"][1]["hooks"][0]["command"] == "rein guard"
    assert notes
    assert added["permissions_allow"] == ["Bash(rein build:*)"]
    assert set(added["hooks"]) == {"PreToolUse", "SessionStart"}
    # Idempotent: a second merge adds nothing and records nothing.
    merged2, notes2, added2 = install.merge_settings(merged, template)
    assert notes2 == [] and merged2 == merged
    assert added2 == {"permissions_allow": [], "hooks": {}}


def test_unmerge_settings_retracts_only_the_recorded_entries() -> None:
    template = {
        "permissions": {"allow": ["Bash(rein build:*)"]},
        "hooks": {"PreToolUse": [{"matcher": "Write", "hooks": [{"type": "command", "command": "rein guard"}]}]},
    }
    existing = {"permissions": {"allow": ["Bash(npm test:*)"]}}
    merged, _notes, added = install.merge_settings(existing, template)
    unmerged, notes = install.unmerge_settings(merged, added)
    assert unmerged == {"permissions": {"allow": ["Bash(npm test:*)"]}}
    assert any("-=" in n for n in notes)


def test_unmerge_settings_leaves_locally_modified_groups() -> None:
    group = {"matcher": "Write", "hooks": [{"type": "command", "command": "rein guard"}]}
    installed = {"permissions_allow": [], "hooks": {"PreToolUse": [group]}}
    modified = {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": "rein guard"}]}
    existing = {"hooks": {"PreToolUse": [modified]}}
    unmerged, notes = install.unmerge_settings(existing, installed)
    assert unmerged["hooks"]["PreToolUse"] == [modified]  # theirs now — left alone
    assert any("locally modified" in n for n in notes)


def test_upgrade_settings_replaces_pristine_and_keeps_modified() -> None:
    old_group = {"matcher": "Write", "hooks": [{"type": "command", "command": "old guard"}]}
    new_group = {"matcher": "Write", "hooks": [{"type": "command", "command": "rein guard"}]}
    installed = {"permissions_allow": [], "hooks": {"PreToolUse": [old_group]}}
    existing = {"hooks": {"PreToolUse": [dict(old_group)]}}
    merged, _notes, added = install.upgrade_settings(existing, installed, {"hooks": {"PreToolUse": [new_group]}})
    assert merged["hooks"]["PreToolUse"] == [new_group]  # pristine → replaced without duplication
    assert added["hooks"]["PreToolUse"] == [new_group]


# --- changelog / marker blocks -----------------------------------------------------------

_CHANGELOG = "# Changelog\n\n## [0.3.0] - 2026-07-08\n- three\n\n## [0.2.0] - 2026-06-01\n- two\n"


def test_changelog_between_returns_sections_newer_than_installed() -> None:
    out = install.changelog_between(_CHANGELOG, "0.2.0", "0.3.0")
    assert "- three" in out and "- two" not in out
    assert install.changelog_between(_CHANGELOG, "0.3.0", "0.3.0") == ""
    assert "installed version unknown" in install.changelog_between(_CHANGELOG, "", "0.3.0")


def test_claude_import_block_roundtrip() -> None:
    text = "# My rules\nDo the thing.\n" + install.claude_import_block()
    assert install.remove_claude_import(text) == "# My rules\nDo the thing.\n"
    assert install.remove_claude_import("# plain\n") == "# plain\n"


def test_agents_pointer_block_roundtrip() -> None:
    text = "# Repo rules\n" + install.agents_pointer_block()
    assert install.remove_agents_pointer(text) == "# Repo rules\n"


# --- sync / install / uninstall end-to-end ---------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> repo_mod.Repo:
    """An initialized repo (greenfield init, gate guard live)."""
    assert init_cmd.run_init(tmp_path, "demo", "build/demo", "src") == 0
    return repo_mod.Repo(tmp_path)


def test_sync_check_is_clean_after_init_and_flags_drift(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install.sync(repo, check=True) == 0
    req = repo.path(".rein/prompts/commands/req.md")
    req.write_text(req.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8")
    capsys.readouterr()
    assert install.sync(repo, check=True) == 1
    assert "prompts/commands/req.md" in capsys.readouterr().out


def test_sync_keeps_local_modifications_unless_forced(repo: repo_mod.Repo) -> None:
    req = repo.path(".rein/prompts/commands/req.md")
    pristine = req.read_text(encoding="utf-8")
    req.write_text(pristine + "\nlocal note\n", encoding="utf-8")
    assert install.sync(repo) == 0
    assert "local note" in req.read_text(encoding="utf-8")  # skip-modified
    assert install.sync(repo, force=True) == 0
    assert req.read_text(encoding="utf-8") == pristine  # forced back to the payload


def test_sync_refreshes_a_pristine_file_deleted_locally(repo: repo_mod.Repo) -> None:
    req = repo.path(".rein/prompts/commands/req.md")
    req.unlink()
    assert install.sync(repo) == 0
    assert req.is_file()


def test_sync_prunes_a_lock_entry_whose_file_left_the_payload(repo: repo_mod.Repo) -> None:
    """A schema/prompt dropped from a later release must not haunt the lock forever."""
    assert install.sync(repo) == 0
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["prompts"]["files"]["schema/retired.schema.json"] = "sha256:" + "0" * 64
    lock_mod.write(repo.lock, data)
    assert install.sync(repo) == 0
    refreshed = lock_mod.read(repo.lock)
    assert refreshed is not None
    assert "schema/retired.schema.json" not in refreshed["prompts"]["files"]


def test_sync_deletes_a_materialized_file_the_payload_no_longer_ships(repo: repo_mod.Repo) -> None:
    """`_plan` iterated the payload alone, so a file dropped by a release stayed on disk forever —
    and the lock rewrite that followed dropped its entry, putting it beyond `uninstall`'s reach as
    well. Nothing in the repository then knew it had ever been installed."""
    assert install.sync(repo) == 0
    retired = repo.path(".rein/prompts/commands/retired.md")
    retired.write_text("what an earlier release shipped\n", encoding="utf-8")
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["prompts"]["files"]["prompts/commands/retired.md"] = lock_mod.norm_hash(retired.read_bytes())
    lock_mod.write(repo.lock, data)

    assert install.sync(repo) == 0
    assert not retired.exists()
    refreshed = lock_mod.read(repo.lock)
    assert refreshed is not None
    assert "prompts/commands/retired.md" not in refreshed["prompts"]["files"]


def test_sync_keeps_an_unshipped_file_somebody_edited_and_keeps_its_record(repo: repo_mod.Repo) -> None:
    """Deleting a local edit is not this command's call. Keeping the lock entry is the point: it is
    the only thing left tying the file to the tool that put it there, so `uninstall` can still
    retract it."""
    assert install.sync(repo) == 0
    retired = repo.path(".rein/prompts/commands/retired.md")
    retired.write_text("what an earlier release shipped\n", encoding="utf-8")
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["prompts"]["files"]["prompts/commands/retired.md"] = "sha256:" + "0" * 64  # not what is on disk
    lock_mod.write(repo.lock, data)

    assert install.sync(repo) == 0
    assert retired.exists()
    refreshed = lock_mod.read(repo.lock)
    assert refreshed is not None
    assert refreshed["prompts"]["files"]["prompts/commands/retired.md"] == "sha256:" + "0" * 64


def test_stale_integrations_compares_versions_canonically() -> None:
    data = {"integrations": {"claude": {"version": "0.1.0"}, "copilot": {"version": "0.1.1"}}}
    assert install.stale_integrations(data, "0.1.1") == {"claude": "0.1.0"}
    assert install.stale_integrations({}, "0.1.1") == {}
    assert install.stale_integrations({"integrations": {"claude": {"version": "0.1.01"}}}, "0.1.1") == {}
    assert install.stale_integrations({"integrations": {"claude": {}}}, "0.1.1") == {"claude": ""}


def test_sync_flags_integration_surfaces_from_an_older_release(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install.install_integration(repo, "claude") == 0
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["integrations"]["claude"]["version"] = "0.0.1"  # simulate surfaces from an older tool
    lock_mod.write(repo.lock, data)
    capsys.readouterr()
    assert install.sync(repo, check=True) == 1
    assert "rein install claude" in capsys.readouterr().out
    assert install.sync(repo) == 0  # a plain sync still succeeds, but repeats the pointer
    assert "rein install claude" in capsys.readouterr().out
    assert install.install_integration(repo, "claude") == 0  # the pointed-at fix clears the skew
    capsys.readouterr()
    assert install.sync(repo, check=True) == 0


def test_install_claude_writes_surfaces_and_merges_settings(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    assert repo.path(".claude/commands/req.md").is_file()
    assert repo.path(".claude/agents/architect.md").is_file()
    settings = json.loads(repo.path(".claude/settings.json").read_text(encoding="utf-8"))
    hook_cmds = [h["command"] for g in settings["hooks"]["PreToolUse"] for h in g["hooks"]]
    assert any("gate_guard" in c or "rein guard" in c for c in hook_cmds)
    assert "rein-rules" in repo.path("CLAUDE.md").read_text(encoding="utf-8")
    data = lock_mod.read(repo.lock)
    assert data is not None and "claude" in data["integrations"]
    assert ".claude/commands/req.md" in data["integrations"]["claude"]["files"]
    assert "settings" in data["integrations"]["claude"]


def test_install_claude_skips_claude_md_when_rules_already_referenced(repo: repo_mod.Repo) -> None:
    hand_written = "# Mine\nRead the rules in `.rein/AGENTS.rein.md`.\n"
    repo.path("CLAUDE.md").write_text(hand_written, encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert repo.path("CLAUDE.md").read_text(encoding="utf-8") == hand_written


def test_install_claude_skips_claude_md_in_template_mode(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    repo.path(".rein/config.yaml").write_bytes(store.dump_yaml(make_config(template_mode=True)))
    mapping = "# Capability mapping\n@AGENTS.md\n"
    repo.path("CLAUDE.md").write_text(mapping, encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert repo.path("CLAUDE.md").read_text(encoding="utf-8") == mapping
    assert "skip          CLAUDE.md" in capsys.readouterr().out


def test_install_prints_the_new_session_and_next_pointers(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    assert install.install_integration(repo, "copilot") == 0
    out = capsys.readouterr().out
    assert "open a new session" in out and "rein next" in out
    assert "(/req …)" in out


def test_install_names_the_entry_point_the_host_actually_has(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """Codex invokes its skills with `$`. Telling someone to type a command their host does not
    have is how a working install gets reported as broken."""
    assert install.install_integration(repo, "codex") == 0
    assert "($req …)" in capsys.readouterr().out


def test_install_copilot_writes_the_github_surfaces(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "copilot") == 0
    assert repo.path(".github/prompts/req.prompt.md").is_file()
    assert repo.path(".github/agents/architect.agent.md").is_file()
    assert repo.path(".github/hooks/rein.json").is_file()
    assert repo.path(".github/instructions/rein.instructions.md").is_file()
    assert not repo.path(".claude").exists()  # strictly the asked-for surface


def test_install_codex_writes_the_skills_and_codex_surfaces(repo: repo_mod.Repo) -> None:
    """Codex's phase entry points are skills, not prompt files: its custom prompts are deprecated
    upstream and live only in the user's home, so a repository can never ship them."""
    assert install.install_integration(repo, "codex") == 0
    assert repo.path(".agents/skills/req/SKILL.md").is_file()
    assert repo.path(".codex/agents/architect.toml").is_file()
    assert repo.path(".codex/hooks.json").is_file()
    assert repo.path(".codex/rein.md").is_file()
    assert not repo.path(".claude").exists() and not repo.path(".github/prompts").exists()


def test_uninstall_codex_retracts_every_surface(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "codex") == 0
    assert install.uninstall_integration(repo, "codex") == 0
    for rel in (".agents/skills/req/SKILL.md", ".codex/agents/architect.toml", ".codex/hooks.json"):
        assert not repo.path(rel).exists()
    assert (lock_mod.read(repo.lock) or {}).get("integrations", {}).get("codex") is None


def test_the_codex_hook_registers_the_same_guard_as_the_other_hosts(repo: repo_mod.Repo) -> None:
    """One guard, three registrations. A Codex-specific guard would be a second implementation of
    the gate rules, and the second one is always the one that falls behind."""
    assert install.install_integration(repo, "codex") == 0
    hooks = json.loads(repo.path(".codex/hooks.json").read_text(encoding="utf-8"))
    commands = [h["command"] for h in hooks["hooks"]["PreToolUse"][0]["hooks"]]
    assert commands == ["rein guard"]
    assert hooks["hooks"]["PreToolUse"][0]["matcher"] == "apply_patch"


def test_guard_denies_a_pending_gate_write_in_an_initialized_repo(repo: repo_mod.Repo) -> None:
    ok, reason = gate_guard.evaluate(str(repo.path("docs/20-design.md")), repo)
    assert ok is False and "requirements" in reason
    # tests/ stays deliberately unguarded (speculative work keeps flowing).
    ok, _ = gate_guard.evaluate(str(repo.path("tests/test_x.py")), repo)
    assert ok is True


def test_uninstall_claude_restores_the_pre_install_state(repo: repo_mod.Repo) -> None:
    before_settings = '{\n  "permissions": {\n    "allow": [\n      "Bash(npm test:*)"\n    ]\n  }\n}\n'
    repo.path(".claude").mkdir()
    repo.path(".claude/settings.json").write_text(before_settings, encoding="utf-8")
    repo.path("CLAUDE.md").write_text("# My rules\n", encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert install.uninstall_integration(repo, "claude") == 0
    assert not repo.path(".claude/commands").exists()
    assert json.loads(repo.path(".claude/settings.json").read_text(encoding="utf-8")) == json.loads(before_settings)
    assert repo.path("CLAUDE.md").read_text(encoding="utf-8") == "# My rules\n"
    data = lock_mod.read(repo.lock)
    assert data is not None and "claude" not in data.get("integrations", {})


def test_uninstall_keeps_locally_modified_wrapper(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    wrapper = repo.path(".claude/commands/req.md")
    wrapper.write_text("customized\n", encoding="utf-8")
    assert install.uninstall_integration(repo, "claude") == 0
    assert wrapper.read_text(encoding="utf-8") == "customized\n"  # theirs now


def test_install_rerun_refreshes_pristine_files(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    wrapper = repo.path(".claude/commands/req.md")
    pristine = wrapper.read_text(encoding="utf-8")
    wrapper.write_text("clobbered\n", encoding="utf-8")
    assert install.install_integration(repo, "claude") == 0
    assert wrapper.read_text(encoding="utf-8") == "clobbered\n"  # modified → kept
    assert install.install_integration(repo, "claude", force=True) == 0
    assert wrapper.read_text(encoding="utf-8") == pristine


def test_uninstall_all_leaves_only_repo_state(repo: repo_mod.Repo) -> None:
    assert install.install_integration(repo, "claude") == 0
    assert install.install_integration(repo, "copilot") == 0
    assert install.uninstall_all(repo) == 0
    assert not repo.path(".claude").exists()
    assert not repo.path(".github").exists()
    assert not repo.path(".rein/prompts").exists()
    assert not repo.path(".rein/AGENTS.rein.md").exists()
    assert not repo.lock.exists()
    # The repo's own state survives untouched.
    assert repo.state.is_file() and repo.config.is_file() and repo.plan.is_file()
    assert repo.path("docs/00-product-brief.md").is_file()
    agents = repo.path("AGENTS.md")
    assert not agents.exists() or "rein-rules" not in agents.read_text(encoding="utf-8")


def test_upgrade_refreshes_and_reports(repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]) -> None:
    assert install.install_integration(repo, "claude") == 0
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["tool_version"] = "0.0.1"  # simulate a repo written by an older tool
    lock_mod.write(repo.lock, data)
    capsys.readouterr()
    assert install.upgrade(repo) == 0
    out = capsys.readouterr().out
    assert "0.0.1 →" in out
    refreshed = lock_mod.read(repo.lock)
    assert refreshed is not None and lock_mod.tool_version_of(refreshed) != "0.0.1"


def test_cmd_wrappers_parse_their_flags(repo: repo_mod.Repo) -> None:
    assert install.cmd_sync(["--check", "--repo", str(repo.root)]) == 0
    assert install.cmd_install(["copilot", "--dry-run", "--repo", str(repo.root)]) == 0
    assert install.cmd_uninstall(["copilot", "--dry-run", "--repo", str(repo.root)]) == 0
    assert install.cmd_upgrade(["--dry-run", "--repo", str(repo.root)]) == 0


# --- an upgrade that breaks a repo document (issue #28) -----------------------


def test_sync_refuses_to_advance_the_lock_past_a_document_it_cannot_read(
    repo: repo_mod.Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    """`sync` materialized the new schema, stamped the new version, and erased the only record of
    where the repo came from — so `rein upgrade` then said "already current", printed no changelog,
    and named none of the renames that had just broken the repository.
    """
    data = lock_mod.read(repo.lock)
    assert data is not None
    data["tool_version"] = "0.0.1"
    lock_mod.write(repo.lock, data)
    config = repo.path(".rein/config.yaml")
    config.write_text(config.read_text(encoding="utf-8") + "\nnot_a_known_key: 1\n", encoding="utf-8")

    capsys.readouterr()
    assert install.sync(repo) == 1
    after = lock_mod.read(repo.lock)
    assert after is not None
    assert after["tool_version"] == "0.0.1", "the transition a human still needs is not erased"


def test_sync_rematerializes_the_review_stub_while_it_holds_no_review(repo: repo_mod.Repo) -> None:
    """The scaffold is written once at `init` and never migrated, so a release that changes the
    document's shape strands every repo that has not reached gate ④ — on a file whose entire
    content is "nothing has happened here"."""
    review_path = repo.path(".rein/review.yaml")
    review_path.write_text("machine:\n  status: not_generated\n  coverage: []\n", encoding="utf-8")
    assert install.refresh_ungenerated_review(repo) is True
    assert install.sync(repo) == 0
    assert models.Review.parse(review_path.read_text(encoding="utf-8")).machine_status == "not_generated"


def test_a_generated_review_is_never_rewritten_by_sync(repo: repo_mod.Repo) -> None:
    """It is evidence bound to a commit. `rein review generate --force` rewrites it by re-reading
    the code; nothing else may."""
    review_path = repo.path(".rein/review.yaml")
    original = review_path.read_text(encoding="utf-8")
    review_path.write_text(original.replace("not_generated", "generated"), encoding="utf-8")
    assert install.refresh_ungenerated_review(repo) is False
    assert "generated" in review_path.read_text(encoding="utf-8")
