"""Tests for agent_cli.py — pointing the AI roles at an adapter.

The behaviour worth protecting is the independence report. A setup where the actual extractor
and the comparator share a model will be blocked at gate ④ as an unexplained failure; saying
so at configuration time is the difference between a tool that surprises you and one that
tells you what it is about to do.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import agent_cli, models
from tests._support import make_config, seed_repo


def config_text(root: Path) -> str:
    return (root / ".rein" / "config.yaml").read_text(encoding="utf-8")


def parsed(root: Path) -> models.Config:
    return models.Config.parse(config_text(root))


SCAFFOLD = """\
executor_profiles:
  implementer:
    kind: host
  reviewer:
    kind: host
agents:
  implementer:
    adapter: claude
  code_reviewer:
    adapter: claude
  actual_extractor:
    adapter: claude          # trailing comment survives
    model: opus
  comparator:
    adapter: claude
    model: sonnet
"""


# --- surgical rewriting -------------------------------------------------------


def test_setting_one_role_leaves_the_others_alone() -> None:
    updated = agent_cli.apply_switch(SCAFFOLD, "codex", ("implementer",))
    assert "  implementer:\n    adapter: codex" in updated
    assert "  code_reviewer:\n    adapter: claude" in updated


def test_the_surgery_does_not_stray_into_another_section() -> None:
    """`executor_profiles` and `agents` both hold a two-space-indented `implementer`. A search
    anchored on the role name alone rewrote the wrong one."""
    updated = agent_cli.apply_switch(SCAFFOLD, "codex", ("implementer",))
    assert "  implementer:\n    kind: host" in updated
    assert "adapter: codex" not in updated.split("agents:")[0]


def test_comments_survive_the_rewrite() -> None:
    """A YAML round-trip would silently delete the comments that explain the file."""
    updated = agent_cli.apply_switch(SCAFFOLD, "codex", ("actual_extractor",))
    assert "# trailing comment survives" in updated


def test_setting_every_role_at_once() -> None:
    updated = agent_cli.apply_switch(SCAFFOLD, "gemini", agent_cli.ROLES[:2])
    assert updated.count("adapter: gemini") == 2


def test_the_model_can_be_set_alongside() -> None:
    updated = agent_cli.apply_switch(SCAFFOLD, "codex", ("comparator",), "o1")
    assert "model: o1" in updated


def test_a_missing_model_key_is_added() -> None:
    updated = agent_cli.apply_switch(SCAFFOLD, "codex", ("code_reviewer",), "o1")
    assert "model: o1" in updated


def test_an_undeclared_role_is_refused() -> None:
    with pytest.raises(agent_cli.AgentCliError, match="not declared"):
        agent_cli.apply_switch(SCAFFOLD, "codex", ("cold_maintainer",))


# --- the independence report --------------------------------------------------


def test_a_shared_group_is_reported_as_a_block() -> None:
    config = make_config()
    config["agents"]["comparator"]["model"] = "opus"  # type: ignore[index]
    warnings = agent_cli.independence_report(models.Config(config))
    assert any("share the independence group" in w for w in warnings)
    assert any("blind spots" in w for w in warnings)


def test_a_missing_group_is_reported() -> None:
    config = make_config()
    del config["agents"]["comparator"]["model"]  # type: ignore[index]
    assert any("no model named for" in w for w in agent_cli.independence_report(models.Config(config)))


def test_two_models_of_one_provider_are_reported_as_weaker_not_equivalent() -> None:
    warnings = agent_cli.independence_report(models.Config(make_config()))
    assert any("same provider" in w and "weaker" in w for w in warnings)


def test_the_same_provider_warning_says_what_to_do_about_it() -> None:
    """It is permanent for every single-provider environment, and a warning that only names a
    weakness is one people learn to scroll past. Procuring a second AI provider is the
    organization's job, so the remedies are the two this release actually offers."""
    warnings = agent_cli.independence_report(models.Config(make_config()))
    assert any("deterministic check" in w and "reduce the scope" in w for w in warnings)


def test_the_level_comes_from_the_branch_not_from_the_wording() -> None:
    """doctor used to recover FAIL-vs-WARN by searching the message text, so rewording a
    sentence here could silently downgrade a blocking finding."""
    shared = make_config()
    shared["agents"]["comparator"]["model"] = "opus"  # type: ignore[index]
    missing = make_config()
    del missing["agents"]["comparator"]["model"]  # type: ignore[index]
    two = make_config()
    # The provider is the adapter now, not a label beside it — two providers means two CLIs.
    two["agents"]["comparator"] = {"adapter": "codex", "model": "gpt"}  # type: ignore[index]

    assert agent_cli.independence_status(models.Config(shared))[0] == "FAIL"
    assert agent_cli.independence_status(models.Config(missing))[0] == "FAIL"
    assert agent_cli.independence_status(models.Config(make_config()))[0] == "WARN"
    assert agent_cli.independence_status(models.Config(two)) == ("PASS", [])


def test_two_providers_report_nothing() -> None:
    config = make_config()
    config["agents"]["comparator"] = {"adapter": "codex", "model": "gpt"}  # type: ignore[index]
    assert agent_cli.independence_report(models.Config(config)) == []


def test_the_pair_under_test_is_the_one_the_plan_names() -> None:
    assert agent_cli.INDEPENDENT_PAIR == ("actual_extractor", "comparator")
    assert set(agent_cli.ROLES) == models.AGENT_ROLE_VALUES


# --- the CLI ------------------------------------------------------------------


def test_show_lists_every_role_and_the_independence_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert agent_cli.main(["--show", "--repo", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    for role in agent_cli.ROLES:
        assert role in out
    assert "### Independence" in out


def test_no_adapter_argument_means_show(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    assert agent_cli.main(["--repo", str(tmp_path)]) == 0
    assert "| role |" in capsys.readouterr().out


def test_setting_an_adapter_rewrites_the_config(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert agent_cli.main(["codex", "--role", "implementer", "--repo", str(tmp_path)]) == 0
    assert parsed(tmp_path).adapter("implementer") == "codex"


def test_the_rewrite_is_still_schema_valid(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    agent_cli.main(["claude", "--role", "comparator", "--model", "haiku", "--repo", str(tmp_path)])
    config = parsed(tmp_path)  # would raise DocumentError if the surgery broke the shape
    assert config.model("comparator") == "haiku"
    assert config.independence_group("comparator") == "claude/haiku", "the group is derived, never authored"


def test_copilot_is_selectable_for_every_role(tmp_path: Path) -> None:
    """copilot was a first-class *host* — surfaces, hooks, an instructions file, a label in
    `doctor` — and not a launchable adapter, so `rein agent copilot` exited 2. It takes a model
    flag, so the bulk switch the scaffold's three model-bearing review roles need also lands."""
    seed_repo(tmp_path)
    assert agent_cli.main(["copilot", "--repo", str(tmp_path)]) == 0
    config = parsed(tmp_path)
    assert {config.adapter(role) for role in agent_cli.ROLES} == {"copilot"}
    assert config.independence_group("actual_extractor") == "copilot/opus"


def test_a_switch_is_recorded_in_the_audit_chain(tmp_path: Path) -> None:
    """`agents` is outside the gate ③ freeze, so nothing asks a human before this file moves —
    which makes the chain the only place the change survives, and the only way gate ④ can be told
    the evidence in front of it came from a different agent than the one gate ③ saw."""
    from rein import event_chain

    root = seed_repo(tmp_path)
    assert agent_cli.main(["codex", "--role", "implementer", "--repo", str(root)]) == 0
    events, defects = event_chain.scan(root / ".rein" / "events.ndjson")
    assert defects == []
    switches = [e for e in events if e.event == "agents_switched"]
    assert len(switches) == 1
    assert switches[0].detail["adapter"] == "codex"
    assert switches[0].detail["roles"] == ["implementer"]
    assert switches[0].detail["before"] != switches[0].detail["after"]


def test_setting_a_role_to_what_it_already_is_records_nothing(tmp_path: Path) -> None:
    """A no-op switch moved no environment, and a chain line saying otherwise is a false record."""
    from rein import event_chain

    root = seed_repo(tmp_path)
    assert agent_cli.main(["claude", "--role", "implementer", "--repo", str(root)]) == 0
    events, _ = event_chain.scan(root / ".rein" / "events.ndjson")
    assert [e for e in events if e.event == "agents_switched"] == []


def test_a_model_the_adapter_cannot_be_told_to_run_is_refused_before_it_is_written(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Schema-valid is not launchable, and the write is the moment that can still say so.

    `codex` takes no model flag this release knows, so the config below is one every launcher
    refuses. It used to be written anyway, exit 0, and discovered at `rein build`.
    """
    seed_repo(tmp_path)
    before = (tmp_path / ".rein/config.yaml").read_text(encoding="utf-8")
    assert agent_cli.main(["codex", "--role", "comparator", "--model", "o1", "--repo", str(tmp_path)]) == 2
    assert "cannot tell 'codex' which model to run" in capsys.readouterr().err
    assert (tmp_path / ".rein/config.yaml").read_text(encoding="utf-8") == before, "nothing is written"


def test_a_bulk_switch_onto_an_adapter_with_no_model_flag_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rein agent codex` on the scaffold: three review roles already name a model `codex` cannot take."""
    seed_repo(tmp_path)
    assert agent_cli.main(["codex", "--repo", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    for role in ("actual_extractor", "comparator", "security_reviewer"):
        assert f"agents.{role}.model" in err, role
    assert parsed(tmp_path).adapter("comparator") == "claude", "the config on disk is untouched"


def test_a_switch_that_collapses_the_pair_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    seed_repo(tmp_path)
    agent_cli.main(["claude", "--role", "comparator", "--model", "opus", "--repo", str(tmp_path)])
    assert "share the independence group" in capsys.readouterr().err


def test_an_unknown_role_is_refused(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    assert agent_cli.main(["codex", "--role", "nonexistent", "--repo", str(tmp_path)]) == 2


def test_a_model_without_a_role_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Setting one model for every role would collapse the pair the check exists to keep apart."""
    seed_repo(tmp_path)
    assert agent_cli.main(["codex", "--model", "o1", "--repo", str(tmp_path)]) == 2
    assert "pass --role too" in capsys.readouterr().err


def test_an_invalid_config_is_reported_not_overwritten(tmp_path: Path) -> None:
    seed_repo(tmp_path)
    (tmp_path / ".rein" / "config.yaml").write_text("project: {}\n", encoding="utf-8")
    assert agent_cli.main(["codex", "--repo", str(tmp_path)]) == 1


def test_a_switch_with_no_cycle_to_record_it_under_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The chain is where this switch survives — nothing else records it — so "there was nowhere to
    write it" is the one thing a silent return must not be."""
    import logging

    seed_repo(tmp_path)
    (tmp_path / ".rein/state.yaml").unlink()  # before `rein init`: there is no cycle to attach it to
    with caplog.at_level(logging.WARNING):
        assert agent_cli.main(["gemini", "--repo", str(tmp_path)]) == 0
    assert any("no cycle yet to record it under" in record.message for record in caplog.records)
    assert (
        models.Config.parse((tmp_path / ".rein/config.yaml").read_text(encoding="utf-8")).adapter("implementer")
        == "gemini"
    ), "the switch itself still lands; the record is what could not be made"
