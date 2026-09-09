from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from quant_mcp.agent import LocalDemoAgent, PiCodingAgent
from quant_mcp.config import load_env_file
from quant_mcp.runner import list_artifacts, read_artifact, run_quant_task


def main() -> None:
    load_env_file()
    parser = argparse.ArgumentParser(description="Local CLI for the Quant MCP POC")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="Run a quant/data/coding task")
    run_parser.add_argument("prompt", nargs="*", help="Natural-language request. Reads stdin if omitted.")
    run_parser.add_argument("--agent", choices=["env", "local", "pi", "smolagents"], default="env")
    run_parser.add_argument("--timeout", type=int, default=120)
    run_parser.add_argument("--json", action="store_true", help="Print full result JSON")

    subparsers.add_parser("list", help="List recent artifact folders")

    read_parser = subparsers.add_parser("read", help="Read one artifact from a task")
    read_parser.add_argument("task_id")
    read_parser.add_argument("filename")

    args = parser.parse_args()
    if args.command == "run":
        _run(args)
    elif args.command == "list":
        _list()
    elif args.command == "read":
        _read(args)


def _run(args: argparse.Namespace) -> None:
    prompt = " ".join(args.prompt).strip() if args.prompt else sys.stdin.read().strip()
    if not prompt:
        raise SystemExit("Provide a prompt argument or pipe one through stdin.")

    agent = _select_agent(args.agent)
    task = run_quant_task(prompt=prompt, agent=agent, timeout_seconds=args.timeout)
    output = {"task_id": task.task_id, "task_dir": str(task.task_dir), **task.result}

    if args.json:
        print(json.dumps(output, indent=2))
        if task.result.get("status") == "failed":
            raise SystemExit(1)
        return

    print(f"Task: {task.task_id}")
    print(f"Folder: {task.task_dir}")
    print(f"Summary: {task.result.get('summary', '')}")
    print()
    print("Artifacts:")
    for artifact in task.result.get("artifacts", []):
        print(f"- {artifact.get('name')}: {artifact.get('path')}")
    if task.result.get("status") == "failed":
        raise SystemExit(1)


def _select_agent(name: str):
    if name == "local":
        return LocalDemoAgent()
    if name == "pi":
        return PiCodingAgent()
    if name == "smolagents":
        from quant_mcp.agent import SmolagentsCodingAgent

        return SmolagentsCodingAgent()
    return None


def _list() -> None:
    rows = list_artifacts()
    if not rows:
        print("No artifacts yet.")
        return
    for row in rows:
        print(f"{row['task_id']} | {row.get('status')} | {row.get('summary')}")
        print(f"  {row['task_dir']}")


def _read(args: argparse.Namespace) -> None:
    content = read_artifact(args.task_id, args.filename)
    path = Path(content)
    if path.exists() and path.suffix.lower() in {".xlsx", ".png", ".pdf"}:
        print(content)
        return
    print(content)


if __name__ == "__main__":
    main()
