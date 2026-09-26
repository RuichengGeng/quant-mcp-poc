import asyncio
import os
from pathlib import Path

from quant_mcp.testing.launcher import MCPTestServer, ServerSpec
from quant_mcp.testing.runner import run_suite
from quant_mcp.testing.scenarios import Scenario


def test_stdio_server_can_initialize_list_tools_and_call(tmp_path):
    (tmp_path / "fixture_library.py").write_text('''import time

def add(left: int, right: int) -> dict:
    return {"value": left + right}

def slow(seconds: float) -> dict:
    time.sleep(seconds)
    return {"value": seconds}
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


def test_suite_handles_concurrency_and_expected_timeout(tmp_path):
    (tmp_path / "fixture_library.py").write_text('''import time

def slow(seconds: float) -> dict:
    time.sleep(seconds)
    return {"value": seconds}
''')
    module = tmp_path / "fixture_server.py"
    module.write_text('''from quant_mcp.framework import MCPFramework
from fixture_library import slow

app = MCPFramework("fixture", artifacts_dir="artifacts")
app.expose(slow, read_only=True)

if __name__ == "__main__":
    app.run()
''')
    spec = ServerSpec(command=os.fspath(Path(os.sys.executable)), args=(os.fspath(module),), cwd=tmp_path)
    scenarios = [
        Scenario.from_mapping({
            "id": "parallel-slow",
            "type": "concurrent_tool",
            "tool": "slow",
            "requests": 3,
            "max_concurrency": 2,
            "arguments": {"seconds": 0.01},
        }, 1),
        Scenario.from_mapping({
            "id": "expected-timeout",
            "tool": "slow",
            "arguments": {"seconds": 0.2},
            "timeout_seconds": 0.01,
            "assertions": {"expect_timeout": True},
        }, 2),
    ]

    report = asyncio.run(run_suite(spec, scenarios, expected_tools=["slow"]))

    assert report["ok"]
    assert [case["status"] for case in report["cases"]] == ["PASS", "PASS"]
