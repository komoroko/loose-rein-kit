"""`upstream` — where this install came from, and what the upstream has now.

The bug this module exists around is that a tag-pinned `uv tool install` does not move under
`uv tool upgrade`, so the command has to be *derived* from the install rather than quoted. The
second bug is subtler and is what `test_dist_name_resolves_the_real_distribution` covers: the
predecessor asked `importlib.metadata` for the *import* name, returned "" on every real install,
and passed its whole test suite because every test either exercised the pure parser or
monkeypatched the one function that touched reality.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rein import upstream

# --- parsing an install's coordinates -------------------------------------------


def test_source_from_direct_url_reconstructs_git_source() -> None:
    vcs = '{"url": "https://example.com/rein", "vcs_info": {"vcs": "git", "commit_id": "abc123"}}'
    assert upstream.source_from_direct_url(vcs) == "git+https://example.com/rein@abc123"
    rev = (
        '{"url": "git+ssh://git@host/rein",'
        ' "vcs_info": {"vcs": "git", "commit_id": "abc", "requested_revision": "v1.0"}}'
    )
    assert upstream.source_from_direct_url(rev) == "git+ssh://git@host/rein@v1.0"


def test_source_from_direct_url_returns_empty_without_vcs_coordinates() -> None:
    assert upstream.source_from_direct_url('{"url": "file:///repo", "dir_info": {"editable": true}}') == ""
    assert upstream.source_from_direct_url("not json") == ""


def test_dist_name_resolves_the_real_distribution() -> None:
    """The import name is `rein`; the distribution is `loose-rein-kit`, and only one of them resolves.

    This is the assertion whose absence let `detect_source` return "" forever. It asserts against
    the live interpreter's metadata, so a rename that breaks the lookup fails here.
    """
    import importlib.metadata as md

    assert md.distribution(upstream.DIST_NAME).metadata["Name"].lower().replace("_", "-") == upstream.DIST_NAME
    with pytest.raises(md.PackageNotFoundError):
        md.distribution("rein")


def test_parse_source_separates_a_rev_from_a_scp_style_url() -> None:
    tagged = upstream.parse_source("git+https://github.com/komoroko/loose-rein-kit.git@v0.3.12")
    assert tagged is not None and tagged.rev == "v0.3.12" and tagged.slug == "komoroko/loose-rein-kit"
    # `git@github.com:o/r` has an '@' that is part of the URL, not a rev separator.
    scp = upstream.parse_source("git+git@github.com:o/r")
    assert scp is not None and scp.rev == "" and scp.slug == "o/r"
    assert upstream.parse_source("https://example.com/x") is None  # not a VCS spelling


def test_slug_is_empty_for_a_non_github_origin() -> None:
    other = upstream.parse_source("git+https://gitlab.com/o/r@v1.0")
    assert other is not None and other.slug == ""


def test_origin_prefers_pep610_over_the_recorded_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """A human's `--source` describes what they typed; PEP 610 describes what is running."""
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/real/pkg@v2.0")
    org = upstream.origin("git+https://github.com/typed/pkg@v1.0")
    assert org is not None and org.slug == "real/pkg"
    monkeypatch.setattr(upstream, "detect_source", lambda: "")
    fallback = upstream.origin("git+https://github.com/typed/pkg@v1.0")
    assert fallback is not None and fallback.slug == "typed/pkg"
    assert upstream.origin("") is None


# --- the one place an upgrade command is spelled ---------------------------------


def test_upgrade_command_forces_a_reinstall_for_a_tag_pinned_install() -> None:
    org = upstream.parse_source("git+https://github.com/komoroko/loose-rein-kit@v0.3.12")
    assert org is not None and org.pinned
    assert (
        upstream.upgrade_command(org, "v0.4.0")
        == "uv tool install --force 'git+https://github.com/komoroko/loose-rein-kit@v0.4.0'"
    )


def test_upgrade_command_uses_uv_tool_upgrade_only_for_a_branch_install() -> None:
    for source in ("git+https://github.com/o/r", "git+https://github.com/o/r@main"):
        org = upstream.parse_source(source)
        assert org is not None and not org.pinned
        assert upstream.upgrade_command(org, "v0.4.0") == f"uv tool upgrade {upstream.DIST_NAME}"


def test_upgrade_command_names_no_command_when_the_origin_is_unknown() -> None:
    text = upstream.upgrade_command(None)
    assert "uv tool" not in text and upstream.RELEASES_URL in text


# --- what the upstream has now ---------------------------------------------------


def test_newer_available_compares_versions_and_tolerates_junk() -> None:
    assert upstream.newer_available("0.3.12", "v0.4.0") is True
    assert upstream.newer_available("0.3.12", "0.3.12") is False
    assert upstream.newer_available("0.4.0", "v0.3.12") is False
    assert upstream.newer_available("0.3.12", "release-2024-06") is False
    assert upstream.newer_available("not-a-version", "v0.4.0") is False


def test_latest_release_returns_the_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _run(cmd: list[str], *args: object, **kwargs: object) -> tuple[int, str]:
        calls.append(cmd)
        return 0, "v0.4.0\n"

    monkeypatch.setattr("rein.common.run", _run)
    org = upstream.parse_source("git+https://github.com/o/r@v1.0")
    assert upstream.latest_release(org) == "v0.4.0"
    assert calls[0][:2] == ["gh", "api"] and "repos/o/r/releases/latest" in calls[0]


@pytest.mark.parametrize("result", [(1, "gh: not found"), (0, ""), (0, "\n")])
def test_latest_release_is_none_when_the_question_could_not_be_answered(
    monkeypatch: pytest.MonkeyPatch, result: tuple[int, str]
) -> None:
    """Every failure is None, and the caller must not read None as "current"."""
    monkeypatch.setattr("rein.common.run", lambda *a, **k: result)
    assert upstream.latest_release(upstream.parse_source("git+https://github.com/o/r@v1.0")) is None


def test_latest_release_asks_nothing_without_a_github_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("no subprocess may run without a GitHub origin")

    monkeypatch.setattr("rein.common.run", _forbidden)
    assert upstream.latest_release(None) is None
    assert upstream.latest_release(upstream.parse_source("git+https://gitlab.com/o/r@v1.0")) is None


def test_no_update_check_env_skips_the_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    def _forbidden(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError(f"{upstream.NO_CHECK_ENV} must keep this off the network")

    monkeypatch.setattr("rein.common.run", _forbidden)
    monkeypatch.setenv(upstream.NO_CHECK_ENV, "1")
    assert upstream.latest_release(upstream.parse_source("git+https://github.com/o/r@v1.0")) is None


# --- the cache `rein start` reads and never writes --------------------------------


def test_cache_round_trips() -> None:
    upstream.write_cache("o/r", "v0.4.0")
    data = upstream.read_cache()
    assert data is not None and data["tag"] == "v0.4.0" and data["repo"] == "o/r"


def test_read_cache_is_none_when_absent_or_corrupt() -> None:
    assert upstream.read_cache() is None
    path = upstream.cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert upstream.read_cache() is None
    path.write_text(json.dumps({"repo": "o/r"}), encoding="utf-8")  # no tag
    assert upstream.read_cache() is None


def test_cached_note_speaks_only_for_a_newer_release(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upstream, "detect_source", lambda: "git+https://github.com/o/r@v0.3.12")
    upstream.write_cache("o/r", "v0.4.0")
    note = upstream.cached_note("0.3.12")
    assert "v0.4.0" in note and "uv tool install --force 'git+https://github.com/o/r@v0.4.0'" in note

    upstream.write_cache("o/r", "v0.3.12")
    assert upstream.cached_note("0.3.12") == ""


def test_cached_note_never_reaches_the_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """`rein start` runs from the SessionStart hook; a fetch there is latency on every session."""

    def _forbidden(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("cached_note must not run a subprocess")

    monkeypatch.setattr("rein.common.run", _forbidden)
    upstream.write_cache("o/r", "v0.4.0")
    assert upstream.cached_note("0.3.12") != ""


def test_cache_write_failure_is_not_an_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A cache home that cannot be written is not a reason for `rein doctor` to fail."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(upstream, "cache_path", lambda: blocker / "rein" / "upstream.json")
    upstream.write_cache("o/r", "v0.4.0")  # must not raise
    assert upstream.read_cache() is None
