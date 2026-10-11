"""Readers for the changed files the review's text analysis cannot read (`review_policy.file_readers`).

A screenshot, a recording, a PDF, a dataset or a compiled artifact made the Coverage Manifest
`insufficient`, and at high risk that shut acceptance. The only exit was naming each file at the
mandate as an `artifact` criterion's path — which a file named by the run that produces it cannot
be — and exempting by location instead (a declared directory) is the hole #79 closed: a compiled
`dist/app.so` under it was exempted with everything else. Accepting the gap by hand is not an exit
either: a critical unknown is not something a person may accept as a risk (`models.DISPOSITION_VALUES`).

So the gap is closed by **reading**, with the reader named. A reader is a deterministic command the
mandate froze in `config.yaml`. It runs in `quality_gate_profile`, which must be an `oci` sandbox,
with nothing mounted but the one file, and it either accounts for the whole of it as the kind of
file it reads (exit 0) or refuses it. The manifest names it beside every file it read, with the
digest of its command and image.

Contained, because the file is the change's: a parser on the host would be handed bytes the change
chose, and a command naming a script in the repository would run whatever the change made of it.
Nothing of the repository exists inside the container, so neither has anything to reach.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
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
    #: False for a forecast (`review.outlook`): a reader is asked only what the ledger already
    #: knows, and a status read never starts a container.
    run: bool
    #: Each blob's verdict in this scope. One review asks about one file once for the whole change
    #: and again for every reading that holds it; a refusal is not cached across runs (a machine's
    #: bad afternoon is not a verdict), and asking it again inside one is a container per reading.
    _verdicts: dict[str, str | None] = field(default_factory=dict, repr=False)

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def digest(self) -> str:
        """Its command and the image it runs in: the same command in another image is another reader."""
        return digests.of({"command": list(self.spec.command), "image": self.profile.image})

    def claims(self, path: str) -> bool:
        return self.spec.claims(path)

    def read(self, path: str) -> str | None:
        """ "" when the reader accounted for the file at `head`, why not when it refused it, and None
        when this scope does not run readers and the ledger has no answer.

        A blob it has read before is not read again (`evidence.Ledger`, keyed by the blob and the
        reader's digest): only an acceptance is recorded, so a refusal is asked afresh by the next
        scope that runs readers.
        """
        blob = _git(self.repo, "rev-parse", f"{self.head}:{path}").decode().strip()
        if blob not in self._verdicts:
            self._verdicts[blob] = self._verdict(blob, PurePosixPath(path).name)
        return self._verdicts[blob]

    def _verdict(self, blob: str, name: str) -> str | None:
        tool = (self.name, self.digest)
        if self.ledger.hit(evidence.KIND_READER, blob, tool):
            return ""
        if not self.run:
            return None
        with tempfile.TemporaryDirectory(prefix="rein-reader-") as scratch:
            (Path(scratch) / name).write_bytes(_git(self.repo, "cat-file", "blob", blob))
            spec = executors.ExecutionSpec(
                command=(*self.spec.command, f"/input/{name}"),
                profile=self.profile,
                mounts=((Path(scratch), "/input", "ro"),),
                workdir="/input",
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
def opened(
    repo: repo_mod.Repo, config: models.Config | None, head: str, *, run: bool
) -> Iterator[Sequence[CommandReader]]:
    """The readers `config` declares, bound to `head`; the ledger is written back on the way out.

    `run` is whether a reader the ledger has no answer for is run here or left unasked. Everything
    that writes a verdict runs them (the review, the build's warm readings, `doctor`); the board,
    read on every stream tick, does not.

    `Config.parse` refuses a declared reader with no `oci` profile to run in, so one reaching here
    is a config that never went through it, and is refused the same way.
    """
    if config is None or not config.file_readers:
        yield ()
        return
    profile = config.quality_gate_profile
    if profile is None or not profile.is_sandboxed:
        raise models.DocumentError("config.yaml", config.problems())
    ledger = evidence.Ledger.for_repo(repo, enabled=evidence.cache_enabled_by_env())
    try:
        yield tuple(CommandReader(spec, profile, repo, head, ledger, run) for spec in config.file_readers)
    finally:
        ledger.flush()


def _git(repo: repo_mod.Repo, *args: str) -> bytes:
    """Bytes, because a reader reads exactly what was committed. A git that cannot answer raises."""
    return subprocess.run(["git", "-C", str(repo.root), *args], check=True, capture_output=True).stdout
