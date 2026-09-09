"""Interactive smolagents demo for reviewing generated quant code before execution."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from smolagents import CodeAgent, OpenAIModel, tool
from smolagents.local_python_executor import CodeOutput, LocalPythonExecutor

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"


@tool
def price_option_call(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
) -> float:
    """Price a European call using the registered deterministic quant library.

    Args:
        spot: Current underlying price.
        strike: Call strike price.
        time_to_expiry: Time to expiry in years, for example 0.25.
        volatility: Volatility as a decimal, for example 0.25 for 25%.
        rate: Continuously compounded risk-free rate as a decimal.
    """
    from quant_mcp.pricing import price_option_t

    return price_option_t("call", spot, strike, time_to_expiry, volatility, rate)


@tool
def call_greeks(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
) -> dict:
    """Calculate European call Greeks using the registered deterministic quant library.

    Args:
        spot: Current underlying price.
        strike: Call strike price.
        time_to_expiry: Time to expiry in years, for example 0.25.
        volatility: Volatility as a decimal, for example 0.25 for 25%.
        rate: Continuously compounded risk-free rate as a decimal.
    """
    from quant_mcp.pricing import calc_greeks_t

    return calc_greeks_t("call", spot, strike, time_to_expiry, volatility, rate)


class ReviewingExecutor:
    """Delegate execution to smolagents while requiring approval for each code action."""

    def __init__(self, workspace: Path, auto_approve: bool = False) -> None:
        self.workspace = workspace
        self.auto_approve = auto_approve
        self.inner = LocalPythonExecutor(
            additional_authorized_imports=["json", "math", "pathlib"],
            timeout_seconds=30,
        )
        self.actions: list[str] = []

    def send_tools(self, tools: dict) -> None:
        self.inner.send_tools(tools)

    def send_variables(self, variables: dict) -> None:
        self.inner.send_variables(variables)

    def __call__(self, code_action: str) -> CodeOutput:
        action_number = len(self.actions) + 1
        self.actions.append(code_action)
        print(f"\n--- Generated action {action_number} ---\n{code_action}\n")
        approved = self.auto_approve or input("Approve this code action? [y/N] ").strip().lower() == "y"
        review_line = f"action_{action_number:02d}: {'approved' if approved else 'rejected'}\n"
        with (self.workspace / "code_review.md").open("a", encoding="utf-8") as handle:
            handle.write(review_line)
        if not approved:
            return CodeOutput(
                output="Execution rejected by human reviewer.",
                logs=review_line,
                is_final_answer=True,
            )
        return self.inner(code_action)


def main() -> None:
    from quant_mcp.config import load_env_file

    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "prompt",
        nargs="*",
        default=["Price a long 110/120 call spread with spot 100, T=0.25, vol 25%, rate 4%"],
    )
    parser.add_argument("--auto-approve", action="store_true", help="Execute generated actions without prompting")
    args = parser.parse_args()
    prompt = " ".join(args.prompt).strip()

    api_key = os.getenv("DEEPSEEK_API_KEY") or os.getenv("PI_API_KEY")
    if not api_key:
        raise SystemExit("Set DEEPSEEK_API_KEY or PI_API_KEY in .env before running this demo.")

    task_dir = ARTIFACTS_DIR / _task_id()
    task_dir.mkdir(parents=True, exist_ok=False)
    (task_dir / "prompt.txt").write_text(prompt + "\n", encoding="utf-8")
    catalog = _catalog()
    (task_dir / "library_catalog.json").write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")

    instruction = f"""You are a quant analyst writing a reproducible Python analysis.

User request:
{prompt}

Use only the registered deterministic tools for pricing and Greeks. Compose their
results into a clear answer. Do not implement Black-Scholes, normal CDFs, or Greeks
yourself. Keep generated code readable because a human will review each action
before it executes. Finish with a concise result.

Registered tools:
- price_option_call(spot, strike, time_to_expiry, volatility, rate)
- call_greeks(spot, strike, time_to_expiry, volatility, rate)
"""
    (task_dir / "agent_instruction.md").write_text(instruction, encoding="utf-8")

    executor = ReviewingExecutor(task_dir, auto_approve=args.auto_approve)
    model = OpenAIModel(
        model_id=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_base=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com"),
        api_key=api_key,
    )
    agent = CodeAgent(
        tools=[price_option_call, call_greeks],
        model=model,
        executor=executor,
        max_steps=8,
        additional_authorized_imports=["json", "math", "pathlib"],
        return_full_result=True,
    )

    try:
        result = agent.run(instruction, return_full_result=True)
        generated_code = agent.memory.return_full_code()
        (task_dir / "analysis.py").write_text(generated_code + "\n", encoding="utf-8")
        (task_dir / "agent_result.json").write_text(
            json.dumps(
                {
                    "output": str(result.output),
                    "steps": len(agent.memory.steps),
                    "reviewed_actions": len(executor.actions),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (task_dir / "result.json").write_text(
            json.dumps(
                {
                    "status": "success",
                    "task_type": "smolagents_review_demo",
                    "summary": str(result.output),
                    "metrics": {"reviewed_actions": len(executor.actions)},
                    "assumptions": [],
                    "artifacts": [
                        {"name": "User request", "path": str(task_dir / "prompt.txt")},
                        {"name": "Generated analysis", "path": str(task_dir / "analysis.py")},
                        {"name": "Code review", "path": str(task_dir / "code_review.md")},
                        {"name": "Agent result", "path": str(task_dir / "agent_result.json")},
                    ],
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        (task_dir / "run.log").write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        raise

    print(f"\nTask completed: {task_dir}")
    print(f"Review: {task_dir / 'code_review.md'}")
    print(f"Code:   {task_dir / 'analysis.py'}")
    print(f"Result: {task_dir / 'result.json'}")


def _catalog() -> dict:
    from quant_mcp.library_catalog import discover_library_catalog

    return discover_library_catalog().to_dict()


def _task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    return f"task_smolagents_{stamp}_{uuid.uuid4().hex[:8]}"


if __name__ == "__main__":
    sys.path.insert(0, str(PROJECT_ROOT / "src"))
    main()
