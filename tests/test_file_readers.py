"""File readers: what makes a changed file the text analysis cannot read count as read (#106)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from rein import diff_facts, evidence, executors, file_readers, human_review, models, review_reading
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

    def read(self, path: str) -> str | None:
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


def test_a_file_a_forecast_did_not_ask_about_is_neither_read_nor_unread() -> None:
    """The board does not start a reader (`review.outlook`). A file whose reader has not answered
    yet is named as such, kept out of the status it cannot know, and never written as a manifest."""

    class _Unasked(_Fake):
        def read(self, path: str) -> str | None:
            return None

    coverage = diff_facts.analyze(_binary("docs/shot.png"), file_readers=[_Unasked("png", "*.png", set())]).coverage

    assert coverage.unasked_files == ({"path": "docs/shot.png", "reader": "png"},)
    assert coverage.unsupported_files == () and coverage.read_files == ()
    assert coverage.coverage_status == "sufficient"
    with pytest.raises(ValueError, match="docs/shot.png"):
        coverage.to_manifest()


def test_the_schema_takes_file_readers_and_the_config_reads_them() -> None:
    config = make_config()
    config["review_policy"]["file_readers"] = [{"name": "png", "paths": ["*.png"], "command": ["pngcheck", "-q"]}]
    assert models.schema_errors(config, "config") == []
    assert models.Config(config).file_readers == (
        models.FileReaderSpec(name="png", paths=("*.png",), command=("pngcheck", "-q")),
    )

    config["review_policy"]["file_readers"] = [{"name": "png", "paths": [], "command": ["pngcheck"]}]
    assert models.schema_errors(config, "config")


# --- where a reader may run -----------------------------------------------------


def _with_readers(readers: list[dict[str, object]], *, kind: str = "oci") -> str:
    profile = {"kind": kind, "containerfile": "python"}
    if kind != "host":
        profile |= {"image": "localhost/x@sha256:" + "a" * 64, "network_profile": "none"}
    config = make_config(profiles={"quality": profile})
    config["review_policy"]["file_readers"] = readers
    return json.dumps(config)


_PNG_READER: dict[str, object] = {"name": "png", "paths": ["*.png"], "command": ["pngcheck", "-q"]}


def test_a_reader_runs_only_in_an_oci_profile() -> None:
    """On the host a parser would be handed bytes the change chose, and a command naming a script in
    the repository would run whatever the change made of it. Refused when the config is read, so the
    mandate cannot freeze it."""
    assert models.Config.parse(_with_readers([_PNG_READER])).file_readers
    for kind in ("host", "oci-agent"):
        with pytest.raises(models.DocumentError, match="runs only in an `oci` profile"):
            models.Config.parse(_with_readers([_PNG_READER], kind=kind))


def test_two_readers_may_not_share_a_name() -> None:
    """The manifest records a reader by name beside the files it read: one name, one command."""
    other: dict[str, object] = {**_PNG_READER, "command": ["identify"]}
    with pytest.raises(models.DocumentError, match="names png more than once"):
        models.Config.parse(_with_readers([_PNG_READER, other]))


def test_a_reader_reaching_opened_without_an_oci_profile_is_refused(committed: repo_mod.Repo) -> None:
    config = make_config()
    config["executors"]["quality_gate_profile"] = "missing"
    config["review_policy"]["file_readers"] = [_PNG_READER]
    with pytest.raises(models.DocumentError, match="quality_gate_profile"):
        with file_readers.opened(committed, models.Config(config), "HEAD", run=True):
            pass


# --- the command, run --------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def committed(tmp_path: Path) -> repo_mod.Repo:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("base\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "base")
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "fake.png").write_bytes(b"MZ\x90\x00 not an image")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "files")
    return repo_mod.Repo(tmp_path)


class _Container:
    """An OCI executor stand-in: runs the argv on the host with `/input` pointed at its one mount,
    and keeps every spec it was handed so a test can see what reached the container."""

    def __init__(self) -> None:
        self.specs: list[executors.ExecutionSpec] = []

    def run(self, spec: executors.ExecutionSpec) -> executors.ExecutionResult:
        self.specs.append(spec)
        [(host, inside, mode)] = spec.mounts
        assert (inside, mode, spec.workdir) == ("/input", "ro", "/input")
        argv = [str(host) + arg[len(inside) :] if arg.startswith(inside + "/") else arg for arg in spec.command]
        done = subprocess.run(argv, capture_output=True, text=True, check=False)
        return executors.ExecutionResult(exit_code=done.returncode, output=done.stdout + done.stderr, image_digest="")


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch) -> _Container:
    fake = _Container()
    monkeypatch.setattr(executors, "for_profile", lambda profile: fake)
    return fake


def _reader(
    repo: repo_mod.Repo, command: tuple[str, ...], ledger: evidence.Ledger, *, run: bool = True
) -> file_readers.CommandReader:
    profile = models.ExecutorProfile(name="quality", raw={"kind": "oci", "image": "img@sha256:" + "0" * 64})
    spec = models.FileReaderSpec(name="png", paths=("*.png",), command=command)
    head = _git(repo.root, "rev-parse", "HEAD").strip()
    return file_readers.CommandReader(spec, profile, repo, head, ledger, run)


def test_a_command_reads_the_committed_bytes_with_nothing_else_mounted(
    committed: repo_mod.Repo, container: _Container
) -> None:
    reader = _reader(committed, IS_PNG, evidence.Ledger(path=None))
    assert reader.read("shot.png") == ""
    assert reader.read("fake.png") == "exit 3"
    assert [spec.command[-1] for spec in container.specs] == ["/input/shot.png", "/input/fake.png"]
    assert all(str(committed.root) not in str(spec.mounts) for spec in container.specs)


def test_a_blob_read_once_is_not_read_again(committed: repo_mod.Repo, container: _Container, tmp_path: Path) -> None:
    ledger = evidence.Ledger(path=tmp_path / "ledger.jsonl")
    assert _reader(committed, IS_PNG, ledger).read("shot.png") == ""
    # The same reader over the same blob in a new scope: the record answers.
    assert _reader(committed, IS_PNG, ledger).read("shot.png") == ""
    assert ledger.hits == 1 and len(container.specs) == 1


def test_a_refusal_is_asked_once_per_scope(committed: repo_mod.Repo, container: _Container) -> None:
    """One review asks about a file for the whole change and again for each reading that holds it.
    A refusal is not recorded across runs, so without this every reading started a container."""
    reader = _reader(committed, IS_PNG, evidence.Ledger(path=None))
    assert reader.read("fake.png") == reader.read("fake.png") == "exit 3"
    assert len(container.specs) == 1


def test_a_forecast_starts_no_reader(committed: repo_mod.Repo, container: _Container, tmp_path: Path) -> None:
    """The board polls the outlook: what the ledger knows is answered, anything else is left unasked."""
    ledger = evidence.Ledger(path=tmp_path / "ledger.jsonl")
    assert _reader(committed, IS_PNG, ledger, run=False).read("shot.png") is None
    assert container.specs == []
    assert _reader(committed, IS_PNG, ledger).read("shot.png") == ""
    assert _reader(committed, IS_PNG, ledger, run=False).read("shot.png") == ""
    assert len(container.specs) == 1


def test_a_readings_manifest_names_what_the_reader_read(committed: repo_mod.Repo, container: _Container) -> None:
    """A reading's manifest goes to its extractor and into its cache key. Taken without the readers
    it called the screenshot unread while the review's manifest said it was read."""
    reader = _reader(committed, IS_PNG, evidence.Ledger(path=None))
    measured = review_reading.read_facts(
        committed,
        base=_git(committed.root, "rev-parse", "HEAD~1").strip(),
        head=_git(committed.root, "rev-parse", "HEAD").strip(),
        exclude=(),
        limits=human_review.DEFAULT_BUDGET,
        evidence=(),
        file_readers=[reader],
    )
    assert measured.coverage["read_files"] == [{"path": "shot.png", "reader": "png"}]
