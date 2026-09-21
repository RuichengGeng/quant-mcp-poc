"""OpenTelemetry instrumentation; SDK/exporter configuration belongs to the app."""
from __future__ import annotations

import importlib
import logging
from contextlib import contextmanager

from opentelemetry import context, propagate, trace
from opentelemetry.trace import SpanKind, Status, StatusCode

tracer = trace.get_tracer("quant_mcp")
_configured: set[str] = set()


def configure_telemetry(setup: str | None) -> None:
    """Invoke an application-owned, importable no-argument setup hook once/process."""
    if setup is None or setup in _configured:
        return
    module, separator, name = setup.partition(":")
    if not separator or not module or not name or module == "__main__":
        raise ValueError("telemetry_setup must be an importable 'module:function'")
    hook = getattr(importlib.import_module(module), name)
    hook()
    _configured.add(setup)


def flush_telemetry() -> None:
    """Flush an application SDK if present; the API-only default has no exporter."""
    flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if flush is not None:
        try:
            if flush(timeout_millis=5000) is False:
                logging.getLogger(__name__).warning("Telemetry flush timed out")
        except Exception as exc:
            logging.getLogger(__name__).warning("Telemetry flush failed (%s)", type(exc).__name__)


def inject_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    propagate.inject(carrier)
    return carrier


@contextmanager
def extracted_context(carrier: dict[str, str]):
    token = context.attach(propagate.extract(carrier))
    try:
        yield
    finally:
        context.detach(token)


def set_outcome(status: str) -> None:
    span = trace.get_current_span()
    span.set_attribute("quant_mcp.outcome", status)
    if status in {"failed", "timed_out", "cancelled", "rejected"}:
        span.set_status(Status(StatusCode.ERROR))


@contextmanager
def operation(name: str, *, kind=SpanKind.INTERNAL, **attributes):
    # Automatic exception recording includes messages/stack traces. Keep the
    # framework's default telemetry to metadata; detailed diagnostics stay local.
    with tracer.start_as_current_span(
        name, kind=kind, attributes=attributes, record_exception=False, set_status_on_exception=False,
    ) as span:
        try:
            yield span
        except BaseException as exc:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            raise
