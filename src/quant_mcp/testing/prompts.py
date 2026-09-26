"""Adapters and assertions for natural-language MCP client scenarios."""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
from typing import Any, Mapping, Protocol

from quant_mcp.testing.scenarios import Scenario, load_document


@dataclass(frozen=True)
class PromptToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class PromptOutcome:
    response: str
    tool_calls: tuple[PromptToolCall, ...] = ()


class PromptAdapter(Protocol):
    async def run(self, scenario: Scenario, server: Any) -> PromptOutcome:
        """Run a prompt through a client/model and return its observable trace."""


class ReplayPromptAdapter:
    """Deterministic adapter for CI and regression tests without model credentials."""

    def __init__(self, outcomes: Mapping[str, PromptOutcome]):
        self.outcomes = outcomes

    async def run(self, scenario: Scenario, server: Any) -> PromptOutcome:
        try:
            return self.outcomes[scenario.id]
        except KeyError as exc:
            raise KeyError(f"No replay outcome configured for prompt {scenario.id!r}") from exc

    @classmethod
    def from_file(cls, path: str | Path) -> "ReplayPromptAdapter":
        document = load_document(path)
        if not isinstance(document, Mapping):
            raise ValueError("prompt replay must be an object keyed by scenario id")
        outcomes = {}
        for scenario_id, value in document.items():
            outcomes[str(scenario_id)] = _outcome_from_mapping(value, str(scenario_id))
        return cls(outcomes)

    @classmethod
    def from_scenarios(cls, scenarios: list[Scenario]) -> "ReplayPromptAdapter":
        outcomes = {}
        for scenario in scenarios:
            if scenario.type != "prompt" or scenario.replay is None:
                continue
            outcomes[scenario.id] = _outcome_from_mapping(scenario.replay, scenario.id)
        return cls(outcomes)


def _outcome_from_mapping(value: Any, scenario_id: str) -> PromptOutcome:
    if not isinstance(value, Mapping) or not isinstance(value.get("response"), str):
        raise ValueError(f"prompt replay {scenario_id!r} needs a string response")
    raw_calls = value.get("tool_calls", [])
    if not isinstance(raw_calls, list):
        raise ValueError(f"prompt replay {scenario_id!r} tool_calls must be a list")
    calls = []
    for call in raw_calls:
        if not isinstance(call, Mapping) or not isinstance(call.get("name"), str):
            raise ValueError(f"prompt replay {scenario_id!r} has an invalid tool call")
        arguments = call.get("arguments", {})
        if not isinstance(arguments, Mapping):
            raise ValueError(f"prompt replay {scenario_id!r} tool arguments must be an object")
        calls.append(PromptToolCall(call["name"], arguments))
    return PromptOutcome(value["response"], tuple(calls))


def load_prompt_adapter(path: str) -> PromptAdapter:
    """Load a project adapter from ``module:attribute`` for real client tests."""
    try:
        module_name, attribute = path.split(":", 1)
    except ValueError as exc:
        raise ValueError("prompt adapter must use module:attribute syntax") from exc
    adapter = getattr(importlib.import_module(module_name), attribute)
    if callable(adapter) and not hasattr(adapter, "run"):
        class FunctionAdapter:
            async def run(self, scenario, server):
                outcome = adapter(scenario, server)
                if hasattr(outcome, "__await__"):
                    outcome = await outcome
                return outcome
        return FunctionAdapter()
    return adapter


def assert_prompt_outcome(outcome: PromptOutcome, assertions: Mapping[str, Any]) -> None:
    """Validate observable client behavior without requiring exact wording."""
    actual_tools = [call.name for call in outcome.tool_calls]
    if "expected_tool_calls" in assertions and actual_tools != list(assertions["expected_tool_calls"]):
        raise AssertionError(f"expected tool calls {assertions['expected_tool_calls']!r}, got {actual_tools!r}")
    response = outcome.response
    for phrase in assertions.get("response_contains_all", []):
        if str(phrase).lower() not in response.lower():
            raise AssertionError(f"response does not contain {phrase!r}")
    phrases = assertions.get("response_contains_any", [])
    if phrases and not any(str(phrase).lower() in response.lower() for phrase in phrases):
        raise AssertionError(f"response contains none of {phrases!r}")
    for phrase in assertions.get("response_not_contains", []):
        if str(phrase).lower() in response.lower():
            raise AssertionError(f"response must not contain {phrase!r}")
    if assertions.get("response_policy") == "refuse_or_explain":
        refusal_terms = (
            "can't", "cannot", "don't have", "do not have", "unable", "outside",
            "unsupported", "not available", "no access", "not able",
        )
        if not any(term in response.lower() for term in refusal_terms):
            raise AssertionError("response does not explain that the request is unsupported")
