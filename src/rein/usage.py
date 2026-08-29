"""What a launch actually cost, as the agent CLI reports it — not as this process estimates it.

Both spend counters in this codebase measured bytes, and both said why: "a token count belongs to
a tokenizer nobody here owns, and reporting an estimate as a measurement is the habit this
codebase is built against." That reasoning is right about an *estimate* and wrong about a
*report*. The CLIs know, and they will say so on request: `claude -p --output-format json` returns
an envelope carrying the input, output, cached and reasoning token counts, the model id that
answered, and the cost — beside the answer itself, under `result`.

Bytes on stdin were never the number anyone wanted. They cannot see the system prompt, the
project instructions the CLI loads on its own, or the cache: a probe of a one-word prompt came
back with 10 input tokens and **20,956 cached ones**, all of it context this process never sent.
A measurement that cannot see that cannot answer what a cycle cost or where it went.

**An adapter that does not report usage records nothing, never zero.** `Usage.unavailable()` is a
state with a name, the same rule the Coverage Manifest follows (plan §2.4): "we did not measure"
and "it was free" must never render the same. Only `claude` is wired here, because it is the only
one whose envelope this release has actually seen.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rein import common

#: The flags that make an adapter answer with a usage envelope instead of bare text.
CLAUDE_JSON_FLAGS: tuple[str, ...] = ("--output-format", "json")


class AdapterEnvelopeError(Exception):
    """An adapter answered with something that is not the envelope its flags promised."""


@dataclass(frozen=True)
class Usage:
    """One launch's measured cost. `available` is False when the adapter does not report it.

    Every count is what the provider billed, not what this process guessed. `cache_read` and
    `cache_creation` are kept apart from `input` because they price differently and because their
    size is the evidence for how much context arrives from outside this process entirely.
    """

    available: bool = False
    launches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    reasoning_tokens: int = 0
    cost_usd: float = 0.0
    #: Model ids that answered, sorted. More than one when a run spanned a model switch.
    models: tuple[str, ...] = ()

    @classmethod
    def unavailable(cls) -> Usage:
        """A launch whose cost this release cannot read. Counted, never priced."""
        return cls(available=False, launches=1)

    @property
    def total_input_tokens(self) -> int:
        """Everything the model read, however it was billed."""
        return self.input_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def __add__(self, other: Usage) -> Usage:
        """Merge two launches. An unreported one still counts as a launch and prices nothing.

        `available` is the OR, so a role with one reported launch and one unreported reports what
        it knows — and `launches` is what shows the rest is missing rather than free.
        """
        return Usage(
            available=self.available or other.available,
            launches=self.launches + other.launches,
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_read_tokens=self.cache_read_tokens + other.cache_read_tokens,
            cache_creation_tokens=self.cache_creation_tokens + other.cache_creation_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
            cost_usd=self.cost_usd + other.cost_usd,
            models=tuple(sorted(set(self.models) | set(other.models))),
        )

    @classmethod
    def from_detail(cls, detail: Mapping[str, Any]) -> Usage:
        """A `Usage` back out of the shape `to_detail` wrote. Unmeasured stays unmeasured.

        The inverse belongs beside the thing it inverts. It was written in `review_cache`, next to
        the one caller that needed it, along with a fifth copy of the int coercion below.
        """
        if detail.get("measured") is not True:
            return cls(available=False, launches=_int(detail, "launches"))
        cost = detail.get("cost_usd")
        seen = detail.get("models")
        return cls(
            available=True,
            launches=_int(detail, "launches"),
            input_tokens=_int(detail, "input_tokens"),
            output_tokens=_int(detail, "output_tokens"),
            cache_read_tokens=_int(detail, "cache_read_tokens"),
            cache_creation_tokens=_int(detail, "cache_creation_tokens"),
            reasoning_tokens=_int(detail, "reasoning_tokens"),
            cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
            models=tuple(str(m) for m in seen) if isinstance(seen, list) else (),
        )

    def to_detail(self) -> dict[str, Any]:
        """The shape an audit event carries. Absent when nothing was measured — not a row of zeros."""
        if not self.available:
            return {"launches": self.launches, "measured": False}
        return {
            "launches": self.launches,
            "measured": True,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cost_usd": round(self.cost_usd, 6),
            "models": list(self.models),
        }


def _int(source: Any, *keys: str) -> int:
    """The int at `keys` under `source`, or 0 — an envelope's nesting walked before the coercion."""
    value: Any = source
    for key in keys:
        if not isinstance(value, dict):
            return 0
        value = value.get(key)
    return common.as_int(value)


def parse_claude_envelope(output: str) -> tuple[str, Usage]:
    """`(the answer, what it cost)` from a `claude -p --output-format json` envelope.

    Raises :class:`AdapterEnvelopeError` when the envelope says the run failed. That matters
    beyond bookkeeping: `is_error` can be set on a process that exited 0, so without this check a
    failure would reach the stage validators as an unparseable answer and be reported as the
    reviewer's fault. The CLI's own words travel with the error.
    """
    try:
        envelope = json.loads(output)
    except ValueError as exc:
        raise AdapterEnvelopeError(f"the adapter was asked for JSON and did not answer with any: {exc}") from None
    if not isinstance(envelope, dict):
        raise AdapterEnvelopeError("the adapter's JSON is not an object")
    if envelope.get("is_error") is True or envelope.get("subtype") not in (None, "success"):
        said = str(envelope.get("result") or envelope.get("subtype") or "")
        raise AdapterEnvelopeError(
            f"the adapter reported a failed run: {said}" if said else "the adapter reported a failed run"
        )
    answer = envelope.get("result")
    if not isinstance(answer, str):
        raise AdapterEnvelopeError("the adapter's envelope carries no `result`")

    usage_block = envelope.get("usage")
    usage_block = usage_block if isinstance(usage_block, dict) else {}
    by_model = envelope.get("modelUsage")
    models = tuple(sorted(by_model)) if isinstance(by_model, dict) else ()
    cost = envelope.get("total_cost_usd")
    return answer, Usage(
        available=True,
        launches=1,
        input_tokens=_int(usage_block, "input_tokens"),
        output_tokens=_int(usage_block, "output_tokens"),
        cache_read_tokens=_int(usage_block, "cache_read_input_tokens"),
        cache_creation_tokens=_int(usage_block, "cache_creation_input_tokens"),
        reasoning_tokens=_int(usage_block, "output_tokens_details", "thinking_tokens"),
        cost_usd=float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else 0.0,
        models=models,
    )


def _tokens(count: int) -> str:
    """A token count a reader can act on: exact while small, then thousands, then millions.

    `0.0k` was what 41 output tokens rendered as, which is the same thing "we did not measure"
    should look like and must not. The millions tier is the other end of the same fault: a cycle
    summed over its runs renders as `3067.3k`, which a reader has to divide before it means
    anything, in a report whose whole job is to be read at a glance.
    """
    if count < 10_000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}k"
    return f"{count / 1_000_000:.2f}M"


def summarize(rows: dict[str, Usage], *, what: str, charged: bool = True) -> str:
    """One line saying where this run's tokens went, worst first. Empty when nothing was launched.

    A role whose adapter does not report usage is listed by name with its launch count and no
    numbers, because that is the honest rendering: leaving it out would make the total read as the
    whole truth, and printing zeros would make an unmeasured role look free.

    `charged=False` is for tokens that were *replayed* rather than launched — a stage served from
    `review_cache`. The counts are real (they are what that answer cost when it was first taken),
    the price is not: nobody paid it on this run. One phrasing for both facts printed a dollar
    figure on the line whose entire reason for being separate is that it is not a bill.

    Cache creation is named beside cache reads because it is the expensive half and was in
    neither number a reader could see: a role showing `3.07M in (1.10M cached)` left 727k of
    premium-priced cache writes unaccounted for, in the report meant to find exactly that.
    """
    live = {role: row for role, row in rows.items() if row.launches}
    if not live:
        return ""
    measured = {role: row for role, row in live.items() if row.available}
    blind = sorted(role for role, row in live.items() if not row.available)

    parts: list[str] = []
    if measured:
        total = Usage()
        for row in measured.values():
            total = total + row
        for role, row in sorted(measured.items(), key=lambda item: -item[1].total_input_tokens):
            cached = f"{_tokens(row.cache_read_tokens)} cached"
            if row.cache_creation_tokens:
                cached += f", {_tokens(row.cache_creation_tokens)} written"
            parts.append(
                f"{role} {_tokens(row.total_input_tokens)} in ({cached}) / {_tokens(row.output_tokens)} out"
                + (f" / {_tokens(row.reasoning_tokens)} reasoning" if row.reasoning_tokens else "")
                + f" over {row.launches}"
            )
        price = f"${total.cost_usd:.2f}" + ("" if charged else " not charged")
        head = (
            f"{what}: {_tokens(total.total_input_tokens)} input + {_tokens(total.output_tokens)} output "
            f"tokens over {total.launches} launch(es), {price}"
            + (f" — {', '.join(total.models)}" if total.models else "")
        )
    else:
        head = f"{what}: no adapter here reports token usage"
    if blind:
        launches = sum(live[role].launches for role in blind)
        parts.append(f"usage unavailable for {', '.join(blind)} ({launches} launch(es), not counted above)")
    return head + (" — " + "; ".join(parts) if parts else "")


def merged(rows: dict[str, Usage], role: str, one: Usage) -> None:
    """Add one launch's usage to `role`'s running total, in place."""
    rows[role] = rows.get(role, Usage()) + one


class Ledger:
    """What a run's launches have cost, by role. Written from every thread that launches.

    One object rather than a dict the caller owns and every layer mutates. Two of these are kept
    while a gate-④ review runs — what the transport paid, and what a replayed stage cost when it
    was first taken — and both are written from the two threads the pipeline runs its stages on.
    `merged` above is a read-modify-write, which is not one operation on any interpreter that is
    not holding a global lock for us: a lost row would under-report a role rather than fail loudly.

    The bill records a launch that *failed* too. A launch that exited nonzero was still paid for,
    and the failure path is where the record has to be made: a raise carries no return value.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rows: dict[str, Usage] = {}

    def add(self, role: str, spent: Usage) -> None:
        with self._lock:
            merged(self._rows, role, spent)

    def totals(self) -> dict[str, Usage]:
        """A copy — the caller must not hold the lock's data."""
        with self._lock:
            return dict(self._rows)
