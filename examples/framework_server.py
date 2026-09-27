"""Runnable framework example: direct tools plus smolagents and artifact tools."""
from pathlib import Path
import os
import sys

# Handle diagnostics before optional application imports, so missing packages
# can be reported by the pre-flight checker instead of aborting startup.
if __name__ == "__main__" and "--check" in sys.argv[1:]:
    from quant_mcp.preflight import main
    raise SystemExit(main([str(Path(__file__).resolve()),
                           *(arg for arg in sys.argv[1:] if arg != "--check")]))

from quant_mcp.config import load_env_file
from quant_mcp.framework import MCPFramework
from csv_data_provider import CsvDataProvider, get_quote_from_csv
from pricing import calc_greeks_t, price_option_t

ROOT = Path(__file__).resolve().parent
load_env_file(ROOT.parent / ".env")

app = MCPFramework(
    "pricing-framework",
    artifacts_dir=ROOT.parent / "artifacts",
    telemetry_setup="telemetry_config:configure" if os.getenv("QUANT_MCP_TRACE_EXPORTER") else None,
)
app.expose(price_option_t, read_only=True)
app.expose(calc_greeks_t, read_only=True)

# The in-process tool reuses this provider instance. The isolated comparison
# function imports its module and reads the CSV again for each call.
csv_provider = CsvDataProvider()
app.manage(csv_provider)
app.expose(csv_provider.get_quote, read_only=True, execution="in_process")
app.expose(get_quote_from_csv, read_only=True, name="get_quote_isolated")

if __name__ == "__main__":
    app.run()
