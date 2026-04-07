"""OTel span-creation middleware and Prometheus request metrics.

``TracingMiddleware`` wraps every inbound request in an OpenTelemetry span and
records the two core request metrics:

- ``gateway_requests_total``   — counter, labelled by vendor/endpoint/status/user
- ``gateway_request_duration_seconds`` — histogram, labelled by vendor/endpoint

It also stores ``trace_id`` and ``span_id`` in ``request.state`` so downstream
middleware and route handlers can include them in logs / response headers.

Middleware ordering
-------------------
``TracingMiddleware`` must be the **outermost** middleware so that every
inbound byte (including requests rejected by inner middleware) is covered by
a span.  In ``create_app`` it should be added *last* via ``add_middleware``
(Starlette reverses the order, so the last added is the outermost wrapper).
"""

from __future__ import annotations

import re
import time

from opentelemetry import trace
from opentelemetry.trace import SpanKind, StatusCode
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from gateway.observability.metrics import (
    gateway_request_duration_seconds,
    gateway_requests_total,
)
from gateway.observability.tracing import get_tracer

# ---------------------------------------------------------------------------
# URL helpers (shared pattern from other middleware)
# ---------------------------------------------------------------------------

_VENDOR_SLUG_RE = re.compile(r"^/(?:vendors|v1)/([^/]+)(.*)?$")


def _parse_vendor_and_endpoint(path: str) -> tuple[str, str]:
    """Return ``(vendor_slug, endpoint)`` from *path*.

    Falls back to ``("unknown", path)`` for non-vendor paths so metric labels
    are always populated strings (Prometheus does not allow None label values).
    """
    m = _VENDOR_SLUG_RE.match(path)
    if m is None:
        return "unknown", path
    slug = m.group(1)
    endpoint = m.group(2) or "/"
    return slug, endpoint


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TracingMiddleware(BaseHTTPMiddleware):
    """Wrap each request in an OTel span and record Prometheus metrics."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        tracer = get_tracer()
        method = request.method
        path = request.url.path
        vendor_slug, endpoint = _parse_vendor_and_endpoint(path)

        span_name = f"{method} {path}"

        with tracer.start_as_current_span(
            span_name,
            kind=SpanKind.SERVER,
        ) as span:
            # ----------------------------------------------------------------
            # Store trace / span IDs in request state for downstream use.
            # ----------------------------------------------------------------
            ctx = span.get_span_context()
            if ctx.is_valid:
                request.state.trace_id = format(ctx.trace_id, "032x")
                request.state.span_id = format(ctx.span_id, "016x")
            else:
                request.state.trace_id = "0" * 32
                request.state.span_id = "0" * 16

            # ----------------------------------------------------------------
            # Set standard HTTP span attributes.
            # ----------------------------------------------------------------
            span.set_attribute("http.method", method)
            span.set_attribute("http.url", str(request.url))
            if vendor_slug != "unknown":
                span.set_attribute("vendor.slug", vendor_slug)

            # ----------------------------------------------------------------
            # Execute the request.
            # ----------------------------------------------------------------
            start = time.monotonic()
            exc_to_raise: BaseException | None = None
            response: Response | None = None

            try:
                response = await call_next(request)
            except Exception as exc:  # noqa: BLE001
                exc_to_raise = exc

            duration = time.monotonic() - start

            # ----------------------------------------------------------------
            # Finalise span with HTTP status.
            # ----------------------------------------------------------------
            status_code: int = 500 if response is None else response.status_code
            span.set_attribute("http.status_code", status_code)

            if status_code >= 500:
                span.set_status(StatusCode.ERROR, f"HTTP {status_code}")
            else:
                span.set_status(StatusCode.OK)

            # ----------------------------------------------------------------
            # Record Prometheus metrics.
            # ----------------------------------------------------------------
            user = getattr(request.state, "user", None)
            user_label: str = (
                getattr(user, "sub", "anonymous") if user is not None else "anonymous"
            )

            gateway_requests_total.labels(
                vendor=vendor_slug,
                endpoint=endpoint,
                status=str(status_code),
                user=user_label,
            ).inc()

            gateway_request_duration_seconds.labels(
                vendor=vendor_slug,
                endpoint=endpoint,
            ).observe(duration)

            # ----------------------------------------------------------------
            # Re-raise any exception that occurred during request processing.
            # ----------------------------------------------------------------
            if exc_to_raise is not None:
                raise exc_to_raise

        return response  # type: ignore[return-value]
