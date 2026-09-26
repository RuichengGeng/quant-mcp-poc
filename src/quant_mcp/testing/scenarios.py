"""Load portable MCP test configurations and scenario files."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping


def load_document(path: str | Path) -> Any:
    path = Path(path)
    with path.open(encoding="utf-8") as stream:
        if path.suffix.lower() == ".json":
            return json.load(stream)
        if path.suffix.lower() in {".yaml", ".yml"}:
            try:
                import yaml
            except ImportError as exc:
                raise RuntimeError(
                    "YAML suites require PyYAML; install the testing extra or use JSON"
                ) from exc
            return yaml.safe_load(stream)
    raise ValueError(f"unsupported test document format: {path.suffix or '<none>'}")


@dataclass(frozen=True)
class Scenario:
    id: str
    type: str
    tool: str | None
    prompt: str | None
    arguments: Mapping[str, Any]
    assertions: Mapping[str, Any]
    replay: Mapping[str, Any] | None = None
    requests: int = 1
    max_concurrency: int = 1
    timeout_seconds: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], index: int) -> "Scenario":
        scenario_id = data.get("id", f"scenario-{index}")
        scenario_type = data.get("type", "tool")
        tool = data.get("tool")
        prompt = data.get("prompt")
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError(f"case {index}: id must be a non-empty string")
        if scenario_type not in {"tool", "prompt", "concurrent_tool"}:
            raise ValueError(f"case {scenario_id}: type must be 'tool', 'prompt', or 'concurrent_tool'")
        if scenario_type in {"tool", "concurrent_tool"} and (not isinstance(tool, str) or not tool):
            raise ValueError(f"case {scenario_id}: tool must be a non-empty string")
        if scenario_type == "prompt" and (not isinstance(prompt, str) or not prompt.strip()):
            raise ValueError(f"case {scenario_id}: prompt must be a non-empty string")
        arguments = data.get("arguments", {})
        assertions = data.get("assertions", {})
        replay = data.get("replay")
        if not isinstance(arguments, Mapping) or not isinstance(assertions, Mapping):
            raise ValueError(f"case {scenario_id}: arguments and assertions must be objects")
        if replay is not None and not isinstance(replay, Mapping):
            raise ValueError(f"case {scenario_id}: replay must be an object")
        requests = data.get("requests", 1)
        max_concurrency = data.get("max_concurrency", requests)
        if scenario_type == "concurrent_tool":
            if not isinstance(requests, int) or requests < 1:
                raise ValueError(f"case {scenario_id}: requests must be a positive integer")
            if not isinstance(max_concurrency, int) or max_concurrency < 1:
                raise ValueError(f"case {scenario_id}: max_concurrency must be a positive integer")
        timeout = data.get("timeout_seconds")
        return cls(
            scenario_id,
            scenario_type,
            tool,
            prompt,
            arguments,
            assertions,
            replay,
            requests,
            max_concurrency,
            None if timeout is None else float(timeout),
        )


@dataclass(frozen=True)
class Suite:
    expected_tools: list[str]
    cases: list[Scenario]
    tool_contracts: Mapping[str, Any]


def load_suite_details(path: str | Path) -> Suite:
    document = load_document(path)
    if isinstance(document, list):
        expected_tools, cases, tool_contracts = [], document, {}
    elif isinstance(document, Mapping):
        expected_tools = document.get("expected_tools", [])
        cases = document.get("cases", [])
        tool_contracts = document.get("tool_contracts", {})
    else:
        raise ValueError("suite must be a list or an object containing cases")
    if not isinstance(expected_tools, list) or not all(isinstance(item, str) for item in expected_tools):
        raise ValueError("expected_tools must be a list of strings")
    if not isinstance(cases, list):
        raise ValueError("cases must be a list")
    if not isinstance(tool_contracts, Mapping):
        raise ValueError("tool_contracts must be an object")
    return Suite(expected_tools, [Scenario.from_mapping(case, index) for index, case in enumerate(cases, 1)], tool_contracts)


def load_suite(path: str | Path) -> tuple[list[str], list[Scenario]]:
    suite = load_suite_details(path)
    return suite.expected_tools, suite.cases
