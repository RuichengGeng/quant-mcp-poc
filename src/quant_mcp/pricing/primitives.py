from __future__ import annotations

from datetime import date, datetime
from math import exp, log, sqrt
from typing import Literal

from scipy.stats import norm

OptionType = Literal["call", "put"]


def _to_date(value: date | str) -> date:
    if isinstance(value, date):
        return value
    return datetime.strptime(value, "%Y-%m-%d").date()


def _year_fraction(valuation_date: date | str, expiry: date | str) -> float:
    start = _to_date(valuation_date)
    end = _to_date(expiry)
    return max((end - start).days / 365.0, 0.0)


def _d1_d2(
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
    dividend_yield: float,
) -> tuple[float, float]:
    if spot <= 0:
        raise ValueError("spot must be positive")
    if strike <= 0:
        raise ValueError("strike must be positive")
    if volatility <= 0:
        raise ValueError("volatility must be positive")
    if time_to_expiry <= 0:
        raise ValueError("time_to_expiry must be positive")

    vol_sqrt_t = volatility * sqrt(time_to_expiry)
    d1 = (
        log(spot / strike)
        + (rate - dividend_yield + 0.5 * volatility * volatility) * time_to_expiry
    ) / vol_sqrt_t
    return d1, d1 - vol_sqrt_t


def price_option(
    option_type: OptionType,
    spot: float,
    strike: float,
    expiry: date | str,
    valuation_date: date | str,
    volatility: float,
    rate: float,
    dividend_yield: float = 0.0,
    model: str = "black_scholes",
) -> float:
    """Price one European vanilla option with Black-Scholes, returning price per share.

    option_type is the string 'call' or 'put'. expiry and valuation_date are
    dates or YYYY-MM-DD strings; ACT/365 determines the time to expiry.
    volatility, rate and dividend_yield are annual decimal fractions:
    pass 0.25 for 25% volatility and 0.04 for a 4% rate, never 25 or 4.
    spot and strike are prices in the same currency. No contract multiplier is applied.
    """
    if model != "black_scholes":
        raise ValueError(f"unsupported model: {model}")
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")

    return price_option_t(
        option_type=option_type,
        spot=spot,
        strike=strike,
        time_to_expiry=_year_fraction(valuation_date, expiry),
        volatility=volatility,
        rate=rate,
        dividend_yield=dividend_yield,
        model=model,
    )


def price_option_t(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
    dividend_yield: float = 0.0,
    model: str = "black_scholes",
) -> float:
    """Price one European vanilla option with Black-Scholes using an exact year fraction.

    option_type is the string 'call' or 'put'. time_to_expiry is years (0.25
    means one quarter of a year). volatility, rate and dividend_yield are annual
    decimal fractions: pass 0.25 for 25% volatility and 0.04 for a 4% rate,
    never 25 or 4. spot and strike are prices in the same currency.
    Returns the option price per share as a float. No contract multiplier is applied.
    """
    if model != "black_scholes":
        raise ValueError(f"unsupported model: {model}")
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")

    t = max(float(time_to_expiry), 0.0)
    if t == 0:
        intrinsic = max(spot - strike, 0.0) if option_type == "call" else max(strike - spot, 0.0)
        return float(intrinsic)

    d1, d2 = _d1_d2(spot, strike, t, volatility, rate, dividend_yield)
    discounted_spot = spot * exp(-dividend_yield * t)
    discounted_strike = strike * exp(-rate * t)

    if option_type == "call":
        return float(discounted_spot * norm.cdf(d1) - discounted_strike * norm.cdf(d2))
    return float(discounted_strike * norm.cdf(-d2) - discounted_spot * norm.cdf(-d1))


def calc_greeks(
    option_type: OptionType,
    spot: float,
    strike: float,
    expiry: date | str,
    valuation_date: date | str,
    volatility: float,
    rate: float,
    dividend_yield: float = 0.0,
    model: str = "black_scholes",
) -> dict[str, float]:
    """Return one-option Greeks. Vega/rho are per 1.00 change; theta is per calendar day.

    option_type is the string 'call' or 'put'. expiry and valuation_date are
    dates or YYYY-MM-DD strings; ACT/365 determines the time to expiry.
    volatility, rate and dividend_yield are annual decimal fractions:
    pass 0.25 for 25% volatility and 0.04 for a 4% rate, never 25 or 4.
    Returns a dict with exactly delta, gamma, vega, theta and rho keys; it
    does not contain premium or price (call price_option for that).
    For vega/rho per one percentage point, divide the returned values by 100.
    """
    if model != "black_scholes":
        raise ValueError(f"unsupported model: {model}")
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")

    return calc_greeks_t(
        option_type=option_type,
        spot=spot,
        strike=strike,
        time_to_expiry=_year_fraction(valuation_date, expiry),
        volatility=volatility,
        rate=rate,
        dividend_yield=dividend_yield,
        model=model,
    )


def calc_greeks_t(
    option_type: OptionType,
    spot: float,
    strike: float,
    time_to_expiry: float,
    volatility: float,
    rate: float,
    dividend_yield: float = 0.0,
    model: str = "black_scholes",
) -> dict[str, float]:
    """Return one-option Greeks using an exact year fraction.

    option_type is the string 'call' or 'put'. time_to_expiry is years (0.25
    means one quarter of a year). volatility, rate and dividend_yield are annual
    decimal fractions: pass 0.25 for 25% volatility and 0.04 for a 4% rate,
    never 25 or 4. spot and strike are prices in the same currency.
    Returns a dict with exactly delta, gamma, vega, theta and rho keys; it
    does not contain premium or price (call price_option_t for that).
    Vega/rho are per 1.00 change; theta is per calendar day.
    For vega/rho per one percentage point, divide the returned values by 100.
    """
    if model != "black_scholes":
        raise ValueError(f"unsupported model: {model}")
    if option_type not in {"call", "put"}:
        raise ValueError("option_type must be 'call' or 'put'")

    t = max(float(time_to_expiry), 0.0)
    if t == 0:
        return {"delta": 0.0, "gamma": 0.0, "vega": 0.0, "theta": 0.0, "rho": 0.0}

    d1, d2 = _d1_d2(spot, strike, t, volatility, rate, dividend_yield)
    pdf_d1 = norm.pdf(d1)
    exp_qt = exp(-dividend_yield * t)
    exp_rt = exp(-rate * t)

    gamma = exp_qt * pdf_d1 / (spot * volatility * sqrt(t))
    vega = spot * exp_qt * pdf_d1 * sqrt(t)

    if option_type == "call":
        delta = exp_qt * norm.cdf(d1)
        theta = (
            -spot * exp_qt * pdf_d1 * volatility / (2 * sqrt(t))
            - rate * strike * exp_rt * norm.cdf(d2)
            + dividend_yield * spot * exp_qt * norm.cdf(d1)
        ) / 365.0
        rho = strike * t * exp_rt * norm.cdf(d2)
    else:
        delta = exp_qt * (norm.cdf(d1) - 1)
        theta = (
            -spot * exp_qt * pdf_d1 * volatility / (2 * sqrt(t))
            + rate * strike * exp_rt * norm.cdf(-d2)
            - dividend_yield * spot * exp_qt * norm.cdf(-d1)
        ) / 365.0
        rho = -strike * t * exp_rt * norm.cdf(-d2)

    return {
        "delta": float(delta),
        "gamma": float(gamma),
        "vega": float(vega),
        "theta": float(theta),
        "rho": float(rho),
    }
