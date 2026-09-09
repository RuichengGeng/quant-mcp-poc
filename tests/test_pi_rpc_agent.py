import json
import time
from pathlib import Path

from quant_mcp.agent import PiRpcAgent
from quant_mcp.runner import run_quant_task


def test_pi_rpc_command_loads_executor_without_exposing_key():
    agent = PiRpcAgent(executable="pi", provider="deepseek", model="v4flash", thinking="off", api_key="test-key")
    command = agent._command(Path("task_1"))
    assert command[:3] == ["pi", "--mode", "rpc"]
    assert command[command.index("--model") + 1] == "deepseek-v4-flash"
    assert Path(command[command.index("--extension") + 1]).is_file()
    assert "bash" not in command[command.index("--tools") + 1]
    assert "run_python" in command[command.index("--tools") + 1].split(",")
    assert "test-key" not in command


def fake_pi(tmp_path, body):
    import sys
    script = tmp_path / "fake-pi"
    script.write_text(f"#!{sys.executable}\nimport json, sys, time\nrequest=json.loads(sys.stdin.readline())\n" + body)
    script.chmod(0o755)
    return str(script)


def test_silent_rpc_is_bounded(tmp_path):
    executable = fake_pi(tmp_path, "time.sleep(30)\n")
    started = time.monotonic()
    result = PiRpcAgent(executable=executable).run_task("task", tmp_path, 1)
    assert result.status == "failed" and "timed out" in result.message
    assert time.monotonic() - started < 4


def test_writing_result_is_not_task_success(tmp_path):
    executable = fake_pi(tmp_path, '''
print(json.dumps({"type":"tool_execution_end","toolName":"write","isError":False,"args":{"path":"result.json"}}), flush=True)
print(json.dumps({"type":"agent_settled"}), flush=True)
time.sleep(10)
''')
    result = PiRpcAgent(executable=executable).run_task("task", tmp_path, 3)
    assert result.status == "failed" and not result.executed


def test_pi_uses_source_prompt_without_import_discovery(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Pi must not inspect/import library definitions to build its prompt")
    monkeypatch.setattr("quant_mcp.runner.discover_library_catalog", forbidden)
    executable = fake_pi(tmp_path, '''
assert "REPOSITORY:" in request["message"]
assert "price_option_t(" not in request["message"]
print(json.dumps({"type":"agent_settled"}), flush=True)
''')
    task = run_quant_task("price a put", agent=PiRpcAgent(executable=executable), timeout_seconds=5)
    assert task.result["status"] == "failed"  # No execution, but source prompt reached Pi.
    assert "REPOSITORY:" in (task.task_dir / "agent_instruction.md").read_text()
    assert not (task.task_dir / "library_catalog.json").exists()


def test_pi_waits_through_failed_execution_for_repair(tmp_path):
    executable = fake_pi(tmp_path, '''
for status in ["failed", "success"]:
 print(json.dumps({"type":"tool_execution_end","toolName":"run_python","isError":False,"result":{"details":{"status":status,"error":"repair needed"}}}), flush=True)
time.sleep(10)
''')
    result = PiRpcAgent(executable=executable).run_task("task", tmp_path, 3)
    assert result.status == "success" and result.executed
    events = [json.loads(line) for line in (tmp_path / "pi_rpc_events.jsonl").read_text().splitlines()]
    assert len(events) == 2


def test_pi_rpc_skips_verbose_stream_events(tmp_path):
    events = tmp_path / "events.jsonl"
    agent = PiRpcAgent()
    agent._append_event(events, {"type":"message_update", "assistantMessageEvent":{"type":"text_delta"}}, "skip")
    agent._append_event(events, {"type":"agent_settled"}, "keep")
    assert events.read_text() == "keep\n"
