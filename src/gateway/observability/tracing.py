"""OpenTelemetry tracer setup.

Initialises the OTel tracer provider with:
- OTLP gRPC exporter when ``settings.otel_endpoint`` is configured.
- ``NoOpSpanExporter`` (zero-overhead) otherwise, which is the default in
  tests and local development without a collector.

Usage::

    from gateway.observability.tracing import tracer

    with tracer.start_as_current_span("my-operation") as span:
        span.set_attribute("key", "value")
"""

from __future__ import annotations

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import NoOpTracer

from gateway.config import settings

# Module-level tracer — replaced by setup_tracing() at app startup.
# Falls back to NoOpTracer so imports before setup_tracing() are safe.
tracer: trace.Tracer = NoOpTracer()

# Keep a reference so tests can inspect spans when needed.
_provider: TracerProvider | None = None


def setup_tracing() -> None:
    """Initialise the global OTel tracer provider.

    Should be called once during application startup (inside ``create_app``).
    Subsequent calls are safe but will reinitialise the provider.
    """
    global tracer, _provider  # noqa: PLW0603

    resource = Resource.create(
        {
            "service.name": settings.otel_service_name,
            "service.version": "0.1.0",
        }
    )

    provider = TracerProvider(resource=resource)

    if settings.otel_endpoint:
        # Real OTLP exporter when a collector endpoint is provided.
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )

        otlp_exporter = OTLPSpanExporter(endpoint=settings.otel_endpoint)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
    else:
        # No-op path: register a simple processor with an in-memory exporter
        # that immediately discards spans.  This keeps trace context propagation
        # working (valid trace/span IDs) without any I/O.
        noop_exporter = InMemorySpanExporter()
        provider.add_span_processor(SimpleSpanProcessor(noop_exporter))
        # Immediately clear the in-memory buffer to avoid memory growth.
        noop_exporter.clear()

    trace.set_tracer_provider(provider)
    _provider = provider

    tracer = get_tracer()


def get_tracer() -> trace.Tracer:
    """Return the configured tracer for this service.

    If ``setup_tracing()`` has not been called yet (e.g. in tests that import
    this module directly), a new tracer is returned from whatever provider is
    currently registered with the OTel API.
    """
    return trace.get_tracer(settings.otel_service_name)
