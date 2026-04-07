"""Request/response structured logging middleware.

Logs one record per request containing all observability fields from the spec:
trace_id, span_id, user_id, service_account, vendor_slug, endpoint, method,
status_code, latency_ms, cache_hit, quota_remaining.
"""

from __future__ import annotations

import re
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# URL parsing helpers — same approach as rate_limit.py
# ---------------------------------------------------------------------------

# Matches /vendors/{slug}/... or /v1/{slug}/...
_VENDOR_SLUG_RE = re.compile(r"^/(?:vendors|v1)/([^/]+)(.*)?$")


def _parse_vendor_and_endpoint(path: str) -> tuple[str | None, str | None]:
    """Return ``(vendor_slug, endpoint)`` from *path*, or ``(None, None)``."""
    m = _VENDOR_SLUG_RE.match(path)
    if m is None:
        return None, None
    slug = m.group(1)
    endpoint = m.group(2) or "/"
    return slug, endpoint


# ---------------------------------------------------------------------------
# OTel span context helper — Phase 7.2 will wire real spans; for now we
# fall back to UUID-based IDs.
# ---------------------------------------------------------------------------


def _get_trace_ids() -> tuple[str, str | None]:
    """Return ``(trace_id, span_id)`` from the current OTel span context.

    If no active span is available (or the OTel SDK is not yet configured),
    generate a random UUID trace_id and return span_id as None.
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            trace_id = format(ctx.trace_id, "032x")
            span_id = format(ctx.span_id, "016x")
            return trace_id, span_id
    except Exception:  # noqa: BLE001
        pass

    return str(uuid.uuid4()), None


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class LoggingMiddleware(BaseHTTPMiddleware):
    """Structured request/response logging middleware.

    Captures all required log fields and emits a single structured log record
    after each request completes (or fails with an unhandled exception).

    The middleware is intentionally fail-safe: logging errors are swallowed so
    that a logging bug never breaks a request.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        # ------------------------------------------------------------------
        # 1. Establish trace / span IDs
        # ------------------------------------------------------------------
        trace_id, span_id = _get_trace_ids()

        # Allow downstream code (and the response path) to read the trace_id.
        request.state.trace_id = trace_id

        # ------------------------------------------------------------------
        # 2. Parse URL fields
        # ------------------------------------------------------------------
        vendor_slug, endpoint = _parse_vendor_and_endpoint(request.url.path)

        # ------------------------------------------------------------------
        # 3. Time the request
        # ------------------------------------------------------------------
        start = time.monotonic()
        exc_to_raise: BaseException | None = None
        response: Response | None = None

        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            exc_to_raise = exc

        latency_ms = (time.monotonic() - start) * 1000

        # ------------------------------------------------------------------
        # 4. Extract response-derived fields
        # ------------------------------------------------------------------
        status_code: int | None = None
        cache_hit: bool = False
        quota_remaining: int | None = None

        if response is not None:
            status_code = response.status_code
            cache_hit = response.headers.get("X-Cache", "").upper() == "HIT"
            quota_header = response.headers.get("X-Quota-Remaining")
            if quota_header is not None:
                try:
                    quota_remaining = int(quota_header)
                except ValueError:
                    pass

        # ------------------------------------------------------------------
        # 5. Extract user identity fields from request state
        #    (set by the auth dependency *after* middleware, so may be absent)
        # ------------------------------------------------------------------
        user = getattr(request.state, "user", None)
        user_id: str | None = getattr(user, "sub", None) if user is not None else None
        service_account: bool = (
            getattr(user, "is_service_account", False) if user is not None else False
        )

        # ------------------------------------------------------------------
        # 6. Emit structured log
        # ------------------------------------------------------------------
        try:
            log_kwargs: dict = {
                "trace_id": trace_id,
                "span_id": span_id,
                "user_id": user_id,
                "service_account": service_account,
                "vendor_slug": vendor_slug,
                "endpoint": endpoint,
                "method": request.method,
                "status_code": status_code,
                "latency_ms": round(latency_ms, 3),
                "cache_hit": cache_hit,
                "quota_remaining": quota_remaining,
            }

            if exc_to_raise is not None:
                logger.error(
                    "request.error",
                    exc_info=exc_to_raise,
                    **log_kwargs,
                )
            else:
                logger.info("request.complete", **log_kwargs)
        except Exception:  # noqa: BLE001
            # Never let a logging failure break a request.
            pass

        # ------------------------------------------------------------------
        # 7. Re-raise any exception that occurred during request processing
        # ------------------------------------------------------------------
        if exc_to_raise is not None:
            raise exc_to_raise

        return response  # type: ignore[return-value]
