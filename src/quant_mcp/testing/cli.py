"""Command-line entry point for deterministic MCP stdio tests."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from quant_mcp.testing.launcher import ServerSpec
from quant_mcp.testing.prompts import load_prompt_adapter
from quant_mcp.testing.runner import run_suite
from quant_mcp.testing.scenarios import load_document, load_suite


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic tests against a local stdio MCP server.")
    parser.add_argument("--config", required=True, type=Path, help="JSON/YAML server launch configuration")
    parser.add_argument("--suite", required=True, type=Path, help="JSON/YAML MCP scenario suite")
    parser.add_argument("--output", type=Path, help="Write the complete JSON report to this path")
    parser.add_argument("--stderr", type=Path, help="Capture server stderr at this path")
    parser.add_argument("--prompt-adapter", help="Project adapter using module:attribute syntax")
    args = parser.parse_args(argv)
    try:
        config = load_document(args.config)
        if not isinstance(config, dict) or not isinstance(config.get("server"), dict):
            raise ValueError("config must contain a server object")
        spec = ServerSpec.from_mapping(config["server"], base_dir=args.config.parent.resolve())
        expected_tools, scenarios = load_suite(args.suite)
        prompt_adapter = load_prompt_adapter(args.prompt_adapter) if args.prompt_adapter else None
        report = asyncio.run(run_suite(
            spec,
            scenarios,
            expected_tools=expected_tools,
            stderr_path=args.stderr,
            prompt_adapter=prompt_adapter,
        ))
    except Exception as exc:
        print(f"MCP test setup failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    tools = report["tools"]
    print(f"Startup: {report['startup']['status']}")
    print(f"Tools: {len(tools['actual'])} discovered")
    for row in report["cases"]:
        print(f"[{row['status']}] {row['id']} ({row['type']}, {row['duration_ms']} ms)")
        if row.get("error"):
            print(f"       {row['error']}")
    print(f"\n{'PASS' if report['ok'] else 'FAIL'}")
    return 0 if report["ok"] else 1
