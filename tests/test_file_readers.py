"""File readers: what makes a changed file the text analysis cannot read count as read (#106)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from rein import diff_facts, evidence, file_readers, models
from rein import repo as repo_mod
from tests._support import make_config

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
#: A reader that accepts a file starting with the PNG signature and refuses anything else.
IS_PNG = (
    sys.executable,
    "-c",
    "import sys; sys.exit(0 if open(sys.argv[1], 'rb').read(8) == b'\\x89PNG\\r\\n\\x1a\\n' else 3)",
)


def _binary(path: str) -> str:
    return f"diff --git a/{path} b/{path}\nnew file mode 100644\nBinary files /dev/null and b/{path} differ\n"


class _Fake:
    """A reader that reads the paths it is told it may read, without running anything."""

    def __init__(self, name: str, pattern: str, accepts: set[str]) -> None:
        self.name, self.digest = name, "sha256:" + "a" * 64
        self.spec = models.FileReaderSpec(name=name, paths=(pattern,), command=("x",))
        self.accepts = accepts
        self.asked: list[str] = []

    def claims(self, path: str) -> bool:
        return self.spec.claims(path)

    def read(self, path: str) -> str:
        self.asked.append(path)
        return "" if path in self.accepts else "exit 3: not a PNG"


def test_screenshots_a_reader_reads_are_read_wherever_they_sit() -> None:
    """#106: seven PNGs under a directory the plan declared made coverage insufficient on every build.
    A reader for the kind settles it without naming any file or any directory."""
    shots = [f"docs/test/measurements/ui-e2e-nofm/0{i}-decks.png" for i in range(1, 8)]
    png = _Fake("png", "*.png", set(shots))

    coverage = diff_facts.analyze("".join(_binary(p) for p in shots), file_readers=[png]).coverage

    assert coverage.coverage_status == "sufficient"
    assert coverage.unsupported_files == ()
    manifest = coverage.to_manifest()
    assert manifest["read_files"] == [{"path": p, "reader": "png"} for p in shots]
    assert manifest["file_readers"] == {"png": "sha256:" + "a" * 64}


def test_a_file_no_reader_reads_stays_unread_whatever_directory_it_is_in() -> None:
    """The hole #79 closed stays closed: a compiled binary under a declared directory is read by
    nothing, so it is unread code, however the reader for screenshots is configured."""
    png = _Fake("png", "*.png", set())
    coverage = diff_facts.analyze(_binary("dist/app.so"), file_readers=[png], evidence=["dist/"]).coverage
    assert [u["path"] for u in coverage.unsupported_files] == ["dist/app.so"]
    assert coverage.unsupported_files[0]["detail"] == "no reader for .so"
    assert png.asked == []


def test_a_reader_that_refuses_a_file_leaves_it_unread_and_says_so() -> None:
    """A `.png` that is not a PNG is exactly the file a name-based exemption would have waved through."""
    png = _Fake("png", "*.png", set())
    [entry] = diff_facts.analyze(_binary("docs/shot.png"), file_readers=[png]).coverage.unsupported_files
    assert entry["reason"] == "binary"
    assert entry["detail"] == "reader png refused it: exit 3: not a PNG"


def test_only_the_first_reader_that_claims_a_file_is_asked() -> None:
    """Trying readers until one says yes is shopping for a verdict."""
    strict = _Fake("strict", "*.png", set())
    lenient = _Fake("lenient", "*", {"docs/shot.png"})
    coverage = diff_facts.analyze(_binary("docs/shot.png"), file_readers=[strict, lenient]).coverage
    assert coverage.read_files == ()
    assert lenient.asked == []


def test_the_schema_takes_file_readers_and_the_config_reads_them() -> None:
    config = make_config()
    config["review_policy"]["file_readers"] = [{"name": "png", "paths": ["*.png"], "command": ["pngcheck", "-q"]}]
    assert models.schema_errors(config, "config") == []
    assert models.Config(config).file_readers == (
        models.FileReaderSpec(name="png", paths=("*.png",), command=("pngcheck", "-q")),
    )

    config["review_policy"]["file_readers"] = [{"name": "png", "paths": [], "command": ["pngcheck"]}]
    assert models.schema_errors(config, "config")


# --- the command, run --------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def committed(tmp_path: Path) -> repo_mod.Repo:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "fake.png").write_bytes(b"MZ\x90\x00 not an image")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "files")
    return repo_mod.Repo(tmp_path)


def _reader(repo: repo_mod.Repo, command: tuple[str, ...], ledger: evidence.Ledger) -> file_readers.CommandReader:
    profile = models.ExecutorProfile(name="quality", raw={"kind": "host"})
    spec = models.FileReaderSpec(name="png", paths=("*.png",), command=command)
    head = _git(repo.root, "rev-parse", "HEAD").strip()
    return file_readers.CommandReader(spec, profile, repo, head, ledger)


def test_a_command_reads_the_committed_bytes_and_refuses_what_it_does_not_account_for(
    committed: repo_mod.Repo,
) -> None:
    reader = _reader(committed, IS_PNG, evidence.Ledger(path=None))
    assert reader.read("shot.png") == ""
    assert reader.read("fake.png") == "exit 3"


def test_a_blob_read_once_is_not_read_again(committed: repo_mod.Repo, tmp_path: Path) -> None:
    ledger = evidence.Ledger(path=tmp_path / "ledger.jsonl")
    assert _reader(committed, IS_PNG, ledger).read("shot.png") == ""
    # The same reader over the same blob, with a command that would now refuse: the record answers.
    assert _reader(committed, IS_PNG, ledger).read("shot.png") == ""
    assert ledger.hits == 1


def test_a_reader_whose_command_lives_in_the_repository_is_refused(committed: repo_mod.Repo) -> None:
    """The change under review could rewrite a script in the tree to accept anything."""
    script = str(committed.root / "read_png.py")
    refused = _reader(committed, (sys.executable, script), evidence.Ledger(path=None)).read("shot.png")
    assert "inside the repository" in refused


def test_a_declared_reader_with_nowhere_to_run_is_a_config_error(committed: repo_mod.Repo) -> None:
    config = make_config()
    config["executors"]["quality_gate_profile"] = "missing"
    config["review_policy"]["file_readers"] = [{"name": "png", "paths": ["*.png"], "command": ["pngcheck"]}]
    with pytest.raises(models.DocumentError, match="quality_gate_profile"):
        with file_readers.opened(committed, models.Config(config), "HEAD"):
            pass
