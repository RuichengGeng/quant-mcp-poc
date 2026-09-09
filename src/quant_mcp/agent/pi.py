from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import queue
import threading
import sys
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
        from quant_mcp.config import PROJECT_ROOT
        from quant_mcp.codebases import load_task_codebases

        if shutil.which(self.executable) is None:
            raise RuntimeError(f"Pi executable '{self.executable}' was not found. Set PI_EXECUTABLE to its absolute path.")
        events_path = workspace_dir / "pi_rpc_events.jsonl"
        env = os.environ.copy()
        env.setdefault("PI_SKIP_VERSION_CHECK", "1")
        env.update({
            "QUANT_PI_WORKSPACE": str(workspace_dir.resolve()),
            "QUANT_PI_PYTHON": sys.executable,
            "QUANT_PI_EXECUTION_TIMEOUT": str(min(timeout_seconds, int(os.getenv("PI_EXECUTION_TIMEOUT_SECONDS", "60")))),
            "QUANT_PI_MAX_EXECUTIONS": os.getenv("PI_MAX_EXECUTIONS", "8"),
        })
        # Never put credentials in argv or the model prompt.
        if self.api_key:
            if (self.provider or "").lower() == "deepseek":
                env["DEEPSEEK_API_KEY"] = self.api_key
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(PROJECT_ROOT / "src"), env.get("PYTHONPATH")]))
        executable = shutil.which(self.executable)
        env["PATH"] = str(Path(executable).parent) + os.pathsep + env.get("PATH", "")
        deadline = time.monotonic() + timeout_seconds
        with (workspace_dir / "pi_rpc_stderr.log").open("w") as stderr:
            process = subprocess.Popen(
                self._command(workspace_dir), cwd=load_task_codebases(workspace_dir).primary_root, env=env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr,
                text=True, bufsize=1,
            )
            lines: queue.Queue[str | None] = queue.Queue()

            def read_events():
                try:
                    for line in process.stdout:
                        lines.put(line)
                finally:
                    lines.put(None)

            reader = threading.Thread(target=read_events, daemon=True)
            reader.start()
            request_id = f"quant-task-{uuid.uuid4()}"
            assistant_text: list[str] = []
            last_error = "Pi did not execute a validated task."
            executions = 0
            try:
                process.stdin.write(json.dumps({"id": request_id, "type": "prompt", "message": instruction}) + "\n")
                process.stdin.flush()
                while time.monotonic() < deadline:
                    try:
                        line = lines.get(timeout=max(0.01, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if line is None:
                        return AgentResult(status="failed", message=f"Pi exited before completing the task (exit={process.poll()}). Check pi_rpc_stderr.log. {last_error}")
                    if not line.strip():
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        return AgentResult(status="failed", message="Pi emitted invalid RPC output; check pi_rpc_stderr.log.")
                    self._append_event(events_path, event, line.rstrip("\n"))
                    if event.get("type") == "response" and event.get("id") == request_id and not event.get("success"):
                        return AgentResult(status="failed", message=f"Pi rejected task: {event.get('error')}")
                    if event.get("type") == "message_update":
                        delta = event.get("assistantMessageEvent", {})
                        if delta.get("type") == "text_delta":
                            assistant_text.append(str(delta.get("delta", "")))
                    if event.get("type") == "message_end" and event.get("message", {}).get("errorMessage"):
                        last_error = event["message"]["errorMessage"]
                    if event.get("type") == "auto_retry_end" and event.get("finalError"):
                        last_error = event["finalError"]
                    if event.get("type") == "tool_execution_end" and event.get("toolName") == "run_python":
                        executions += 1
                        details = event.get("result", {}).get("details", {})
                        if not event.get("isError") and details.get("status") == "success":
                            # The execution tool owns validation. A write or agent reply is never success.
                            return AgentResult(status="success", message=f"Pi executed and validated {executions} Python attempt(s)", executed=True)
                        last_error = details.get("error") or str(event.get("result", {}))[-1500:]
                        if executions >= int(env["QUANT_PI_MAX_EXECUTIONS"]):
                            return AgentResult(status="failed", message=f"Pi exhausted its execution budget. {last_error}")
                    if event.get("type") == "agent_settled":
                        return AgentResult(status="failed", message=f"Pi settled without a validated execution. {''.join(assistant_text)[-2000:]} {last_error}")
                return AgentResult(status="failed", message=f"Pi RPC task timed out after {timeout_seconds} seconds. {last_error}")
            finally:
                self._abort(process)
                self._terminate(process)
                reader.join(timeout=1)
                process.stdin.close()
                process.stdout.close()

    def _command(self, workspace_dir: Path) -> list[str]:
        extension = Path(__file__).resolve().parents[1] / "pi_extensions" / "quant.ts"
        command = [
            self.executable, "--mode", "rpc", "--no-session",
            "--no-context-files", "--no-extensions", "--no-skills",
            "--no-prompt-templates", "--no-themes",
            "--extension", str(extension),
            "--tools", "read,grep,find,ls,write,edit,run_python",
            "--name", workspace_dir.name,
        ]
        if self.provider:
            command.extend(["--provider", self.provider])
        if self.model:
            command.extend(["--model", self.model])
        if self.thinking:
            command.extend(["--thinking", self.thinking])
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



PiCodingAgent = PiRpcAgent


def _normalize_model(provider: str | None, model: str | None) -> str | None:
    if not model or (provider or "").lower() != "deepseek":
        return model
    key = model.lower().replace("-", "").replace("_", "")
    return DEEPSEEK_MODEL_ALIASES.get(key, model)
