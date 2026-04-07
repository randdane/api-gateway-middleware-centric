"""Integration tests for the Prometheus /metrics scrape endpoint.

These tests start the full application via ``TestClient`` (in-process, no
Docker required) and verify that:

1. The ``/metrics`` endpoint is reachable and returns valid Prometheus text.
2. After making a request, ``gateway_requests_total`` appears in the output.

Because the tests run in the same process as the app, they share the default
``prometheus_client`` registry with every other test.  We therefore assert on
the *presence* of metric names / lines rather than on exact counter values,
which avoids coupling to test execution order.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def app_client():
    """Create a TestClient for the full gateway app.

    Redis / DB calls in the lifespan are patched out so the test doesn't
    need running infrastructure.
    """
    # Patch infrastructure that isn't available in CI / unit test environment.
    with (
        patch("gateway.main.init_redis"),
        patch("gateway.main.close_redis", new_callable=AsyncMock),
        patch("gateway.main.start_background_worker", return_value=MagicMock(cancel=lambda: None)),
        patch("gateway.db.session.engine") as mock_engine,
    ):
        mock_engine.dispose = AsyncMock()

        from gateway.main import create_app

        application = create_app()
        with TestClient(application, raise_server_exceptions=False) as client:
            yield client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_200(self, app_client: TestClient):
        """/metrics returns HTTP 200."""
        response = app_client.get("/metrics")
        assert response.status_code == 200

    def test_metrics_content_type_is_prometheus(self, app_client: TestClient):
        """/metrics content-type indicates Prometheus text format."""
        response = app_client.get("/metrics")
        # Prometheus text format uses text/plain; version=0.0.4
        assert "text/plain" in response.headers.get("content-type", "")

    def test_gateway_requests_total_present_after_request(self, app_client: TestClient):
        """After making a request, gateway_requests_total appears in /metrics output."""
        # Make a request that will be tracked by TracingMiddleware.
        app_client.get("/health")

        metrics_response = app_client.get("/metrics")
        body = metrics_response.text

        assert "gateway_requests_total" in body, (
            "gateway_requests_total not found in /metrics output.\n"
            f"First 2000 chars:\n{body[:2000]}"
        )

    def test_metrics_output_contains_all_defined_metrics(self, app_client: TestClient):
        """All 7 defined metric families appear in /metrics output after activity."""
        # Trigger a request to ensure counters/histograms have observations.
        app_client.get("/health")

        body = app_client.get("/metrics").text

        expected_metric_names = [
            "gateway_requests_total",
            "gateway_request_duration_seconds",
            "gateway_cache_hits_total",
            "gateway_cache_misses_total",
            "gateway_quota_remaining",
            "gateway_rate_limit_rejections_total",
            "gateway_vendor_errors_total",
        ]

        missing = [name for name in expected_metric_names if name not in body]
        assert not missing, f"Missing metrics in /metrics output: {missing}"

    def test_gateway_requests_total_increments(self, app_client: TestClient):
        """Making additional requests increases the gateway_requests_total sample."""
        import re

        def _extract_count(text: str) -> int:
            """Sum all gateway_requests_total sample values in the metrics text."""
            total = 0
            for line in text.splitlines():
                if line.startswith("gateway_requests_total{"):
                    # Line looks like: gateway_requests_total{...} 3.0
                    match = re.search(r"\}\s+([\d.e+]+)$", line)
                    if match:
                        total += float(match.group(1))
            return int(total)

        body_before = app_client.get("/metrics").text
        count_before = _extract_count(body_before)

        # Make two more requests.
        app_client.get("/health")
        app_client.get("/health")

        body_after = app_client.get("/metrics").text
        count_after = _extract_count(body_after)

        assert count_after >= count_before + 2, (
            f"Expected at least {count_before + 2} total requests, got {count_after}"
        )
