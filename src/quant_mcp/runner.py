from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from quant_mcp.agent import CodingAgent, LocalDemoAgent, PiCodingAgent, SmolagentsCodingAgent
from quant_mcp.agent.base import AgentResult
from quant_mcp.config import load_env_file
from quant_mcp.library_catalog import LibraryCatalog, discover_library_catalog, write_catalog

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"


@dataclass(frozen=True)
class QuantTaskResult:
    task_id: str
    task_dir: Path
    result: dict


def build_agent() -> CodingAgent:
    load_env_file()
    selected = os.getenv("QUANT_MCP_AGENT", "local").lower()
    if selected == "pi":
        return PiCodingAgent()
    if selected == "smolagents":
        # MCP and non-interactive runner calls have no terminal stdin for approval.
        return SmolagentsCodingAgent(auto_approve=True)
    return LocalDemoAgent()


def run_quant_task(
    prompt: str,
    agent: CodingAgent | None = None,
    timeout_seconds: int = 120,
    repair_attempts: int = 2,
) -> QuantTaskResult:
    task_id = _new_task_id()
    task_dir = ARTIFACTS_DIR / task_id
    task_dir.mkdir(parents=True, exist_ok=False)

    catalog = discover_library_catalog()
    instruction = _agent_instruction(prompt, task_dir)
    (task_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (task_dir / "agent_instruction.md").write_text(instruction, encoding="utf-8")
    write_catalog(catalog, task_dir / "library_catalog.json")

    selected_agent = agent or build_agent()
    agent_result = _run_agent_with_fallback(
        selected_agent, agent, instruction, task_dir, timeout_seconds
    )
    if agent_result.status != "success":
        return _write_failure(task_id, task_dir, f"Agent failed: {agent_result.message}")

    if agent_result.executed:
        # The agent's executor already ran and validated this exact script.
        # Replaying it here would repeat side effects and bypass its repair loop.
        try:
            result = json.loads((task_dir / "result.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return _write_failure(task_id, task_dir, f"Executed task has no valid result: {exc}")
        result = _validate_result(task_id, task_dir, result)
        return QuantTaskResult(task_id=task_id, task_dir=task_dir, result=result)

    analysis_path = task_dir / "analysis.py"
    if not analysis_path.exists():
        result_path = task_dir / "result.json"
        if result_path.exists():
            log_path = task_dir / "run.log"
            if not log_path.exists():
                log_path.write_text(
                    f"agent_status={agent_result.status}\n"
                    f"agent_message={agent_result.message}\n\n"
                    "No analysis.py was created because the agent classified this as a no-code response.\n",
                    encoding="utf-8",
                )
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                return _write_failure(task_id, task_dir, f"result.json is invalid JSON: {exc}")
            result = _validate_result(task_id, task_dir, result)
            return QuantTaskResult(task_id=task_id, task_dir=task_dir, result=result)
        return _write_failure(task_id, task_dir, "Agent did not create analysis.py or result.json")

    repair_attempts = max(int(repair_attempts), 0)
    for attempt_index in range(repair_attempts + 1):
        validation_error = _validate_analysis_source(prompt, analysis_path, catalog)
        if validation_error:
            failure_message = validation_error
            _write_attempt_log(task_dir, agent_result, failure_message)
        else:
            (task_dir / "result.json").unlink(missing_ok=True)
            failure_message = _execute_analysis(task_dir, analysis_path, agent_result, timeout_seconds)
            if failure_message is None:
                result_path = task_dir / "result.json"
                if not result_path.exists():
                    failure_message = "analysis.py did not create result.json"
                else:
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError as exc:
                        failure_message = f"result.json is invalid JSON: {exc}"
                    else:
                        result = _validate_result(task_id, task_dir, result)
                        if result.get("status") != "failed":
                            return QuantTaskResult(task_id=task_id, task_dir=task_dir, result=result)
                        failure_message = result.get("summary", "result.json reported failure")

        if attempt_index >= repair_attempts:
            return _write_failure(task_id, task_dir, failure_message)

        snapshot_number = attempt_index + 1
        _snapshot_attempt(task_dir, snapshot_number, failure_message)
        repair_instruction = _repair_instruction(
            prompt, task_dir, snapshot_number + 1, failure_message
        )
        (task_dir / f"repair_instruction_{snapshot_number:02d}.md").write_text(
            repair_instruction, encoding="utf-8"
        )
        agent_result = _run_agent_with_fallback(
            selected_agent, agent, repair_instruction, task_dir, timeout_seconds
        )
        if agent_result.status != "success":
            return _write_failure(task_id, task_dir, f"Repair agent failed: {agent_result.message}")
        if not analysis_path.exists():
            return _write_failure(task_id, task_dir, "Repair agent did not recreate analysis.py")

    return _write_failure(task_id, task_dir, "Task exhausted repair attempts")


def run_option_analysis(
    prompt: str,
    agent: CodingAgent | None = None,
    timeout_seconds: int = 120,
    repair_attempts: int = 2,
) -> QuantTaskResult:
    return run_quant_task(
        prompt=prompt,
        agent=agent,
        timeout_seconds=timeout_seconds,
        repair_attempts=repair_attempts,
    )


def _run_agent_with_fallback(
    selected_agent: CodingAgent,
    agent_argument: CodingAgent | None,
    instruction: str,
    task_dir: Path,
    timeout_seconds: int,
) -> AgentResult:
    try:
        agent_result = selected_agent.run_task(instruction, task_dir, timeout_seconds)
    except Exception as exc:
        agent_result = AgentResult(status="failed", message=f"{type(exc).__name__}: {exc}")
    if agent_result.status == "success" or not _allow_fallback(agent_argument, selected_agent):
        return agent_result

    fallback_result = LocalDemoAgent().run_task(instruction, task_dir, timeout_seconds)
    return AgentResult(
        status=fallback_result.status,
        message=(
            f"Primary agent failed: {agent_result.message}. "
            f"Fallback agent: {fallback_result.message}"
        ),
    )


def _execute_analysis(
    task_dir: Path,
    analysis_path: Path,
    agent_result: AgentResult,
    timeout_seconds: int,
) -> str | None:
    log_path = task_dir / "run.log"
    env = os.environ.copy()
    python_path = str(PROJECT_ROOT / "src")
    env["PYTHONPATH"] = python_path if not env.get("PYTHONPATH") else f"{python_path}{os.pathsep}{env['PYTHONPATH']}"
    try:
        completed = subprocess.run(
            [sys.executable, str(analysis_path)],
            cwd=str(task_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        message = f"analysis.py timed out after {timeout_seconds} seconds"
        log_path.write_text(
            f"agent_status={agent_result.status}\nagent_message={agent_result.message}\n\n"
            f"{message}.\nstdout:\n{exc.stdout or ''}\n\nstderr:\n{exc.stderr or ''}\n",
            encoding="utf-8",
        )
        return message

    log_path.write_text(
        f"agent_status={agent_result.status}\nagent_message={agent_result.message}\n\n"
        f"stdout:\n{completed.stdout}\n\nstderr:\n{completed.stderr}\n",
        encoding="utf-8",
    )
    if completed.returncode != 0:
        return f"analysis.py failed with exit code {completed.returncode}"
    return None


def _write_attempt_log(task_dir: Path, agent_result: AgentResult, message: str) -> None:
    (task_dir / "run.log").write_text(
        f"agent_status={agent_result.status}\nagent_message={agent_result.message}\n\n"
        f"pre-execution validation failed: {message}\n",
        encoding="utf-8",
    )


def _snapshot_attempt(task_dir: Path, attempt_number: int, failure_message: str) -> None:
    attempt_dir = task_dir / "attempts" / f"attempt_{attempt_number:02d}"
    attempt_dir.mkdir(parents=True, exist_ok=True)
    for filename in ("analysis.py", "run.log"):
        source = task_dir / filename
        if source.exists():
            shutil.copy2(source, attempt_dir / filename)
    (attempt_dir / "failure.txt").write_text(failure_message + "\n", encoding="utf-8")


def _repair_instruction(
    prompt: str,
    task_dir: Path,
    attempt_number: int,
    failure_message: str,
) -> str:
    return f"""You are repairing a failed task in an existing workspace.

USER REQUEST:
{prompt}

WORKSPACE:
{task_dir}

This is repair attempt {attempt_number}. The previous execution failed:
{failure_message}

Read the current analysis.py and run.log in WORKSPACE. Fix the task in the same
workspace. Preserve the user's requested behavior and registered deterministic
library usage. Do not create a new task folder. Rewrite analysis.py as needed;
the local runner will execute it again after you finish. Make all optional
summary lookups None-safe and do not invent missing data.
"""


def list_artifacts() -> list[dict]:
    if not ARTIFACTS_DIR.exists():
        return []
    rows = []
    for task_dir in sorted(ARTIFACTS_DIR.iterdir(), reverse=True):
        result_path = task_dir / "result.json"
        if result_path.exists():
            result = json.loads(result_path.read_text(encoding="utf-8"))
            rows.append(
                {
                    "task_id": task_dir.name,
                    "status": result.get("status"),
                    "summary": result.get("summary"),
                    "task_dir": str(task_dir),
                }
            )
    return rows


def read_artifact(task_id: str, filename: str) -> str:
    task_dir = _resolve_task_dir(task_id)
    artifact_path = (task_dir / filename).resolve()
    if task_dir not in artifact_path.parents and artifact_path != task_dir:
        raise ValueError("artifact path escapes task directory")
    if not artifact_path.exists():
        raise FileNotFoundError(str(artifact_path))
    if artifact_path.suffix.lower() in {".xlsx", ".png", ".pdf"}:
        return str(artifact_path)
    return artifact_path.read_text(encoding="utf-8")


def _resolve_task_dir(task_id: str) -> Path:
    task_dir = (ARTIFACTS_DIR / task_id).resolve()
    if not task_dir.exists():
        raise FileNotFoundError(task_id)
    if ARTIFACTS_DIR.resolve() not in task_dir.parents:
        raise ValueError("task_id escapes artifacts directory")
    return task_dir


def _new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"task_{stamp}_{uuid.uuid4().hex[:8]}"


def _agent_instruction(prompt: str, task_dir: Path) -> str:
    catalog = discover_library_catalog()
    return f"""You are a coding agent for a quant analyst's local task runner.

USER REQUEST:
{prompt}

WORKSPACE:
{task_dir}

{catalog.render_for_prompt()}

Your job:
- Decide whether the user is asking for a concrete coding/data/quant task.
- If no concrete task is requested, do not invent one. Write result.json with status "success",
  task_type "no_action", a short natural response, assumptions [], metrics {{}}, and artifacts [].
- If a task is requested, write task-specific Python code and artifacts in WORKSPACE.
- If the request cannot be completed with the provided information or tools, do not invent results.
  Write result.json with status "failed", a clear summary, assumptions [], metrics {{}}, and artifacts [].

Available local library examples:
- Use the registered library APIs above for deterministic domain calculations.
- Do not copy, approximate, or reimplement a registered library function in analysis.py.
- Use price_option/calc_greeks when expiry and valuation_date are dates or "YYYY-MM-DD" strings.
- Use price_option_t/calc_greeks_t when the user gives an exact numeric T such as T=0.25 years.
- If the user gives a relative expiry such as 3 months, choose clear date strings such as
  valuation_date="2026-09-04" and expiry="2026-12-04", then state the day-count assumption.

For option pricing tasks, you must import and call the registered quant_mcp.pricing functions.
Do not reimplement Black-Scholes formulas, normal CDF/PDF, d1/d2 pricing logic, or Greeks formulas in analysis.py.
These option functions are available when the task needs option pricing. They are not the whole system.
Future local quant libraries should be used the same way: import stable library functions, then write
bespoke analysis/reporting code around them.

Create a reproducible task output inside WORKSPACE.
You may write files in WORKSPACE. The local MCP runner will execute analysis.py after you finish.
The project root is:
{PROJECT_ROOT}

Fast-path rules:
- Do not inspect the repository for small self-contained pricing/reporting tasks.
- Do not create helper, scratch, exploration, placeholder, or temporary files.
- Write the requested deliverables directly.
- If analysis.py can generate result.json and the requested artifacts when executed, it is enough to create analysis.py.
- Prefer one direct write of analysis.py. Avoid multi-turn exploration unless the request truly depends on project internals.

Required files for coding/data tasks:
- analysis.py
- result.json
- any task-relevant artifacts requested by the user, such as html/csv/xlsx/png/yaml

Rules:
- Do not modify local libraries.
- Put every generated output in WORKSPACE.
- result.json must include status, summary, task_type, metrics, assumptions, and artifacts.
- artifacts must be a list of objects with name and path.
"""


def _allow_fallback(agent: CodingAgent | None, selected_agent: CodingAgent) -> bool:
    return (
        agent is None
        and not isinstance(selected_agent, LocalDemoAgent)
        and os.getenv("QUANT_MCP_ALLOW_FALLBACK", "").lower() in {"1", "true", "yes"}
    )


def _validate_analysis_source(
    prompt: str,
    analysis_path: Path,
    catalog: LibraryCatalog | None = None,
) -> str | None:
    catalog = catalog or discover_library_catalog()
    if not _looks_like_library_task(prompt):
        return None

    try:
        source = analysis_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError) as exc:
        return f"analysis.py could not be inspected before execution: {exc}"

    if _looks_like_option_pricing_prompt(prompt):
        forbidden_reason = _find_forbidden_option_pricing_reimplementation(tree)
        if forbidden_reason:
            return (
                "Generated analysis.py appears to reimplement option pricing "
                f"({forbidden_reason}) instead of relying on quant_mcp.pricing."
            )
    if _uses_registered_library_api(tree, catalog):
        return None
    return (
        "Generated analysis.py did not call a registered deterministic library API. "
        "Rejecting the task instead of accepting a self-implemented domain calculation."
    )


def _looks_like_library_task(prompt: str) -> bool:
    text = prompt.lower()
    terms = {
        "quant", "option", "call", "put", "strike", "expiry", "spread", "vol",
        "price", "pricing", "premium", "greek", "portfolio", "position", "market",
        "scenario", "pnl", "analytics", "data",
    }
    return any(term in text for term in terms)


def _looks_like_option_pricing_prompt(prompt: str) -> bool:
    text = prompt.lower()
    option_terms = {"option", "call", "put", "strike", "expiry", "expiring", "spread", "volatility", "vol"}
    pricing_terms = {"price", "pricing", "premium", "greek", "delta", "gamma", "vega", "theta", "rho"}
    return any(term in text for term in option_terms) and any(term in text for term in pricing_terms)


def _find_forbidden_option_pricing_reimplementation(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scipy.stats":
                    return "imports scipy.stats directly"
        elif isinstance(node, ast.ImportFrom):
            if node.module == "statistics" and any(alias.name == "NormalDist" for alias in node.names):
                return "imports NormalDist"
            if node.module == "scipy.stats" and any(alias.name == "norm" for alias in node.names):
                return "imports scipy.stats.norm directly"
        elif isinstance(node, ast.FunctionDef) and _looks_like_local_pricer_name(node.name):
            return f"defines local pricing function {node.name}"
        elif isinstance(node, ast.Call):
            call_name = _dotted_name(node.func)
            if call_name in {"math.erf", "erf"}:
                return "calls erf for normal CDF logic"
            if call_name in {"statistics.NormalDist", "NormalDist"}:
                return "uses NormalDist for distribution math"
    return None


def _looks_like_local_pricer_name(name: str) -> bool:
    normalized = name.lower()
    return normalized in {"bs", "black_scholes"} or normalized.startswith(("bs_", "black_scholes_"))


def _uses_registered_library_api(tree: ast.AST, catalog: LibraryCatalog) -> bool:
    module_aliases: dict[str, set[str]] = {}
    direct_functions: set[tuple[str, str]] = set()
    function_names_by_module = catalog.function_names_by_module()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in function_names_by_module:
                    module_aliases.setdefault(alias.name, set()).add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module in function_names_by_module:
                for alias in node.names:
                    if alias.name in function_names_by_module[node.module]:
                        direct_functions.add((node.module, alias.asname or alias.name))
            for module_name in function_names_by_module:
                parent, _, child = module_name.rpartition(".")
                if node.module == parent:
                    for alias in node.names:
                        if alias.name == child:
                            module_aliases.setdefault(module_name, set()).add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and any(name == node.func.id for _, name in direct_functions):
            return True
        if isinstance(node.func, ast.Attribute):
            for module_name, aliases in module_aliases.items():
                if node.func.attr in function_names_by_module[module_name] and _dotted_name(node.func.value) in aliases:
                    return True
    return False


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        if base:
            return f"{base}.{node.attr}"
    return None


def _validate_result(task_id: str, task_dir: Path, result: dict) -> dict:
    if result.get("status") not in {"success", "failed"}:
        return _write_failure(task_id, task_dir, f"result status is invalid: {result.get('status')}").result
    for key in ["summary", "metrics", "assumptions", "artifacts"]:
        if key not in result:
            return _write_failure(task_id, task_dir, f"result.json missing key: {key}").result
    for key, expected_type in [("summary", str), ("metrics", dict), ("assumptions", list)]:
        if not isinstance(result[key], expected_type):
            return _write_failure(task_id, task_dir, f"result.json {key} must be a {expected_type.__name__}").result
    artifacts = result["artifacts"]
    if not isinstance(artifacts, list) or any(not isinstance(item, dict) for item in artifacts):
        return _write_failure(task_id, task_dir, 'artifacts must be a list of objects with name and path fields, or []').result
    result["artifacts"] = _with_core_artifacts(task_dir, artifacts)
    for artifact in result["artifacts"]:
        raw_path = artifact.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return _write_failure(task_id, task_dir, "artifact is missing a path").result
        path = Path(raw_path)
        resolved = (path if path.is_absolute() else task_dir / path).resolve()
        if task_dir.resolve() not in resolved.parents:
            return _write_failure(task_id, task_dir, f"artifact path escapes task directory: {raw_path}").result
        if not resolved.exists():
            return _write_failure(task_id, task_dir, f"artifact does not exist: {resolved}").result
    return result


def _with_core_artifacts(task_dir: Path, artifacts: list[dict]) -> list[dict]:
    existing = {item.get("path") for item in artifacts if isinstance(item, dict)}
    core = [
        ("User request", task_dir / "prompt.txt"),
        ("LLM instruction", task_dir / "agent_instruction.md"),
        ("Library catalog", task_dir / "library_catalog.json"),
        ("Generated analysis code", task_dir / "analysis.py"),
        ("Execution log", task_dir / "run.log"),
    ]
    if (task_dir / "attempts").exists():
        core.append(("Repair attempts", task_dir / "attempts"))
    result = list(artifacts)
    for name, path in core:
        path_string = str(path)
        if path.exists() and path_string not in existing:
            result.append({"name": name, "path": path_string})
    return result


def _write_failure(task_id: str, task_dir: Path, message: str) -> QuantTaskResult:
    result = {
        "status": "failed",
        "task_type": "failed",
        "summary": message,
        "metrics": {},
        "assumptions": [],
        "artifacts": _with_core_artifacts(
            task_dir,
            [
                {"name": "Task directory", "path": str(task_dir)},
                {"name": "Failure log", "path": str(task_dir / "run.log")},
            ],
        ),
    }
    task_dir.mkdir(parents=True, exist_ok=True)
    log_path = task_dir / "run.log"
    if not log_path.exists():
        log_path.write_text(f"status=failed\nmessage={message}\n", encoding="utf-8")
    (task_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return QuantTaskResult(task_id=task_id, task_dir=task_dir, result=result)
