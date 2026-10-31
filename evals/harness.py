"""Behaviour tests for the sentences rein hands an agent (devr 論点 Q).

rein refuses a writer's account as evidence for the product: the quality gate is re-run by the
caller, and a test that also passes against the old code is sent back by the negative control. The
sentences rein writes for agents — `prompts/`, `build_prompts.py`, `AGENTS.md` — are claims of the
same kind ("an agent that reads this does that"), and until this existed nothing checked one. A
change request to a prompt was finished when the sentence was there.

**A scenario holds one sentence to account, against a control.** It builds a repository in a known
state, hands a real agent CLI the prompt rein would hand it, and reads what the launch left behind.
It runs in two arms: the prompt as rein builds it, and the same prompt with the guarded sentence
taken out. The sentence is judged by the difference between the arms — never by a pass mark, which
would become the target (devr 論点 A). When the arm without the sentence does not fail either, the
scenario has not produced the failure the sentence is meant to prevent: that is not evidence the
sentence is dead until a stronger scenario cannot produce it either.

**Scored from artefacts, never by a model.** What a launch leaves — the arguments it called `rein`
with (a stub on PATH records them), the files it wrote, its findings file — is read by code. A
behaviour that leaves nothing to read cannot have a scenario, and that is a finding about the
sentence rather than a gap in this file.

**Where it runs and which model answers are the operator's choice, and both are recorded.**
`--profile <name>` launches inside that `kind: oci-agent` profile of this repository's
`.rein/config.yaml`; `--host` launches on this machine, with this user's CLI login. `--model` is
passed to the CLI, and a launch that reports another model answering is refused. None is assumed.

**The launch carries nothing of the operator's but the login.** The CLI is started in safe mode with
no setting sources: the operator's `CLAUDE.md`, rules, `language`, output style and permission mode
are not read, so `ALLOWED` and the stub are all a launch may run. They reached every launch before, and none was
in the key: an operator rule that prefixes `PATH` sent agents past the stub to the installed `rein`,
and the trials that did so were scored as the sentence failing. Policy settings the machine's
administrator installed still apply, and are part of what `where` names.

**A record is about the launch it measured (devr 論点 T).** Its key is each arm's launch digest —
the command, the prompt and the repository the agent was handed — the scenario's own digest — its
fixture and its score — the model, the CLI's version, where it ran and how long a launch may run.
`template_lint` rebuilds the launches from the sources as they stand and refuses a record whose
digests no longer match: a count measured on a prompt that has since changed is not evidence about
the one that ships.

**Judged by the difference, and trials are added, never re-drawn.** Each run spends the scenario's
own `trials` per arm. A run whose key matches the record adds to it rather than replacing it, so
running again cannot discard the trials that came out the wrong way. Nor can stopping part-way: each
trial is written as it ends, and the next run of the key fills the stopped one (`Ledger`,
devr 論点 X). A launch that runs past `--timeout` is a trial, scored on what it left by then: running
long is something an arm does, and drawing it again until it finishes in time would choose the
sample. The arms are compared by a one-sided Fisher exact test at `ALPHA` per look, over at most
`LOOKS` looks — two at 0.025 keep the whole under 0.05. The verdict is `holds`, `not-reproduced`
(the arm without the sentence never failed: the scenario did not produce what the sentence
prevents), `undecided` (look again) or `does-not-hold`; once one other than `undecided` is reached,
the key is not run again. A record whose key differs replaces the last one for that scenario and
model.

**The harness keeps each trial's transcript for as long as the record it counts in (devr 論点 V).**
A count says how often an arm passed, not why a trial failed, and the score is code that can be
wrong about a launch: a scenario's record in 0.16.0 was read as its sentence failing until the
transcripts the CLI had kept were read. The CLI is told not to keep its own copy; the harness
writes the stream it printed, with the trial's score and its `rein` calls, under
`evals/transcripts/<scenario>.<model>/`, beside the key it belongs to. They are working material,
not evidence, and are not committed: the directory starts afresh with its record's first run, and
goes when its record does. The scratch repositories are deleted after each trial.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from math import comb
from pathlib import Path
from typing import Any

from rein import adapters, common, executors, models, usage
from rein import repo as repo_mod
from rein import store as store_mod

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent
RESULTS = EVALS / "results"
TRANSCRIPTS = EVALS / "transcripts"
SCENARIOS = EVALS / "scenarios"
ARMS = ("with", "without")
ALPHA = 0.025
LOOKS = 2
VERDICTS = ("holds", "not-reproduced", "undecided", "does-not-hold")

#: The git verbs a launch may run, plain or as `git -C <the trial's repository>`: reading, and the
#: two a repair commits with. Not `-C` alone, which would let any verb through.
GIT = ("diff", "log", "status", "show", "ls-tree", "grep", "cat-file", "rev-parse", "add", "commit")
#: The quality gate every scenario's prompt names, which the agent is told to run.
GATE = "python -m pytest"
#: The tools a scenario's launch may use without asking. A headless `-p` launch cannot answer a
#: permission prompt, so anything not named here is refused — which is what keeps a scenario to
#: what it says it exercises. A refusal falls on whichever arm meets it, so what a reviewer or an
#: implementer needs to read a change and run its gate is allowed: when it was not, refused trials
#: were scored as the sentence failing.
ALLOWED = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(rein:*)",
    *(f"Bash(git {verb}:*)" for verb in GIT),
    f"Bash({GATE}:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(wc:*)",
    "Bash(echo:*)",
    "Bash(which:*)",
)

_STUB = """#!/bin/sh
python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "$@" >> "{log}"
echo "recorded"
"""


class HarnessError(RuntimeError):
    """A scenario that cannot be run as written, or a run that cannot be recorded."""


@dataclass(frozen=True)
class Guard:
    """The sentence a scenario holds to account, where it lives, and the change request it came from.

    `section` is a markdown heading (without the `#`s) for a document, or a function name for a
    module. `template_lint` refuses a scenario whose file or section no longer exists.
    """

    file: str
    section: str
    sentence: str
    cr: str


@dataclass(frozen=True)
class Scenario:
    name: str
    guard: Guard
    trials: int
    #: Lays the repository out: files, commits, branches. `root` is an empty directory.
    fixture: Callable[[Path], None]
    #: The prompt rein would hand the agent in that repository, built by rein's own code.
    prompt: Callable[[Path], str]
    #: Did the launch do what the sentence claims? Reads `root` and the stub's recorded calls.
    score: Callable[[Path, list[list[str]]], bool]
    #: The prompt the `without` arm is handed, when what the guarded sentence replaced is a whole
    #: prompt rather than the absence of the sentence (CR-93 replaced the full re-read with a
    #: confirmation). The guarded sentence must still be in the `with` arm's prompt exactly once.
    control: Callable[[Path], str] | None = None


def load(name: str) -> Scenario:
    module = importlib.import_module(f"evals.scenarios.{name}")
    scenario = getattr(module, "SCENARIO", None)
    if not isinstance(scenario, Scenario):
        raise HarnessError(f"evals/scenarios/{name}.py defines no SCENARIO")
    return scenario


def names() -> list[str]:
    return sorted(p.stem for p in SCENARIOS.glob("*.py") if not p.stem.startswith("_"))


def arm_prompt(prompt: str, sentence: str, arm: str) -> str:
    """The prompt as `arm` sees it. The sentence must be in it exactly once, or there is no control."""
    found = prompt.count(sentence)
    if found != 1:
        raise HarnessError(f"the guarded sentence occurs {found} time(s) in the prompt, not once: {sentence!r}")
    if arm == "with":
        return prompt
    if arm == "without":
        return prompt.replace(sentence, "", 1)
    raise HarnessError(f"unknown arm {arm!r} (one of {', '.join(ARMS)})")


def calls(root: Path) -> list[list[str]]:
    """What the launch called `rein` with, in order."""
    log = root / ".evalbin" / "rein.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout


def prepare(root: Path, scenario: Scenario) -> None:
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "eval@example.com")
    git(root, "config", "user.name", "eval")
    # The fixture's files are part of the launch digest, and a conflicted one is written in whatever
    # style this user's global config names: pinned, so every machine rebuilds the same launch.
    git(root, "config", "merge.conflictstyle", "merge")
    scenario.fixture(root)
    stub = root / ".evalbin" / "rein"
    stub.parent.mkdir(parents=True, exist_ok=True)
    stub.write_text(_STUB.format(log=root / ".evalbin" / "rein.log"), encoding="utf-8")
    stub.chmod(0o755)
    (root / ".git" / "info" / "exclude").write_text(".evalbin/\n", encoding="utf-8")


#: What keeps a launch to the scenario's own inputs, and its transcript to the harness. Not the
#: adapter's `config_isolation`, which `rein build` uses on purpose to carry the operator's settings.
ISOLATION = (
    "--safe-mode",
    "--setting-sources",
    "",
    "--strict-mcp-config",
    "--no-session-persistence",
    "--output-format",
    "stream-json",
    "--verbose",
)


def command(prompt: str, model: str, root: Path) -> list[str]:
    """`rein` is allowed under every name the stub answers to: an agent that ran `which rein` and
    called the path it printed was refused, and a correct report was scored as no report. `GIT` is
    allowed against the trial's repository by its path as well."""
    claude = adapters.ADAPTER_TABLE["claude"]
    stub = [f"Bash({name}:*)" for name in (".evalbin/rein", "./.evalbin/rein", f"{root}/.evalbin/rein")]
    there = [f"Bash(git -C {root} {verb}:*)" for verb in GIT]
    settings = json.dumps({"permissions": {"allow": [*ALLOWED, *stub, *there]}})
    return [
        *claude.argv,
        *ISOLATION,
        "--settings",
        settings,
        "--model",
        model,
        prompt,
    ]


def stage(scenario: Scenario, arm: str, root: Path, model: str) -> list[str]:
    """Lay `arm` of the scenario out in the empty directory `root`, and return the command that launches it."""
    prepare(root, scenario)
    if scenario.control is not None:
        prompt = arm_prompt(scenario.prompt(root), scenario.guard.sentence, "with")
        prompt = prompt if arm == "with" else scenario.control(root)
    else:
        prompt = arm_prompt(scenario.prompt(root), scenario.guard.sentence, arm)
    return command(prompt, model, root)


def launch_digest(root: Path, argv: Sequence[str]) -> str:
    """The command and every file of the repository it is launched in, with `root` itself taken out.

    Every trial gets a new scratch directory, and the prompt and the files name paths in it: a
    digest that moved with it would call the same launch a different one. `.git` is left out — its
    object ids carry commit times — and the history is the fixture's, which the scenario digest holds.
    """
    hashed = hashlib.sha256(json.dumps(list(argv)).replace(str(root), "<root>").encode("utf-8"))
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        if rel.startswith((".git/", ".evalbin/")):
            continue
        hashed.update(b"\0" + rel.encode("utf-8") + b"\0")
        hashed.update(path.read_bytes().replace(str(root).encode("utf-8"), b"<root>"))
    return "sha256:" + hashed.hexdigest()


def scenario_digest(name: str) -> str:
    """The scenario's module and the fixtures it shares: its repository's history and its score."""
    hashed = hashlib.sha256()
    for path in (SCENARIOS / f"{name}.py", SCENARIOS / "_fixtures.py"):
        hashed.update(path.read_bytes() + b"\0")
    return "sha256:" + hashed.hexdigest()


def current_digests(scenario: Scenario, model: str) -> dict[str, str]:
    """Each arm's launch digest as the sources stand now, without launching anything."""
    out: dict[str, str] = {}
    for arm in ARMS:
        root = Path(tempfile.mkdtemp(prefix=f"rein-eval-{scenario.name}-"))
        try:
            out[arm] = launch_digest(root, stage(scenario, arm, root, model))
        finally:
            shutil.rmtree(root, ignore_errors=True)
    return out


def fisher(with_passed: int, without_passed: int, trials: int) -> float:
    """One-sided Fisher exact p: how likely an arm `with` at least this far ahead, if the sentence did nothing."""
    passed = with_passed + without_passed
    total = comb(2 * trials, trials)
    return (
        sum(
            comb(passed, k) * comb(2 * trials - passed, trials - k) for k in range(with_passed, min(passed, trials) + 1)
        )
        / total
    )


def judge(with_passed: int, without_passed: int, trials: int, looks: int) -> tuple[float, str]:
    p = fisher(with_passed, without_passed, trials)
    if p <= ALPHA:
        return p, "holds"
    if without_passed == trials:
        return p, "not-reproduced"
    return p, "undecided" if looks < LOOKS else "does-not-hold"


@dataclass(frozen=True)
class Launched:
    rc: int
    output: str
    models: tuple[str, ...]
    #: Stopped at the timeout. Not a failed launch: what it left is scored like any other's.
    timed_out: bool = False


def launch_host(root: Path, argv: list[str], timeout: float) -> Launched:
    env = {**os.environ, "PATH": f"{root / '.evalbin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    # In its own session, so the timeout reaches what the CLI started — a test run left behind
    # would go on changing the repository the trial is scored on.
    with subprocess.Popen(
        argv,
        cwd=root,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as launched:
        try:
            out, err = launched.communicate(timeout=timeout)
        except BaseException as stopped:
            common.kill_group(launched)
            out, _ = launched.communicate()
            if not isinstance(stopped, subprocess.TimeoutExpired):
                raise
            return _read(0, out, timed_out=True)
    return _read(launched.returncode, out + err if launched.returncode else out)


def launch_contained(profile: models.ExecutorProfile) -> Callable[[Path, list[str], float], Launched]:
    def run(root: Path, argv: list[str], timeout: float) -> Launched:
        spec = executors.ExecutionSpec(
            command=tuple(argv),
            profile=profile,
            mounts=((root, str(root), "rw"),),
            env=dict(os.environ),
            env_always={"PATH": f"{root / '.evalbin'}:/usr/local/bin:/usr/bin:/bin"},
            workdir=str(root),
            timeout_sec=timeout,
        )
        result = executors.for_profile(profile).run(spec)
        return _read(result.exit_code, result.output, timed_out=result.timed_out)

    return run


def _read(rc: int, output: str, *, timed_out: bool = False) -> Launched:
    """Each turn's closing `result` event is the envelope `--output-format json` would have printed.

    A stream stopped at the timeout has no `result` event, and the models are read off the
    assistant messages it did print. Its lines are read one by one and the ones that are not JSON
    passed over: the kill cuts the last line short, and an executor appends its own note. One that
    printed no assistant message names no model, and `Ledger.add` refuses it like a failed launch:
    no model answered, so it measured nothing of one.
    """
    if timed_out:
        answered: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            message = event.get("message") if isinstance(event, dict) and event.get("type") == "assistant" else None
            if isinstance(message, dict) and isinstance(message.get("model"), str):
                answered.append(message["model"])
        return Launched(rc, output, tuple(dict.fromkeys(answered)), timed_out=True)
    if rc != 0:
        return Launched(rc, output, ())
    events = [json.loads(line) for line in output.splitlines() if line.strip()]
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "result"]
    if not results:
        raise HarnessError("the launch's stream closed with 0 result event(s)")
    # One per turn: a host that re-enters the agent when a background command exits — what `/build`
    # tells a lead to rely on — goes on to another turn within the same launch.
    answered = [model for result in results for model in usage.parse_claude_envelope(json.dumps(result))[1].models]
    return Launched(rc, output, tuple(dict.fromkeys(answered)))


@dataclass(frozen=True)
class Trial:
    passed: bool
    #: Taken before the launch, over what the agent is handed.
    digest: str
    models: tuple[str, ...]
    #: What the launch printed — its transcript — and what it called `rein` with.
    output: str
    calls: list[list[str]]
    timed_out: bool = False


def run_arm(
    scenario: Scenario, arm: str, launch: Callable[[Path, list[str], float], Launched], timeout: float, model: str
) -> Trial:
    """One trial of one arm. The scratch repository is removed whatever happened; the transcript
    travels in the result, to be kept with the record it counts in."""
    root = Path(tempfile.mkdtemp(prefix=f"rein-eval-{scenario.name}-"))
    try:
        argv = stage(scenario, arm, root, model)
        digest = launch_digest(root, argv)
        launched = launch(root, argv, timeout)
        if launched.rc != 0 and not launched.timed_out:
            raise HarnessError(
                f"{scenario.name} [{arm}]: the launch failed (rc {launched.rc}):\n{launched.output[-2000:]}"
            )
        made = calls(root)
        return Trial(scenario.score(root, made), digest, launched.models, launched.output, made, launched.timed_out)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def cli_version() -> str:
    return subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True).stdout.strip()


def record_path(name: str, model: str) -> Path:
    return RESULTS / f"{name}.{model}.json"


def key(
    scenario: Scenario, *, model: str, cli: str, where: str, timeout: float, digests: dict[str, str]
) -> dict[str, Any]:
    """What a record is about. Two runs with the same key measured the same thing, and add up."""
    if not cli:
        raise HarnessError("the CLI's version is unknown — the run cannot be recorded")
    return {
        "model": model,
        "cli": cli,
        "where": where,
        "timeout": timeout,
        "scenario_digest": scenario_digest(scenario.name),
        "launch_digests": {arm: digests[arm] for arm in ARMS},
    }


#: What `key` returns, by name: a record without one of them was measured under another key.
KEY_FIELDS = ("model", "cli", "where", "timeout", "scenario_digest", "launch_digests")


def _key_of(document: dict[str, Any]) -> dict[str, Any]:
    """A record's key. A field it lacks is None, which no key has: the record is of another key."""
    return {k: document.get(k) for k in KEY_FIELDS}


def prior(scenario: Scenario, run_key: dict[str, Any]) -> dict[str, Any] | None:
    """The record this run adds to, or None when it starts a new one. A decided key is not run again."""
    path = record_path(scenario.name, run_key["model"])
    if not path.exists():
        return None
    document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if _key_of(document) != run_key:
        return None
    if document["verdict"] != "undecided":
        raise HarnessError(
            f"{scenario.name}: already {document['verdict']} for this launch (evals/results/{path.name}) — "
            "more trials would be choosing a sample; change the sentence or the scenario to measure again"
        )
    return document


def transcripts_path(name: str, model: str) -> Path:
    return TRANSCRIPTS / f"{name}.{model}"


def prune() -> list[Path]:
    """Remove every transcript directory whose record is gone or was measured under another key."""
    removed: list[Path] = []
    for folder in sorted(TRANSCRIPTS.iterdir()) if TRANSCRIPTS.exists() else []:
        path = RESULTS / f"{folder.name}.json"
        key_file = folder / "key.json"
        if path.exists() and key_file.exists():
            kept = json.loads(key_file.read_text(encoding="utf-8"))
            if _key_of(json.loads(path.read_text(encoding="utf-8"))) == kept:
                continue
        shutil.rmtree(folder)
        removed.append(folder)
    return removed


class Ledger:
    """The record of one scenario under one key, written after every trial (devr 論点 X).

    A run's trials collect in the record's `pending` as each one ends; a run whose arms both reach
    the scenario's `trials` is closed into `runs` and judged. A run that stops — a launch that fails,
    an operator who interrupts — leaves its trials there, and the next run of the same key fills the
    rest rather than starting over: a trial that was taken is never drawn again. A launch that fails
    is not a trial, having nothing to score, but it is written to `aborted` before the run stops, so
    a failure that falls on one arm is in the record. A launch stopped at the timeout is a trial,
    and is named in `timed_out` beside its count. `template_lint` refuses a record with a `pending`
    run: it is not a count yet.
    """

    def __init__(self, scenario: Scenario, run_key: dict[str, Any]) -> None:
        self.scenario = scenario
        self.key = run_key
        self.path = record_path(scenario.name, run_key["model"])
        before = prior(scenario, run_key)
        if before is None:
            # A new record. Its transcripts start afresh: what is there belongs to another key, or
            # to a record of this one that was deleted.
            folder = transcripts_path(scenario.name, run_key["model"])
            if folder.exists():
                shutil.rmtree(folder)
            before = {"runs": []}
        self.runs: list[dict[str, Any]] = before["runs"]
        self.pending: dict[str, Any] = before.get("pending") or {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            **{arm: [] for arm in ARMS},
            "aborted": [],
            "timed_out": [],
        }

    def remaining(self, arm: str) -> int:
        return self.scenario.trials - len(self.pending[arm])

    def add(self, arm: str, trial: Trial) -> None:
        """Count one trial, or refuse it — and stop — when it is not a trial of this key."""
        if trial.digest != self.key["launch_digests"][arm]:
            raise HarnessError(f"{self.scenario.name} [{arm}]: the launch differed from the one this run was keyed on")
        if not trial.models:
            raise HarnessError("the launch did not report which model answered — not recorded")
        if set(trial.models) != {self.key["model"]}:
            raise HarnessError(f"asked for {self.key['model']}, answered by {', '.join(trial.models)} — not recorded")
        self.pending[arm].append(trial.passed)
        if trial.timed_out:
            self.pending["timed_out"].append({"arm": arm, "trial": len(self.pending[arm])})
        self._keep(arm, len(self.pending[arm]), trial)
        self._write()

    def abort(self, arm: str, reason: str) -> None:
        self.pending["aborted"].append({"arm": arm, "trial": len(self.pending[arm]) + 1, "reason": reason})
        self._write()

    def close(self) -> Path:
        """Close the run once both arms are complete, and judge every trial the record holds."""
        if any(self.remaining(arm) for arm in ARMS):
            raise HarnessError(f"{self.scenario.name}: the run is not complete — it cannot be closed")
        run = {
            "at": self.pending["at"],
            "trials": self.scenario.trials,
            **{arm: sum(self.pending[arm]) for arm in ARMS},
        }
        for named in ("aborted", "timed_out"):
            if self.pending[named]:
                run[named] = self.pending[named]
        self.runs.append(run)
        self.pending = {}
        self._write()
        prune()
        return self.path

    def _keep(self, arm: str, number: int, trial: Trial) -> None:
        folder = transcripts_path(self.scenario.name, self.key["model"])
        (folder / f"run-{len(self.runs) + 1}").mkdir(parents=True, exist_ok=True)
        (folder / "key.json").write_text(json.dumps(self.key, indent=2) + "\n", encoding="utf-8")
        stem = folder / f"run-{len(self.runs) + 1}" / f"{arm}-{number}"
        stem.with_suffix(".jsonl").write_text(trial.output, encoding="utf-8")
        score = {"passed": trial.passed, "timed_out": trial.timed_out, "calls": trial.calls}
        stem.with_suffix(".json").write_text(json.dumps(score, indent=2) + "\n", encoding="utf-8")

    def _write(self) -> None:
        document: dict[str, Any] = {
            "scenario": self.scenario.name,
            "guard": {
                "file": self.scenario.guard.file,
                "section": self.scenario.guard.section,
                "cr": self.scenario.guard.cr,
            },
            **self.key,
            "runs": self.runs,
        }
        if self.runs:
            trials = sum(r["trials"] for r in self.runs)
            passed = {arm: sum(r[arm] for r in self.runs) for arm in ARMS}
            p, verdict = judge(passed["with"], passed["without"], trials, len(self.runs))
            document.update(trials=trials, passed=passed, p=round(p, 4), verdict=verdict)
        else:
            document.update(trials=0, passed=dict.fromkeys(ARMS, 0), p=None, verdict="undecided")
        if self.pending:
            document["pending"] = self.pending
        RESULTS.mkdir(parents=True, exist_ok=True)
        # Written beside and moved over: a run interrupted mid-write keeps the record it had, rather
        # than a truncated one the next run cannot read and every trial in it with it. A kill the
        # process cannot see leaves the copy, and the next write of this record replaces it.
        written = self.path.with_name(self.path.name + ".tmp")
        try:
            written.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            os.replace(written, self.path)
        except BaseException:
            written.unlink(missing_ok=True)
            raise


def evaluate(
    scenario: Scenario,
    run_key: dict[str, Any],
    launch: Callable[[Path, list[str], float], Launched],
    *,
    timeout: float,
) -> Path:
    """Fill the key's run to the scenario's `trials` per arm, writing each trial as it ends, and close it."""
    ledger = Ledger(scenario, run_key)
    for arm in ARMS:
        while ledger.remaining(arm):
            number = scenario.trials - ledger.remaining(arm) + 1
            try:
                trial = run_arm(scenario, arm, launch, timeout, run_key["model"])
                ledger.add(arm, trial)
            except (Exception, KeyboardInterrupt) as exc:
                # Written, then raised: the run stops here and the next one resumes at this trial.
                ledger.abort(arm, (str(exc).splitlines() or [type(exc).__name__])[0])
                raise
            said = ("yes" if trial.passed else "no") + (" (timed out)" if trial.timed_out else "")
            print(f"  {scenario.name} [{arm}] trial {number}/{scenario.trials}: {said}", flush=True)
    return ledger.close()


def _profile(name: str) -> models.ExecutorProfile:
    config = store_mod.Store(repo_mod.Repo(ROOT)).read_config()
    profile = config.profiles.get(name) if config is not None else None
    if profile is None or profile.kind != "oci-agent":
        raise HarnessError(f"{name!r} is not a `kind: oci-agent` profile in this repository's .rein/config.yaml")
    return profile


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="run behaviour tests of rein's prompts against a real agent CLI")
    parser.add_argument("scenarios", nargs="*", help=f"scenario names (default: all — {', '.join(names())})")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("--profile", help="launch inside this `kind: oci-agent` profile of .rein/config.yaml")
    where.add_argument("--host", action="store_true", help="launch on this machine, with this user's CLI login")
    parser.add_argument("--model", required=True, help="the model the CLI is asked for, and recorded under")
    parser.add_argument("--timeout", type=float, default=900.0, help="seconds one launch may run")
    args = parser.parse_args(argv)
    try:
        launch = launch_host if args.host else launch_contained(_profile(args.profile))
        place = "host" if args.host else f"oci-agent:{args.profile}"
        cli = cli_version()
        for name in args.scenarios or names():
            scenario = load(name)
            run_key = key(
                scenario,
                model=args.model,
                cli=cli,
                where=place,
                timeout=args.timeout,
                digests=current_digests(scenario, args.model),
            )
            path = evaluate(scenario, run_key, launch, timeout=args.timeout)
            document = json.loads(path.read_text(encoding="utf-8"))
            counts = ", ".join(f"{arm} {document['passed'][arm]}/{document['trials']}" for arm in ARMS)
            print(f"{name}: {counts}, p {document['p']}: {document['verdict']} → {path.relative_to(ROOT)}")
    except (HarnessError, executors.ExecutorError, subprocess.SubprocessError) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    # Through the package, not this `__main__` copy: the scenarios import `evals.harness`, and two
    # copies of `Scenario` are two classes.
    from evals import harness

    raise SystemExit(harness.main())
