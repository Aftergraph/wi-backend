"""OpenTelemetry tracing configuration."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI


def setup_tracing(app: FastAPI) -> None:
    """Configure OpenTelemetry tracing for FastAPI.

    Environment variables:
    - OTEL_ENABLED=true          (default: false)
    - OTEL_SERVICE_NAME          (default: aftergraph-work-intelligence)
    - OTEL_EXPORTER_OTLP_ENDPOINT (default: http://localhost:4317)
    - OTEL_EXPORTER_TYPE=otlp    (otlp, console, none)
    """
    if os.getenv("OTEL_ENABLED", "false").lower() != "true":
        return

    try:
        from opentelemetry import trace
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )

        service_name = os.getenv("OTEL_SERVICE_NAME", "aftergraph-work-intelligence")
        exporter_type = os.getenv("OTEL_EXPORTER_TYPE", "otlp")

        # Create resource
        resource = Resource(attributes={SERVICE_NAME: service_name})

        # Create tracer provider
        provider = TracerProvider(resource=resource)

        # Add exporter
        exporter: Any
        if exporter_type == "otlp":
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )
            endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
            exporter = OTLPSpanExporter(endpoint=endpoint)
        elif exporter_type == "console":
            exporter = ConsoleSpanExporter()
        else:
            return

        provider.add_span_processor(BatchSpanProcessor(exporter))

        # Set global provider
        trace.set_tracer_provider(provider)

        # Instrument FastAPI
        FastAPIInstrumentor.instrument_app(app)

    except ImportError:
        pass  # OpenTelemetry not installed
    except Exception:
        pass  # Graceful failure
