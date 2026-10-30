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

**A record is about the launch it measured (devr 論点 T).** Its key is each arm's launch digest —
the command, the prompt and the repository the agent was handed — the scenario's own digest — its
fixture and its score — the model, the CLI's version and where it ran. `template_lint` rebuilds the
launches from the sources as they stand and refuses a record whose digests no longer match: a count
measured on a prompt that has since changed is not evidence about the one that ships.

**Judged by the difference, and trials are added, never re-drawn.** Each run spends the scenario's
own `trials` per arm. A run whose key matches the record adds to it rather than replacing it, so
running again cannot discard the trials that came out the wrong way. The arms are compared by a
one-sided Fisher exact test at `ALPHA` per look, over at most `LOOKS` looks — two at 0.025 keep the
whole under 0.05. The verdict is `holds`, `not-reproduced` (the arm without the sentence never
failed: the scenario did not produce what the sentence prevents), `undecided` (look again) or
`does-not-hold`; once one other than `undecided` is reached, the key is not run again. A record
whose key differs replaces the last one for that scenario and model. The scratch repositories and
transcripts are deleted once the record is written.
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

from rein import adapters, executors, models, usage
from rein import repo as repo_mod
from rein import store as store_mod

EVALS = Path(__file__).resolve().parent
ROOT = EVALS.parent
RESULTS = EVALS / "results"
SCENARIOS = EVALS / "scenarios"
ARMS = ("with", "without")
ALPHA = 0.025
LOOKS = 2
VERDICTS = ("holds", "not-reproduced", "undecided", "does-not-hold")

#: The tools a scenario's launch may use without asking. A headless `-p` launch cannot answer a
#: permission prompt, so anything not named here is refused — which is what keeps a scenario to
#: what it says it exercises.
ALLOWED = (
    "Read",
    "Edit",
    "Write",
    "Glob",
    "Grep",
    "Bash(rein:*)",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git status:*)",
    "Bash(git show:*)",
    "Bash(git add:*)",
    "Bash(git commit:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
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
    #: Where the guarded file goes in the repository, when the agent is sent to read it rather than
    #: handed it in the prompt (`implementer.md` is read from `.rein/prompts/`). The harness writes
    #: it there, with the arm applied, before the fixture runs.
    carries: str = ""
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


def command(prompt: str, model: str) -> list[str]:
    claude = adapters.ADAPTER_TABLE["claude"]
    settings = json.dumps({"permissions": {"allow": list(ALLOWED)}})
    return [
        *claude.argv,
        *claude.config_isolation,
        *claude.usage_flags,
        "--settings",
        settings,
        "--model",
        model,
        prompt,
    ]


def stage(scenario: Scenario, arm: str, root: Path, model: str) -> list[str]:
    """Lay `arm` of the scenario out in the empty directory `root`, and return the command that launches it."""
    if scenario.carries:
        # Written before the fixture runs, so whatever the fixture commits already holds it:
        # nothing in the repository's history says which arm this is.
        source = (ROOT / scenario.guard.file).read_text(encoding="utf-8")
        (root / scenario.carries).parent.mkdir(parents=True, exist_ok=True)
        (root / scenario.carries).write_text(arm_prompt(source, scenario.guard.sentence, arm), encoding="utf-8")
    prepare(root, scenario)
    if scenario.carries:
        prompt = scenario.prompt(root)
    elif scenario.control is not None:
        prompt = arm_prompt(scenario.prompt(root), scenario.guard.sentence, "with")
        prompt = prompt if arm == "with" else scenario.control(root)
    else:
        prompt = arm_prompt(scenario.prompt(root), scenario.guard.sentence, arm)
    return command(prompt, model)


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


def launch_host(root: Path, argv: list[str], timeout: float) -> Launched:
    env = {**os.environ, "PATH": f"{root / '.evalbin'}{os.pathsep}{os.environ.get('PATH', '')}"}
    done = subprocess.run(
        argv, cwd=root, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout
    )
    return _read(done.returncode, done.stdout + done.stderr if done.returncode else done.stdout)


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
        return _read(result.exit_code, result.output)

    return run


def _read(rc: int, output: str) -> Launched:
    if rc != 0:
        return Launched(rc, output, ())
    _, spent = usage.parse_claude_envelope(output)
    return Launched(rc, output, spent.models)


def run_arm(
    scenario: Scenario, arm: str, launch: Callable[[Path, list[str], float], Launched], timeout: float, model: str
) -> tuple[bool, str, tuple[str, ...]]:
    """One trial of one arm: `(passed, launch digest, models that answered)`.

    The digest is taken before the launch, over what the agent is handed. The scratch repository is
    removed whatever happened: a run's record is the count, and a transcript kept past it is a
    second record nobody reads.
    """
    root = Path(tempfile.mkdtemp(prefix=f"rein-eval-{scenario.name}-"))
    try:
        argv = stage(scenario, arm, root, model)
        digest = launch_digest(root, argv)
        launched = launch(root, argv, timeout)
        if launched.rc != 0:
            raise HarnessError(
                f"{scenario.name} [{arm}]: the launch failed (rc {launched.rc}):\n{launched.output[-2000:]}"
            )
        return scenario.score(root, calls(root)), digest, launched.models
    finally:
        shutil.rmtree(root, ignore_errors=True)


def cli_version() -> str:
    return subprocess.run(["claude", "--version"], capture_output=True, text=True, check=True).stdout.strip()


def record_path(name: str, model: str) -> Path:
    return RESULTS / f"{name}.{model}.json"


def key(scenario: Scenario, *, model: str, cli: str, where: str, digests: dict[str, str]) -> dict[str, Any]:
    """What a record is about. Two runs with the same key measured the same thing, and add up."""
    if not cli:
        raise HarnessError("the CLI's version is unknown — the run cannot be recorded")
    return {
        "model": model,
        "cli": cli,
        "where": where,
        "scenario_digest": scenario_digest(scenario.name),
        "launch_digests": {arm: digests[arm] for arm in ARMS},
    }


def _key_of(document: dict[str, Any]) -> dict[str, Any]:
    return {k: document[k] for k in ("model", "cli", "where", "scenario_digest", "launch_digests")}


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


def record(scenario: Scenario, run_key: dict[str, Any], outcome: dict[str, Any]) -> Path:
    """Add the run to the record its key names, or start one; write the verdict over every trial."""
    answered = {m for arm in ARMS for m in outcome[arm]["models"]}
    if not answered:
        raise HarnessError("no launch reported which model answered — the run cannot be recorded")
    if answered != {run_key["model"]}:
        raise HarnessError(f"asked for {run_key['model']}, answered by {', '.join(sorted(answered))} — not recorded")
    for arm in ARMS:
        if outcome[arm]["digest"] != run_key["launch_digests"][arm]:
            raise HarnessError(f"{scenario.name} [{arm}]: the launch differed from the one this run was keyed on")
    before = prior(scenario, run_key)
    runs = [
        *(before["runs"] if before else []),
        {
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "trials": scenario.trials,
            **{arm: outcome[arm]["passed"] for arm in ARMS},
        },
    ]
    trials = sum(r["trials"] for r in runs)
    passed = {arm: sum(r[arm] for r in runs) for arm in ARMS}
    p, verdict = judge(passed["with"], passed["without"], trials, len(runs))
    document = {
        "scenario": scenario.name,
        "guard": {"file": scenario.guard.file, "section": scenario.guard.section, "cr": scenario.guard.cr},
        **run_key,
        "runs": runs,
        "trials": trials,
        "passed": passed,
        "p": round(p, 4),
        "verdict": verdict,
    }
    path = record_path(scenario.name, run_key["model"])
    RESULTS.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def evaluate(
    scenario: Scenario, launch: Callable[[Path, list[str], float], Launched], *, model: str, timeout: float
) -> dict[str, Any]:
    outcome: dict[str, Any] = {}
    trials = scenario.trials
    for arm in ARMS:
        passed = 0
        digests: set[str] = set()
        seen: set[str] = set()
        for trial in range(trials):
            ok, digest, answered = run_arm(scenario, arm, launch, timeout, model)
            passed += int(ok)
            digests.add(digest)
            seen.update(answered)
            print(f"  {scenario.name} [{arm}] trial {trial + 1}/{trials}: {'yes' if ok else 'no'}", flush=True)
        if len(digests) != 1:
            raise HarnessError(f"{scenario.name} [{arm}]: the prompt differed between trials")
        outcome[arm] = {"passed": passed, "digest": digests.pop(), "models": sorted(seen)}
    return outcome


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
                scenario, model=args.model, cli=cli, where=place, digests=current_digests(scenario, args.model)
            )
            prior(scenario, run_key)  # refuses a decided key before anything is launched
            outcome = evaluate(scenario, launch, model=args.model, timeout=args.timeout)
            path = record(scenario, run_key, outcome)
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
