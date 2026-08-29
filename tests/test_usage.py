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
    """ "We did not measure" and "it was free" must never render the same (plan §2.4)."""
    codex = adapters.ADAPTER_TABLE["codex"]
    assert codex.usage_flags == () and codex.envelope is None
    answer, spent = codex.read_output("some free-form output")
    assert answer == "some free-form output"
    assert spent.launches == 1 and not spent.available
    assert spent.to_detail() == {"launches": 1, "measured": False}


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

    assert adapters.ADAPTER_TABLE["codex"].model_flags == ()
    config = models.Config({"agents": {"implementer": {"adapter": "codex", "model": "gpt"}}})
    with pytest.raises(adapters.LaunchRefused, match="cannot tell"):
        adapters.launch_argv(config, "implementer")


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
