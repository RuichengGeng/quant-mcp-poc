"""Run from the project environment; writes result.json in the current directory.

This example teaches orchestration and reporting. Pricing logic stays in the
library. Rates/volatility are decimals; the Greek units are documented in source.
"""
import json
from pathlib import Path

from quant_mcp.pricing import calc_greeks_t, price_option_t


def main() -> None:
    inputs = dict(
        option_type="put", spot=100.0, strike=110.0,
        time_to_expiry=0.25, volatility=0.25, rate=0.04,
        dividend_yield=0.0,
    )
    premium = price_option_t(**inputs)
    greeks = calc_greeks_t(**inputs)
    Path("result.json").write_text(json.dumps({
        "status": "success",
        "task_type": "option_pricing",
        "summary": "European put premium per underlying unit and Greeks.",
        "metrics": {"premium": premium, **greeks},
        "assumptions": [
            "Time to expiry is exactly 0.25 years.",
            "Vega and rho are per 1.00 change, theta is per calendar day.",
        ],
        "artifacts": [],
    }, indent=2) + "\n")


if __name__ == "__main__":
    main()
