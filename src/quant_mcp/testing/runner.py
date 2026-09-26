"""Execute deterministic MCP tool scenarios and produce JSON-safe reports."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from pathlib import Path
import time
from typing import Any, Mapping

from quant_mcp.testing.assertions import assert_tool_result
from quant_mcp.testing.launcher import MCPTestServer, ServerSpec
from quant_mcp.testing.prompts import (
    PromptAdapter,
    PromptOutcome,
    ReplayPromptAdapter,
    assert_prompt_outcome,
)
from quant_mcp.testing.scenarios import Scenario


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if is_dataclass(value):
        return _jsonable(asdict(value))
    model_dump = getattr(value, "model_dump", None)
    if model_dump is not None:
        return _jsonable(model_dump(mode="json"))
    return repr(value)


async def run_suite(
    spec: ServerSpec,
    scenarios: list[Scenario],
    *,
    expected_tools: list[str] | None = None,
    stderr_path: Path | None = None,
    prompt_adapter: PromptAdapter | None = None,
) -> dict[str, Any]:
    embedded_prompt_adapter = None if prompt_adapter is not None else ReplayPromptAdapter.from_scenarios(scenarios)
    report: dict[str, Any] = {
        "ok": False,
        "startup": None,
        "tools": {"expected": expected_tools or [], "actual": [], "missing": [], "unexpected": []},
        "cases": [],
    }
    async with MCPTestServer(spec, stderr_path=stderr_path) as server:
        report["startup"] = {"status": "PASS", "duration_ms": server.startup_duration_ms}
        actual_tools = [tool.name for tool in await server.list_tools()]
        report["tools"]["actual"] = actual_tools
        expected = expected_tools or []
        report["tools"]["missing"] = sorted(set(expected) - set(actual_tools))
        report["tools"]["unexpected"] = sorted(set(actual_tools) - set(expected)) if expected else []
        if report["tools"]["missing"]:
            report["startup"] = {
                "status": "FAIL",
                "message": "Expected tools are missing",
                "duration_ms": server.startup_duration_ms,
            }
        for scenario in scenarios:
            started = time.perf_counter()
            row: dict[str, Any] = {
                "id": scenario.id,
                "type": scenario.type,
                "tool": scenario.tool,
                "prompt": scenario.prompt,
                "status": "FAIL",
            }
            try:
                if scenario.type == "tool":
                    result = await server.call_tool(
                        scenario.tool,
                        scenario.arguments,
                        timeout_seconds=scenario.timeout_seconds,
                    )
                    assert_tool_result(result, scenario.assertions)
                    row.update({"status": "PASS", "result": _jsonable(result)})
                elif prompt_adapter is None and (
                    embedded_prompt_adapter is None or scenario.id not in embedded_prompt_adapter.outcomes
                ):
                    raise RuntimeError("prompt scenario needs embedded replay data or a prompt adapter")
                else:
                    adapter = prompt_adapter or embedded_prompt_adapter
                    outcome = await adapter.run(scenario, server)
                    if not isinstance(outcome, PromptOutcome):
                        raise TypeError("prompt adapter must return PromptOutcome")
                    assert_prompt_outcome(outcome, scenario.assertions)
                    row.update({"status": "PASS", "result": _jsonable(outcome)})
            except Exception as exc:
                row.update({"error": f"{type(exc).__name__}: {exc}"})
            row["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            report["cases"].append(row)
    report["ok"] = (
        report["startup"]["status"] == "PASS"
        and not report["tools"]["missing"]
        and all(row["status"] == "PASS" for row in report["cases"])
    )
    return report
