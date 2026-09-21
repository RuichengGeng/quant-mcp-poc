"""CodeAgent composes the explicitly exposed functions, with no source discovery."""
from __future__ import annotations

import asyncio
import base64
import importlib
import inspect
import functools
from contextvars import copy_context
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from quant_mcp.agent.model import env_int, build_model
from quant_mcp.artifacts import ArtifactStore
from quant_mcp.function_process import call_function
from quant_mcp.progress import RunContext, current_run, emit_event, flush_progress_logs, new_task_id, timestamp
from quant_mcp.tracing import operation, set_outcome
from quant_mcp.agent.model import instrument_model, restore_model


INSTRUCTIONS = """Solve the request by composing the registered functions in Python.
Use their documented arguments and units. You may loop, branch, retain variables
between code actions, aggregate results and format reports. Call functions directly
by their tool names; do not import office libraries, inspect repositories, or invent
missing domain algorithms. There are no source-search tools or run_python tool.
Function results are JSON-compatible values. Repair errors using tool observations.
Use write_artifact to save reports (UTF-8 text or base64 binary); inspect your saved
reports with read_written_artifact. Only these helpers write/read task output files.
End with final_answer containing a dictionary:
{"task_type": "analysis", "summary": "...", "metrics": {}, "assumptions": []}.
Analysis requires at least one successful registered function call. Compute the
reported values from the actual results. Artifacts are attached automatically.
For greetings or conceptual questions use task_type='no_action'. For essential
missing inputs use 'needs_input'; for missing capabilities use 'unsupported'.
Do not claim these types as completed calculations. Do not repeat side-effecting
calls blindly after errors. Keep planning brief and proportional to the request.
"""


class Answer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_type: Literal["analysis", "no_action", "needs_input", "unsupported"]
    summary: str = Field(min_length=1)
    metrics: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)


def _input_type(schema: dict, definitions: dict) -> str | list[str]:
    if "$ref" in schema:
        return _input_type(definitions.get(schema["$ref"].split("/")[-1], {}), definitions)
    if "anyOf" in schema:
        types = []
        for branch in schema["anyOf"]:
            value = _input_type(branch, definitions)
            types.extend(value if isinstance(value, list) else [value])
        return list(dict.fromkeys(types))
    return schema.get("type", "any")


def run_registered_task(prompt: str, functions: list[dict], artifacts_dir: Path,
                        timeout_seconds: int, *, model=None, trace: RunContext | None = None) -> dict:
    """Run in the task worker. The optional model is for deterministic testing."""
    trace = trace or RunContext(artifacts_dir.resolve(), uuid.uuid4().hex, "coding_agent", "run_coding_task")
    token = current_run.set(trace)
    try:
        with operation("agent.run", **{"quant_mcp.request_id": trace.request_id}):
            result = _run_registered_task(prompt, functions, artifacts_dir, timeout_seconds,
                                          model=model, trace=trace)
            set_outcome(result["status"])
            return result
    finally:
        current_run.reset(token)
        flush_progress_logs()


def _run_registered_task(prompt, functions, artifacts_dir, timeout_seconds, *, model, trace):
    # smolagents executes generated code in its own thread. Capture the agent
    # span and run metadata here, then enter a fresh context for each tool call.
    execution_context = copy_context()

    def in_agent_context(function):
        @functools.wraps(function)
        def invoke(*args, **kwargs):
            return execution_context.copy().run(function, *args, **kwargs)
        return invoke

    task_id = trace.task_id = trace.task_id or new_task_id()
    workspace = artifacts_dir.resolve() / task_id
    workspace.mkdir(parents=True, exist_ok=True)
    emit_event(trace, "agent_started", registered_functions=[spec["name"] for spec in functions])
    (workspace / "prompt.txt").write_text(prompt, encoding="utf-8")
    (workspace / "registry.json").write_text(json.dumps(functions, indent=2), encoding="utf-8")
    deadline = time.monotonic() + timeout_seconds
    calls: list[dict] = []
    outputs: dict[str, dict] = {}
    action_count = 0
    agent = None
    active_model = None
    response = {"status": "failed", "task_type": "failed", "summary": "Agent did not complete",
                "metrics": {}, "assumptions": [], "artifacts": []}
    python_paths = tuple(Path(p).resolve() for p in sys.path)

    def remaining() -> float:
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Task deadline reached")
        return seconds

    def validate_answer(value):
        answer = Answer.model_validate(value)
        if not answer.summary.strip():
            raise ValueError("summary must not be blank")
        if answer.task_type == "analysis" and not any(c["status"] == "success" for c in calls):
            raise ValueError("Call a registered function successfully before claiming analysis completion")
        if answer.task_type == "no_action" and calls:
            raise ValueError("Use an analysis, needs_input or unsupported answer after executing functions")
        if answer.task_type != "analysis" and answer.metrics:
            raise ValueError("Only an analysis answer can report calculated metrics")
        json.dumps(answer.model_dump(mode="json"), allow_nan=False)
        return answer

    try:
        from rich.console import Console
        from smolagents import CodeAgent, Tool, tool
        from smolagents.memory import ActionStep
        from smolagents.monitoring import AgentLogger

        class RegisteredTool(Tool):
            skip_forward_signature_validation = True
            output_type = "any"

            def __init__(self, spec):
                self.spec = spec
                self.name = spec["name"]
                schema = spec["input_schema"]
                self.description = spec["description"] + "\nArgument JSON schema: " + json.dumps(schema)
                self.inputs = {
                    name: {"type": _input_type(field, schema.get("$defs", {})),
                           "description": field.get("description", name),
                           **({"nullable": True} if name not in schema.get("required", []) else {})}
                    for name, field in schema["properties"].items()
                }
                module, name = spec["target"].split(":", 1)
                self.signature = inspect.signature(getattr(importlib.import_module(module), name))
                super().__init__()

            @in_agent_context
            def forward(self, *args, **kwargs):
                with operation("function.call", **{"code.function.name": self.name}):
                    if len(calls) >= env_int("SMOLAGENTS_MAX_TOOL_CALLS", 100):
                        raise ValueError("Registered function call budget exhausted")
                    budget = min(remaining(), self.spec["timeout_seconds"])
                    entry = {"call_id": uuid.uuid4().hex, "tool": self.name, "status": "running",
                             "request_id": trace.request_id, "started_at": timestamp()}
                    calls.append(entry)
                    started = time.monotonic()
                    error_type = None
                    emit_event(trace, "function_call_started", function=self.name, call_id=entry["call_id"])
                    (workspace / "function_calls.json").write_text(json.dumps(calls, indent=2))
                    try:
                        arguments = self.signature.bind(*args, **kwargs).arguments
                        value = asyncio.run(call_function(
                            self.spec["target"], arguments, python_paths=python_paths,
                            log_path=workspace / "function_logs" / f"{entry['call_id']}.log",
                            timeout_seconds=budget, own_process_group=False,
                        ))
                        entry["status"] = "success"
                        return value
                    except Exception as exc:
                        error_type = type(exc).__name__
                        entry["status"] = "timed_out" if isinstance(exc, TimeoutError) else "failed"
                        raise
                    finally:
                        entry["finished_at"] = timestamp()
                        entry["duration_ms"] = round((time.monotonic() - started) * 1000)
                        (workspace / "function_calls.json").write_text(json.dumps(calls, indent=2))
                        set_outcome(entry["status"])
                        emit_event(trace, "function_call_finished", function=self.name, call_id=entry["call_id"],
                                   status=entry["status"], error_type=error_type, duration_ms=entry["duration_ms"])

        @tool
        def write_artifact(filename: str, content: str, encoding: str = "utf-8") -> dict:
            """Save a report in this task's outputs directory (maximum 1 MiB per file).

            Args:
                filename: Relative filename, e.g. scenarios.csv. No absolute paths or parent traversal.
                content: Text content or base64-encoded binary data.
                encoding: Either utf-8 or base64.
            """
            remaining()
            relative = Path(filename)
            if not filename or relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Use a relative output filename without parent traversal")
            path = workspace / "outputs" / relative
            if any(p.is_symlink() for p in (path, *path.parents)):
                raise ValueError("Artifact symlinks are not supported")
            if encoding not in {"utf-8", "base64"}:
                raise ValueError("encoding must be utf-8 or base64")
            if len(content) > 2 * 1024 * 1024:
                raise ValueError("Artifact exceeds 1 MiB")
            data = content.encode("utf-8") if encoding == "utf-8" else base64.b64decode(content, validate=True)
            if len(data) > 1024 * 1024:
                raise ValueError("Artifact exceeds 1 MiB")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            name = path.relative_to(workspace).as_posix()
            outputs[name] = {"name": filename, "path": name}
            emit_event(trace, "artifact_written", filename=name, size_bytes=len(data))
            return {"filename": filename, "path": name, "size_bytes": len(data)}

        @tool
        def read_written_artifact(filename: str, offset: int = 0,
                                  max_bytes: int = 65536, encoding: str = "utf-8") -> dict:
            """Read back a report saved by write_artifact, in bounded chunks.

            Args:
                filename: The relative filename originally passed to write_artifact.
                offset: Byte offset; continue at next_offset returned by an earlier read.
                max_bytes: Chunk length, between 1 and 262144 bytes.
                encoding: Either utf-8 or base64.
            """
            remaining()
            name = "outputs/" + filename
            if name not in outputs:
                raise ValueError("Only reports written by this task can be read")
            return ArtifactStore(artifacts_dir).read(task_id, name, offset, max_bytes, encoding)

        for helper in (write_artifact, read_written_artifact):
            helper.forward = in_agent_context(helper.forward)

        def check_answer(value, memory, agent):
            remaining()
            validate_answer(value)
            return True

        def record_action(step, agent):
            nonlocal action_count
            action_count += 1
            directory = workspace / "agent_actions"
            directory.mkdir(exist_ok=True)
            if step.code_action:
                (directory / f"action_{action_count:03d}.py").write_text(step.code_action + "\n")
            emit_event(trace, "agent_step_finished", step=action_count,
                       status="failed" if step.error else "success",
                       error_type=type(step.error).__name__ if step.error else None,
                       code_file=f"agent_actions/action_{action_count:03d}.py" if step.code_action else None)
            remaining()

        active_model = instrument_model(model if model is not None else build_model(timeout_seconds), trace)
        agent = CodeAgent(
            tools=[*(RegisteredTool(spec) for spec in functions), write_artifact, read_written_artifact],
            model=active_model,
            instructions=INSTRUCTIONS, add_base_tools=False,
            max_steps=env_int("SMOLAGENTS_MAX_STEPS", 12),
            planning_interval=env_int("SMOLAGENTS_PLANNING_INTERVAL", 3, minimum=0) or None,
            executor_kwargs={"timeout_seconds": min(timeout_seconds, 120)},
            final_answer_checks=[check_answer], return_full_result=True,
            step_callbacks={ActionStep: record_action},
            logger=AgentLogger(console=Console(stderr=True)),
        )
        for key in ("initial_plan", "update_plan_post_messages"):
            agent.prompt_templates["planning"][key] += "\n\n" + INSTRUCTIONS
        (workspace / "agent_instruction.md").write_text(agent.system_prompt + "\n\n" + prompt)
        result = agent.run(prompt)
        if result.state != "success":
            raise RuntimeError(f"smolagents stopped: {result.state}")
        answer = validate_answer(result.output)
        response = {"status": "failed" if answer.task_type == "unsupported" else "success",
                    **answer.model_dump(mode="json"), "artifacts": list(outputs.values())}
    except ImportError as exc:
        response["summary"] = f"Dependency unavailable: {exc}. Install the framework's required dependencies and your function packages."
    except Exception as exc:
        response["summary"] = f"{type(exc).__name__}: {exc}"
    finally:
        if active_model is not None:
            restore_model(active_model)
        if agent is not None:
            (workspace / "smolagents_result.json").write_text(json.dumps({
                "status": response["status"], "actions": action_count,
                "steps": agent.memory.get_full_steps(),
            }, indent=2, default=str))
            agent.cleanup()
        response["artifacts"] = list(outputs.values())
        (workspace / "function_calls.json").write_text(json.dumps(calls, indent=2))
        (workspace / "result.json").write_text(json.dumps(response, indent=2))
        emit_event(trace, "agent_finished", status=response["status"], task_type=response["task_type"],
                   actions=action_count, function_calls=len(calls))
    return {"task_id": task_id, "task_dir": str(workspace), "request_id": trace.request_id, **response}
