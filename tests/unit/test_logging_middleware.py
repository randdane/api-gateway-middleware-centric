"""Unit tests for gateway.middleware.logging.LoggingMiddleware.

All tests use a minimal FastAPI app with the middleware attached — no external
services required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from gateway.middleware.logging import LoggingMiddleware, _parse_vendor_and_endpoint


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_app(
    *,
    response_status: int = 200,
    response_headers: dict | None = None,
    raise_exc: Exception | None = None,
) -> FastAPI:
    """Build a minimal FastAPI app with LoggingMiddleware attached."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/vendors/{slug}/endpoint")
    async def vendor_endpoint(slug: str, request: Request):
        headers = response_headers or {}
        return Response(
            content='{"ok": true}',
            status_code=response_status,
            headers=headers,
            media_type="application/json",
        )

    @app.get("/boom")
    async def boom():
        if raise_exc:
            raise raise_exc
        return {"ok": True}

    app.add_middleware(LoggingMiddleware)
    return app


# ---------------------------------------------------------------------------
# _parse_vendor_and_endpoint unit tests
# ---------------------------------------------------------------------------


class TestParseVendorAndEndpoint:
    def test_vendors_prefix(self):
        slug, endpoint = _parse_vendor_and_endpoint("/vendors/stripe/charges")
        assert slug == "stripe"
        assert endpoint == "/charges"

    def test_v1_prefix(self):
        slug, endpoint = _parse_vendor_and_endpoint("/v1/acme/items")
        assert slug == "acme"
        assert endpoint == "/items"

    def test_slug_only(self):
        slug, endpoint = _parse_vendor_and_endpoint("/vendors/stripe")
        assert slug == "stripe"
        assert endpoint == "/"

    def test_no_match(self):
        slug, endpoint = _parse_vendor_and_endpoint("/health")
        assert slug is None
        assert endpoint is None

    def test_nested_path(self):
        slug, endpoint = _parse_vendor_and_endpoint("/vendors/foo/a/b/c")
        assert slug == "foo"
        assert endpoint == "/a/b/c"


# ---------------------------------------------------------------------------
# LoggingMiddleware integration tests
# ---------------------------------------------------------------------------


class TestLoggingMiddlewareFields:
    def test_log_contains_method(self):
        """method field is logged."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        mock_logger.info.assert_called_once()
        _, kwargs = mock_logger.info.call_args
        assert kwargs["method"] == "GET"

    def test_log_contains_status_code(self):
        """status_code field reflects the actual response status."""
        app = _make_app(response_status=201)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["status_code"] == 201

    def test_log_contains_latency_ms(self):
        """latency_ms is a non-negative number."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert isinstance(kwargs["latency_ms"], (int, float))
        assert kwargs["latency_ms"] >= 0

    def test_log_contains_trace_id(self):
        """trace_id is always present and non-empty."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert "trace_id" in kwargs
        assert kwargs["trace_id"]  # non-empty string

    def test_log_contains_vendor_slug(self):
        """vendor_slug is extracted from the URL."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["vendor_slug"] == "stripe"

    def test_log_contains_endpoint(self):
        """endpoint is the path segment after the vendor slug."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["endpoint"] == "/endpoint"

    def test_log_contains_span_id_key(self):
        """span_id key is present (may be None when no OTel span is active)."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert "span_id" in kwargs

    def test_all_required_fields_present(self):
        """All spec-required log fields are present in every log record."""
        required = {
            "trace_id",
            "span_id",
            "user_id",
            "service_account",
            "vendor_slug",
            "endpoint",
            "method",
            "status_code",
            "latency_ms",
            "cache_hit",
            "quota_remaining",
        }
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        missing = required - set(kwargs.keys())
        assert not missing, f"Missing log fields: {missing}"


# ---------------------------------------------------------------------------
# cache_hit field
# ---------------------------------------------------------------------------


class TestCacheHitField:
    def test_cache_hit_true_when_x_cache_hit(self):
        """cache_hit is True when the response carries X-Cache: HIT."""
        app = _make_app(response_headers={"X-Cache": "HIT"})
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["cache_hit"] is True

    def test_cache_hit_false_when_x_cache_miss(self):
        """cache_hit is False when X-Cache is MISS."""
        app = _make_app(response_headers={"X-Cache": "MISS"})
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["cache_hit"] is False

    def test_cache_hit_false_when_no_header(self):
        """cache_hit is False when X-Cache header is absent."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["cache_hit"] is False

    def test_cache_hit_case_insensitive(self):
        """X-Cache: hit (lowercase) is also treated as a cache hit."""
        app = _make_app(response_headers={"X-Cache": "hit"})
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["cache_hit"] is True


# ---------------------------------------------------------------------------
# trace_id stored in request.state
# ---------------------------------------------------------------------------


class TestTraceIdInRequestState:
    def test_trace_id_stored_in_request_state(self):
        """trace_id is stored in request.state so route handlers can access it."""
        captured_trace_id: list[str] = []

        app = FastAPI()

        @app.get("/capture")
        async def capture(request: Request):
            tid = getattr(request.state, "trace_id", None)
            if tid:
                captured_trace_id.append(tid)
            return {"ok": True}

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app)
        client.get("/capture")

        assert len(captured_trace_id) == 1
        assert captured_trace_id[0]  # non-empty

    def test_trace_id_is_consistent_within_request(self):
        """The trace_id stored in state matches the one logged."""
        logged_trace_ids: list[str] = []
        state_trace_ids: list[str] = []

        app = FastAPI()

        @app.get("/capture")
        async def capture(request: Request):
            tid = getattr(request.state, "trace_id", None)
            if tid:
                state_trace_ids.append(tid)
            return {"ok": True}

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/capture")

        _, kwargs = mock_logger.info.call_args
        logged_trace_ids.append(kwargs["trace_id"])

        assert state_trace_ids == logged_trace_ids


# ---------------------------------------------------------------------------
# Exception handling
# ---------------------------------------------------------------------------


class TestExceptionHandling:
    def test_exception_is_reraised(self):
        """Unhandled exceptions from route handlers are re-raised after logging."""
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("something broke")

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app, raise_server_exceptions=True)

        with patch("gateway.middleware.logging.logger"):
            with pytest.raises(RuntimeError, match="something broke"):
                client.get("/boom")

    def test_exception_is_logged_at_error_level(self):
        """Exceptions cause logger.error (not logger.info) to be called."""
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise ValueError("oops")

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/boom")

        mock_logger.error.assert_called_once()
        mock_logger.info.assert_not_called()

    def test_error_log_contains_required_fields(self):
        """Error-level log still includes method, trace_id, latency_ms."""
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("fail")

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/boom")

        _, kwargs = mock_logger.error.call_args
        assert "method" in kwargs
        assert "trace_id" in kwargs
        assert "latency_ms" in kwargs


# ---------------------------------------------------------------------------
# Non-vendor paths
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# quota_remaining field
# ---------------------------------------------------------------------------


class TestQuotaRemainingField:
    def test_quota_remaining_parsed_from_header(self):
        """quota_remaining is parsed as int from X-Quota-Remaining header."""
        app = _make_app(response_headers={"X-Quota-Remaining": "42"})
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["quota_remaining"] == 42

    def test_quota_remaining_none_when_header_absent(self):
        """quota_remaining is None when X-Quota-Remaining header is not present."""
        app = _make_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["quota_remaining"] is None

    def test_quota_remaining_non_integer_value(self):
        """quota_remaining is None (or raw string) when header is not a valid integer."""
        app = _make_app(response_headers={"X-Quota-Remaining": "not-a-number"})
        client = TestClient(app, raise_server_exceptions=False)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/vendors/stripe/endpoint")

        _, kwargs = mock_logger.info.call_args
        # The middleware swallows the ValueError and leaves quota_remaining as None
        assert kwargs["quota_remaining"] is None


# ---------------------------------------------------------------------------
# Non-vendor paths
# ---------------------------------------------------------------------------


class TestNonVendorPaths:
    def test_health_path_has_null_vendor_slug(self):
        """vendor_slug is None for non-vendor paths like /health."""
        app = FastAPI()

        @app.get("/health")
        async def health():
            return {"ok": True}

        app.add_middleware(LoggingMiddleware)
        client = TestClient(app)

        with patch("gateway.middleware.logging.logger") as mock_logger:
            client.get("/health")

        _, kwargs = mock_logger.info.call_args
        assert kwargs["vendor_slug"] is None
        assert kwargs["endpoint"] is None
