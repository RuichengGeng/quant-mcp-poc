from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Callable

from quant_mcp.agent.base import AgentResult
from quant_mcp.library_catalog import discover_library_catalog

ApprovalCallback = Callable[[str, int], bool]

SCRIPT_SYSTEM_PROMPT = """You write and debug complete Python task scripts.
Each action must contain one complete, self-contained Python script between
{{code_block_opening_tag}} and {{code_block_closing_tag}}.
The executor saves that code as analysis.py and runs it with normal Python in
WORKSPACE. Each attempt starts a fresh Python process; variables do not persist.
Import the registered Python libraries directly. They are installed already.
Start by writing the complete solution, not an import probe or exploration.
Do not wrap your script in a string or write analysis.py yourself.
Your script must create result.json with status, task_type, summary, metrics,
assumptions and artifacts, plus any files the user requests.
artifacts must be a list of objects with name and path fields, for example
[{"name": "Report", "path": "report.csv"}], or [] if no extra files are needed.
summary is a string, metrics is an object, and assumptions is a list of strings.
Compute reported
numbers from the actual library returns. Never invent dictionary keys or values.
Use print for debugging if needed. Do not call final_answer: the executor ends
the task automatically once the script succeeds and result.json is validated.
If execution fails, the observation includes the traceback. Correct the problem
and return the complete replacement script in your next action.
Keep the script concise. All generated files belong in WORKSPACE.
When reporting payoff and profit, label gross payoff and profit after costs separately.
"""


class SmolagentsCodingAgent:
    """CodeAgent writes whole scripts; one executor owns running and repairing them."""

    def __init__(self, approval_callback: ApprovalCallback | None = None, auto_approve: bool = False):
        self.approval_callback = approval_callback
        self.auto_approve = auto_approve

    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        try:
            from rich.console import Console
            from smolagents import CodeAgent, OpenAIModel
            from smolagents.agents import EMPTY_PROMPT_TEMPLATES
            from smolagents.local_python_executor import CodeOutput
            from smolagents.memory import ActionStep
            from smolagents.monitoring import AgentLogger
        except ImportError:
            return AgentResult(status="failed", message="smolagents is not installed; install the smolagents-demo extra")

        from quant_mcp import runner

        api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("PI_API_KEY")
        if not api_key:
            return AgentResult(status="failed", message="DEEPSEEK_API_KEY or PI_API_KEY is not set")

        catalog = discover_library_catalog()
        prompt_path = workspace_dir / "prompt.txt"
        prompt = prompt_path.read_text() if prompt_path.exists() else instruction
        max_steps = _env_int("SMOLAGENTS_MAX_STEPS", 8)
        completed: dict | None = None
        rejected = False
        actions = 0
        last_error = "No script was executed"
        agent = None
        owner = self

        class ScriptExecutor:
            def send_tools(self, tools: dict) -> None:
                pass  # Scripts use ordinary imports, not injected function wrappers.

            def send_variables(self, variables: dict) -> None:
                pass

            def __call__(self, code_action: str) -> CodeOutput:
                nonlocal completed, rejected, actions, last_error
                actions += 1
                if owner.approval_callback is not None:
                    approved = owner.approval_callback(code_action, actions)
                else:
                    approved = owner.auto_approve or _terminal_approval(code_action, actions)
                with (workspace_dir / "code_review.md").open("a", encoding="utf-8") as log:
                    log.write(f"action_{actions:02d}: {'approved' if approved else 'rejected'}\n")
                if not approved:
                    rejected = True
                    return CodeOutput(output="Execution rejected by reviewer", logs="", is_final_answer=True)

                analysis_path = workspace_dir / "analysis.py"
                analysis_path.write_text(code_action + "\n", encoding="utf-8")
                result_path = workspace_dir / "result.json"
                result_path.unlink(missing_ok=True)
                status = AgentResult(status="success", message=f"smolagents action {actions}")
                error = runner._validate_analysis_source(prompt, analysis_path, catalog)
                if error:
                    runner._write_attempt_log(workspace_dir, status, error)
                else:
                    error = runner._execute_analysis(workspace_dir, analysis_path, status, timeout_seconds)
                if not error:
                    try:
                        result = json.loads(result_path.read_text(encoding="utf-8"))
                        if not isinstance(result, dict):
                            raise ValueError("result.json must contain an object")
                        result = runner._validate_result(workspace_dir.name, workspace_dir, result)
                        if result.get("status") == "failed":
                            error = result.get("summary", "Task reported failure")
                        else:
                            completed = result
                            return CodeOutput(output=result, logs="Script executed and result validated.", is_final_answer=True)
                    except (OSError, ValueError, TypeError, AttributeError) as exc:
                        error = f"Script must write a valid result.json: {exc}"
                last_error = str(error)
                runner._snapshot_attempt(workspace_dir, actions, last_error)
                execution_log = (workspace_dir / "run.log").read_text(encoding="utf-8")
                if execution_log.strip():
                    last_error += ": " + execution_log.strip().splitlines()[-1][:500]
                return CodeOutput(
                    output="Fix the error and submit the complete replacement Python script.",
                    logs=f"{last_error}\n{execution_log[-12000:]}",
                    is_final_answer=False,
                )

        try:
            model_id = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
            model_options = {}
            if model_id.startswith("deepseek-"):
                thinking = os.getenv("DEEPSEEK_THINKING", "disabled").lower()
                if thinking not in {"enabled", "disabled"}:
                    raise ValueError("DEEPSEEK_THINKING must be enabled or disabled")
                model_options["extra_body"] = {"thinking": {"type": thinking}}
            model = OpenAIModel(
                model_id=model_id,
                api_base=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
                api_key=api_key,
                client_kwargs={
                    "timeout": min(_env_int("SMOLAGENTS_MODEL_TIMEOUT_SECONDS", 120), timeout_seconds),
                    "max_retries": 1,
                },
                **model_options,
            )
            templates = copy.deepcopy(EMPTY_PROMPT_TEMPLATES)
            templates["system_prompt"] = SCRIPT_SYSTEM_PROMPT
            agent = CodeAgent(
                tools=[], model=model, executor=ScriptExecutor(), max_steps=max_steps,
                prompt_templates=templates,
                logger=AgentLogger(console=Console(stderr=True)),
            )
            # This adapter's executor, not the model, saves and executes analysis.py.
            task = f"USER REQUEST:\n{prompt}\n\nWORKSPACE:\n{workspace_dir}\n\n{catalog.render_for_prompt()}"
            (workspace_dir / "agent_instruction.md").write_text(
                agent.system_prompt + "\n\n" + task, encoding="utf-8"
            )
            stream = agent.run(task, stream=True)
            try:
                for step in stream:
                    if isinstance(step, ActionStep):
                        if completed is not None or rejected:
                            break
                        if step.error:
                            last_error = str(step.error)
                        if step.step_number >= max_steps:
                            # Do not spend another model request on a summary of failure.
                            break
            finally:
                stream.close()
            if rejected:
                return AgentResult(status="failed", message="Execution rejected by reviewer")
            if completed is None:
                return AgentResult(status="failed", message=f"No validated result after {max_steps} steps: {last_error}")
            return AgentResult(status="success", message=f"Executed and validated in {actions} code action(s)", executed=True)
        except Exception as exc:
            return AgentResult(status="failed", message=f"{type(exc).__name__}: {exc}")
        finally:
            if agent is not None:
                (workspace_dir / "smolagents_result.json").write_text(json.dumps({
                    "status": "success" if completed is not None else "failed",
                    "actions": actions,
                    "last_error": None if completed is not None else last_error,
                    "steps": agent.memory.get_full_steps(),
                }, indent=2, default=str), encoding="utf-8")


def _terminal_approval(code_action: str, action_number: int) -> bool:
    print(f"\n--- Generated action {action_number} ---\n{code_action}\n")
    return input("Approve this code action? [y/N] ").strip().lower() == "y"


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 1)
    except ValueError:
        return default
