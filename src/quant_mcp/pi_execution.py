"""Trusted execution entry point for Pi's run_python tool (ordinary local Python)."""
from __future__ import annotations

import ast
import hashlib
import json
import os
import runpy
import subprocess
import sys
from pathlib import Path

from quant_mcp.config import PROJECT_ROOT
from quant_mcp.codebases import load_codebases, load_task_codebases


def library_modules() -> tuple[str, ...]:
    return load_codebases().modules


def repository_root() -> Path:
    return load_codebases().primary_root


def _trace_script(script: Path) -> None:
    modules = load_task_codebases(script.parent).modules
    calls: dict[tuple[str, str, str], int] = {}

    def profile(frame, event, arg):
        if event != "call":
            return
        module = frame.f_globals.get("__name__", "")
        name = frame.f_code.co_name
        if name.startswith("_") or name.startswith("<"):
            return
        if any(module == m or module.startswith(m + ".") for m in modules):
            # Ignore calls made while a library is being imported, without eagerly
            # importing every configured library (which could initialize resources).
            caller = frame.f_back
            while caller is not None:
                if caller.f_code.co_filename == str(script):
                    break
                if caller.f_code.co_name == "<module>":
                    return
                caller = caller.f_back
            if caller is None:
                return
            key = (module, frame.f_code.co_qualname, frame.f_code.co_filename)
            calls[key] = calls.get(key, 0) + 1

    digest = hashlib.sha256(script.read_bytes()).hexdigest()
    sys.setprofile(profile)
    try:
        runpy.run_path(str(script), run_name="__main__")
    finally:
        sys.setprofile(None)
        Path("library_calls.json").write_text(json.dumps({
            "script_sha256": digest,
            "calls": [dict(module=m, symbol=s, file=f, count=n) for (m, s, f), n in sorted(calls.items())],
        }, indent=2) + "\n")


def execute_script(task_dir: Path, timeout_seconds: int) -> dict:
    from quant_mcp import runner

    script = task_dir / "analysis.py"
    result_path = task_dir / "result.json"
    trace_path = task_dir / "library_calls.json"
    result_path.unlink(missing_ok=True)
    trace_path.unlink(missing_ok=True)
    log_path = task_dir / "run.log"
    log_path.unlink(missing_ok=True)
    error = None
    try:
        source = script.read_text()
        tree = ast.parse(source)
        prompt = (task_dir / "prompt.txt").read_text()
        if runner._looks_like_option_pricing_prompt(prompt):
            reason = runner._find_forbidden_option_pricing_reimplementation(tree)
            if reason:
                raise ValueError(f"Reuse policy: handwritten option pricing detected ({reason}). Read and call the existing library.")
        env = os.environ.copy()
        config = load_task_codebases(task_dir)
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [
            str(PROJECT_ROOT / "src"), *(str(p) for p in config.python_paths), env.get("PYTHONPATH"),
        ]))
        with log_path.open("w") as log:
            completed = subprocess.run(
                [sys.executable, "-m", "quant_mcp.pi_execution", "--child", str(script)],
                cwd=task_dir, env=env, stdout=log, stderr=log, timeout=timeout_seconds,
            )
        if completed.returncode:
            raise ValueError(f"Python exited with code {completed.returncode}. Read the traceback and repair the script.")
        trace = json.loads(trace_path.read_text())
        if not trace["calls"]:
            raise ValueError("Reuse policy: no registered library function or public method actually ran. Imports, constructors, and dead code do not count. Read the library and examples, then call its existing domain logic. If no suitable capability exists, report that instead of implementing a replacement.")
        if trace["script_sha256"] != hashlib.sha256(script.read_bytes()).hexdigest():
            raise ValueError("analysis.py changed during execution; submit a stable script.")
        result = json.loads(result_path.read_text())
        if not isinstance(result, dict):
            raise ValueError("result.json must be an object")
        result = runner._validate_result(task_dir.name, task_dir, result)
        if result.get("status") != "success":
            raise ValueError(result.get("summary", "Task did not succeed"))
        result_path.write_text(json.dumps(result, indent=2) + "\n")
        return {"status": "success", "result": result, "library_calls": trace["calls"]}
    except (OSError, ValueError, SyntaxError, KeyError, subprocess.TimeoutExpired) as exc:
        error = f"{type(exc).__name__}: {exc}"
    if not log_path.exists():
        log_path.write_text(error + "\n")
    attempts = task_dir / "attempts"
    attempt = 1 + len(list(attempts.glob("attempt_*")))
    runner._snapshot_attempt(task_dir, attempt, error)
    # An old or invalid result must never be accepted after a failed execution.
    result_path.unlink(missing_ok=True)
    return {"status": "failed", "error": error, "log": log_path.read_text()[-12000:]}


def main() -> None:
    if sys.argv[1] == "--child":
        _trace_script(Path(sys.argv[2]))
    else:
        result = execute_script(Path(sys.argv[1]).resolve(), int(sys.argv[2]))
        print(json.dumps(result))


if __name__ == "__main__":
    main()
