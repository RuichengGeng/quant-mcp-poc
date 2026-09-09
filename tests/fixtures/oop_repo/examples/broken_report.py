"""Deliberately broken report for testing the agent's repair loop.

Keep the library calculation. Fix the report using the documented return keys.
"""
import json
from pathlib import Path
from sample_risk import PortfolioAnalytics, Position

metrics = PortfolioAnalytics([Position(3, 7), Position(-2, 4)]).summarize()
Path("result.json").write_text(json.dumps({
    "status": "success", "summary": "Portfolio values",
    "metrics": {"net_value": metrics["total"], "gross_value": metrics["gross_value"]},
    "assumptions": [], "artifacts": [],
}))
