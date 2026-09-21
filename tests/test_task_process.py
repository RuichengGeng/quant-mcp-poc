import asyncio
import os
import sys
from datetime import timedelta
from pathlib import Path


def test_stdio_task_worker_survives_noisy_model_and_returns_artifacts(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (tmp_path / "office_tools.py").write_text('''def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b
''')
    hook = tmp_path / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text('''import os
from smolagents.models import Model, ChatMessage
from quant_mcp.agent import registered

class ModelStub(Model):
    def __init__(self):
        super().__init__(model_id="test-model")
    def generate(self, messages, **kwargs):
        print("worker-python-noise", flush=True)
        os.write(1, b"worker-native-noise\\n")
        return ChatMessage(role="assistant", content="""<code>
value = add(a=1, b=2)
write_artifact(filename="answer.txt", content=str(value))
final_answer({"task_type":"analysis", "summary":"Calculated", "metrics":{"value":value}})
</code>""")

registered.build_model = lambda timeout: ModelStub()
''')
    artifacts = tmp_path / "outputs"
    entrypoint = tmp_path / "office_server.py"
    entrypoint.write_text(f'''from quant_mcp.framework import MCPFramework
from office_tools import add
app = MCPFramework("test", artifacts_dir={str(artifacts)!r}, telemetry_setup="telemetry_config:configure")
app.expose(add)
app.run()
''')

    async def exercise():
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([
            str(hook), str(tmp_path), str(Path(__file__).resolve().parents[1] / "src"),
            str(Path(__file__).resolve().parents[1] / "examples"),
        ])
        env["SMOLAGENTS_PLANNING_INTERVAL"] = "0"
        env["QUANT_MCP_TRACE_EXPORTER"] = "console"
        params = StdioServerParameters(command=sys.executable, args=[str(entrypoint)], env=env)
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool("run_coding_task", {
                        "prompt": "Add numbers and save the result", "timeout_seconds": 15,
                    }, read_timeout_seconds=timedelta(seconds=20))
                    assert not response.isError
                    result = response.structuredContent
                    assert result["status"] == "success", result
                    assert result["metrics"] == {"value": 3}
                    assert Path(result["task_dir"]).parent == artifacts
                    log = Path(result["worker_log"]).read_text()
                    assert "worker-python-noise" in log and "worker-native-noise" in log
                    assert '"name": "agent.run"' in log
                    assert '"name": "model.generate"' in log
                    report = await session.call_tool("read_artifact", {
                        "task_id": result["task_id"], "filename": "outputs/answer.txt",
                    })
                    assert not report.isError
                    assert report.structuredContent["content"] == "3"
                    assert (await session.list_tools()).tools

    asyncio.run(exercise())
