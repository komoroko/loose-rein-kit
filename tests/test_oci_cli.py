"""`rein oci` — the build/verify surface, and the profile↔Containerfile mapping it prints.

The bug these cover ran in both directions. `--profile` takes a *Containerfile* name, so telling
someone to run `--profile quality` handed them a command that exits 1; and after a successful
`--profile python`, telling them to pin the digest under `executor_profiles.python` sent them to a
profile that does not exist — the image belongs to `quality`. Neither end had a test.
"""

from __future__ import annotations

from pathlib import Path

from rein import executors, models, oci_cli
from tests._support import make_config, seed_repo

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _repo_with(profiles: dict[str, dict[str, object]], tmp_path: Path) -> Path:
    raw = make_config(profiles=profiles)
    return seed_repo(tmp_path, config=raw)


def test_the_pin_hint_names_the_profile_that_uses_the_image(tmp_path: Path) -> None:
    """`python` builds the quality profile's image. The hint must say `quality`."""
    root = _repo_with(
        {
            "quality": {"kind": "host", "containerfile": "python"},
            "reviewer": {"kind": "host", "containerfile": "reviewer"},
            "implementer": {"kind": "host", "containerfile": "implementer"},
        },
        tmp_path,
    )
    assert oci_cli._profiles_built_from("python", str(root)) == ["quality"]
    assert oci_cli._profiles_built_from("reviewer", str(root)) == ["reviewer"]


def test_several_profiles_sharing_one_image_are_all_named(tmp_path: Path) -> None:
    root = _repo_with(
        {
            "quality": {"kind": "host", "containerfile": "python"},
            "smoke": {"kind": "host", "containerfile": "python"},
            "reviewer": {"kind": "host", "containerfile": "reviewer"},
            "implementer": {"kind": "host", "containerfile": "implementer"},
        },
        tmp_path,
    )
    assert oci_cli._profiles_built_from("python", str(root)) == ["quality", "smoke"]


def test_an_unreadable_config_falls_back_rather_than_failing_a_good_build(tmp_path: Path) -> None:
    """The build already succeeded by then; a printed hint is not worth losing the digest over."""
    assert oci_cli._profiles_built_from("python", str(tmp_path)) == ["python"]


def test_every_shipped_profile_names_a_containerfile_that_exists() -> None:
    """`containerfile:` is the mapping every instruction site now reads, so a profile pointing at
    a Containerfile that does not ship would reintroduce the unrunnable command."""
    text = (_REPO_ROOT / ".rein/config.yaml").read_text(encoding="utf-8")
    config = models.Config.parse(text)
    known = set(executors.containerfile_names())
    for name, profile in sorted(config.profiles.items()):
        built_from = profile.containerfile
        assert built_from in known, f"profile {name!r} builds from {built_from!r}, which is not packaged"


def test_build_targets_are_containerfiles_not_profile_names() -> None:
    text = (_REPO_ROOT / ".rein/config.yaml").read_text(encoding="utf-8")
    config = models.Config.parse(text)
    assert set(config.unsandboxed_build_targets()) <= set(executors.containerfile_names())


# --- --write-config: the copy-the-digest-by-hand step ----------------------------

_PINS = {
    "quality": "localhost/rein-python@sha256:" + "a" * 64,
    "reviewer": "localhost/rein-reviewer@sha256:" + "b" * 64,
    "implementer": "localhost/rein-implementer@sha256:" + "c" * 64,
}


def _shipped_config() -> str:
    return (_REPO_ROOT / ".rein/config.yaml").read_text(encoding="utf-8")


def test_pinning_the_shipped_config_leaves_it_valid_and_sandboxed() -> None:
    """The whole point of the flag: `doctor`'s sandbox FAIL clears without hand-editing YAML."""
    text, missing = oci_cli.pin_profiles(_shipped_config(), _PINS)
    assert missing == []
    config = models.Config.parse(text)
    for name, image in _PINS.items():
        assert config.profiles[name].kind == "oci"
        assert config.profiles[name].image == image
        assert config.profiles[name].network_profile == "none"
    assert config.unsandboxed_code_profiles() == []


def test_pinning_keeps_every_comment() -> None:
    """config.yaml is a document a human maintains; a YAML round-trip would drop the reasoning."""
    original = _shipped_config()
    text, _ = oci_cli.pin_profiles(original, _PINS)
    assert text.count("#") == original.count("#")


def test_pinning_does_not_uncomment_the_hint_into_a_duplicate_key() -> None:
    """The shipped profiles carry `# kind: oci` under the live `kind: host`; strict_yaml rejects
    two `kind` keys in one mapping, so uncommenting it would break the file being pinned."""
    text, _ = oci_cli.pin_profiles(_shipped_config(), _PINS)
    quality = text.split("  quality:", 1)[1].split("\n  reviewer:", 1)[0]
    assert [line.strip() for line in quality.splitlines()].count("kind: oci") == 1
    assert "# kind: oci" in quality  # the hint survives as a comment


def test_pinning_is_idempotent() -> None:
    once, _ = oci_cli.pin_profiles(_shipped_config(), _PINS)
    twice, _ = oci_cli.pin_profiles(once, _PINS)
    assert twice == once


def test_an_unknown_profile_is_reported_rather_than_silently_skipped() -> None:
    _, missing = oci_cli.pin_profiles(_shipped_config(), {**_PINS, "nope": "localhost/x@sha256:" + "d" * 64})
    assert missing == ["nope"]
