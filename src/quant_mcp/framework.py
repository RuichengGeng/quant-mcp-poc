"""Public facade for wrapping an existing Python library as an MCP server."""
from __future__ import annotations

import functools
import asyncio
import importlib
import inspect
import keyword
import logging
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from opentelemetry.trace import SpanKind

from quant_mcp.artifacts import ArtifactStore
from quant_mcp.function_process import call_function
from quant_mcp.task_process import run_task_process
from quant_mcp.progress import RunContext, current_run, emit_event, flush_progress_logs, logger, setup_progress_logging
from quant_mcp.tracing import configure_telemetry, flush_telemetry, operation, set_outcome

class _TracedMCP(FastMCP):
    """Observe tools/call before SDK validation, including invalid tool calls."""

    def __init__(self, name, artifacts_root, modes, telemetry_setup, **options):
        self.artifacts_root = artifacts_root
        self.tool_modes = modes
        self.telemetry_setup = telemetry_setup
        super().__init__(name, **options)

    async def call_tool(self, name, arguments):
        with operation("mcp.tool.call", kind=SpanKind.SERVER, **{"mcp.tool.name": name,
                       "quant_mcp.mode": self.tool_modes.get(name, "unknown")}):
            return await self._call_traced_tool(name, arguments)

    async def _call_traced_tool(self, name, arguments):
        trace = RunContext(self.artifacts_root, uuid.uuid4().hex,
                         self.tool_modes.get(name, "unknown"), name,
                         telemetry_setup=self.telemetry_setup)
        token = current_run.set(trace)
        started = time.monotonic()
        error_type = None
        emit_event(trace, "request_started")
        try:
            result = await super().call_tool(name, arguments)
            trace.status = trace.status or "success"
            return result
        except asyncio.CancelledError:
            trace.status = "cancelled"
            raise
        except Exception as exc:
            trace.status = trace.status if trace.status in {"rejected", "timed_out"} else "failed"
            cause = exc
            while cause.__cause__ is not None:
                cause = cause.__cause__
            error_type = type(cause).__name__
            raise
        finally:
            set_outcome(trace.status or "failed")
            emit_event(trace, "request_finished", status=trace.status or "failed",
                       task_type=trace.task_type, error_type=error_type,
                       duration_ms=round((time.monotonic() - started) * 1000))
            current_run.reset(token)
            # Drain in a thread so disk I/O/locks cannot block other MCP calls.
            import anyio
            with anyio.CancelScope(shield=True):
                await anyio.to_thread.run_sync(flush_progress_logs)


class MCPFramework:
    """Explicit direct tools plus a built-in smolagents coding-task tool.

    Use one instance per application. Libraries must be importable in the same
    environment. expose() registers functions for both MCP and the coding agent.
    Function execution is process-isolated, not sandboxed.
    """

    def __init__(self, name: str, *, artifacts_dir: str | Path,
                 max_concurrent_calls: int = 2,
                 max_timeout_seconds: int = 300, telemetry_setup: str | None = None, **mcp_options):
        if max_concurrent_calls < 1 or max_timeout_seconds < 1:
            raise ValueError("Concurrency and timeout limits must be positive")
        configure_telemetry(telemetry_setup)
        setup_progress_logging()
        self._functions: list[dict] = []
        self.artifacts = ArtifactStore(Path(artifacts_dir))
        self.max_timeout_seconds = max_timeout_seconds
        self.max_concurrent_calls = max_concurrent_calls
        self._active = 0
        self._names: set[str] = set()
        self._modes: dict[str, str] = {}
        self.mcp = _TracedMCP(name, self.artifacts.root, self._modes, telemetry_setup, **mcp_options)

        async def run_coding_task(prompt: str, timeout_seconds: int = min(300, self.max_timeout_seconds)) -> dict:
            """Use smolagents to compose the exposed functions in Python.

            Returns task_id, status, summary, metrics and artifacts. Read saved
            results with get_task_result/list_artifacts/read_artifact afterwards.
            Code runs locally with the server's permissions.
            """
            if not prompt.strip():
                raise ValueError("prompt must not be empty")
            self._check_timeout(timeout_seconds)
            return await run_task_process(
                prompt, timeout_seconds, artifacts_dir=self.artifacts.root,
                functions=list(self._functions), python_paths=self._python_paths(),
            )

        def list_tasks(limit: int = 20) -> list[dict]:
            """List recent task IDs; use get_task_result for their status and summary."""
            return self.artifacts.list_tasks(limit)

        def get_task_result(task_id: str) -> dict:
            """Read a saved task's structured result, metrics and artifact references."""
            return self.artifacts.result(task_id)

        def list_artifacts(task_id: str, offset: int = 0, limit: int = 100) -> dict:
            """List a task's files, relative filenames, sizes and MIME types."""
            return self.artifacts.list_files(task_id, offset, limit)

        def read_artifact(task_id: str, filename: str, offset: int = 0,
                          max_bytes: int = 65536, encoding: str = "utf-8") -> dict:
            """Read artifact bytes through MCP, including remote clients.

            Use utf-8 for text and base64 for binary files. Offsets are bytes.
            Continue using next_offset until null. Maximum chunk: 262144 bytes.
            """
            return self.artifacts.read(task_id, filename, offset, max_bytes, encoding)

        for function in (run_coding_task, list_tasks, get_task_result, list_artifacts, read_artifact):
            self._register(function, read_only=function is not run_coding_task,
                           limited=function is run_coding_task,
                           mode="coding_agent" if function is run_coding_task else "artifact")

    def _check_timeout(self, timeout_seconds: int) -> None:
        if not 1 <= timeout_seconds <= self.max_timeout_seconds:
            raise ValueError(f"timeout_seconds must be 1..{self.max_timeout_seconds}")

    @staticmethod
    def _python_paths() -> tuple[Path, ...]:
        # Inherit ordinary Python imports, including an application's local modules.
        # No repository scanning or source-path configuration is involved.
        return tuple(dict.fromkeys(Path(p).resolve() for p in sys.path))

    def _register(self, function: Callable, *, name: str | None = None,
                  read_only: bool = False, limited: bool = False, mode: str = "direct") -> None:
        tool_name = name or function.__name__
        if tool_name in self._names:
            raise ValueError(f"Duplicate or reserved tool name: {tool_name}")

        @functools.wraps(function)
        async def invoke(*args, **kwargs):
            trace = current_run.get()
            admitted = False
            result = None
            try:
                if limited:
                    if self._active >= self.max_concurrent_calls:
                        trace.status = "rejected"
                        emit_event(trace, "request_rejected", reason="server_busy")
                        raise RuntimeError("Server busy; retry later")
                    self._active += 1
                    admitted = True
                if inspect.iscoroutinefunction(function):
                    result = await function(*args, **kwargs)
                else:
                    # Artifact operations are bounded; avoid blocking the event loop on disk I/O.
                    import anyio
                    result = await anyio.to_thread.run_sync(functools.partial(function, *args, **kwargs))
                if mode == "coding_agent" and isinstance(result, dict):
                    trace.status = trace.status or result.get("status")
                    trace.task_type = result.get("task_type")
                return result
            finally:
                if admitted:
                    self._active -= 1

        signature = inspect.signature(function, eval_str=True)
        # FastMCP cannot infer structured output for a bare `dict` annotation.
        if signature.return_annotation is dict:
            signature = signature.replace(return_annotation=dict[str, Any])
        invoke.__signature__ = signature
        self.mcp.add_tool(invoke, name=tool_name, annotations=ToolAnnotations(readOnlyHint=read_only))
        self._names.add(tool_name)
        self._modes[tool_name] = mode

    def expose(self, function: Callable, *, name: str | None = None,
               read_only: bool = False, timeout_seconds: int = 60) -> Callable:
        """Expose one importable top-level function, preserving its typed schema.

        Functions should return JSON-compatible values (or Pydantic models).
        Use a top-level wrapper for class methods, DataFrames or custom objects.
        This same function name, documentation and schema are used by CodeAgent.
        """
        self._check_timeout(timeout_seconds)
        if not inspect.isfunction(function) or function.__qualname__ != function.__name__ or function.__module__ == "__main__":
            raise ValueError("Expose an importable top-level function; wrap methods in your library")
        module = function.__module__
        tool_name = name or function.__name__
        if not tool_name.isidentifier() or keyword.iskeyword(tool_name):
            raise ValueError("Tool name must be a valid Python identifier")
        if tool_name in {"final_answer", "write_artifact", "read_written_artifact"}:
            raise ValueError(f"Reserved agent tool name: {tool_name}")
        if getattr(importlib.import_module(module), function.__name__, None) is not function:
            raise ValueError("Function must be importable by its original name")
        signature = inspect.signature(function, eval_str=True)
        if any(p.kind in {p.POSITIONAL_ONLY, p.VAR_POSITIONAL, p.VAR_KEYWORD} for p in signature.parameters.values()):
            raise ValueError("MCP functions need named parameters; wrap positional-only or variadic functions")
        if any(p.annotation is inspect.Parameter.empty for p in signature.parameters.values()) or signature.return_annotation is inspect.Signature.empty:
            raise ValueError("Exposed functions require parameter and return type hints")

        @functools.wraps(function)
        async def invoke(**kwargs):
            with operation("function.call", **{"code.function.name": tool_name}):
                trace = current_run.get()
                call_id = trace.request_id
                started = time.monotonic()
                status, error_type = "failed", None
                emit_event(trace, "function_call_started", function=tool_name, call_id=call_id)
                try:
                    result = await call_function(
                        f"{module}:{function.__name__}", kwargs,
                        python_paths=self._python_paths(),
                        log_path=self.artifacts.root / "function_logs" / f"{call_id}.log",
                        timeout_seconds=timeout_seconds,
                    )
                    status = "success"
                    return result
                except asyncio.CancelledError:
                    status = "cancelled"
                    raise
                except Exception as exc:
                    error_type = type(exc).__name__
                    if isinstance(exc, TimeoutError):
                        status = trace.status = "timed_out"
                    # Do not return library tracebacks, secrets or filesystem paths to clients.
                    raise RuntimeError(f"Tool execution failed; server call ID: {call_id}") from None
                finally:
                    set_outcome(status)
                    emit_event(trace, "function_call_finished", function=tool_name, call_id=call_id,
                               status=status, error_type=error_type,
                               duration_ms=round((time.monotonic() - started) * 1000))

        invoke.__signature__ = signature
        self._register(invoke, name=name, read_only=read_only, limited=True)
        from mcp.server.fastmcp.utilities.func_metadata import func_metadata
        self._functions.append({
            "name": tool_name, "target": f"{module}:{function.__name__}",
            "description": inspect.getdoc(function) or f"Call {tool_name}.",
            "input_schema": func_metadata(function).arg_model.model_json_schema(),
            "timeout_seconds": timeout_seconds,
        })
        return function

    def run(self, transport: str = "stdio") -> None:
        """Run the server; the default stdio transport needs no network listener."""
        if transport == "stdio" and sys.stdin.isatty():
            print(
                "Starting MCP server over stdio. Waiting for an MCP client.\n"
                "This terminal accepts MCP JSON-RPC messages, not chat or blank lines.\n"
                "Let your MCP client launch this server; see the README client configuration.",
                file=sys.stderr,
                flush=True,
            )
        if not any(isinstance(handler, logging.StreamHandler) for handler in logger.handlers):
            handler = logging.StreamHandler()  # stderr; stdout belongs to MCP
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
            logger.setLevel(logging.INFO)
            logger.propagate = False
        try:
            self.mcp.run(transport=transport)
        finally:
            flush_progress_logs()
            flush_telemetry()
