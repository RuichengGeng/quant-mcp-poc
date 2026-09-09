from quant_mcp.pricing import (
    calc_greeks,
    calc_greeks_t,
    price_option,
    price_option_t,
)


def test_black_scholes_call_price_is_reasonable() -> None:
    price = price_option(
        option_type="call",
        spot=100,
        strike=100,
        expiry="2027-09-03",
        valuation_date="2026-09-03",
        volatility=0.2,
        rate=0.05,
    )

    assert round(price, 2) == 10.45


def test_black_scholes_call_price_supports_exact_time_to_expiry() -> None:
    dated_price = price_option(
        option_type="call",
        spot=100,
        strike=110,
        expiry="2026-12-04",
        valuation_date="2026-09-04",
        volatility=0.25,
        rate=0.04,
    )
    exact_t_price = price_option_t(
        option_type="call",
        spot=100,
        strike=110,
        time_to_expiry=91 / 365,
        volatility=0.25,
        rate=0.04,
    )

    assert exact_t_price == dated_price


def test_put_delta_is_negative() -> None:
    greeks = calc_greeks(
        option_type="put",
        spot=100,
        strike=100,
        expiry="2027-09-03",
        valuation_date="2026-09-03",
        volatility=0.2,
        rate=0.05,
    )

    assert greeks["delta"] < 0
    assert greeks["gamma"] > 0


def test_greeks_support_exact_time_to_expiry() -> None:
    dated_greeks = calc_greeks(
        option_type="call",
        spot=100,
        strike=110,
        expiry="2026-12-04",
        valuation_date="2026-09-04",
        volatility=0.25,
        rate=0.04,
    )
    exact_t_greeks = calc_greeks_t(
        option_type="call",
        spot=100,
        strike=110,
        time_to_expiry=91 / 365,
        volatility=0.25,
        rate=0.04,
    )

    assert exact_t_greeks == dated_greeks
