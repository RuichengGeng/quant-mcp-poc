from types import SimpleNamespace

import pytest

from quant_mcp.testing.assertions import AssertionFailure, assert_tool_contracts, assert_tool_result


def result(*, structured=None, error=False, text=""):
    return SimpleNamespace(
        structuredContent=structured,
        isError=error,
        content=[SimpleNamespace(text=text)] if text else [],
    )


def test_assertions_support_nested_approximate_values_and_subsets():
    assert_tool_result(
        result(structured={"result": {"delta": 0.63683, "extra": 1}}),
        {"structured_content": {"result": {"delta": {"approximately": 0.6368, "tolerance": 0.01}}}},
    )


def test_assertions_report_mcp_errors():
    with pytest.raises(AssertionFailure, match="MCP error"):
        assert_tool_result(result(error=True, text="bad input"), {"not_error": True})


def test_assertions_can_require_text():
    assert_tool_result(result(text="completed successfully"), {"text_contains": ["successfully"]})


def test_tool_contracts_check_schema_and_annotations():
    tool = SimpleNamespace(
        name="price",
        inputSchema={
            "required": ["spot"],
            "properties": {"spot": {"type": "number"}},
        },
        annotations=SimpleNamespace(readOnlyHint=True),
        description="Price an option",
    )
    assert_tool_contracts([tool], {
        "price": {
            "required": ["spot"],
            "properties": {"spot": {"type": "number"}},
            "description_contains": ["option"],
            "read_only": True,
        },
    })
