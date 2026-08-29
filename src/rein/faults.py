"""Which side a nonzero exit came from: the code under test, or the machine running it.

The build loop records verdicts it earned. A task status is evidence *about the task*; a
container runtime that is not installed, an agent CLI that is not on PATH, a session limit that
resets at 3am, a supervisor's SIGTERM — none of those are evidence about anything the
implementer wrote. Collapsing the two is how a task ends up `blocked` with a `task_failed` and a
`knowledge_gap` in the audit chain for a reason that has nothing to do with it, and the chain is
append-only: there is no taking it back.

So the distinction is drawn once, here, as a pure function of `(rc, output)` — the deterministic
side of build_loop's determinism boundary, testable without a subprocess:

  - :data:`Fault.CONTENT` — the command ran and its verdict is about the code.
  - :data:`Fault.ENV_TRANSIENT` — the machine failed in a way that time alone can fix. Retry.
  - :data:`Fault.ENV_PERMANENT` — the machine failed in a way that a re-run cannot fix. Stop and
    say what to repair.

**An agent launch never yields CONTENT.** Launching an implementer produces no quality-gate
verdict — the gate steps run afterwards, separately — so a nonzero launch rc is by construction
a statement about the machine, and the only open question is whether waiting would help. That is
what keeps the classification off heuristics at its core: for launches the rc alone decides the
category, and the text matching below only ever splits *transient from permanent*, never
"machine" from "code".

A cmd step is the opposite default: it exists to judge the code, so it is CONTENT unless the
command could not be run at all. The boundary is stated rather than papered over — `make test`
where `make` exists but an inner tool does not exits 127 from make itself, with none of the
markers below, and is classified CONTENT. Detecting that would mean parsing every build tool's
output, which is exactly the kind of guessing this module refuses to do.

One narrow exception: every sandboxed step runs with `network: none` (`executors.py`), so a step
that needs to resolve a hostname — an install step whose dependency closure was not baked into
the pinned image — fails the same way on every retry, which is a fact about the sandbox's network
policy, not the code. The markers for that (below) are OS/resolver strings (glibc's "Temporary
failure in resolving", Node's `ENOTFOUND`, curl's "Could not resolve host") that an application's
own test output does not plausibly produce on its own — narrow enough not to be the guessing this
module otherwise refuses.
"""

from __future__ import annotations

import enum
import json
import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: What :func:`rein.common.run` returns when the launch itself failed (OSError: no such file,
#: not executable, missing cwd). It is the *entire* output in that case, so anchoring at the
#: start plus the 127 rc makes a false positive from a command's own stdout implausible.
_UNLAUNCHABLE_PREFIX = "could not run "
_RC_UNLAUNCHABLE = 127

#: How much of a non-JSON tail is worth showing. Long enough for a stack trace's last
#: frames, short enough that nobody scrolls past it to find the sentence that matters.
_OUTPUT_TAIL = 1000

#: Signals that mean "something outside this process ended the launch": a supervisor's SIGTERM,
#: the OOM killer's SIGKILL, a closing terminal's SIGHUP. SIGINT is deliberately absent — a
#: human pressing Ctrl-C is a decision to stop, and a supervisor that read it as "retry later"
#: would restart exactly what they just stopped.
_EXTERNAL_SIGNALS = frozenset({1, 9, 15})  # SIGHUP, SIGKILL, SIGTERM

#: Agent-capacity exhaustion, as the CLIs report it. Only ever consulted to decide *transient*,
#: which is already the default for a launch — so a miss costs nothing but a slightly less
#: specific console line, and a false hit cannot turn a code failure into a machine one.
_CAPACITY_RE = re.compile(
    r"""
      \bsession\ limit\b
    | \busage\ limit\b
    | \brate[\ _-]?limit
    | \bquota\ (?:exceeded|exhausted)\b
    | \boverloaded_error\b
    | \btoo\ many\ requests\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: Credentials, not capacity. These do not resolve by waiting, and classifying them transient
#: would leave a supervisor loop retrying every 15 minutes forever against a CLI that will never
#: log itself in.
_UNAUTHENTICATED_RE = re.compile(
    r"""
      \binvalid\ api\ key\b
    | \bauthentication[_\ ]error\b
    | \bnot\ logged\ in\b
    | \bplease\ (?:run\ )?log\ ?in\b
    | \bunauthorized\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: A sandboxed step always runs with no network — these are the OS/tooling resolver's own words
#: for "could not reach the network", not a guess at what an application's test output means.
_NETWORK_UNREACHABLE_RE = re.compile(
    r"""
      \btemporary\ failure\ in\ resolving\b
    | \bname\ or\ service\ not\ known\b
    | \bnetwork\ is\ unreachable\b
    | \bcould\ not\ resolve\ host\b
    | \bnodename\ nor\ servname\ provided\b
    | \bENOTFOUND\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: The request did not fit. Deliberately a *predicate* and not a branch in :func:`classify_launch`:
#: whether waiting helps is not the same question as whether a fresh launch would. It never helps
#: — the same request will be the same size in an hour — but a *resumed* session that has outgrown
#: its window is fixed by relaunching cold, which is why the classifier keeps calling this
#: transient and the callers ask this separately before deciding what to do about it.
_CONTEXT_OVERFLOW_RE = re.compile(
    r"""
      \bprompt\ is\ too\ long\b
    | \bcontext[\ _-]?length[\ _-]?exceeded\b
    | \bmaximum\ context\ length\b
    | \bcontext\ window\ (?:exceeded|is\ full)\b
    | \binput\ (?:is\ )?too\ long\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

#: A capacity message usually carries the time it lifts ("resets 3:30am (Asia/Tokyo)").
_RESET_RE = re.compile(r"\bresets?\b[^\n]{0,72}", re.IGNORECASE)

#: The same message, read rather than quoted: the clock time and, when the CLI names one, the zone.
_RESET_AT_RE = re.compile(
    r"\bresets?\b\s*(?:at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*(?P<meridiem>am|pm)?"
    r"(?:\s*\((?P<zone>[A-Za-z][A-Za-z0-9_+\-/]*)\))?",
    re.IGNORECASE,
)

#: How long past a named reset to wait before trying again. Long enough that a clock a few seconds
#: out of step does not spend an attempt; short enough to be invisible next to a session limit.
RESET_MARGIN_SEC = 60


class Fault(enum.Enum):
    """Whose failure a nonzero exit was."""

    CONTENT = "content"
    ENV_TRANSIENT = "environment_transient"
    ENV_PERMANENT = "environment_permanent"

    @property
    def is_environment(self) -> bool:
        return self is not Fault.CONTENT


def _killed_externally(rc: int) -> bool:
    """Was the process ended by one of :data:`_EXTERNAL_SIGNALS`?

    Both spellings count. `subprocess` reports a signal death as a negative returncode, while a
    CLI that traps the signal and exits itself reports the shell's 128+N — the reported rc=143
    in the field was the latter, and the direct-kill case is the former.
    """
    if rc < 0:
        return -rc in _EXTERNAL_SIGNALS
    return rc > 128 and (rc - 128) in _EXTERNAL_SIGNALS


def _unlaunchable(rc: int, output: str) -> bool:
    return rc == _RC_UNLAUNCHABLE and output.startswith(_UNLAUNCHABLE_PREFIX)


def classify_launch(rc: int, output: str) -> Fault:
    """Classify a failed agent-CLI launch. Never returns :data:`Fault.CONTENT`.

    Call only with a nonzero `rc` — a launch that succeeded has nothing to classify.
    """
    if rc == 0:
        raise ValueError("classify_launch is for a failed launch (rc != 0)")
    if _unlaunchable(rc, output) or _UNAUTHENTICATED_RE.search(output):
        return Fault.ENV_PERMANENT
    # Everything else — capacity, signals, timeouts, and whatever a future CLI invents — is
    # treated as worth one more try. The launch budget is what stops that being unbounded.
    return Fault.ENV_TRANSIENT


def classify_step(rc: int, output: str) -> Fault:
    """Classify a failed quality-gate command step. CONTENT unless it could not be run at all.

    A timeout stays CONTENT on purpose: a test suite that hangs is a fact about the code, and
    the retry budget the step already carries is the right place for it. It never reaches the
    signal check below either, because :func:`rein.common.run` returns its own
    :data:`~rein.common.RC_TIMEOUT` rather than the rc of the group kill it performs — so
    "we stopped it" and "something else stopped it" stay distinguishable.

    A DNS/network-unreachable failure is ENV_PERMANENT, not ENV_TRANSIENT: `network: none` is a
    fixed sandbox policy, so the step fails the same way on every retry until the dependency is
    baked into the pinned image — waiting does not help, unlike a launch's capacity limit.

    A step killed from outside is ENV_TRANSIENT, and this is the reading that was missing:
    :func:`_killed_externally` existed for a reported rc=143 in the field and nothing consulted
    it, so the OOM killer, a supervisor's SIGTERM and a closing terminal all charged the step's
    retry budget and were recorded as facts about the code. A container the kernel killed for
    exceeding `memory_mb` also arrives here as 137, and that *is* partly about the code — but
    the honest direction is the one that costs a re-run rather than a wrong verdict, so it reads
    as the machine's failure and the console line names the limit.
    """
    if rc == 0:
        raise ValueError("classify_step is for a failed step (rc != 0)")
    if _unlaunchable(rc, output) or is_network_unreachable(output):
        return Fault.ENV_PERMANENT
    if _killed_externally(rc):
        return Fault.ENV_TRANSIENT
    return Fault.CONTENT


def is_capacity(output: str) -> bool:
    """Does this output look like the agent ran out of capacity rather than out of luck?"""
    return bool(_CAPACITY_RE.search(output))


def is_network_unreachable(output: str) -> bool:
    """Does this output look like a DNS/connection failure from a `network: none` sandbox?"""
    return bool(_NETWORK_UNREACHABLE_RE.search(output))


def is_context_overflow(output: str) -> bool:
    """Did the launch fail because the request did not fit the model's window?

    Worth asking on its own because it is the one machine failure that **re-running identically
    cannot fix**: an unattended retry loop would spend the same request, at the same size, for the
    same answer, forever. What can fix it is sending less — a narrower review scope, or a cold
    launch in place of a resumed session that has outgrown its window.
    """
    return bool(_CONTEXT_OVERFLOW_RE.search(output))


def reset_hint(output: str) -> str:
    """The CLI's own words about when capacity returns ("" when it said nothing).

    Quoted for the console. What *schedules* against it is :func:`reset_at`, which reads the same
    sentence — the two are separate because a quote can be wrong in ways a human notices and a
    schedule cannot.
    """
    match = _RESET_RE.search(said(output))
    return match.group(0).strip() if match else ""


def reset_at(output: str, *, now: datetime | None = None) -> datetime | None:
    """When the CLI said capacity returns, as a moment (None when it named none it could read).

    "Quoted, never parsed" was the rule and it was written for the *review's evidence*, where it
    is right: nothing about a verdict may rest on a sentence a model produced. A retry delay is not
    evidence. `--supervise` exists precisely because nobody is at the console, and it was extracting
    this time, printing it, and sleeping a fixed fifteen minutes anyway — one field run made seven
    attempts, each refused in about 0.4 seconds, and the host session ended before the reset it had
    printed seven times.

    The next occurrence of the named clock time, in the named zone when it resolves and locally
    otherwise. A zone this machine does not know is dropped rather than guessed at: the local
    reading is wrong by whole hours at worst, and `_supervise` clamps how long it will wait either
    way.
    """
    match = _RESET_AT_RE.search(said(output))
    if not match:
        return None
    hour, minute = int(match.group("hour")), int(match.group("minute") or 0)
    meridiem = (match.group("meridiem") or "").lower()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    tz: tzinfo | None = None
    if match.group("zone"):
        try:
            tz = ZoneInfo(match.group("zone"))
        except (ZoneInfoNotFoundError, ValueError):
            tz = None
    current = (now or datetime.now(timezone.utc)).astimezone(tz)
    target = current.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target if target > current else target + timedelta(days=1)


def said(output: str) -> str:
    """What the launch reported, out of whatever the CLI wrote to its streams.

    The agent CLIs are launched with a JSON output format, so the reason a run failed arrives as a
    field — and every reader here was taking a **byte slice of the tail** instead. What an operator
    actually saw:

        [supervise] attempt 1: the comparator adapter exited 1, saying:
        nput_tokens":0,"cache_creation_input_tokens":0,...,"api_error_status":429,"result":"You've
        hit your session limit · resets 6:50am (Asia/Tokyo)","type":"result",...

    — the reason buried in ~900 bytes of telemetry, the message starting mid-token, and
    :func:`reset_hint`'s regex then matching *into* the trailing JSON so that the hint itself
    carried `","type":"result","duration_ms":412`. Reading the field costs one `json.loads` and
    gives every reader below one sentence instead of a slice.
    """
    envelope = _envelope(output)
    if envelope is None:
        return output.strip()[-_OUTPUT_TAIL:]
    for key in ("result", "error", "message", "subtype"):
        value = envelope.get(key)
        if isinstance(value, str) and value:
            return value
    return output.strip()[-_OUTPUT_TAIL:]


def status_code(output: str) -> int | None:
    """The HTTP status the CLI attributed the failure to, when it named one.

    This is what "would waiting help?" is really a question about: 429 is capacity, 401 is a
    credential, and both used to be decided by matching English in a truncated string.
    """
    envelope = _envelope(output)
    value = envelope.get("api_error_status") if envelope else None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _envelope(output: str) -> dict[str, object] | None:
    """The CLI's JSON envelope, or None for output that is not one."""
    try:
        loaded = json.loads(output.strip())
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


class EnvironmentFault(Exception):
    """The machine failed, so nothing was learned about the code.

    Deliberately **not** a :class:`rein.common.StopLoop`. StopLoop means "stop, a human has to
    look at this task"; this means "stop, nothing here is about a task at all". Letting the
    second be caught as the first is the whole defect this type exists to prevent, so they are
    siblings and every `except StopLoop` in the loop keeps ignoring this one.
    """

    def __init__(self, fault: Fault, *, where: str, rc: int, output: str) -> None:
        self.fault = fault
        self.where = where
        self.rc = rc
        self.output = said(output)
        super().__init__(self.summary())

    @property
    def retryable(self) -> bool:
        return self.fault is Fault.ENV_TRANSIENT

    def summary(self) -> str:
        """One console-ready paragraph: what failed, why, and what the reader should do."""
        reset = reset_hint(self.output)
        network = is_network_unreachable(self.output)
        killed = _killed_externally(self.rc)
        if is_capacity(self.output):
            cause = "agent capacity"
        elif killed:
            cause = f"killed from outside (rc={self.rc})"
        else:
            cause = f"rc={self.rc}"
        if network:
            advice = (
                "The sandbox runs with network: none, so this fails the same way on every "
                "retry — bake the dependency into the pinned image (see the packaged "
                "Containerfiles) rather than resolving it at test time."
            )
        elif killed:
            advice = (
                "Nothing here is about the code: a signal ended it. Nothing was marked, so "
                "re-running `rein build` continues from the preserved work. If this repeats at "
                "the same point, the OOM killer is the usual cause — raise the profile's "
                "`memory_mb` in .rein/config.yaml rather than spending the step's retries on it."
            )
        elif self.retryable:
            advice = (
                "Nothing was marked: every task keeps the status and the retry budget it had. "
                "Re-run `rein build` when it clears — it resumes from the preserved work."
            )
        else:
            advice = "Re-running will not help until this is repaired."
        lines = [f"{self.where}: {cause} — the launch never produced a quality-gate verdict.", advice]
        if reset:
            lines.append(f"The CLI said: {reset}")
        if self.output:
            lines.append(self.output)
        return "\n".join(lines)
