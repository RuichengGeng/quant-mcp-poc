import os

import pytest


@pytest.fixture(autouse=True)
def isolated_model_and_telemetry_environment(monkeypatch):
    """Keep tests offline and prevent model/config tests leaking environment state."""
    prefixes = ("SMOLAGENTS_", "DEEPSEEK_", "OTEL_", "QUANT_MCP_TRACE_")
    for key in tuple(os.environ):
        if key.startswith(prefixes):
            monkeypatch.delenv(key)
    yield
    # load_env_file also adds keys directly, outside monkeypatch's bookkeeping.
    for key in tuple(os.environ):
        if key.startswith(prefixes):
            os.environ.pop(key, None)


@pytest.fixture
def otel_spans(monkeypatch):
    """An isolated SDK provider; do not replace the process-global app provider."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from quant_mcp import tracing

    provider = TracerProvider(shutdown_on_exit=False)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "tracer", provider.get_tracer("quant_mcp"))
    yield exporter
    provider.shutdown()
