import asyncio
import os
from pathlib import Path

from quant_mcp.testing.launcher import MCPTestServer, ServerSpec
from quant_mcp.testing.runner import run_suite
from quant_mcp.testing.scenarios import Scenario


def test_stdio_server_can_initialize_list_tools_and_call(tmp_path):
    (tmp_path / "fixture_library.py").write_text('''def add(left: int, right: int) -> dict:
    return {"value": left + right}
''')
    module = tmp_path / "fixture_server.py"
    module.write_text('''from quant_mcp.framework import MCPFramework
from fixture_library import add

app = MCPFramework("fixture", artifacts_dir="artifacts")
app.expose(add, read_only=True)

if __name__ == "__main__":
    app.run()
''')
    spec = ServerSpec(
        command=os.fspath(Path(os.sys.executable)),
        args=(os.fspath(module),),
        cwd=tmp_path,
    )

    async def exercise():
        async with MCPTestServer(spec) as server:
            tools = await server.list_tools()
            assert {tool.name for tool in tools} >= {"add"}
            response = await server.call_tool("add", {"left": 2, "right": 3})
            assert not response.isError
            assert response.structuredContent == {"value": 5}

    asyncio.run(exercise())


def test_suite_can_run_tool_and_prompt_scenarios(tmp_path):
    (tmp_path / "fixture_library.py").write_text('''def add(left: int, right: int) -> dict:
    return {"value": left + right}
''')
    module = tmp_path / "fixture_server.py"
    module.write_text('''from quant_mcp.framework import MCPFramework
from fixture_library import add

app = MCPFramework("fixture", artifacts_dir="artifacts")
app.expose(add, read_only=True)

if __name__ == "__main__":
    app.run()
''')
    spec = ServerSpec(command=os.fspath(Path(os.sys.executable)), args=(os.fspath(module),), cwd=tmp_path)
    scenarios = [
        Scenario.from_mapping({
            "id": "add",
            "tool": "add",
            "arguments": {"left": 2, "right": 3},
            "assertions": {"structured_content": {"value": 5}},
        }, 1),
    ]

    report = asyncio.run(run_suite(
        spec,
        [
            scenarios[0],
            Scenario.from_mapping({
                "id": "unsupported-weather",
                "type": "prompt",
                "prompt": "What's the weather?",
                "replay": {"response": "I cannot answer weather questions."},
                "assertions": {"expected_tool_calls": [], "response_policy": "refuse_or_explain"},
            }, 2),
        ],
        expected_tools=["add"],
    ))

    assert report["ok"]
    assert [case["status"] for case in report["cases"]] == ["PASS", "PASS"]
