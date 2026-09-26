"""Offline startup diagnostics for an MCPFramework application entry point."""
from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import runpy
import signal
import subprocess
import sys
import tempfile
import traceback
from urllib.parse import urlsplit


def check(name, status, message, fix=None):
    return {"name": name, "status": status, "message": message, **({"fix": fix} if fix else {})}


def failure(name, exc):
    # Exception messages/import output can contain credentials. Keep them in the
    # private diagnostic files, not the terminal or JSON report.
    if isinstance(exc, ModuleNotFoundError):
        missing = exc.name or "unknown"
        fix = {
            "scipy": "Install the example dependencies: uv sync --group examples",
            "smolagents": "Restore required framework dependencies in this Python environment: uv sync",
            "openai": "Restore required framework dependencies in this Python environment: uv sync",
        }.get(missing, "Install the missing package in the Python environment shown in this report.")
        if missing.startswith("opentelemetry"):
            fix = "Install the telemetry extra when enabling an SDK/exporter: uv sync --extra telemetry --group examples"
        return check(name, "FAIL", f"Missing module: {missing}", fix)
    return check(name, "FAIL", f"{type(exc).__name__}; see the diagnostic log.",
                 "Fix the entry-point imports/configuration or registration error shown in the diagnostic log.")


def spawn_stage(arguments, folder, label, timeout, *, env=None, cwd=None):
    """Bound imports, capture noisy libraries, and stop descendants on timeout."""
    with (folder / f"{label}.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "quant_mcp.preflight", *arguments],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            cwd=cwd, env=env, start_new_session=label == "inspection",
        )
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if label == "inspection":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            return check(label, "FAIL", f"Timed out after {timeout:g} seconds.",
                         "Check for blocking module imports or an unguarded app.run(); increase --timeout for slow imports.")
    if process.returncode != 0:
        return check(label, "FAIL", f"Diagnostic process exited with code {process.returncode}.",
                     f"Inspect {folder / (label + '.log')}.")
    return None


def agent_checks():
    rows = []
    environment = os.environ.copy()
    try:
        importlib.import_module("smolagents")
        importlib.import_module("openai")
        rows.append(check("agent.dependencies", "PASS", "smolagents and its OpenAI client are importable."))
    except Exception as exc:
        traceback.print_exc()
        rows.append(check("agent.dependencies", "FAIL", f"Required agent dependencies unavailable ({type(exc).__name__}).",
                          "Restore required framework dependencies in this Python environment: uv sync"))
    finally:
        # Some libraries load .env files during import. A dependency
        # probe must not silently change the application's effective settings.
        os.environ.clear()
        os.environ.update(environment)
    key = os.getenv("SMOLAGENTS_API_KEY") or os.getenv("DEEPSEEK_API_KEY")
    if not key or not key.strip():
        rows.append(check("agent.credentials", "WARN", "No model API key configured.",
                          "Set SMOLAGENTS_API_KEY or DEEPSEEK_API_KEY in the server environment/.env for coding tasks."))
    else:
        rows.append(check("agent.credentials", "PASS", "A model API key is present; authentication was not tested."))
    base = os.getenv("SMOLAGENTS_API_BASE") or os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    try:
        url = urlsplit(base)
        valid = url.scheme in {"http", "https"} and bool(url.hostname)
        _ = url.port
    except ValueError:
        valid = False
    rows.append(check("agent.endpoint", "PASS" if valid else "WARN",
                      "Model endpoint URL is structurally valid; connectivity was not tested." if valid else "Model endpoint must be an HTTP(S) URL.",
                      None if valid else "Set SMOLAGENTS_API_BASE or DEEPSEEK_API_BASE to your compatible model endpoint."))
    model_id = os.getenv("SMOLAGENTS_MODEL_ID") or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if not model_id.strip():
        rows.append(check("agent.model", "WARN", "Model identifier is blank.", "Set SMOLAGENTS_MODEL_ID or DEEPSEEK_MODEL."))
    elif model_id.startswith("deepseek-") and os.getenv("DEEPSEEK_THINKING", "disabled").lower() not in {"enabled", "disabled"}:
        rows.append(check("agent.model", "WARN", "Invalid DeepSeek thinking setting.", "Set DEEPSEEK_THINKING to enabled or disabled."))
    else:
        rows.append(check("agent.model", "PASS", "Model settings are locally valid; model availability was not tested."))
    for name, minimum in (("SMOLAGENTS_MAX_STEPS", 1), ("SMOLAGENTS_MAX_TOOL_CALLS", 1),
                          ("SMOLAGENTS_MODEL_TIMEOUT_SECONDS", 1), ("SMOLAGENTS_PLANNING_INTERVAL", 0)):
        if name in os.environ:
            try:
                valid = int(os.environ[name]) >= minimum
            except ValueError:
                valid = False
            if not valid:
                rows.append(check("agent.budget", "WARN", f"{name} would be defaulted or clamped at runtime.",
                                  f"Set {name} to an integer >= {minimum}."))
    return rows


def inspect_application(server, attribute, folder, timeout):
    rows, expected = [], []
    try:
        # Matches Python's script import path, without executing its __main__ block.
        sys.path.insert(0, str(server.parent))
        namespace = runpy.run_path(str(server), run_name="__quant_mcp_preflight__")
        from quant_mcp.framework import MCPFramework
        app = namespace.get(attribute)
        if not isinstance(app, MCPFramework):
            rows.append(check("application", "FAIL", f"No MCPFramework instance named {attribute!r} found.",
                              "Expose the application as a module-level variable; use --app for a different name. Guard app.run() with if __name__ == '__main__'."))
            return {"checks": rows, "tools": expected}
        rows.append(check("application", "PASS", "Entry point imports and exposes an MCPFramework instance."))
        tools = asyncio.run(app.mcp.list_tools())
        expected = [tool.name for tool in tools]
        rows.append(check("registration", "PASS", f"{len(tools)} MCP tools registered: {', '.join(expected)}"))
        if not app._functions:
            rows.append(check("functions", "WARN", "No domain functions exposed.", "Register selected library functions with app.expose(...)."))
        try:
            app.artifacts.root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=app.artifacts.root) as stream:
                stream.write(b"preflight")
                stream.flush()
            rows.append(check("artifacts", "PASS", f"Artifacts directory is writable: {app.artifacts.root}"))
        except OSError as exc:
            traceback.print_exc()
            rows.append(check("artifacts", "FAIL", f"Artifacts directory is not writable ({type(exc).__name__}).",
                              "Choose a writable artifacts_dir or correct its filesystem permissions."))
        request = folder / "imports-request.json"
        output = folder / "imports-result.json"
        request.write_text(json.dumps({"functions": app._functions}), encoding="utf-8")
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(map(str, app._python_paths()))
        error = spawn_stage(["_imports", str(request), str(output)], folder, "imports", timeout,
                            env=env, cwd=folder)
        rows.extend([error] if error else json.loads(output.read_text()))
        rows.extend(agent_checks())
        rows.append(check("telemetry", "PASS", "Application telemetry setup completed; backend delivery was not tested."
                          if app.mcp.telemetry_setup else "No telemetry setup hook requested; progress logs work without an SDK."))
    except Exception as exc:
        traceback.print_exc()
        rows.append(failure("application", exc))
    return {"checks": rows, "tools": expected}


def inspect_imports(request):
    rows = []
    for spec in request["functions"]:
        try:
            module, name = spec["target"].split(":", 1)
            function = getattr(importlib.import_module(module), name)
            if not callable(function):
                raise TypeError("Registered target is no longer callable")
            rows.append(check("function.import", "PASS", f"{spec['name']} imports in a fresh worker; function was not called."))
        except Exception as exc:
            traceback.print_exc()
            row = failure("function.import", exc)
            row["message"] = spec["name"] + ": " + row["message"]
            rows.append(row)
    return rows


async def protocol_check(server, expected, folder, timeout):
    from quant_mcp.testing.launcher import MCPTestServer, ServerSpec

    class Capture(logging.FileHandler):
        failed = False
        def emit(self, record):
            if record.levelno >= logging.ERROR:
                self.failed = True
            super().emit(record)

    log = logging.getLogger("mcp.client.stdio")
    root_log = logging.getLogger()
    handler = Capture(folder / "client.log")
    old_handlers, old_propagate = log.handlers[:], log.propagate
    old_root_handlers = root_log.handlers[:]
    log.handlers, log.propagate = [handler], False
    root_log.handlers = [handler]
    try:
        spec = ServerSpec(
            command=sys.executable,
            args=(str(server),),
            cwd=Path.cwd(),
            startup_timeout_seconds=timeout,
            call_timeout_seconds=timeout,
        )
        async with MCPTestServer(spec, stderr_path=folder / "server.log") as client:
            names = [tool.name for tool in await client.list_tools()]
        if handler.failed:
            return check("mcp.stdio", "FAIL", "Server emitted malformed JSON-RPC on stdout.",
                         "Send startup messages/library output to stderr. Inspect client.log and server.log.")
        if set(names) != set(expected):
            return check("mcp.stdio", "FAIL", "Launched server advertises different tools from the inspected application.",
                         "Ensure the __main__ block runs the same application and registrations.")
        return check("mcp.stdio", "PASS", "Real stdio initialize, ping and tools/list passed; no tools were executed.")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            raise
        with (folder / "protocol.log").open("w", encoding="utf-8") as stream:
            traceback.print_exc(file=stream)
        return check("mcp.stdio", "FAIL", f"MCP handshake failed or timed out ({type(exc).__name__}).",
                     "Check server.log/client.log/protocol.log. Guard app.run(), use stdio, and keep stdout JSON-RPC only; increase --timeout for slow startup.")
    finally:
        log.handlers, log.propagate = old_handlers, old_propagate
        root_log.handlers = old_root_handlers
        handler.close()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "_inspect":
        server, attribute, folder, timeout = argv[1:]
        folder = Path(folder)
        report = inspect_application(Path(server), attribute, folder, float(timeout))
        (folder / "inspection.json").write_text(json.dumps(report), encoding="utf-8")
        return 0
    if argv and argv[0] == "_imports":
        rows = inspect_imports(json.loads(Path(argv[1]).read_text()))
        Path(argv[2]).write_text(json.dumps(rows), encoding="utf-8")
        return 0
    parser = argparse.ArgumentParser(description="Check an MCPFramework entry point without executing tools or calling a model API.")
    parser.add_argument("server", type=Path, help="Python server entry point (module-level app; guarded app.run())")
    parser.add_argument("--app", default="app", help="Application variable name (default: app)")
    parser.add_argument("--timeout", type=float, default=30, help="Per-stage timeout in seconds (default: 30)")
    parser.add_argument("--require-agent", action="store_true", help="Treat missing/invalid agent configuration as a failure")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")
    args = parser.parse_args(argv)
    if not 0 < args.timeout <= 300:
        parser.error("--timeout must be greater than 0 and at most 300 seconds")
    server = args.server.expanduser().resolve()
    folder = Path(tempfile.mkdtemp(prefix="quant-mcp-preflight-"))
    if not args.json:
        print(f"MCP pre-flight: {server}\nChecking startup, configuration and worker imports...", flush=True)
    rows = [check("python", "PASS" if sys.version_info >= (3, 11) else "FAIL", f"Python {sys.version.split()[0]}: {sys.executable}"),
            check("platform", "PASS" if os.name == "posix" else "FAIL", "POSIX workers required (macOS/Linux).")]
    for package in ("mcp", "anyio", "pydantic", "opentelemetry-api"):
        try:
            rows.append(check("dependency", "PASS", f"{package} {importlib.metadata.version(package)} installed."))
        except importlib.metadata.PackageNotFoundError:
            rows.append(check("dependency", "FAIL", f"{package} is missing.",
                              "Install/sync the framework in the Python environment shown above."))
    if not server.is_file():
        rows.append(check("entrypoint", "FAIL", f"File does not exist: {server}", "Run from the project root or pass an absolute path."))
    if not any(row["status"] == "FAIL" for row in rows):
        error = spawn_stage(["_inspect", str(server), args.app, str(folder), str(args.timeout)],
                            folder, "inspection", args.timeout)
        if error:
            rows.append(error)
        elif (folder / "inspection.json").exists():
            inspection = json.loads((folder / "inspection.json").read_text())
            rows.extend(inspection["checks"])
            if not any(row["status"] == "FAIL" for row in inspection["checks"]):
                if not args.json:
                    print("Checking the real MCP stdio connection...", flush=True)
                rows.append(asyncio.run(protocol_check(server, inspection["tools"], folder, args.timeout)))
            else:
                rows.append(check("mcp.stdio", "SKIP", "Resolve inspection failures before the protocol check."))
        else:
            rows.append(check("inspection", "FAIL", "Application exited before inspection completed.", "Check inspection.log; guard executable startup code with __main__."))
    if args.require_agent:
        for row in rows:
            if row["name"].startswith("agent.") and row["status"] == "WARN":
                row["status"] = "FAIL"
    ok = not any(row["status"] == "FAIL" for row in rows)
    report = {"ok": ok, "server": str(server), "diagnostics_dir": str(folder), "checks": rows}
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print()
        for row in rows:
            print(f"[{row['status']}] {row['name']}: {row['message']}")
            if row.get("fix"):
                print(f"       Fix: {row['fix']}")
        print(f"\n{'PASS' if ok else 'FAIL'}: {sum(r['status'] == 'WARN' for r in rows)} warning(s). Diagnostics: {folder}")
        print("Model authentication, backend connectivity and business results were not tested.")
        print("The server is stopped. Let your MCP client launch it to ask questions; do not type into its stdio terminal.")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
