from __future__ import annotations

import json

from quant_mcp.runner import run_option_analysis


def main() -> None:
    task = run_option_analysis(
        "I am long AAPL 180/200 call spread expiring Dec 2026. "
        "Spot 185, vol 24%, rate 4%. Show value, Greeks and scenario PnL."
    )
    print(json.dumps({"task_id": task.task_id, "task_dir": str(task.task_dir), **task.result}, indent=2))


if __name__ == "__main__":
    main()

