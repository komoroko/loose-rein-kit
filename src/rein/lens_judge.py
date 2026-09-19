"""Deciding the lens conditions that cannot be read off the plan.

Every lens carries a condition, and `lenses.py` sorts them by how confidently that condition can
be decided. `standard` is decidable from the plan — paths, claim counts, named facts — and
`conditional` is the class for the other kind: **there is a condition, and settling it takes
reading the deliverable.** Six of the packaged lenses say so in their own words. "Nothing in the
plan says whether they do; it takes reading them." "The plan cannot see that; the design document
can." "Whether it does takes reading the diff."

Nothing read them. The `when:` block on those six is `min_claims: 1` or `min_tasks: 1` — true of
any cycle that reaches a gate — so what actually decided them was the human at the mandate, and
**the mandate is before the deliverable exists.** A person approving a plan has no design document,
no tickets and no diff to read, which is exactly what `applies_when` said the answer required. The
condition was written down, in prose, and the thing that could answer it was missing.

This module is that thing, and the shape of it is set by what it must not become:

* **It answers, it does not write conditions.** The question put to it *is* `applies_when`,
  unchanged. A second field holding a differently-worded question for the machine would be the
  same claim in two places, and one of them would go stale. If the prose is not good enough to
  decide against, the prose is what to fix.
* **It reaches `conditional` only.** Not `standard`, whose licence to run without asking anybody
  is a machine-decidable condition — "ask a model" in a `when:` block is the empty `when:` block
  all over again, and `lenses._lens` refuses that one. Not `unclassified` either: a lens qualifies
  by coming back a second time, and the reason is that **one occurrence does not tell you the
  condition**. A prose question can be written from one occurrence, so a route from here into that
  class would retire the qualification rule by making it unnecessary.
* **It can only remove.** A verdict never adds a lens to the frozen selection. What the mandate
  approved is the ceiling, and the audit guarantee — that no lens reaches a reviewer without a
  human having seen it on the approval screen — does not depend on anything here.
* **It is not a gate input.** With no decider configured, unreachable, or answering nothing
  intelligible, every conditional lens stays a candidate and the cycle proceeds. The degradation
  is toward *more* review, which costs tokens and finds nothing; the opposite degradation would
  cost findings. `rein build` never depends on it.
* **It asks once.** The verdict is recorded in the audit chain and read back from there, the same
  way the selection itself is resolved once and read back. A reviewer asking twice and getting two
  answers would put the review's inputs back at the mercy of machine-local state, which is the
  property the freeze exists to hold.

**The decider is a command, not an HTTP call.** Nothing in `rein` opens a socket: git, `gh`, the
agent CLIs and the container runtime are all subprocesses, and a typed-decision service is reached
the same way — an argv array, never a shell string, exactly as `quality_gate` steps are. That keeps
the vendor, the credential and the retry policy outside this repository, where `20-open.md` item 15
can still change its mind about all three.

The wire shape is the one a typed-decision model already takes: one *state* and a set of named
questions, each answered independently against it, each answer a probability. On stdin::

    {"state": "<the deliverable>",
     "questions": {"L-CODE-CONCURRENCY": {"type": "noul", "instructions": "<applies_when>"}}}

and on stdout::

    {"answers": {"L-CODE-CONCURRENCY": {"probability": 0.82}}}

An answer this cannot read is `unavailable`, never `false`. **A missing record and a negative
answer are different facts**, and collapsing them is how a decider that was never reachable comes
to look like one that decided everything was irrelevant.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger(__name__)

#: A condition this deliverable meets — the lens is worth applying.
HOLDS = "holds"
#: A condition this deliverable does not meet. Only this one can drop a lens, and only when the
#: settings say a verdict may.
DOES_NOT_HOLD = "does_not_hold"
#: Nothing was asked, or nothing intelligible came back. Deliberately its own value rather than a
#: falsy `does_not_hold`: a cycle whose decider was down has no verdicts, and a tally that cannot
#: tell that from "no lens applied" reads an outage as a finding about the library.
UNAVAILABLE = "unavailable"
OUTCOMES = frozenset({HOLDS, DOES_NOT_HOLD, UNAVAILABLE})

#: What one deliverable may be sent as, in UTF-8 bytes — which is what a transport carries, and
#: what a design document written in a language outside ASCII is three times as many of per
#: character. Past this the state is the wrong size for one decision and the honest answer is
#: `unavailable`: a truncated design document answers a different question than the one
#: `applies_when` asks, and answers it confidently.
MAX_STATE = 200_000


@dataclass(frozen=True)
class Settings:
    """What `review_policy.lens_judgement` says, reduced to what this module acts on.

    `may_drop` is separate from `command` because judging and acting on the judgement are separate
    decisions. Until the verdicts have been read beside the human's own calls at the gate — the one
    period in which both exist, since it ends the moment the model replaces the person — what this
    is for is the record, not the removal.
    """

    command: tuple[str, ...] = ()
    threshold: float = 0.5
    may_drop: bool = False
    timeout: int = 60

    @property
    def configured(self) -> bool:
        return bool(self.command)

    @classmethod
    def of(cls, config: Any) -> Settings:
        """Read off a `models.Config`. Anything absent leaves the default, which decides nothing."""
        raw = getattr(config, "lens_judgement", None) if config is not None else None
        if not isinstance(raw, Mapping):
            return cls()
        command = raw.get("command")
        threshold = raw.get("threshold")
        timeout = raw.get("timeout_seconds")
        return cls(
            command=tuple(str(part) for part in command) if isinstance(command, list) else (),
            threshold=float(threshold)
            if isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            else 0.5,
            may_drop=raw.get("may_drop") is True,
            timeout=timeout if isinstance(timeout, int) and not isinstance(timeout, bool) else 60,
        )


@dataclass(frozen=True)
class Verdict:
    """One lens, against one deliverable.

    `probability` is kept even when it did not change the outcome, and especially then. A threshold
    is a knob somebody has to be able to move, and the only thing that makes moving it an informed
    act is the distribution it would have been applied to — recording the side of the line a lens
    fell on records nothing about how far.
    """

    lens_id: str
    outcome: str
    probability: float | None = None
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"lens": self.lens_id, "outcome": self.outcome}
        if self.probability is not None:
            out["probability"] = round(self.probability, 4)
        if self.reason:
            out["reason"] = self.reason
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> Verdict | None:
        lens_id, outcome = str(raw.get("lens", "")), str(raw.get("outcome", ""))
        if not lens_id or outcome not in OUTCOMES:
            return None
        probability = raw.get("probability")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            return cls(lens_id=lens_id, outcome=outcome)
        return cls(lens_id=lens_id, outcome=outcome, probability=float(probability))


class Transport(Protocol):
    """How the request reaches whatever answers it. A seam, so the tests never need a network."""

    def __call__(self, command: Sequence[str], payload: str, *, timeout: int) -> str: ...


def run_command(command: Sequence[str], payload: str, *, timeout: int) -> str:
    """The shipped transport: argv in, JSON on stdin, JSON on stdout.

    No shell. `quality_gate` takes its commands as argv arrays for the same reason — a pipe or a
    redirect has to be visible in a script somebody can read, not hidden in a string this file
    hands to `sh`.
    """
    result = subprocess.run(  # noqa: S603 - argv, never a shell string; the schema forbids one
        list(command),
        input=payload,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"exit {result.returncode}: {(result.stderr or '').strip()[:200]}")
    return result.stdout


def request(state: str, questions: Mapping[str, str]) -> str:
    """The payload, in the shape a typed-decision model already takes.

    One state, many questions, each asked against it independently — which is why the whole stage
    goes in one call and why adding a lens costs a question rather than another reading.
    """
    return json.dumps(
        {
            "state": state,
            "questions": {
                lens_id: {"type": "noul", "instructions": prose} for lens_id, prose in sorted(questions.items())
            },
        }
    )


def _answers(raw: str) -> dict[str, float]:
    """`{lens_id: probability}` out of a reply, or `{}` if it is not the documented shape.

    Strict on purpose. A reply this guesses at is a verdict nobody can trace back to what was
    asked, and the cost of refusing one is a cycle that reviews too much.
    """
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    answers = body.get("answers") if isinstance(body, Mapping) else None
    if not isinstance(answers, Mapping):
        return {}
    out: dict[str, float] = {}
    for lens_id, answer in answers.items():
        value = answer.get("probability") if isinstance(answer, Mapping) else None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # A probability is a finite number in [0, 1], and being one is what makes the comparison
        # against a threshold mean anything. `json.loads` accepts `NaN` and `Infinity` by default,
        # and NaN compares false against every threshold — so a decider answering with one would
        # come back as `does_not_hold` and drop the lens, while carrying a value the audit chain
        # has no canonical form for. Anything outside the range is the same class of answer: not a
        # probability, and there is no honest way to read one out of it.
        if value != value or value in (float("inf"), float("-inf")) or not 0.0 <= value <= 1.0:
            logger.warning(f"the lens decider answered {lens_id} with {value!r}, which is not a probability")
            continue
        out[str(lens_id)] = float(value)
    return out


def judge(
    settings: Settings,
    *,
    state: str,
    questions: Mapping[str, str],
    transport: Transport = run_command,
) -> list[Verdict]:
    """One verdict per question, in id order. Every failure path ends in `unavailable`.

    Never raises. Everything this can be handed — no decider, a command that does not exist, one
    that times out, one that answers with a web page — is a reason to review more than necessary,
    and none of them is a reason to stop the cycle.

    Whether there is a deliverable to decide against is the caller's question, not this one's: a
    hand-off with nothing to read has nothing to record either, and `lens_cmd._judge_handoff` is
    where that difference can be acted on.
    """
    if not questions:
        return []
    ordered = sorted(questions)
    if not settings.configured:
        return [Verdict(lens_id, UNAVAILABLE, reason="no decider configured") for lens_id in ordered]
    if len(state.encode("utf-8")) > MAX_STATE:
        return [
            Verdict(lens_id, UNAVAILABLE, reason=f"the deliverable is over {MAX_STATE} bytes") for lens_id in ordered
        ]
    try:
        raw = transport(settings.command, request(state, questions), timeout=settings.timeout)
    except (OSError, subprocess.SubprocessError, RuntimeError, ValueError) as exc:
        logger.warning(
            f"the lens decider did not answer ({exc}) — every conditional lens stays a candidate, "
            "which reviews more than necessary and stops nothing."
        )
        return [Verdict(lens_id, UNAVAILABLE, reason="the decider did not answer") for lens_id in ordered]
    answers = _answers(raw)
    if not answers:
        logger.warning("the lens decider's reply had no readable answers — every conditional lens stays a candidate")
    out: list[Verdict] = []
    for lens_id in ordered:
        probability = answers.get(lens_id)
        if probability is None:
            out.append(Verdict(lens_id, UNAVAILABLE, reason="no answer for this lens, or not a probability"))
            continue
        outcome = HOLDS if probability >= settings.threshold else DOES_NOT_HOLD
        out.append(Verdict(lens_id, outcome, probability=probability))
    return out


def dropped(verdicts: Sequence[Verdict], settings: Settings) -> set[str]:
    """The ids a verdict removes from this hand-off — empty unless the settings allow it.

    `may_drop` off is the shipped default and the whole of the difference between recording a
    judgement and acting on one.
    """
    if not settings.may_drop:
        return set()
    return {v.lens_id for v in verdicts if v.outcome == DOES_NOT_HOLD}
