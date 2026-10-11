"""Readers for the changed files the review's text analysis cannot read (`review_policy.file_readers`).

A screenshot, a recording, a PDF, a dataset or a compiled artifact made the Coverage Manifest
`insufficient`, and at high risk that shut acceptance. The only exit was naming each file at the
mandate as an `artifact` criterion's path — which a file named by the run that produces it cannot
be — and exempting by location instead (a declared directory) is the hole #79 closed: a compiled
`dist/app.so` under it was exempted with everything else. Accepting the gap by hand is not an exit
either: a critical unknown is not something a person may accept as a risk (`models.DISPOSITION_VALUES`).

So the gap is closed by **reading**, with the reader named. A reader is a deterministic command the
mandate froze in `config.yaml`. It runs in `quality_gate_profile` with nothing of the repository
mounted, given only the one file, and it either accounts for the whole of it as the kind of file it
reads (exit 0) or refuses it. The manifest names it beside every file it read, with the digest of
its command and image.

The command is never a path inside the repository: the change under review could rewrite it.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from rein import digests, evidence, executors, models
from rein import repo as repo_mod

#: How long one reader may take over one file. A reader parses one file; a minute is generous.
TIMEOUT_SEC = 60

#: How much of a refusing reader's output the manifest keeps: enough to say why.
_REFUSAL_CHARS = 300


@dataclass
class CommandReader:
    """One declared reader, bound to the commit whose files it reads (`diff_facts.FileReader`)."""

    spec: models.FileReaderSpec
    profile: models.ExecutorProfile
    repo: repo_mod.Repo
    head: str
    ledger: evidence.Ledger

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def digest(self) -> str:
        """Its command and where it runs: the same reader in another image is another reader."""
        where = self.profile.image if self.profile.runs_contained else "host"
        return digests.of({"command": list(self.spec.command), "image": where})

    def claims(self, path: str) -> bool:
        return self.spec.claims(path)

    def read(self, path: str) -> str:
        """ "" when the reader accounted for the file at `head`; otherwise why not.

        A blob it has read before is not read again (`evidence.Ledger`, keyed by the blob and the
        reader's digest): only an acceptance is recorded, so a refusal is always asked afresh.
        """
        inside = [arg for arg in self.spec.command if _inside(arg, self.repo.root)]
        if inside:
            return f"its command names {inside[0]}, inside the repository the change under review can rewrite"
        blob = _git(self.repo, "rev-parse", f"{self.head}:{path}").decode().strip()
        tool = (self.name, self.digest)
        if self.ledger.hit(evidence.KIND_READER, blob, tool):
            return ""
        name = PurePosixPath(path).name
        with tempfile.TemporaryDirectory(prefix="rein-reader-") as scratch:
            (Path(scratch) / name).write_bytes(_git(self.repo, "cat-file", "blob", blob))
            contained = self.profile.runs_contained
            spec = executors.ExecutionSpec(
                command=(*self.spec.command, f"/input/{name}" if contained else str(Path(scratch) / name)),
                profile=self.profile,
                mounts=((Path(scratch), "/input", "ro"),) if contained else (),
                workdir="/input" if contained else scratch,
                timeout_sec=TIMEOUT_SEC,
            )
            try:
                result = executors.for_profile(self.profile).run(spec)
            except executors.ExecutorError as exc:
                return f"could not be run: {exc}"
        if result.exit_code == 0:
            self.ledger.record(evidence.KIND_READER, blob, tool)
            return ""
        said = " ".join(result.output.split())[-_REFUSAL_CHARS:]
        return f"exit {result.exit_code}" + (f": {said}" if said else "")


@contextmanager
def opened(repo: repo_mod.Repo, config: models.Config | None, head: str) -> Iterator[Sequence[CommandReader]]:
    """The readers `config` declares, bound to `head`; the ledger is written back on the way out.

    None when nothing is declared. A declared reader with no `quality_gate_profile` to run in is a
    config error and raised as one: reading on the host instead would put a file the change wrote in
    front of a parser outside the sandbox, and saying nothing about it.
    """
    specs = config.file_readers if config is not None else ()
    if not specs:
        yield ()
        return
    profile = config.quality_gate_profile if config is not None else None
    if profile is None:
        raise models.DocumentError(
            "config.yaml",
            [
                "review_policy.file_readers is declared, and executors.quality_gate_profile names "
                "no profile to run them in"
            ],
        )
    ledger = evidence.Ledger.for_repo(repo, enabled=evidence.cache_enabled_by_env())
    try:
        yield tuple(CommandReader(spec, profile, repo, head, ledger) for spec in specs)
    finally:
        ledger.flush()


def _inside(arg: str, root: Path) -> bool:
    candidate = Path(arg)
    if not candidate.is_absolute():
        return False
    try:
        candidate.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _git(repo: repo_mod.Repo, *args: str) -> bytes:
    """Bytes, because a reader reads exactly what was committed. A git that cannot answer raises."""
    return subprocess.run(["git", "-C", str(repo.root), *args], check=True, capture_output=True).stdout
