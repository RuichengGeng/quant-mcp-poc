import asyncio

import pytest

from quant_mcp.testing.prompts import (
    PromptOutcome,
    PromptToolCall,
    ReplayPromptAdapter,
    assert_prompt_outcome,
)


def test_prompt_assertions_accept_a_refusal_without_tool_calls():
    outcome = PromptOutcome("I can't answer weather questions because live weather data is unavailable.")
    assert_prompt_outcome(outcome, {
        "expected_tool_calls": [],
        "response_policy": "refuse_or_explain",
        "response_contains_any": ["weather"],
    })


def test_prompt_assertions_reject_an_invented_answer():
    with pytest.raises(AssertionError, match="unsupported"):
        assert_prompt_outcome(PromptOutcome("Singapore is sunny today."), {
            "expected_tool_calls": [],
            "response_policy": "refuse_or_explain",
        })


def test_prompt_assertions_check_tool_selection():
    outcome = PromptOutcome("The result is 10.45.", (PromptToolCall("price_option_t", {}),))
    assert_prompt_outcome(outcome, {"expected_tool_calls": ["price_option_t"]})


def test_replay_adapter_returns_outcome():
    adapter = ReplayPromptAdapter({"unsupported": PromptOutcome("I cannot help with that.")})
    scenario = type("Scenario", (), {"id": "unsupported"})()
    result = asyncio.run(adapter.run(scenario, None))
    assert result.response == "I cannot help with that."
