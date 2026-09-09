import json

from quant_mcp.library_catalog import discover_library_catalog, write_catalog


def test_catalog_discovers_public_pricing_functions(tmp_path) -> None:
    catalog = discover_library_catalog(("quant_mcp.pricing",))

    names = {function.name for function in catalog.functions}
    assert {"price_option", "calc_greeks", "price_option_t", "calc_greeks_t"} <= names
    assert all(function.signature for function in catalog.functions)
    price = next(function for function in catalog.functions if function.name == "price_option_t")
    assert "Literal['call', 'put']" in price.signature
    assert "OptionType" not in price.signature
    assert "0.25 for 25% volatility" in price.description
    greeks = next(function for function in catalog.functions if function.name == "calc_greeks_t")
    assert "delta, gamma, vega, theta and rho" in greeks.description

    path = tmp_path / "library_catalog.json"
    write_catalog(catalog, path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["modules"] == ["quant_mcp.pricing"]
    assert any(item["name"] == "price_option" for item in saved["functions"])


def test_catalog_is_included_in_agent_instruction(tmp_path) -> None:
    from quant_mcp.runner import _agent_instruction

    instruction = _agent_instruction("price a call option", tmp_path)

    assert "REGISTERED DETERMINISTIC LIBRARY APIs:" in instruction
    assert "quant_mcp.pricing.price_option" in instruction
