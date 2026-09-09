from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from quant_mcp.agent.base import AgentResult


DEEPSEEK_MODEL_ALIASES = {
    "v4flash": "deepseek-v4-flash",
    "v4pro": "deepseek-v4-pro",
    "v4flashvision": "deepseek-v4-flash-vision-exp",
    "v4flashvisionexp": "deepseek-v4-flash-vision-exp",
}
VERBOSE_EVENT_TYPES = {"thinking_delta", "text_delta", "toolcall_delta"}


class PiRpcAgent:
    """Adapter that drives the pi.dev coding agent through RPC mode."""

    def __init__(
        self,
        executable: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        tools: str | None = None,
        api_key: str | None = None,
        session_dir: str | None = None,
    ) -> None:
        self.executable = executable or os.getenv("PI_EXECUTABLE", "pi")
        self.provider = provider or os.getenv("PI_PROVIDER")
        self.model = _normalize_model(provider=self.provider, model=model or os.getenv("PI_MODEL"))
        self.thinking = thinking or os.getenv("PI_THINKING")
        self.tools = tools or os.getenv("PI_TOOLS")
        self.api_key = api_key or os.getenv("PI_API_KEY")
        self.session_dir = session_dir or os.getenv("PI_SESSION_DIR")

    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        if shutil.which(self.executable) is None:
            raise RuntimeError(
                f"Pi executable '{self.executable}' was not found. Install pi.dev or set PI_EXECUTABLE."
            )

        events_path = workspace_dir / "pi_rpc_events.jsonl"
        stderr_path = workspace_dir / "pi_rpc_stderr.log"
        command = self._command(workspace_dir)
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")

        with stderr_path.open("w", encoding="utf-8") as stderr_handle:
            process = subprocess.Popen(
                command,
                cwd=str(workspace_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr_handle,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env=env,
            )

            request_id = f"quant-task-{uuid.uuid4()}"
            command_payload = {"id": request_id, "type": "prompt", "message": instruction}
            assert process.stdin is not None
            process.stdin.write(json.dumps(command_payload) + "\n")
            process.stdin.flush()

            deadline = time.monotonic() + timeout_seconds
            accepted = False
            settled = False
            assistant_text: list[str] = []
            write_paths: dict[str, str] = {}

            try:
                assert process.stdout is not None
                while time.monotonic() < deadline:
                    line = process.stdout.readline()
                    if line == "":
                        if process.poll() is not None:
                            break
                        time.sleep(0.05)
                        continue

                    line = line[:-1]
                    if line.endswith("\r"):
                        line = line[:-1]
                    if not line:
                        continue

                    event = json.loads(line)
                    self._append_event(events_path, event, line)
                    if event.get("type") == "tool_execution_start" and event.get("toolName") == "write":
                        tool_call_id = event.get("toolCallId")
                        path = event.get("args", {}).get("path")
                        if tool_call_id and path:
                            write_paths[str(tool_call_id)] = str(path)
                    if event.get("type") == "response" and event.get("id") == request_id:
                        if not event.get("success"):
                            return AgentResult(
                                status="failed",
                                message=f"Pi rejected prompt: {event.get('error') or event}",
                            )
                        accepted = True
                    elif event.get("type") == "message_update":
                        delta = event.get("assistantMessageEvent", {})
                        if delta.get("type") == "text_delta":
                            assistant_text.append(str(delta.get("delta", "")))
                    elif self._wrote_runner_deliverable(event, workspace_dir, write_paths):
                        return AgentResult(
                            status="success",
                            message="Pi RPC wrote a runner deliverable; local runner will execute and validate it.",
                        )
                    elif event.get("type") == "agent_settled":
                        settled = True
                        break

                if not settled:
                    self._abort(process)
                    return AgentResult(
                        status="failed",
                        message=f"Pi RPC task timed out after {timeout_seconds} seconds",
                    )

                if not accepted:
                    return AgentResult(status="failed", message="Pi RPC task settled before prompt acceptance")

                return AgentResult(
                    status="success",
                    message="Pi RPC task completed. " + "".join(assistant_text).strip()[:1000],
                )
            finally:
                self._terminate(process)

    def _command(self, workspace_dir: Path) -> list[str]:
        command = [
            self.executable,
            "--mode",
            "rpc",
            "--no-session",
            "--no-context-files",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-themes",
            "--name",
            workspace_dir.name,
        ]
        if self.provider:
            command.extend(["--provider", self.provider])
        if self.model:
            command.extend(["--model", self.model])
        if self.thinking:
            command.extend(["--thinking", self.thinking])
        if self.tools:
            command.extend(["--tools", self.tools])
        if self.api_key:
            command.extend(["--api-key", self.api_key])
        if self.session_dir:
            command.extend(["--session-dir", self.session_dir])
        return command

    def _abort(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None or process.stdin is None:
            return
        try:
            process.stdin.write(json.dumps({"type": "abort"}) + "\n")
            process.stdin.flush()
        except BrokenPipeError:
            return

    def _terminate(self, process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)

    def _append_event(self, events_path: Path, event: dict, raw_line: str) -> None:
        event_type = event.get("assistantMessageEvent", {}).get("type")
        if event.get("type") == "message_update" and event_type in VERBOSE_EVENT_TYPES:
            return
        with events_path.open("a", encoding="utf-8") as events_handle:
            events_handle.write(raw_line + "\n")

    def _wrote_runner_deliverable(
        self,
        event: dict,
        workspace_dir: Path,
        write_paths: dict[str, str] | None = None,
    ) -> bool:
        if event.get("type") != "tool_execution_end" or event.get("toolName") != "write":
            return False
        if event.get("isError"):
            return False
        path = event.get("args", {}).get("path")
        if not path and write_paths:
            path = write_paths.get(str(event.get("toolCallId")))
        if not path:
            return False
        try:
            written_path = Path(path).resolve()
            workspace = workspace_dir.resolve()
        except OSError:
            return False
        if workspace not in written_path.parents:
            return False
        return written_path.name in {"analysis.py", "result.json"}


PiCodingAgent = PiRpcAgent


def _normalize_model(provider: str | None, model: str | None) -> str | None:
    if not model or (provider or "").lower() != "deepseek":
        return model
    key = model.lower().replace("-", "").replace("_", "")
    return DEEPSEEK_MODEL_ALIASES.get(key, model)
