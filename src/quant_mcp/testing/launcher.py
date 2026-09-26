"""Start a local MCP server and communicate with it over stdio."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
import os
from pathlib import Path
import sys
import time
from typing import Any, Mapping

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


@dataclass(frozen=True)
class ServerSpec:
    """The portable launch contract for a local stdio MCP server."""

    command: str
    args: tuple[str, ...] = ()
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_seconds: float = 30.0
    call_timeout_seconds: float = 30.0

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, base_dir: Path | None = None) -> "ServerSpec":
        command = data.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("server.command must be a non-empty string")
        args = data.get("args", [])
        if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
            raise ValueError("server.args must be a list of strings")
        raw_cwd = data.get("cwd")
        cwd = None
        if raw_cwd is not None:
            if not isinstance(raw_cwd, str):
                raise ValueError("server.cwd must be a string")
            cwd = Path(raw_cwd)
            if not cwd.is_absolute() and base_dir is not None:
                cwd = (base_dir / cwd).resolve()
        raw_env = data.get("env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw_env.items()
        ):
            raise ValueError("server.env must be a mapping of strings")
        startup_timeout = float(data.get("startup_timeout_seconds", 30))
        call_timeout = float(data.get("call_timeout_seconds", 30))
        if startup_timeout <= 0 or call_timeout <= 0:
            raise ValueError("server timeouts must be positive")
        return cls(command, tuple(args), cwd, raw_env, startup_timeout, call_timeout)

    def parameters(self) -> StdioServerParameters:
        environment = os.environ.copy()
        environment.update(self.env)
        return StdioServerParameters(
            command=self.command,
            args=list(self.args),
            env=environment,
            cwd=str(self.cwd) if self.cwd else None,
        )


class MCPTestServer:
    """Async context manager for a server launched through MCP stdio."""

    def __init__(self, spec: ServerSpec, *, stderr_path: Path | None = None):
        self.spec = spec
        self.stderr_path = stderr_path
        self.session: ClientSession | None = None
        self.startup_duration_ms: float | None = None
        self._stack: AsyncExitStack | None = None
        self._stderr = None

    async def __aenter__(self) -> "MCPTestServer":
        self._stack = AsyncExitStack()
        await self._stack.__aenter__()
        if self.stderr_path is not None:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr = self.stderr_path.open("w", encoding="utf-8")
        else:
            self._stderr = sys.stderr
        read, write = await self._stack.enter_async_context(
            stdio_client(self.spec.parameters(), errlog=self._stderr)
        )
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        started = time.perf_counter()
        try:
            async with asyncio.timeout(self.spec.startup_timeout_seconds):
                await self.session.initialize()
                await self.session.send_ping()
        except BaseException:
            await self.__aexit__(None, None, None)
            raise
        self.startup_duration_ms = round((time.perf_counter() - started) * 1000, 3)
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        if self._stack is not None:
            await self._stack.__aexit__(exc_type, exc, traceback)
        if self._stderr is not None and self._stderr is not sys.stderr:
            self._stderr.close()
        self._stack = None
        self.session = None
        self._stderr = None

    async def list_tools(self) -> list[Any]:
        if self.session is None:
            raise RuntimeError("MCPTestServer is not running")
        tools = []
        cursor = None
        while True:
            result = await self.session.list_tools(cursor=cursor)
            tools.extend(result.tools)
            cursor = result.nextCursor
            if cursor is None:
                return tools

    async def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None, *, timeout_seconds: float | None = None) -> Any:
        if self.session is None:
            raise RuntimeError("MCPTestServer is not running")
        timeout = self.spec.call_timeout_seconds if timeout_seconds is None else timeout_seconds
        if timeout <= 0:
            raise ValueError("tool call timeout must be positive")
        async with asyncio.timeout(timeout):
            return await self.session.call_tool(name, dict(arguments or {}))
