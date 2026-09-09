import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from quant_mcp.task_process import run_task_process


@pytest.fixture
def worker_stub(tmp_path, monkeypatch):
    """Replace only the agent in child interpreters; use the real MCP and runner."""
    hook = tmp_path / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text('''
import os
import subprocess
import sys
import time
from pathlib import Path
from quant_mcp import runner
from quant_mcp.agent.base import AgentResult

runner.ARTIFACTS_DIR = Path(os.environ["QUANT_TEST_ARTIFACTS"])

class StubAgent:
    def run_task(self, instruction, workspace_dir, timeout_seconds):
        print(12345, flush=True)
        os.write(1, b'"plain string from native stdout"\\n')
        if "STALL_WORKER" in instruction:
            child = subprocess.Popen([sys.executable, "-S", "-c",
                "import time; from pathlib import Path; time.sleep(3); "
                + "Path(" + repr(os.environ["QUANT_TEST_MARKER"]) + ").touch()"])
            print("stall-ready", flush=True)
            time.sleep(60)
        (workspace_dir / "analysis.py").write_text(
            "import json\\nfrom pathlib import Path\\n"
            "from quant_mcp.pricing import price_option_t\\n"
            "price = price_option_t('call', 100, 110, 0.25, 0.25, 0.04)\\n"
            "Path('result.json').write_text(json.dumps({"
            "'status':'success','task_type':'option_pricing','summary':'priced',"
            "'metrics':{'premium':price},'assumptions':[],'artifacts':[]}))\\n"
        )
        return AgentResult(status="success", message="stub completed")

runner.build_agent = lambda: StubAgent()
''', encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(hook) + os.pathsep + str(Path(__file__).resolve().parents[1] / "src"))
    monkeypatch.setenv("QUANT_TEST_ARTIFACTS", str(tmp_path / "artifacts"))
    monkeypatch.setenv("QUANT_TEST_MARKER", str(tmp_path / "child-survived"))
    monkeypatch.setenv("QUANT_MCP_REVIEW_MODE", "interactive")
    return tmp_path


def test_stdio_mcp_survives_noisy_agent_and_returns_result(worker_stub):
    from datetime import timedelta
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def exercise():
        params = StdioServerParameters(
            command=sys.executable, args=["-m", "quant_mcp.server"], env=dict(os.environ),
        )
        with (worker_stub / "mcp-stderr.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    # No elicitation callback/capability is provided.
                    await session.initialize()
                    for name in ["run_quant_coding_task", "analyze_option_portfolio"]:
                        response = await session.call_tool(
                            name, {"prompt": "price a call", "timeout_seconds": 15},
                            read_timeout_seconds=timedelta(seconds=20),
                        )
                        assert not response.isError
                        result = response.structuredContent or json.loads(response.content[0].text)
                        assert result["status"] == "success"
                        assert result["metrics"]["premium"] > 0
                        log = Path(result["worker_log"]).read_text()
                        assert "12345" in log and "native stdout" in log
                    assert (await session.list_tools()).tools

    asyncio.run(exercise())


def test_timeout_kills_worker_and_its_child(worker_stub):
    async def exercise():
        result = await run_task_process("STALL_WORKER", 2)
        assert result["status"] == "failed"
        assert "overall limit" in result["summary"]
        assert "stall-ready" in Path(result["worker_log"]).read_text()
        await asyncio.sleep(3)
        assert not (worker_stub / "child-survived").exists()
    asyncio.run(exercise())
