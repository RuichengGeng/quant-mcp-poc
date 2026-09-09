import asyncio
from quant_mcp.server import analyze_option_portfolio, mcp, run_quant_coding_task


def test_server_returns_failed_task_status(monkeypatch, tmp_path) -> None:
    async def fake_run_task_process(prompt: str, timeout_seconds: int) -> dict:
        return {"status": "failed", "summary": "Agent failed: simulated timeout"}

    monkeypatch.setattr("quant_mcp.server.run_task_process", fake_run_task_process)

    result = asyncio.run(run_quant_coding_task("price a call spread", timeout_seconds=5))

    assert result["status"] == "failed"
    assert "simulated timeout" in result["summary"]

    legacy = asyncio.run(analyze_option_portfolio("price a call spread", timeout_seconds=5))
    assert legacy == result


def test_mcp_does_not_offer_review_mode(monkeypatch) -> None:
    monkeypatch.setenv("QUANT_MCP_REVIEW_MODE", "interactive")
    tools = asyncio.run(mcp.list_tools())
    task = next(tool for tool in tools if tool.name == "run_quant_coding_task")
    assert set(task.inputSchema["properties"]) == {"prompt", "timeout_seconds"}
