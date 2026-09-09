from pathlib import Path

from quant_mcp.agent import PiRpcAgent


def test_pi_rpc_command_uses_rpc_mode() -> None:
    agent = PiRpcAgent(
        executable="pi",
        provider="openai",
        model="gpt-demo",
        thinking="medium",
        tools="write",
        api_key="test-key",
        session_dir="/tmp/pi-sessions",
    )

    assert agent._command(Path("task_1")) == [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--name",
        "task_1",
        "--provider",
        "openai",
        "--model",
        "gpt-demo",
        "--thinking",
        "medium",
        "--tools",
        "write",
        "--api-key",
        "test-key",
        "--session-dir",
        "/tmp/pi-sessions",
    ]


def test_pi_rpc_normalizes_deepseek_model_aliases(monkeypatch) -> None:
    monkeypatch.delenv("PI_THINKING", raising=False)
    monkeypatch.delenv("PI_TOOLS", raising=False)
    monkeypatch.delenv("PI_API_KEY", raising=False)
    monkeypatch.delenv("PI_SESSION_DIR", raising=False)
    agent = PiRpcAgent(executable="pi", provider="deepseek", model="v4flash")

    assert agent._command(Path("task_1")) == [
        "pi",
        "--mode",
        "rpc",
        "--no-session",
        "--no-context-files",
        "--no-extensions",
        "--no-skills",
        "--no-prompt-templates",
        "--no-themes",
        "--name",
        "task_1",
        "--provider",
        "deepseek",
        "--model",
        "deepseek-v4-flash",
    ]


def test_pi_rpc_skips_verbose_stream_events(tmp_path) -> None:
    agent = PiRpcAgent(executable="pi")
    events_path = tmp_path / "events.jsonl"

    agent._append_event(
        events_path,
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "hello"}},
        '{"type":"message_update"}',
    )
    agent._append_event(events_path, {"type": "agent_settled"}, '{"type":"agent_settled"}')

    assert events_path.read_text(encoding="utf-8") == '{"type":"agent_settled"}\n'


def test_pi_rpc_recognizes_runner_deliverable_writes(tmp_path) -> None:
    agent = PiRpcAgent(executable="pi")

    assert agent._wrote_runner_deliverable(
        {
            "type": "tool_execution_end",
            "toolName": "write",
            "isError": False,
            "args": {"path": str(tmp_path / "analysis.py")},
        },
        tmp_path,
    )

    assert agent._wrote_runner_deliverable(
        {
            "type": "tool_execution_end",
            "toolCallId": "call_1",
            "toolName": "write",
            "isError": False,
        },
        tmp_path,
        {"call_1": str(tmp_path / "result.json")},
    )


def test_pi_rpc_ignores_scratch_writes(tmp_path) -> None:
    agent = PiRpcAgent(executable="pi")

    assert not agent._wrote_runner_deliverable(
        {
            "type": "tool_execution_end",
            "toolName": "write",
            "isError": False,
            "args": {"path": str(tmp_path / "check_sig.py")},
        },
        tmp_path,
    )
