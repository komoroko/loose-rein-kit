"""Verify registry.py: the user-global project registry and its `rein project` CLI."""

from __future__ import annotations

from pathlib import Path

import pytest

from rein import registry


def _make_repo(base: Path, name: str) -> Path:
    root = base / name
    (root / ".rein").mkdir(parents=True)
    return root


@pytest.fixture(autouse=True)
def isolate_config_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the registry at a throwaway config home so tests never touch the real ~/.config."""
    home = tmp_path / "cfg"
    monkeypatch.setenv("REIN_CONFIG_HOME", str(home))
    return home


# --- storage location + round-trip ---------------------------------------------


def test_registry_path_honors_config_home(tmp_path: Path) -> None:
    # REIN_CONFIG_HOME points straight at the rein config dir (no extra /rein).
    assert registry.registry_path() == tmp_path / "cfg" / "projects.yaml"


def test_missing_file_is_an_empty_registry() -> None:
    reg = registry.load()
    assert reg.projects == {} and reg.active is None


def test_add_save_load_round_trip(tmp_path: Path) -> None:
    web = _make_repo(tmp_path, "web")
    reg = registry.load()
    reg.add("web", web)
    reg.active = "web"
    registry.save(reg)

    again = registry.load()
    assert again.projects == {"web": web.resolve()}
    assert again.active == "web"


# --- validation ----------------------------------------------------------------


def test_add_rejects_a_non_rein_path(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    reg = registry.Registry()
    with pytest.raises(registry.RegistryError) as exc:
        reg.add("plain", plain)
    assert "no .rein/" in str(exc.value)


def test_add_rejects_a_bad_slug(tmp_path: Path) -> None:
    web = _make_repo(tmp_path, "web")
    reg = registry.Registry()
    with pytest.raises(registry.RegistryError):
        reg.add("Bad Name!", web)


def test_set_active_rejects_unknown_name() -> None:
    reg = registry.Registry(projects={"web": Path("/x")}, active=None)
    with pytest.raises(registry.RegistryError):
        reg.set_active("api")


def test_remove_clears_active_when_it_was_the_active_one(tmp_path: Path) -> None:
    web = _make_repo(tmp_path, "web")
    reg = registry.Registry()
    reg.add("web", web)
    reg.set_active("web")
    reg.remove("web")
    assert "web" not in reg.projects and reg.active is None


# --- record_use (the ui-startup MRU convenience) -------------------------------


def test_record_use_registers_new_and_reuses_existing(tmp_path: Path) -> None:
    api = _make_repo(tmp_path, "api")
    reg = registry.Registry()
    first = registry.record_use(reg, api)
    assert first == "api" and reg.projects["api"] == api.resolve()
    # A second call for the same root returns the existing name, no duplicate entry.
    again = registry.record_use(reg, api)
    assert again == "api" and len(reg.projects) == 1


def test_record_use_disambiguates_a_name_collision(tmp_path: Path) -> None:
    a = _make_repo(tmp_path / "one", "proj")
    b = _make_repo(tmp_path / "two", "proj")
    reg = registry.Registry()
    assert registry.record_use(reg, a) == "proj"
    assert registry.record_use(reg, b) == "proj-2"  # same dir name, different root


def test_entries_flags_active_and_missing(tmp_path: Path) -> None:
    web = _make_repo(tmp_path, "web")
    reg = registry.Registry()
    reg.add("web", web)
    reg.projects["gone"] = tmp_path / "gone"  # never created
    reg.set_active("web")
    by_name = {e["name"]: e for e in reg.entries()}
    assert by_name["web"]["active"] is True and by_name["web"]["exists"] is True
    assert by_name["gone"]["active"] is False and by_name["gone"]["exists"] is False


# --- the CLI -------------------------------------------------------------------


def test_cli_add_list_use_remove(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    web = _make_repo(tmp_path, "web")
    api = _make_repo(tmp_path, "api")

    assert registry.main(["add", "web", str(web)]) == 0
    assert registry.main(["add", "api", str(api)]) == 0
    # first add with no prior active becomes active automatically
    assert registry.load().active == "web"

    assert registry.main(["use", "api"]) == 0
    assert registry.load().active == "api"

    capsys.readouterr()
    assert registry.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "web" in out and "api" in out and "* api" in out  # active marked

    assert registry.main(["remove", "api"]) == 0
    assert "api" not in registry.load().projects


def test_cli_add_rejects_bad_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert registry.main(["add", "plain", str(plain)]) == 1
    assert "no .rein/" in capsys.readouterr().err


def test_cli_drops_the_appended_global_repo_flag(tmp_path: Path) -> None:
    # cli.py appends `--repo <path>` to every verb; the project verb must ignore it, not choke.
    web = _make_repo(tmp_path, "web")
    assert registry.main(["add", "web", str(web), "--repo", "/somewhere"]) == 0
    assert "web" in registry.load().projects
