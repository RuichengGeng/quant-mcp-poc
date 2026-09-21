"""Optional application-owned OTel setup, imported by server and worker processes."""
import os
import sys


def configure():
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    exporter_name = os.getenv("QUANT_MCP_TRACE_EXPORTER", "console")
    if exporter_name == "console":
        # stdout is reserved for MCP; worker stderr is captured in diagnostic logs.
        exporter = ConsoleSpanExporter(out=sys.stderr)
    elif exporter_name == "otlp":
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter()  # Standard OTEL_EXPORTER_OTLP_* settings.
    else:
        raise ValueError("QUANT_MCP_TRACE_EXPORTER must be console or otlp")

    provider = TracerProvider(resource=Resource.create({
        "service.name": os.getenv("OTEL_SERVICE_NAME", "quant-mcp-example"),
    }))
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
