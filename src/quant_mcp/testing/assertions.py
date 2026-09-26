"""Small deterministic assertions for MCP tool results."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from numbers import Real
from typing import Any


class AssertionFailure(AssertionError):
    """A scenario assertion failed."""


def _is_error(result: Any) -> bool:
    return bool(getattr(result, "isError", getattr(result, "is_error", False)))


def _structured_content(result: Any) -> Any:
    return getattr(result, "structuredContent", getattr(result, "structured_content", None))


def _text_content(result: Any) -> str:
    texts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(str(text))
    return "\n".join(texts)


def _matches(actual: Any, expected: Any, path: str) -> None:
    if isinstance(expected, Mapping) and "approximately" in expected:
        if not isinstance(actual, Real):
            raise AssertionFailure(f"{path}: expected a number, got {actual!r}")
        target = expected["approximately"]
        tolerance = expected.get("tolerance", 0.0)
        if abs(actual - target) > tolerance:
            raise AssertionFailure(
                f"{path}: expected {actual!r} to be within {tolerance!r} of {target!r}"
            )
        return
    if isinstance(expected, Mapping) and "contains" in expected:
        needle = expected["contains"]
        if isinstance(actual, str) and str(needle) not in actual:
            raise AssertionFailure(f"{path}: expected {actual!r} to contain {needle!r}")
        if isinstance(actual, Sequence) and not isinstance(actual, (str, bytes)) and needle not in actual:
            raise AssertionFailure(f"{path}: expected {actual!r} to contain {needle!r}")
        return
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            raise AssertionFailure(f"{path}: expected an object, got {actual!r}")
        for key, value in expected.items():
            if key not in actual:
                raise AssertionFailure(f"{path}: missing key {key!r}")
            _matches(actual[key], value, f"{path}.{key}")
        return
    if isinstance(expected, list):
        if actual != expected:
            raise AssertionFailure(f"{path}: expected {expected!r}, got {actual!r}")
        return
    if actual != expected:
        raise AssertionFailure(f"{path}: expected {expected!r}, got {actual!r}")


def assert_tool_result(result: Any, assertions: Mapping[str, Any]) -> None:
    """Apply scenario assertions to an MCP ``CallToolResult``."""
    if assertions.get("not_error", True) and _is_error(result):
        raise AssertionFailure(f"tool returned an MCP error: {_text_content(result)}")
    if "is_error" in assertions and _is_error(result) != bool(assertions["is_error"]):
        raise AssertionFailure(f"expected is_error={assertions['is_error']!r}, got {_is_error(result)!r}")
    if "text_contains" in assertions:
        text = _text_content(result)
        for needle in assertions["text_contains"]:
            if str(needle) not in text:
                raise AssertionFailure(f"text output does not contain {needle!r}: {text!r}")
    if "structured_content" in assertions:
        _matches(_structured_content(result), assertions["structured_content"], "structured_content")
