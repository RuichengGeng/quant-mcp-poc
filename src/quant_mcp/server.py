from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from quant_mcp.runner import list_artifacts as list_task_artifacts
from quant_mcp.runner import read_artifact as read_task_artifact
from quant_mcp.task_process import run_task_process

mcp = FastMCP("quant-mcp-poc")


@mcp.tool()
async def run_quant_coding_task(
    prompt: str,
    timeout_seconds: int = 300,
) -> dict:
    """Write, run, and debug a quant task automatically, without code-review prompts.

    timeout_seconds bounds the whole task, including model calls and repairs.
    """
    return await run_task_process(prompt, timeout_seconds)


@mcp.tool()
async def analyze_option_portfolio(prompt: str, timeout_seconds: int = 120) -> dict:
    """Backward-compatible option-analysis tool; prefer run_quant_coding_task for general use."""
    return await run_task_process(prompt, timeout_seconds)


@mcp.tool()
def list_artifacts() -> list[dict]:
    """List recent generated quant analysis artifacts."""
    return list_task_artifacts()


@mcp.tool()
def read_artifact(task_id: str, filename: str) -> str:
    """Read a text artifact, or return the path for binary artifacts such as xlsx."""
    content = read_task_artifact(task_id, filename)
    if filename.endswith(".json"):
        return json.dumps(json.loads(content), indent=2)
    return content


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
