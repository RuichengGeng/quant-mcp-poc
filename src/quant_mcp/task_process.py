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

from quant_mcp.progress import RunContext, current_run, emit_event, flush_progress_logs, new_task_id
from quant_mcp.tracing import configure_telemetry, extracted_context, flush_telemetry, inject_context, operation, set_outcome

async def run_task_process(
    prompt: str, timeout_seconds: int, *, artifacts_dir: Path,
    functions: list[dict], python_paths: tuple[Path, ...] = (),
) -> dict:
    with operation("task.worker"):
        result = await _run_task_process(prompt, timeout_seconds, artifacts_dir=artifacts_dir,
                                         functions=functions, python_paths=python_paths)
        set_outcome((current_run.get().status if current_run.get() else None) or result["status"])
        return result


async def _run_task_process(
    prompt: str, timeout_seconds: int, *, artifacts_dir: Path,
    functions: list[dict], python_paths: tuple[Path, ...] = (),
) -> dict:
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    artifacts_dir = artifacts_dir.resolve()
    trace = current_run.get() or RunContext(artifacts_dir, uuid.uuid4().hex, "coding_agent", "run_coding_task")
    trace.task_id = new_task_id()
    emit_event(trace, "task_created")
    await asyncio.to_thread(flush_progress_logs)
    log_dir = artifacts_dir / "worker_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{trace.request_id}.log"
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        filter(None, [str(Path(__file__).resolve().parents[1]),
                      *map(str, python_paths), env.get("PYTHONPATH")])
    )
    with tempfile.TemporaryDirectory(prefix="quant-mcp-") as temp_dir:
        request_path = Path(temp_dir) / "request.json"
        result_path = Path(temp_dir) / "result.json"
        request_path.write_text(json.dumps({
            "prompt": prompt,
            "timeout_seconds": timeout_seconds,
            "artifacts_dir": str(artifacts_dir),
            "functions": functions,
            "run_context": trace.payload(),
            "otel_context": inject_context(),
        }), encoding="utf-8")
        # Redirect OS file descriptors too: libraries and subprocesses may print
        # without going through Python's sys.stdout. MCP stdout stays JSON-RPC only.
        with log_path.open("w", encoding="utf-8") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "quant_mcp.task_process",
                str(request_path), str(result_path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log, stderr=log,
                cwd=temp_dir, env=env,
                start_new_session=True,
            )
            emit_event(trace, "task_worker_started", worker_pid=process.pid,
                       worker_log=str(log_path))
            failure_message = None
            cancellation = None
            try:
                await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
            except TimeoutError:
                trace.status = "timed_out"
                failure_message = f"Task timed out after {timeout_seconds} seconds (overall limit)."
            except asyncio.CancelledError as exc:
                trace.status = "cancelled"
                failure_message = "Task cancelled by the caller."
                cancellation = exc
            finally:
                # Also stop active function workers on timeout or cancellation.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                emit_event(trace, "task_worker_finished", worker_pid=process.pid,
                           return_code=process.returncode,
                           status=trace.status or ("success" if process.returncode == 0 else "failed"))

        # Persist the terminal outcome only after the worker can no longer
        # overwrite it with a late result.
        if failure_message is not None:
            result = _failure(failure_message, log_path, trace)
            if cancellation is not None:
                raise cancellation
            return result
        if process.returncode != 0:
            return _failure(f"Task worker exited with code {process.returncode}.", log_path, trace)
        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict) or result.get("status") not in {"success", "failed"}:
                raise ValueError("invalid task result")
        except (OSError, ValueError) as exc:
            return _failure(f"Task worker did not return a valid result: {exc}", log_path, trace)
        result["worker_log"] = str(log_path)
        result["request_id"] = trace.request_id
        return result


def _failure(message: str, log_path: Path, trace: RunContext) -> dict:
    trace.status = trace.status or "failed"
    result = {
        "status": "failed", "task_type": "failed", "summary": message,
        "metrics": {}, "assumptions": [], "artifacts": [],
    }
    task_dir = trace.root / trace.task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return {**result, "task_id": trace.task_id, "task_dir": str(task_dir),
            "request_id": trace.request_id, "worker_log": str(log_path)}


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    run = RunContext.from_payload(request["run_context"])
    configure_telemetry(run.telemetry_setup)
    from quant_mcp.agent.registered import run_registered_task
    try:
        with extracted_context(request["otel_context"]):
            response = run_registered_task(
                request["prompt"], request["functions"], Path(request["artifacts_dir"]),
                request["timeout_seconds"], trace=run,
            )
        Path(sys.argv[2]).write_text(json.dumps(response), encoding="utf-8")
    finally:
        flush_progress_logs()
        flush_telemetry()


if __name__ == "__main__":
    main()
