"""Unit tests for gateway.middleware.tracing.TracingMiddleware.

All tests use a minimal in-process FastAPI app with the middleware attached —
no external services required.

OTel spans
----------
The tests rely on ``opentelemetry.sdk.trace.export.InMemorySpanExporter``
to capture spans without a real collector.  Because the OTel global
``TracerProvider`` can only be set once via ``set_tracer_provider``, we
force-reset the module-level state before each test that needs span inspection.

Prometheus metrics
------------------
``prometheus_client`` uses a process-wide default registry.  Helper
``_hist_count`` reads the observation count directly from the ``+Inf`` bucket
of a labelled histogram child so tests can assert on increments without
patching internals.
"""

from __future__ import annotations

import opentelemetry.trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from gateway.middleware.tracing import TracingMiddleware


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reset_and_install_provider() -> InMemorySpanExporter:
    """Force-reset the OTel global and install a fresh InMemorySpanExporter.

    ``trace.set_tracer_provider`` is guarded by a ``Once`` sentinel; we clear
    it so each test gets its own isolated provider.

    Returns the exporter so callers can inspect finished spans.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Force-reset the global sentinel so set_tracer_provider is accepted.
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = False
    otel_trace._TRACER_PROVIDER = None
    otel_trace.set_tracer_provider(provider)

    return exporter


def _make_app(
    *,
    response_status: int = 200,
    capture_state: list | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with TracingMiddleware attached."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/vendors/{slug}/endpoint")
    async def vendor_endpoint(slug: str, request: Request):
        if capture_state is not None:
            capture_state.append(
                {
                    "trace_id": getattr(request.state, "trace_id", None),
                    "span_id": getattr(request.state, "span_id", None),
                }
            )
        return Response(
            content='{"ok": true}',
            status_code=response_status,
            media_type="application/json",
        )

    app.add_middleware(TracingMiddleware)
    return app


def _read_counter(metric, **labels) -> float:
    """Read the current value of a Prometheus counter for the given labels."""
    return metric.labels(**labels)._value.get()


def _hist_count(metric, **labels) -> float:
    """Return the observation count for a labelled Histogram child.

    Reads the ``_count`` sample emitted by ``_child_samples()``.
    """
    child = metric.labels(**labels)
    for sample in child._child_samples():
        if sample.name == "_count":
            return sample.value
    return 0.0


# ---------------------------------------------------------------------------
# Span creation
# ---------------------------------------------------------------------------


class TestSpanCreation:
    def test_span_is_created_per_request(self):
        """A finished span exists in the exporter after each request."""
        exporter = _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/health")

        spans = exporter.get_finished_spans()
        assert len(spans) >= 1

    def test_span_name_contains_method_and_path(self):
        """Span name is '{METHOD} {path}'."""
        exporter = _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        spans = exporter.get_finished_spans()
        assert any("GET" in s.name and "/vendors/stripe/endpoint" in s.name for s in spans)

    def test_span_has_http_method_attribute(self):
        """http.method attribute is set on the span."""
        exporter = _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        spans = exporter.get_finished_spans()
        vendor_spans = [s for s in spans if "/vendors/stripe/endpoint" in s.name]
        assert vendor_spans, "No span found for vendor endpoint"
        assert vendor_spans[0].attributes.get("http.method") == "GET"

    def test_span_has_http_status_code_attribute(self):
        """http.status_code attribute reflects the actual response status."""
        exporter = _reset_and_install_provider()
        app = _make_app(response_status=201)
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        spans = exporter.get_finished_spans()
        vendor_spans = [s for s in spans if "/vendors/stripe/endpoint" in s.name]
        assert vendor_spans, "No span found for vendor endpoint"
        assert vendor_spans[0].attributes.get("http.status_code") == 201

    def test_span_has_vendor_slug_attribute(self):
        """vendor.slug attribute is set for vendor paths."""
        exporter = _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/acme/endpoint")

        spans = exporter.get_finished_spans()
        vendor_spans = [s for s in spans if "/vendors/acme/endpoint" in s.name]
        assert vendor_spans, "No span found for /vendors/acme/endpoint"
        assert vendor_spans[0].attributes.get("vendor.slug") == "acme"

    def test_span_no_vendor_slug_for_non_vendor_path(self):
        """vendor.slug attribute is absent for non-vendor paths."""
        exporter = _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/health")

        spans = exporter.get_finished_spans()
        health_spans = [s for s in spans if "/health" in s.name]
        assert health_spans, "No span found for /health"
        assert "vendor.slug" not in health_spans[0].attributes


# ---------------------------------------------------------------------------
# Prometheus metrics
# ---------------------------------------------------------------------------


class TestMetricsRecording:
    def test_requests_total_incremented(self):
        """gateway_requests_total is incremented after a request."""
        from gateway.observability.metrics import gateway_requests_total

        _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        before = _read_counter(
            gateway_requests_total,
            vendor="stripe",
            endpoint="/endpoint",
            status="200",
            user="anonymous",
        )
        client.get("/vendors/stripe/endpoint")
        after = _read_counter(
            gateway_requests_total,
            vendor="stripe",
            endpoint="/endpoint",
            status="200",
            user="anonymous",
        )

        assert after == before + 1

    def test_requests_total_uses_correct_status(self):
        """gateway_requests_total records the actual HTTP status code."""
        from gateway.observability.metrics import gateway_requests_total

        _reset_and_install_provider()
        app = _make_app(response_status=404)
        client = TestClient(app, raise_server_exceptions=False)

        before = _read_counter(
            gateway_requests_total,
            vendor="stripe",
            endpoint="/endpoint",
            status="404",
            user="anonymous",
        )
        client.get("/vendors/stripe/endpoint")
        after = _read_counter(
            gateway_requests_total,
            vendor="stripe",
            endpoint="/endpoint",
            status="404",
            user="anonymous",
        )

        assert after == before + 1

    def test_request_duration_observed(self):
        """gateway_request_duration_seconds has observations after a request."""
        from gateway.observability.metrics import gateway_request_duration_seconds

        _reset_and_install_provider()
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        before_count = _hist_count(
            gateway_request_duration_seconds,
            vendor="stripe",
            endpoint="/endpoint",
        )
        client.get("/vendors/stripe/endpoint")
        after_count = _hist_count(
            gateway_request_duration_seconds,
            vendor="stripe",
            endpoint="/endpoint",
        )

        assert after_count == before_count + 1


# ---------------------------------------------------------------------------
# request.state — trace_id and span_id
# ---------------------------------------------------------------------------


class TestRequestState:
    def test_trace_id_stored_in_request_state(self):
        """trace_id is set in request.state."""
        _reset_and_install_provider()
        captured: list[dict] = []
        app = _make_app(capture_state=captured)
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        assert len(captured) == 1
        assert captured[0]["trace_id"] is not None
        assert len(captured[0]["trace_id"]) == 32  # 128-bit hex

    def test_span_id_stored_in_request_state(self):
        """span_id is set in request.state."""
        _reset_and_install_provider()
        captured: list[dict] = []
        app = _make_app(capture_state=captured)
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        assert len(captured) == 1
        assert captured[0]["span_id"] is not None
        assert len(captured[0]["span_id"]) == 16  # 64-bit hex

    def test_trace_id_matches_active_span(self):
        """The trace_id stored in state matches the finished span's trace_id."""
        exporter = _reset_and_install_provider()
        captured: list[dict] = []
        app = _make_app(capture_state=captured)
        client = TestClient(app, raise_server_exceptions=False)

        client.get("/vendors/stripe/endpoint")

        spans = exporter.get_finished_spans()
        vendor_spans = [s for s in spans if "/vendors/stripe/endpoint" in s.name]
        assert vendor_spans, "No vendor span found"

        expected_trace_id = format(vendor_spans[0].context.trace_id, "032x")
        assert captured[0]["trace_id"] == expected_trace_id
