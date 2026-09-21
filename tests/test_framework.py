import asyncio
import base64
import importlib
import json
import logging
import os
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from quant_mcp.artifacts import ArtifactStore
from quant_mcp.framework import MCPFramework


def scripted_model(scripts):
    from smolagents.models import ChatMessage, Model

    class ScriptModel(Model):
        def __init__(self):
            super().__init__(model_id="test-model")
            self.scripts = iter(scripts)
            self.requests = []

        def generate(self, messages, **kwargs):
            if kwargs.get("stop_sequences") == ["<end_plan>"]:
                return ChatMessage(role="assistant", content="Compose the registered functions, check results and report.")
            self.requests.append(str(messages))
            return ChatMessage(role="assistant", content="<code>\n" + next(self.scripts) + "\n</code>")

    return ScriptModel()


@pytest.fixture
def office(tmp_path, monkeypatch):
    module = tmp_path / "office_fixture.py"
    module.write_text('''from __future__ import annotations
import os
import time
from pydantic import BaseModel

class Position(BaseModel):
    quantity: float
    price: float

def position_value(position: Position, multiplier: float = 1.0) -> dict:
    """Compute value using the office library."""
    print("Python stdout noise", flush=True)
    os.write(1, b"Native stdout noise\\n")
    return {"value": position.quantity * position.price * multiplier}

async def echo(value: str) -> str:
    """Return the input."""
    return value

def fail(value: str) -> str:
    """Fail for testing."""
    raise ValueError("private details: " + value)

def stall(seconds: int) -> str:
    """Wait for testing."""
    time.sleep(seconds)
    return "done"

def delayed_file(filename: str) -> str:
    """Start a child and wait, for process-group cleanup testing."""
    import subprocess
    import sys
    from pathlib import Path
    subprocess.Popen([sys.executable, "-S", "-c",
        "import time; from pathlib import Path; time.sleep(4); Path(" + repr(filename) + ").touch()"])
    Path(filename + ".ready").touch()
    time.sleep(60)
    return "done"
''')
    monkeypatch.syspath_prepend(str(tmp_path))
    sys.modules.pop("office_fixture", None)
    library = importlib.import_module("office_fixture")
    app = MCPFramework("office", artifacts_dir=tmp_path / "artifacts")
    yield app, library
    sys.modules.pop("office_fixture", None)


def test_schemas_execution_logs_and_explicit_registration(office, caplog):
    app, library = office
    app.expose(library.position_value, read_only=True)
    app.expose(library.echo)
    caplog.set_level(logging.INFO, logger="quant_mcp.audit")

    async def exercise():
        tools = {t.name: t for t in await app.mcp.list_tools()}
        assert set(tools) == {"run_coding_task", "list_tasks", "get_task_result",
                              "list_artifacts", "read_artifact", "position_value", "echo"}
        assert tools["position_value"].inputSchema["properties"]["multiplier"]["default"] == 1.0
        assert tools["position_value"].annotations.readOnlyHint
        response = await app.mcp.call_tool("position_value", {"position": {"quantity": 3, "price": 7}})
        assert response[1] == {"value": 21}
        assert (await app.mcp.call_tool("echo", {"value": "private-input"}))[1] == {"result": "private-input"}

    asyncio.run(exercise())
    logs = list((app.artifacts.root / "function_logs").glob("*.log"))
    assert any("Native stdout noise" in p.read_text() for p in logs)
    records = [json.loads(r.message) for r in caplog.records if r.name == "quant_mcp.audit"]
    assert len(records) == 8
    for request_id in {r["request_id"] for r in records}:
        events = [r for r in records if r["request_id"] == request_id]
        assert [r["event"] for r in events] == ["request_started", "function_call_started",
                                               "function_call_finished", "request_finished"]
        assert all(r["mode"] == "direct" and r["timestamp"].endswith("Z") for r in events)
        assert events[-1]["status"] == "success"
    assert [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()] == records
    assert {p.stem for p in logs} == {r["request_id"] for r in records}
    assert "private-input" not in " ".join(r.message for r in caplog.records)
    with pytest.raises(ValueError, match="Duplicate"):
        app.expose(library.echo)
    with pytest.raises(ValueError, match="reserved"):
        app.expose(library.echo, name="run_coding_task")


def test_failures_hide_library_tracebacks_and_timeout(office, otel_spans):
    app, library = office
    app.expose(library.fail)
    app.expose(library.stall, timeout_seconds=1)

    async def exercise():
        for name, arguments in [("fail", {"value": "secret"}), ("stall", {"seconds": 60})]:
            with pytest.raises(Exception, match="server call ID") as exc:
                await app.mcp.call_tool(name, arguments)
            assert "private details" not in str(exc.value)
            assert app._active == 0
    asyncio.run(exercise())

    events = [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()]
    assert [(e["tool"], e["status"]) for e in events if e["event"] == "request_finished"] == [
        ("fail", "failed"), ("stall", "timed_out")]
    assert "secret" not in json.dumps(events)
    spans = [s for s in otel_spans.get_finished_spans() if s.name == "mcp.tool.call"]
    assert [s.attributes["quant_mcp.outcome"] for s in spans] == ["failed", "timed_out"]
    assert all(s.status.status_code.name == "ERROR" for s in spans)
    assert "secret" not in "".join(s.to_json() for s in spans)


def test_coding_task_configuration_busy_limit_and_cancellation(office, monkeypatch, otel_spans):
    app, _ = office
    app.max_concurrent_calls = 1
    called = []

    async def exercise():
        entered, release = asyncio.Event(), asyncio.Event()

        async def fake_worker(prompt, timeout_seconds, **kwargs):
            called.append(kwargs)
            entered.set()
            await release.wait()
            return {"status": "success", "task_id": "task_example"}

        monkeypatch.setattr("quant_mcp.framework.run_task_process", fake_worker)
        running = asyncio.create_task(app.mcp.call_tool("run_coding_task", {"prompt": "analyse"}))
        await entered.wait()
        from quant_mcp.progress import flush_progress_logs
        await asyncio.to_thread(flush_progress_logs)
        # The arrival is visible while execution is still blocked.
        events = [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()]
        assert [e["event"] for e in events] == ["request_started"]
        assert events[0]["mode"] == "coding_agent"
        with pytest.raises(Exception, match="busy"):
            await app.mcp.call_tool("run_coding_task", {"prompt": "second"})
        running.cancel()
        with pytest.raises(asyncio.CancelledError):
            await running
        assert app._active == 0
        release.set()
        await app.mcp.call_tool("run_coding_task", {"prompt": "retry"})
        with pytest.raises(Exception, match="1..300"):
            await app.mcp.call_tool("run_coding_task", {"prompt": "task", "timeout_seconds": 301})
        assert all(c == {"functions": [], "python_paths": app._python_paths(),
                         "artifacts_dir": app.artifacts.root} for c in called)

    asyncio.run(exercise())
    events = [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()]
    finished = [e for e in events if e["event"] == "request_finished"]
    assert [e["status"] for e in finished] == ["rejected", "cancelled", "success", "failed"]
    assert len({e["request_id"] for e in finished}) == 4
    spans = [s for s in otel_spans.get_finished_spans() if s.name == "mcp.tool.call"]
    assert [s.attributes["quant_mcp.outcome"] for s in spans] == ["rejected", "cancelled", "success", "failed"]
    assert [s.status.status_code.name for s in spans] == ["ERROR", "ERROR", "UNSET", "ERROR"]


def test_trace_captures_validation_failures_without_arguments(office):
    app, library = office
    app.expose(library.position_value)

    async def exercise():
        for name, args in [("position_value", {"position": "secret-invalid-input"}),
                           ("unknown_function", {})]:
            with pytest.raises(Exception):
                await app.mcp.call_tool(name, args)

    asyncio.run(exercise())
    content = (app.artifacts.root / "events.jsonl").read_text()
    events = [json.loads(line) for line in content.splitlines()]
    assert [e["event"] for e in events] == ["request_started", "request_finished"] * 2
    assert [e["mode"] for e in events] == ["direct", "direct", "unknown", "unknown"]
    assert all(e["status"] == "failed" for e in events if e["event"] == "request_finished")
    assert "secret-invalid-input" not in content


def test_trace_write_failure_does_not_fail_business_call(office, monkeypatch, caplog):
    app, library = office
    app.expose(library.echo)

    def fail_append(*args):
        raise OSError("private filesystem details")

    monkeypatch.setattr("quant_mcp.progress._append", fail_append)
    result = asyncio.run(app.mcp.call_tool("echo", {"value": "ok"}))
    assert result[1] == {"result": "ok"}
    assert "Could not write lifecycle log" in caplog.text
    assert "private filesystem details" not in caplog.text


def test_artifact_transport_chunks_and_boundaries(tmp_path):
    store = ArtifactStore(tmp_path / "artifacts")
    task = store.root / "task_example"
    task.mkdir(parents=True)
    (task / "report.txt").write_text("a€b")
    (task / "binary.bin").write_bytes(b"\xff\x00\x81")
    (task / "result.json").write_text('{"status":"success","metrics":{"value":42}}')
    (task / "escape.txt").symlink_to(tmp_path / "private.txt")
    (tmp_path / "private.txt").write_text("private")
    (store.root / "task_escape").symlink_to(tmp_path, target_is_directory=True)
    assert store.list_tasks() == [{"task_id": "task_example"}]
    first = store.read("task_example", "report.txt", max_bytes=3)
    assert first["content"] == "a" and first["next_offset"] == 1
    second = store.read("task_example", "report.txt", offset=first["next_offset"])
    assert second["content"] == "€b" and second["next_offset"] is None
    binary = store.read("task_example", "binary.bin", encoding="base64")
    assert base64.b64decode(binary["content"]) == b"\xff\x00\x81"
    assert store.result("task_example")["metrics"] == {"value": 42}
    assert len(store.list_files("task_example")["files"]) == 3
    assert store.list_files("task_example", limit=1)["next_offset"] == 1
    for task_id, filename in [("../private", "report.txt"), ("task_escape", "private.txt"),
                              ("task_example", "../private.txt"), ("task_example", "escape.txt"),
                              ("task_example", "/etc/passwd")]:
        with pytest.raises(ValueError):
            store.read(task_id, filename)
    with pytest.raises(ValueError, match="max_bytes"):
        store.read("task_example", "report.txt", max_bytes=999999)


def test_real_stdio_framework_survives_noisy_library(office, tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    app, _ = office
    entrypoint = tmp_path / "server.py"
    entrypoint.write_text(f'''from quant_mcp.framework import MCPFramework
from office_fixture import position_value
app = MCPFramework("office", artifacts_dir={str(app.artifacts.root)!r})
app.expose(position_value)
app.run()
''')

    async def exercise():
        env = dict(os.environ)
        env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(Path(__file__).resolve().parents[1] / "src")])
        params = StdioServerParameters(command=sys.executable, args=[str(entrypoint)], env=env)
        with (tmp_path / "stderr.log").open("w") as errlog:
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool("position_value", {"position": {"quantity": 2, "price": 7}},
                                                       read_timeout_seconds=timedelta(seconds=15))
                    assert not response.isError
                    assert response.structuredContent == {"value": 14}
                    assert (await session.list_tools()).tools

    asyncio.run(exercise())
    stderr = (tmp_path / "stderr.log").read_text()
    assert '"event": "request_started"' in stderr
    assert '"event": "request_finished"' in stderr


def test_agent_composes_exact_registry_with_persistent_state_and_artifacts(office, monkeypatch):
    from quant_mcp.agent.registered import run_registered_task

    app, library = office
    app.expose(library.position_value, name="value_position")
    app.expose(library.echo)
    monkeypatch.setenv("SMOLAGENTS_PLANNING_INTERVAL", "2")
    model = scripted_model([
        'values = [value_position(position={"quantity": q, "price": 7})["value"] for q in [1, 2, 3]]\nprint(values)',
        'label = echo(value="Portfolio values")\n'
        'report = label + "\\n" + "\\n".join([str(v) for v in values])\n'
        'write_artifact(filename="report.txt", content=report)\n'
        'assert read_written_artifact(filename="report.txt")["content"] == report\n'
        'final_answer({"task_type":"analysis", "summary":label, "metrics":{"total":sum(values)}})',
    ])
    response = run_registered_task("Sum position values and save a report", app._functions,
                                   app.artifacts.root, 30, model=model)
    assert response["status"] == "success", response
    assert response["metrics"] == {"total": 42}
    task = Path(response["task_dir"])
    assert (task / "outputs/report.txt").read_text().splitlines() == ["Portfolio values", "7.0", "14.0", "21.0"]
    assert response["artifacts"] == [{"name": "report.txt", "path": "outputs/report.txt"}]
    assert not (task / "codebases.json").exists()
    assert not (task / "analysis.py").exists()
    trace = json.loads((task / "function_calls.json").read_text())
    assert [c["tool"] for c in trace] == ["value_position"] * 3 + ["echo"]
    assert all(c["status"] == "success" for c in trace)
    assert len(list((task / "agent_actions").glob("*.py"))) == 2
    assert '"name": "fail"' not in (task / "registry.json").read_text()
    events = [json.loads(line) for line in (task / "events.jsonl").read_text().splitlines()]
    assert {e["request_id"] for e in events} == {response["request_id"]}
    assert len([e for e in events if e["event"] == "model_call_started"]) >= 3
    assert all(e["streaming"] is False for e in events if e["event"] == "model_call_started")
    assert len([e for e in events if e["event"] == "function_call_finished"]) == 4
    assert len([e for e in events if e["event"] == "agent_step_finished"]) == 2
    assert events[-1]["event"] == "agent_finished" and events[-1]["status"] == "success"


def test_agent_repairs_errors_and_cannot_claim_unexecuted_analysis(office, monkeypatch):
    from quant_mcp.agent.registered import run_registered_task

    app, library = office
    app.expose(library.position_value)
    monkeypatch.setenv("SMOLAGENTS_PLANNING_INTERVAL", "0")
    monkeypatch.setenv("SMOLAGENTS_MAX_STEPS", "6")
    model = scripted_model([
        'final_answer({"task_type":"analysis", "summary":"Invented", "metrics":{"value":42}})',
        'import office_fixture\nprint(office_fixture.echo("unregistered"))',
        'print(position_value(position={"quantity":"not-a-number", "price":7}))',
        'result = position_value(position={"quantity":6, "price":7})\n'
        'final_answer({"task_type":"analysis", "summary":"Calculated", "metrics":result})',
    ])
    response = run_registered_task("Calculate value", app._functions, app.artifacts.root, 30, model=model)
    assert response["status"] == "success", response
    assert response["metrics"] == {"value": 42}
    assert "Call a registered function successfully" in model.requests[1]
    assert "not allowed" in model.requests[2] or "not authorized" in model.requests[2]
    assert "validation error" in model.requests[3]
    trace = json.loads((Path(response["task_dir"]) / "function_calls.json").read_text())
    assert [c["status"] for c in trace] == ["failed", "success"]


def test_agent_artifact_writes_are_confined_and_nonexecution_answers_work(office, monkeypatch):
    from quant_mcp.agent.registered import run_registered_task

    app, _ = office
    monkeypatch.setenv("SMOLAGENTS_PLANNING_INTERVAL", "0")
    model = scripted_model([
        'write_artifact(filename="../result.json", content="overwrite")',
        'write_artifact(filename="plot.bin", content="/wCB", encoding="base64")\n'
        'final_answer({"task_type":"unsupported", "summary":"No analysis functions are registered"})',
    ])
    response = run_registered_task("Calculate something", [], app.artifacts.root, 30, model=model)
    assert response["status"] == "failed" and response["task_type"] == "unsupported"
    assert "parent traversal" in model.requests[1]
    task = Path(response["task_dir"])
    assert (task / "outputs/plot.bin").read_bytes() == b"\xff\x00\x81"
    assert json.loads((task / "result.json").read_text())["task_type"] == "unsupported"


def test_agent_tool_names_are_validated(office):
    app, library = office
    for name in ("final_answer", "write_artifact", "read_written_artifact", "a-b", "class"):
        with pytest.raises(ValueError):
            app.expose(library.echo, name=name)
    assert app._functions == []


def test_registry_task_roundtrip_through_worker_and_artifact_mcp(office, tmp_path, monkeypatch):
    app, library = office
    app.expose(library.position_value)
    # Stub only model generation inside the subprocess, retaining the real task
    # worker, CodeAgent interpreter, function process and artifact MCP tools.
    hook = tmp_path / "hooks"
    hook.mkdir()
    (hook / "sitecustomize.py").write_text('''from smolagents.models import Model, ChatMessage
from quant_mcp.agent import registered

class ModelStub(Model):
    def __init__(self):
        super().__init__(model_id="test-model")
    def generate(self, messages, **kwargs):
        return ChatMessage(role="assistant", content="""<code>
result = position_value(position={"quantity": 6, "price": 7})
write_artifact(filename="value.txt", content=str(result["value"]))
final_answer({"task_type":"analysis", "summary":"Calculated", "metrics":result})
</code>""")

registered.build_model = lambda timeout: ModelStub()
''')
    monkeypatch.setenv("PYTHONPATH", str(hook))
    monkeypatch.setenv("SMOLAGENTS_PLANNING_INTERVAL", "0")

    async def exercise():
        response = (await app.mcp.call_tool("run_coding_task", {"prompt": "Calculate value", "timeout_seconds": 20}))[1]
        assert response["status"] == "success", response
        assert response["metrics"] == {"value": 42}
        task_id = response["task_id"]
        saved = (await app.mcp.call_tool("get_task_result", {"task_id": task_id}))[1]
        assert saved["metrics"] == response["metrics"]
        report = (await app.mcp.call_tool("read_artifact", {
            "task_id": task_id, "filename": "outputs/value.txt"}))[1]
        assert report["content"] == "42.0"
        registry = json.loads((Path(response["task_dir"]) / "registry.json").read_text())
        assert [spec["name"] for spec in registry] == ["position_value"]
        trace = (await app.mcp.call_tool("read_artifact", {
            "task_id": task_id, "filename": "events.jsonl"}))[1]
        events = [json.loads(line) for line in trace["content"].splitlines()]
        assert events[0]["event"] == "task_created"
        assert events[-1]["event"] == "request_finished"
        assert events[-1]["status"] == "success"
        assert {e["request_id"] for e in events} == {response["request_id"]}
        assert {e["task_id"] for e in events} == {task_id}
        assert {e["mode"] for e in events} == {"coding_agent"}
        global_events = [json.loads(line) for line in (app.artifacts.root / "events.jsonl").read_text().splitlines()]
        assert all(e in global_events for e in events)
        assert any(e["mode"] == "artifact" for e in global_events)
        assert any(e["event"] == "artifact_written" for e in events)

    asyncio.run(exercise())


def test_registry_task_timeout_kills_function_and_descendants(office, tmp_path, monkeypatch):
    app, library = office
    app.expose(library.delayed_file)
    marker = tmp_path / "child-survived"
    hook = tmp_path / "hooks"
    hook.mkdir()
    code = f"delayed_file(filename={str(marker)!r})"
    (hook / "sitecustomize.py").write_text('''from smolagents.models import Model, ChatMessage
from quant_mcp.agent import registered
class ModelStub(Model):
    def __init__(self):
        super().__init__(model_id="test-model")
    def generate(self, messages, **kwargs):
        return ChatMessage(role="assistant", content=''' + repr("<code>\n" + code + "\n</code>") + ''')
registered.build_model = lambda timeout: ModelStub()
''')
    monkeypatch.setenv("PYTHONPATH", str(hook))
    monkeypatch.setenv("SMOLAGENTS_PLANNING_INTERVAL", "0")

    async def exercise():
        response = (await app.mcp.call_tool("run_coding_task", {
            "prompt": "Exercise timeout", "timeout_seconds": 2}))[1]
        assert response["status"] == "failed"
        assert "overall limit" in response["summary"]
        assert Path(str(marker) + ".ready").exists(), "Function must start before timeout"
        await asyncio.sleep(4)
        assert not marker.exists(), "Function descendants must be killed with the worker"
        assert app._active == 0
        saved = app.artifacts.result(response["task_id"])
        assert saved["summary"] == response["summary"]
        events = [json.loads(line) for line in (Path(response["task_dir"]) / "events.jsonl").read_text().splitlines()]
        assert events[-1]["event"] == "request_finished"
        assert events[-1]["status"] == "timed_out"
        assert events[-1]["request_id"] == response["request_id"]

    asyncio.run(exercise())
