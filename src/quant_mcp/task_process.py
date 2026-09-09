"""Keep task output and blocking agent work out of the MCP transport process."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import uuid
from pathlib import Path

from quant_mcp import runner
from quant_mcp.config import load_env_file


async def run_task_process(prompt: str, timeout_seconds: int) -> dict:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    load_env_file()
    log_dir = runner.ARTIFACTS_DIR / "worker_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{uuid.uuid4().hex}.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(runner.PROJECT_ROOT / "src"), env.get("PYTHONPATH")])
    )
    with tempfile.TemporaryDirectory(prefix="quant-mcp-") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        result_path = Path(temp_dir) / "result.json"
        request_path.write_text(json.dumps({
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "artifacts_dir": str(runner.ARTIFACTS_DIR),
        }), encoding="utf-8")
        # Redirect OS file descriptors too: libraries and subprocesses may print
        # without going through Python's sys.stdout. MCP stdout stays JSON-RPC only.
        with log_path.open("w", encoding="utf-8") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "quant_mcp.task_process",
                str(request_path), str(result_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log, stderr=log,
                cwd=str(runner.PROJECT_ROOT), env=env,
                start_new_session=True,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except TimeoutError:
                return _failure(
                    f"Task timed out after {timeout_seconds} seconds (overall limit).",
                    log_path,
                )
            finally:
                # Also stop analysis.py/Pi descendants on timeout or cancellation.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()

        if process.returncode != 0:
            return _failure(f"Task worker exited with code {process.returncode}.", log_path)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or result.get("status") not in {"success", "failed"}:
                raise ValueError("invalid task result")
        except (OSError, ValueError) as exc:
            return _failure(f"Task worker did not return a valid result: {exc}", log_path)
        result["worker_log"] = str(log_path)
        return result


def _failure(message: str, log_path: Path) -> dict:
    return {
        "status": "failed", "task_type": "failed", "summary": message,
        "metrics": {}, "assumptions": [], "artifacts": [],
        "worker_log": str(log_path),
    }


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    runner.ARTIFACTS_DIR = Path(request["artifacts_dir"])
    # build_agent selects auto_approve=True for smolagents. This worker never
    # constructs a terminal reviewer or sends an MCP elicitation request.
    task = runner.run_quant_task(
        prompt=request["prompt"], timeout_seconds=request["timeout_seconds"],
    )
    Path(sys.argv[2]).write_text(json.dumps({
        "task_id": task.task_id, "task_dir": str(task.task_dir), **task.result,
    }), encoding="utf-8")


if __name__ == "__main__":
    main()
