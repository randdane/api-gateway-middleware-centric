"""Prometheus metric definitions for the API gateway.

All metrics are module-level singletons.  Import the names you need::

    from gateway.observability.metrics import (
        gateway_requests_total,
        gateway_request_duration_seconds,
    )

    gateway_requests_total.labels(
        vendor="stripe", endpoint="/charges", status="200", user="u-123"
    ).inc()

Duplicate-registration safety
------------------------------
``prometheus_client`` raises ``ValueError`` if a metric with the same name is
registered twice in the same process (a common problem in test suites that
import modules multiple times).  We guard each definition with a try/except so
re-imports are safe.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram


def _counter(name: str, documentation: str, labelnames: list[str]) -> Counter:
    """Create or retrieve a Counter, guarding against duplicate registration."""
    try:
        return Counter(name, documentation, labelnames)
    except ValueError:
        # Already registered — retrieve the existing instance from the registry.
        from prometheus_client import REGISTRY  # noqa: PLC0415

        return REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


def _histogram(name: str, documentation: str, labelnames: list[str]) -> Histogram:
    """Create or retrieve a Histogram, guarding against duplicate registration."""
    try:
        return Histogram(name, documentation, labelnames)
    except ValueError:
        from prometheus_client import REGISTRY  # noqa: PLC0415

        return REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


def _gauge(name: str, documentation: str, labelnames: list[str]) -> Gauge:
    """Create or retrieve a Gauge, guarding against duplicate registration."""
    try:
        return Gauge(name, documentation, labelnames)
    except ValueError:
        from prometheus_client import REGISTRY  # noqa: PLC0415

        return REGISTRY._names_to_collectors[name]  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Request metrics
# ---------------------------------------------------------------------------

gateway_requests_total: Counter = _counter(
    "gateway_requests_total",
    "Total number of requests handled by the gateway.",
    ["vendor", "endpoint", "status", "user"],
)

gateway_request_duration_seconds: Histogram = _histogram(
    "gateway_request_duration_seconds",
    "Request duration in seconds.",
    ["vendor", "endpoint"],
)

# ---------------------------------------------------------------------------
# Cache metrics
# ---------------------------------------------------------------------------

gateway_cache_hits_total: Counter = _counter(
    "gateway_cache_hits_total",
    "Total number of cache hits.",
    ["vendor"],
)

gateway_cache_misses_total: Counter = _counter(
    "gateway_cache_misses_total",
    "Total number of cache misses.",
    ["vendor"],
)

# ---------------------------------------------------------------------------
# Quota / rate-limit metrics
# ---------------------------------------------------------------------------

gateway_quota_remaining: Gauge = _gauge(
    "gateway_quota_remaining",
    "Remaining quota for a vendor/key combination.",
    ["vendor", "key"],
)

gateway_rate_limit_rejections_total: Counter = _counter(
    "gateway_rate_limit_rejections_total",
    "Total number of requests rejected by rate limiting.",
    ["vendor", "scope"],
)

# ---------------------------------------------------------------------------
# Vendor error metrics
# ---------------------------------------------------------------------------

gateway_vendor_errors_total: Counter = _counter(
    "gateway_vendor_errors_total",
    "Total number of errors returned by vendor APIs.",
    ["vendor", "error_type"],
)
