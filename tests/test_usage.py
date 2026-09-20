"""What a launch cost, as the adapter reports it — and what it means when it does not report."""

from __future__ import annotations

import json

import pytest

from rein import adapters, usage

# The shape a real `claude -p --output-format json` run returned, trimmed to the fields read here.
# Recorded from an actual launch rather than invented: the whole point of this module is that the
# number comes from the provider, so a fixture nobody ever saw would defeat it.
ENVELOPE = {
    "is_error": False,
    "subtype": "success",
    "result": '{"actual_statements": []}',
    "total_cost_usd": 0.0198386,
    "usage": {
        "input_tokens": 10,
        "cache_creation_input_tokens": 8730,
        "cache_read_input_tokens": 12226,
        "output_tokens": 42,
        "output_tokens_details": {"thinking_tokens": 35},
    },
    "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 891, "outputTokens": 53}},
}


def test_the_answer_and_the_cost_come_out_of_one_envelope() -> None:
    answer, spent = usage.parse_claude_envelope(json.dumps(ENVELOPE))
    assert answer == '{"actual_statements": []}'
    assert spent.available and spent.launches == 1
    assert (spent.input_tokens, spent.output_tokens) == (10, 42)
    assert (spent.cache_read_tokens, spent.cache_creation_tokens) == (12226, 8730)
    assert spent.reasoning_tokens == 35
    assert spent.models == ("claude-haiku-4-5-20251001",)
    assert spent.cost_usd == pytest.approx(0.0198386)


def test_what_the_process_sent_is_a_fraction_of_what_the_launch_read() -> None:
    """The reason bytes on stdin were never the measurement anyone wanted.

    This envelope is a one-word prompt. 20,956 of its input tokens are the system prompt, the CLI's
    own project instructions and the cache — context rein never sent and could not have counted.
    """
    _, spent = usage.parse_claude_envelope(json.dumps(ENVELOPE))
    assert spent.total_input_tokens == 20_966
    assert spent.total_input_tokens > 100 * spent.input_tokens


def test_a_failed_run_reported_on_a_zero_exit_is_raised_not_returned() -> None:
    """`is_error` can be set on a process that exited 0. Passing that on as the answer would make
    the stage validator report the reviewer's fault for something the launch said about itself."""
    envelope = {**ENVELOPE, "is_error": True, "subtype": "error_during_execution", "result": "Prompt is too long"}
    with pytest.raises(usage.AdapterEnvelopeError, match="Prompt is too long"):
        usage.parse_claude_envelope(json.dumps(envelope))


def test_output_that_is_not_the_promised_envelope_is_refused() -> None:
    with pytest.raises(usage.AdapterEnvelopeError, match="did not answer with any"):
        usage.parse_claude_envelope("thinking about it…")
    with pytest.raises(usage.AdapterEnvelopeError, match="no `result`"):
        usage.parse_claude_envelope(json.dumps({"usage": {}}))


def test_an_adapter_that_does_not_report_records_unmeasured_rather_than_zero() -> None:
    """ "We did not measure" and "it was free" must never render the same (plan §2.4).

    `copilot` is the one left: its programmatic reference documents no machine-readable envelope,
    and `-s` makes its answer readable without making its bill knowable — two different things.
    """
    copilot = adapters.ADAPTER_TABLE["copilot"]
    assert copilot.usage_flags == () and copilot.envelope is None
    answer, spent = copilot.read_output("some free-form output")
    assert answer == "some free-form output"
    assert spent.launches == 1 and not spent.available
    assert spent.to_detail() == {"launches": 1, "measured": False}


def test_every_gate_four_adapter_answers_something_that_parses_as_one_json_object() -> None:
    """Acceptance's three stages ask for "one JSON object and no other text" and parse the whole of
    stdout strictly. A CLI that prints a banner, its reasoning or a stats footer around that object
    has not given a smaller answer — it has given an unreadable one, and every stage reported it as
    the reviewer's fault. Only `claude` had an envelope, so only `claude` ever worked there."""
    from rein import review_policy
    from tests._support import agent_output

    for name, record in adapters.ADAPTER_TABLE.items():
        raw = agent_output(list(record.launch_argv()), '{"verdict": "ok"}')
        answer, _ = record.read_output(raw)
        assert review_policy.parse_reviewer_output(answer, what=name) == {"verdict": "ok"}


def test_a_gemini_envelope_is_read_for_its_answer_and_its_bill() -> None:
    envelope = {
        "response": "the answer",
        "stats": {"models": {"gemini-3-pro": {"tokens": {"prompt": 10, "candidates": 5, "cached": 3, "thoughts": 2}}}},
    }
    answer, spent = usage.parse_gemini_envelope(json.dumps(envelope))
    assert answer == "the answer"
    assert (spent.input_tokens, spent.output_tokens, spent.cache_read_tokens, spent.reasoning_tokens) == (10, 5, 3, 2)
    assert spent.available and spent.models == ("gemini-3-pro",)


def test_a_gemini_run_that_failed_is_not_read_as_an_answer() -> None:
    """`error` can arrive on a process that exited 0; without this the failure reaches the stage
    validator and is reported as a malformed reviewer answer."""
    with pytest.raises(usage.AdapterEnvelopeError, match="quota"):
        usage.parse_gemini_envelope(json.dumps({"error": {"type": "ApiError", "message": "quota exceeded"}}))
    with pytest.raises(usage.AdapterEnvelopeError, match="no `response`"):
        usage.parse_gemini_envelope(json.dumps({"stats": {}}))


def test_a_codex_stream_answers_with_its_last_agent_message() -> None:
    """The earlier items are the agent talking to itself on the way there. Taking the first would
    hand acceptance a paragraph of reasoning where it asked for one JSON object."""
    from tests._support import codex_events

    answer, spent = usage.parse_codex_envelope(codex_events("the answer", input_tokens=7, output_tokens=3))
    assert answer == "the answer"
    assert (spent.input_tokens, spent.output_tokens) == (7, 3)
    assert spent.available


def test_a_codex_stream_that_never_finished_a_turn_is_a_failed_run() -> None:
    """A process that exited mid-turn with nobody saying why is not an agent that said nothing."""
    with pytest.raises(usage.AdapterEnvelopeError, match="no events"):
        usage.parse_codex_envelope("just some words")
    with pytest.raises(usage.AdapterEnvelopeError, match="without a completed turn"):
        usage.parse_codex_envelope('{"type": "thread.started"}')
    with pytest.raises(usage.AdapterEnvelopeError, match="rate limited"):
        usage.parse_codex_envelope('{"type": "turn.failed", "error": {"message": "rate limited"}}')


def test_a_codex_turn_that_said_nothing_is_not_a_failure() -> None:
    """An implementer that edited files and reported no message ran fine; the caller that actually
    needed words is the one that says so, in its own vocabulary."""
    answer, spent = usage.parse_codex_envelope('{"type": "turn.completed", "usage": {"input_tokens": 4}}')
    assert answer == "" and spent.available and spent.input_tokens == 4


def test_only_an_adapter_with_an_envelope_asks_for_one() -> None:
    """Flags without a reader is how the answer stops parsing, so they travel together."""
    for adapter in adapters.ADAPTER_TABLE.values():
        assert bool(adapter.usage_flags) == (adapter.envelope is not None), adapter.name
    assert adapters.ADAPTER_TABLE["claude"].launch_argv()[-2:] == usage.CLAUDE_JSON_FLAGS


def test_merging_a_reported_launch_with_an_unreported_one_keeps_both_facts() -> None:
    """The total says what is known; the launch count says the rest is missing, not free."""
    _, reported = usage.parse_claude_envelope(json.dumps(ENVELOPE))
    total = reported + usage.Usage.unavailable()
    assert total.available and total.launches == 2
    assert total.output_tokens == 42


def test_a_role_whose_adapter_reports_nothing_is_named_in_the_summary() -> None:
    _, reported = usage.parse_claude_envelope(json.dumps(ENVELOPE))
    line = usage.summarize({"comparator": reported, "security_reviewer": usage.Usage.unavailable()}, what="review")
    assert "review: 21.0k input + 42 output tokens" in line
    assert "usage unavailable for security_reviewer (1 launch(es), not counted above)" in line


def test_the_summary_is_empty_when_nothing_launched() -> None:
    assert usage.summarize({}, what="review") == ""
    assert usage.summarize({"comparator": usage.Usage()}, what="review") == ""


# --- the model a role declares is the model that runs --------------------------
#
# `independence_group` used to be authored beside the adapter and passed to nothing: two roles
# could declare `claude/opus` and `claude/sonnet`, run the same model on the same CLI, and pass the
# critical-independence check on the strength of two different strings.


def test_a_named_model_reaches_the_launch() -> None:
    claude = adapters.ADAPTER_TABLE["claude"]
    assert claude.launch_argv("opus")[:4] == ("claude", "-p", "--model", "opus")
    assert "--model" not in claude.launch_argv(), "no model named means the CLI's own default"


def test_the_group_is_derived_from_what_launches() -> None:
    """One field, so a separation cannot be declared without being performed."""
    from rein import models

    config = models.Config({"agents": {"comparator": {"adapter": "claude", "model": "sonnet"}}})
    assert config.independence_group("comparator") == "claude/sonnet"
    assert models.Config({"agents": {"comparator": {"adapter": "claude"}}}).independence_group("comparator") == ""


def test_a_model_an_adapter_cannot_be_told_to_run_is_refused_not_dropped() -> None:
    """Launching the CLI's default under another model's name is the exact lie the field exists to
    stop — the independence check is derived from it."""
    from rein import models

    assert adapters.ADAPTER_TABLE["amp"].model_flags == (), "amp's execute-mode reference documents none"
    config = models.Config({"agents": {"implementer": {"adapter": "amp", "model": "gpt"}}})
    with pytest.raises(adapters.LaunchRefused, match="cannot tell"):
        adapters.launch_argv(config, "implementer")


def test_the_codex_thread_id_is_read_off_the_stream_it_was_always_in() -> None:
    """`codex exec --json` opens with `thread.started`, whose id `codex exec resume <id>` takes.

    The adapter was recorded as having no session at all on the reading that its resume verb takes
    only the *last* one — so every retry re-read the ticket, the design slice and the code from
    cold. The id was in the first line of the stream the envelope parser was already walking.
    """
    from tests._support import codex_events

    assert usage.codex_session(codex_events("done")) == "t-1"


def test_a_codex_stream_that_names_no_thread_is_cold_rather_than_broken() -> None:
    """A run that produced an answer has not failed because it cannot be continued."""
    stream = "\n".join(
        json.dumps(e)
        for e in (
            {"type": "item.completed", "item": {"id": "1", "type": "agent_message", "text": "x"}},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        )
    )
    assert usage.codex_session(stream) == ""
    assert usage.parse_codex_envelope(stream)[0] == "x", "and the answer still reads"


def test_a_replay_is_not_priced_as_a_bill() -> None:
    """The counts are real — they are what that answer cost when it was first taken. The price is
    not: nobody paid it on this run. One phrasing for both facts printed a dollar figure on the
    line whose entire reason for being separate is that it is not a bill."""
    replayed = usage.Usage(available=True, launches=2, input_tokens=480_000, cost_usd=7.59)
    line = usage.summarize({"comparator": replayed}, what="replayed", charged=False)
    assert "$7.59 not charged" in line
    assert "$7.59 not charged" not in usage.summarize({"comparator": replayed}, what="billed")


def test_cache_creation_is_visible_beside_cache_reads() -> None:
    """It is the expensive half and it was in neither number a reader could see: a role showing
    `3.07M in (1.10M cached)` left 727k of premium-priced cache writes unaccounted for, in the
    report that exists to find exactly that."""
    row = usage.Usage(
        available=True, launches=1, input_tokens=1_240_000, cache_read_tokens=1_100_000, cache_creation_tokens=727_272
    )
    line = usage.summarize({"extractor": row}, what="billed")
    assert "(1.10M cached, 727.3k written)" in line
    # A role that created no cache says nothing about it rather than printing a zero.
    plain = usage.Usage(available=True, launches=1, input_tokens=50_000, cache_read_tokens=1_000)
    assert "written" not in usage.summarize({"extractor": plain}, what="billed")


def test_a_count_in_the_millions_is_rendered_in_millions() -> None:
    """`3067.3k` is a number a reader has to divide before it means anything, in a report whose
    whole job is to be read at a glance. The small end keeps its exact rendering for the reason it
    always had: `0.0k` and "we did not measure" must not look the same."""
    assert usage._tokens(41) == "41"
    assert usage._tokens(34_000) == "34.0k"
    assert usage._tokens(999_999) == "1000.0k"
    assert usage._tokens(3_067_272) == "3.07M"


def test_a_cursor_envelope_is_read_for_its_answer_and_claims_no_bill() -> None:
    """Its object shape is claude's, and its own reference says neither format carries token
    counts. Reusing claude's parser would have reported every cursor launch as measured and free,
    which is the one thing `Usage.unavailable()` exists to prevent (plan §2.4)."""
    from tests._support import cursor_envelope

    answer, spent = usage.parse_cursor_envelope(cursor_envelope('{"verdict": "ok"}'))
    assert answer == '{"verdict": "ok"}'
    assert not spent.available and spent.launches == 1
    with pytest.raises(usage.AdapterEnvelopeError, match="failed run"):
        usage.parse_cursor_envelope(json.dumps({"is_error": True, "result": "out of credit"}))


def test_an_opencode_stream_answers_with_its_last_finished_text_part() -> None:
    from tests._support import opencode_events

    answer, spent = usage.parse_opencode_envelope(opencode_events("the answer"))
    assert answer == "the answer", "reasoning is never the answer"
    assert spent.available and (spent.input_tokens, spent.output_tokens) == (100, 20)


def test_an_opencode_step_with_no_token_mapping_is_unmeasured_not_free() -> None:
    """There is no published schema for that part. A count defaulted to zero would report the
    launch as free; absent counts have to read as "we did not measure"."""
    stream = '{"type": "step_finish", "part": {"type": "step-finish"}}\n{"type": "text", "part": {"text": "hi"}}'
    answer, spent = usage.parse_opencode_envelope(stream)
    assert answer == "hi" and not spent.available


def test_an_opencode_error_event_is_not_read_as_an_answer() -> None:
    with pytest.raises(usage.AdapterEnvelopeError, match="rate"):
        usage.parse_opencode_envelope('{"type": "error", "error": {"message": "rate limited"}}')
    with pytest.raises(usage.AdapterEnvelopeError, match="no events"):
        usage.parse_opencode_envelope("just some words")


# --- the spend ceiling: the one limit on the side that cannot judge -------------


def _priced(cost: float, launches: int = 1) -> usage.Usage:
    return usage.Usage(available=True, launches=launches, cost_usd=cost)


def test_no_ceiling_is_the_default_and_never_binds() -> None:
    """Absent is unbounded, not zero. A shipped number would be this tool deciding what a cycle
    is worth, which is the reader's judgement and not its writer's."""
    assert usage.over_ceiling(0.0, usage.Spend(usd=999.0, launches=40)) == ""


def test_spend_under_the_ceiling_says_nothing() -> None:
    assert usage.over_ceiling(10.0, usage.Spend(usd=9.99, launches=3)) == ""


def test_reaching_it_stops_and_offers_no_cheaper_way_to_carry_on() -> None:
    """The stop is the whole behaviour. Degrading instead — a cheaper model, a thinner review —
    is an automatic judgement about quality by the side that cannot judge quality."""
    reason = usage.over_ceiling(10.0, usage.Spend(usd=10.0, launches=7))

    assert "$10.00 of the $10.00 ceiling" in reason
    assert "Nothing is degraded" in reason
    assert "raise the ceiling or fix what is repeating" in reason


def test_launches_nobody_could_price_are_named_rather_than_counted_as_free() -> None:
    """An adapter that reports no usage records `unavailable`, never zero. What was priced still
    decides the stop; the count of what was not says the real figure is above it."""
    spend = usage.Spend.of({"implementer": _priced(6.0, 2), "reviewer": usage.Usage.unavailable()})

    assert spend == usage.Spend(usd=6.0, launches=3, unpriced_launches=1)
    assert "1 launch(es) reported no cost at all" in usage.over_ceiling(5.0, spend)


def test_a_run_nothing_could_price_stops_rather_than_running_unbounded() -> None:
    """The hole the old rule left. Summing only dollars made a cycle whose every launch came back
    unpriced total `$0.00`, stay under any ceiling, and run with no bound at all while its
    `config.yaml` said it had one — and the launches that record `unavailable` are the failure
    paths, which is the shape a runaway takes. "No figure to compare" is its own answer."""
    spend = usage.Spend.of({"reviewer": usage.Usage.unavailable(), "implementer": usage.Usage.unavailable()})

    assert spend.usd == 0.0 and spend.blind
    reason = usage.over_ceiling(5.0, spend)

    assert "not one of its 2 launch(es) reported a cost" in reason
    assert "unbounded, not free" in reason


def test_the_stop_on_an_unmeasurable_run_prices_nothing_on_its_behalf() -> None:
    """It refuses the run, never the estimate. Charging an unpriced launch at some average would
    enforce a ceiling against a number nobody measured, which is the thing this module exists to
    refuse — so the message names the repair and no dollar figure is invented."""
    reason = usage.over_ceiling(5.0, usage.Spend(usd=0.0, launches=3, unpriced_launches=3))

    assert "$0.00" not in reason
    assert "why the adapter reports no usage" in reason


def test_a_cycle_that_really_cost_nothing_is_not_an_unmeasurable_one() -> None:
    """`blind` is "nothing could be priced", not "the price was zero". A launch priced at $0.00 was
    measured, so a free cycle under a ceiling goes on exactly as before."""
    spend = usage.Spend.of({"implementer": _priced(0.0, 4)})

    assert not spend.blind
    assert usage.over_ceiling(5.0, spend) == ""


def test_no_ceiling_means_an_unmeasurable_run_is_nobody_business() -> None:
    """The stop belongs to the ceiling, not to the measurement. With no ceiling set there is
    nothing to enforce and nothing to say — a repository that never asked for a bound is not told
    its adapter reports no cost."""
    assert usage.over_ceiling(0.0, usage.Spend(usd=0.0, launches=9, unpriced_launches=9)) == ""


#: The only two places allowed to consult the ceiling, and what each stops: `rein build` before it
#: starts another batch, `rein review generate` before it launches the reviewers. Both stop; neither
#: decides anything about the code.
CEILING_READERS = {"build_loop", "review"}


#: The two names that are the ceiling: the rule, and the accessor that reads the number.
_CEILING_NAMES = frozenset({"over_ceiling", "max_cost_usd"})


def _modules_consulting_the_ceiling() -> set[str]:
    """Every module under `src/rein` that calls `over_ceiling` or reads `max_cost_usd`.

    **Every way of naming them, not one.** Matching `ast.Attribute` alone meant the guarantee
    held only against `usage_mod.over_ceiling(...)`: an `approve.py` that wrote
    `from rein.usage import over_ceiling` and then called the bare name passed this test, which
    is the one import form somebody reaching for a function they were told not to use would
    naturally write. `ast.Name` catches the call, and `ast.ImportFrom` catches the import even
    where the name is then aliased — a check that can be stepped around by a spelling is a
    convention, which is the same thing this file says about a boundary.
    """
    import ast
    from pathlib import Path

    source_root = Path(__file__).resolve().parent.parent / "src" / "rein"
    found: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
        if path.stem in {"usage", "models"}:  # where the rule and the accessor are defined
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            named = (
                (isinstance(node, ast.Attribute) and node.attr in _CEILING_NAMES)
                or (isinstance(node, ast.Name) and node.id in _CEILING_NAMES)
                or (isinstance(node, ast.ImportFrom) and any(a.name in _CEILING_NAMES for a in node.names))
            )
            if named:
                found.add(path.stem)
    return found


def test_only_the_two_launchers_consult_the_ceiling() -> None:
    """`00-concept.md` (論点 A) allows this number to bound the machine and nothing else. A
    ceiling something consults to *decide* is the kind of limit that degrades quality: the
    approval-screen budget became one, was raised twice, and came out.

    So the guarantee is a property of the source, not of a message: an `over_ceiling` appearing in
    `approve`, `review_policy`, `lens_cmd` or `gate_guard` fails here, and whoever put it there
    decides whether the guarantee or the caller goes.
    """
    assert _modules_consulting_the_ceiling() == CEILING_READERS


def test_the_guarantee_holds_whatever_import_form_the_caller_writes() -> None:
    """The check itself, checked. It read attribute access only, so the bare-name call that a
    `from rein.usage import over_ceiling` produces went straight past it — and that is the form
    somebody adds when they want the function and not the module."""
    import ast

    module = ast.parse("from rein.usage import over_ceiling\n\n\ndef f(s):\n    return over_ceiling(1.0, s)\n")
    hits = [
        node
        for node in ast.walk(module)
        if (isinstance(node, ast.Name) and node.id in _CEILING_NAMES)
        or (isinstance(node, ast.ImportFrom) and any(a.name in _CEILING_NAMES for a in node.names))
    ]

    assert hits, "a caller importing the name directly must still be seen by the guarantee"
