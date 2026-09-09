from __future__ import annotations

import re
import json
import textwrap
from datetime import date
from pathlib import Path

import yaml

from quant_mcp.agent.base import AgentResult


class LocalDemoAgent:
    """Small deterministic stand-in for the coding agent during local demos."""

    def run_task(self, instruction: str, workspace_dir: Path, timeout_seconds: int) -> AgentResult:
        prompt = _extract_user_prompt(instruction)
        if not _looks_like_option_task(prompt):
            result = {
                "status": "success",
                "task_type": "no_action",
                "summary": "I am ready. Send me a concrete quant, data, or coding task and I will create a reproducible artifact bundle.",
                "metrics": {},
                "assumptions": [],
                "artifacts": [],
            }
            (workspace_dir / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
            return AgentResult(status="success", message="Local demo agent classified prompt as no_action")

        portfolio = _parse_prompt(prompt)
        (workspace_dir / "portfolio.yaml").write_text(yaml.safe_dump(portfolio, sort_keys=False), encoding="utf-8")
        (workspace_dir / "analysis.py").write_text(_analysis_script(), encoding="utf-8")
        return AgentResult(status="success", message="Local demo agent wrote portfolio.yaml and analysis.py")


def _extract_user_prompt(instruction: str) -> str:
    marker = "USER REQUEST:"
    if marker not in instruction:
        return instruction
    return instruction.split(marker, 1)[1].split("\n\n", 1)[0].strip()


def _looks_like_option_task(prompt: str) -> bool:
    text = prompt.lower()
    task_words = {"price", "value", "greek", "delta", "gamma", "vega", "theta", "rho", "pnl", "scenario"}
    option_words = {"option", "call", "put", "strike", "expiry", "expiring", "spread", "vol"}
    position_words = {"long", "short", "buy", "sell"}
    has_option_context = any(word in text for word in option_words)
    has_explicit_task = any(word in text for word in task_words)
    has_position = any(word in text for word in position_words) and ("call" in text or "put" in text)
    return has_option_context and (has_explicit_task or has_position)


def _parse_prompt(prompt: str) -> dict:
    text = prompt.lower()
    valuation_date = _find_date(text) or date.today().isoformat()
    underlying = _find_underlying(prompt)
    spot = _find_number_after(text, "spot") or _find_underlying_spot(text) or 100.0
    volatility = (
        _find_percent_after(text, "implied volatility")
        or _find_percent_after(text, "volatility")
        or _find_percent_after(text, "vol")
        or 0.2
    )
    rate = _find_percent_after(text, "rate") or 0.04
    expiry = _find_expiry(text, valuation_date) or _add_months(date.fromisoformat(valuation_date), 12).isoformat()
    option_type = "put" if " put" in f" {text}" else "call"

    strikes = _find_spread_strikes(text)
    if not strikes:
        strikes = [float(x) for x in re.findall(r"(?:strike|strikes?)\s*(?:at|=|:)?\s*(\d+(?:\.\d+)?)", text)]
    if not strikes:
        strikes = [spot]

    is_short = text.startswith("short ") or re.search(r"\bshort\s+(?:call\s+|put\s+)?spread\b", text) is not None
    side_first = "short" if is_short else "long"

    positions = []
    if len(strikes) >= 2 and "spread" in text:
        second_side = "long" if side_first == "short" else "short"
        positions.append(_position(underlying, option_type, side_first, strikes[0], expiry, 1))
        positions.append(_position(underlying, option_type, second_side, strikes[1], expiry, 1))
    else:
        quantity = _find_quantity(text) or 1
        positions.append(_position(underlying, option_type, side_first, strikes[0], expiry, quantity))

    return {
        "valuation_date": valuation_date,
        "market": {
            "spot": {underlying: spot},
            "rate": rate,
            "dividend_yield": 0.0,
            "volatility": {underlying: volatility},
        },
        "positions": positions,
        "scenario": {
            "spot_min": round(spot * 0.7, 2),
            "spot_max": round(spot * 1.3, 2),
            "spot_steps": 31,
        },
        "assumptions": [
            "Local demo parser was used; Pi agent can replace this parser for richer position extraction.",
            "European Black-Scholes pricing with flat rate, dividend yield, and volatility.",
        ],
    }


def _position(
    underlying: str,
    option_type: str,
    side: str,
    strike: float,
    expiry: str,
    quantity: int,
) -> dict:
    return {
        "underlying": underlying,
        "option_type": option_type,
        "side": side,
        "strike": strike,
        "expiry": expiry,
        "quantity": quantity,
    }


def _find_underlying(prompt: str) -> str:
    symbols = re.findall(r"\b([A-Z]{1,6})\b", prompt)
    parameters = {"S", "K", "K1", "K2", "T", "R", "Q"}
    for symbol in symbols:
        if symbol not in parameters:
            return symbol
    return "DEMO"


def _find_number_after(text: str, label: str) -> float | None:
    match = re.search(rf"{label}\D+(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _find_underlying_spot(text: str) -> float | None:
    match = re.search(r"\bunderlying\s*(?:price|spot|=|:)?\s*(\d+(?:\.\d+)?)", text)
    return float(match.group(1)) if match else None


def _find_spread_strikes(text: str) -> list[float]:
    slash = re.search(r"(?:strike|strikes?)?\D*\b(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\b", text)
    if slash:
        return [float(slash.group(1)), float(slash.group(2))]

    k1 = re.search(r"\b(?:strike\s*)?k1\s*=\s*(\d+(?:\.\d+)?)\b", text)
    k2 = re.search(r"\b(?:strike\s*)?k2\s*=\s*(\d+(?:\.\d+)?)\b", text)
    if k1 and k2:
        return [float(k1.group(1)), float(k2.group(1))]

    long_strike = re.search(r"\blong\s+call\s+strike\s+\w*\s*=\s*(\d+(?:\.\d+)?)\b", text)
    short_strike = re.search(r"\bshort\s+call\s+strike\s+\w*\s*=\s*(\d+(?:\.\d+)?)\b", text)
    if long_strike and short_strike:
        return [float(long_strike.group(1)), float(short_strike.group(1))]

    return []


def _find_percent_after(text: str, label: str) -> float | None:
    match = re.search(rf"{label}\D+(\d+(?:\.\d+)?)\s*%?", text)
    if not match:
        return None
    value = float(match.group(1))
    return value / 100.0 if value > 1 else value


def _find_quantity(text: str) -> int | None:
    match = re.search(r"\b(?:long|short|buy|sell)\s+(\d+)\b", text)
    return int(match.group(1)) if match else None


def _find_date(text: str) -> str | None:
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    return match.group(1) if match else None


def _find_expiry(text: str, valuation_date: str) -> str | None:
    explicit = re.search(r"(?:expiring|expiry)\D+(20\d{2}-\d{2}-\d{2})", text)
    if explicit:
        return explicit.group(1)
    relative_months = re.search(r"(?:expiring|expiry|expires|matures|in)\D+(\d+)\s*(?:month|months|mo|mos)\b", text)
    if relative_months:
        return _add_months(date.fromisoformat(valuation_date), int(relative_months.group(1))).isoformat()
    parenthesized_months = re.search(r"\((\d+)\s*(?:month|months|mo|mos)\)", text)
    if parenthesized_months:
        return _add_months(date.fromisoformat(valuation_date), int(parenthesized_months.group(1))).isoformat()
    years = re.search(r"(?:time\s+to\s+expiry|expiry|expires|maturity|t)\D+(\d+(?:\.\d+)?)\s*(?:year|years|yr|yrs)\b", text)
    if years:
        days = max(1, round(float(years.group(1)) * 365))
        return date.fromordinal(date.fromisoformat(valuation_date).toordinal() + days).isoformat()
    year = re.search(r"\b(20\d{2})\b", text)
    month_map = {
        "jan": "01",
        "feb": "02",
        "mar": "03",
        "apr": "04",
        "may": "05",
        "jun": "06",
        "jul": "07",
        "aug": "08",
        "sep": "09",
        "oct": "10",
        "nov": "11",
        "dec": "12",
    }
    for name, month in month_map.items():
        if name in text and year:
            return f"{year.group(1)}-{month}-20"
    return None


def _add_months(start: date, months: int) -> date:
    month_index = start.month - 1 + months
    year = start.year + month_index // 12
    month = month_index % 12 + 1
    month_lengths = [31, 29 if _is_leap_year(year) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    day = min(start.day, month_lengths[month - 1])
    return date(year, month, day)


def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _analysis_script() -> str:
    return textwrap.dedent(
        """
        from __future__ import annotations

        import json
        from pathlib import Path

        import numpy as np
        import pandas as pd
        import yaml

        from quant_mcp.pricing import calc_greeks, price_option

        TASK_DIR = Path(__file__).resolve().parent


        def signed_quantity(position):
            sign = -1 if position["side"] in {"short", "sell"} else 1
            return sign * float(position.get("quantity", 1))


        def load_config():
            with (TASK_DIR / "portfolio.yaml").open("r", encoding="utf-8") as handle:
                return yaml.safe_load(handle)


        def enrich_positions(config):
            rows = []
            totals = {"value": 0.0, "delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}
            for position in config["positions"]:
                underlying = position["underlying"]
                spot = float(config["market"]["spot"][underlying])
                vol = float(config["market"]["volatility"][underlying])
                rate = float(config["market"]["rate"])
                dividend_yield = float(config["market"].get("dividend_yield", 0.0))
                quantity = signed_quantity(position)
                price = price_option(
                    option_type=position["option_type"],
                    spot=spot,
                    strike=float(position["strike"]),
                    expiry=position["expiry"],
                    valuation_date=config["valuation_date"],
                    volatility=vol,
                    rate=rate,
                    dividend_yield=dividend_yield,
                )
                greeks = calc_greeks(
                    option_type=position["option_type"],
                    spot=spot,
                    strike=float(position["strike"]),
                    expiry=position["expiry"],
                    valuation_date=config["valuation_date"],
                    volatility=vol,
                    rate=rate,
                    dividend_yield=dividend_yield,
                )
                row = {**position, "unit_price": price, "signed_quantity": quantity, "market_value": price * quantity}
                for greek_name, greek_value in greeks.items():
                    row[greek_name] = greek_value * quantity
                for metric in totals:
                    totals[metric] += row["market_value"] if metric == "value" else row[metric]
                rows.append(row)
            return pd.DataFrame(rows), totals


        def scenario_table(config):
            scenario = config["scenario"]
            grid = np.linspace(float(scenario["spot_min"]), float(scenario["spot_max"]), int(scenario["spot_steps"]))
            rows = []
            for spot in grid:
                total_value = 0.0
                for position in config["positions"]:
                    underlying = position["underlying"]
                    vol = float(config["market"]["volatility"][underlying])
                    price = price_option(
                        option_type=position["option_type"],
                        spot=float(spot),
                        strike=float(position["strike"]),
                        expiry=position["expiry"],
                        valuation_date=config["valuation_date"],
                        volatility=vol,
                        rate=float(config["market"]["rate"]),
                        dividend_yield=float(config["market"].get("dividend_yield", 0.0)),
                    )
                    total_value += price * signed_quantity(position)
                rows.append({"spot": spot, "portfolio_value": total_value})
            frame = pd.DataFrame(rows)
            frame["pnl_vs_current"] = frame["portfolio_value"] - frame["portfolio_value"].iloc[len(frame) // 2]
            return frame


        def render_report(config, positions, metrics, scenarios):
            metrics_rows = "".join(
                f"<tr><th>{name}</th><td>{value:,.6f}</td></tr>" for name, value in metrics.items()
            )
            return f\"\"\"<!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>Option Portfolio Analysis</title>
          <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 32px; color: #202124; }}
            h1 {{ margin-bottom: 6px; }}
            table {{ border-collapse: collapse; width: 100%; margin: 18px 0; }}
            th, td {{ border-bottom: 1px solid #ddd; padding: 8px; text-align: right; }}
            th:first-child, td:first-child {{ text-align: left; }}
            .note {{ color: #5f6368; }}
          </style>
        </head>
        <body>
          <h1>Option Portfolio Analysis</h1>
          <p class="note">Valuation date: {config["valuation_date"]}</p>
          <h2>Portfolio Metrics</h2>
          <table>{metrics_rows}</table>
          <h2>Positions</h2>
          {positions.to_html(index=False)}
          <h2>Scenarios</h2>
          {scenarios.to_html(index=False)}
          <h2>Assumptions</h2>
          <ul>{"".join(f"<li>{item}</li>" for item in config.get("assumptions", []))}</ul>
        </body>
        </html>\"\"\"


        def main():
            config = load_config()
            positions, metrics = enrich_positions(config)
            scenarios = scenario_table(config)

            positions.to_csv(TASK_DIR / "positions.csv", index=False)
            pd.DataFrame([metrics]).to_csv(TASK_DIR / "metrics.csv", index=False)
            scenarios.to_csv(TASK_DIR / "scenarios.csv", index=False)
            with pd.ExcelWriter(TASK_DIR / "scenarios.xlsx") as writer:
                positions.to_excel(writer, sheet_name="positions", index=False)
                pd.DataFrame([metrics]).to_excel(writer, sheet_name="metrics", index=False)
                scenarios.to_excel(writer, sheet_name="scenarios", index=False)

            (TASK_DIR / "report.html").write_text(render_report(config, positions, metrics, scenarios), encoding="utf-8")
            result = {
                "status": "success",
                "task_type": "option_portfolio_analysis",
                "summary": f"Portfolio value {metrics['value']:.4f}; delta {metrics['delta']:.4f}; gamma {metrics['gamma']:.6f}; vega {metrics['vega']:.4f}.",
                "metrics": metrics,
                "assumptions": config.get("assumptions", []),
                "artifacts": [
                    {"name": "Portfolio config", "path": str(TASK_DIR / "portfolio.yaml")},
                    {"name": "Analysis code", "path": str(TASK_DIR / "analysis.py")},
                    {"name": "Run log", "path": str(TASK_DIR / "run.log")},
                    {"name": "HTML report", "path": str(TASK_DIR / "report.html")},
                    {"name": "Positions CSV", "path": str(TASK_DIR / "positions.csv")},
                    {"name": "Metrics CSV", "path": str(TASK_DIR / "metrics.csv")},
                    {"name": "Scenarios CSV", "path": str(TASK_DIR / "scenarios.csv")},
                    {"name": "Scenarios workbook", "path": str(TASK_DIR / "scenarios.xlsx")},
                ],
            }
            (TASK_DIR / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")


        if __name__ == "__main__":
            main()
        """
    ).strip() + "\n"
