"""Run explicitly registered library functions away from MCP's stdout."""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import signal
import sys
import tempfile
import traceback
from pathlib import Path

from pydantic_core import to_jsonable_python

from quant_mcp.progress import RunContext, current_run
from quant_mcp.tracing import configure_telemetry, extracted_context, flush_telemetry, inject_context, operation, set_outcome


async def call_function(target: str, arguments: dict, *, python_paths: tuple[Path, ...],
                        log_path: Path, timeout_seconds: float,
                        own_process_group: bool = True) -> object:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([
        str(Path(__file__).resolve().parents[1]), *map(str, python_paths),
        env.get("PYTHONPATH", ""),
    ])
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="mcp-function-") as directory:
        request = Path(directory) / "request.json"
        response = Path(directory) / "response.json"
        run = current_run.get()
        request.write_text(json.dumps({"target": target,
                                      "arguments": to_jsonable_python(arguments),
                                      "run_context": run.payload() if run else None,
                                      "otel_context": inject_context()}))
        with log_path.open("w") as log:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "quant_mcp.function_process", str(request), str(response),
                stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log,
                env=env, cwd=directory, start_new_session=own_process_group,
            )
            try:
                await asyncio.wait_for(process.wait(), timeout_seconds)
            finally:
                try:
                    if own_process_group:
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        # Agent tools inherit the task worker's process group so
                        # the overall task timeout also terminates their children.
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if process.returncode != 0:
            raise RuntimeError("Library function failed; consult the server's call log")
        if not response.exists() or response.stat().st_size > 1024 * 1024:
            raise ValueError("Function result missing or exceeds 1 MiB; return a smaller JSON result")
        payload = json.loads(response.read_text())
        if not payload["ok"]:
            raise RuntimeError(payload["error"])
        return payload["value"]


async def main() -> None:
    from mcp.server.fastmcp.tools import Tool

    request = json.loads(Path(sys.argv[1]).read_text())
    run = RunContext.from_payload(request["run_context"]) if request.get("run_context") else None
    configure_telemetry(run.telemetry_setup if run else None)
    token = current_run.set(run)
    try:
        with extracted_context(request["otel_context"]):
            with operation("library.function", **{"code.function.name": request["target"]}) as span:
                if run:
                    span.set_attribute("quant_mcp.request_id", run.request_id)
                try:
                    module, name = request["target"].split(":", 1)
                    function = getattr(importlib.import_module(module), name)
                    result = await Tool.from_function(function).run(request["arguments"])
                    payload = {"ok": True, "value": to_jsonable_python(result)}
                    set_outcome("success")
                except Exception as exc:
                    traceback.print_exc()
                    span.set_attribute("error.type", type(exc).__name__)
                    set_outcome("failed")
                    payload = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        Path(sys.argv[2]).write_text(json.dumps(payload))
    finally:
        current_run.reset(token)
        flush_telemetry()


if __name__ == "__main__":
    asyncio.run(main())
