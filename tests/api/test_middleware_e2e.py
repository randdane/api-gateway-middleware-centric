"""End-to-end middleware stack tests.

Verifies that RateLimitMiddleware, LoggingMiddleware, and TracingMiddleware
are wired correctly and interact as expected in the full request pipeline.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.cache.redis import get_client, get_redis
from gateway.db.session import get_db
from gateway.main import create_app
from gateway.vendors.registry import VendorConfig, VendorRegistry

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

VENDOR_SLUG = "mw-vendor"
VENDOR_ID = str(uuid.uuid4())
VENDOR_BASE_URL = "https://api.mw-vendor.example.com"

FIXED_USER = UserIdentity(sub="mw-user-1", email="mw@example.com")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vendor_config() -> VendorConfig:
    return VendorConfig(
        id=VENDOR_ID,
        name="MW Vendor",
        slug=VENDOR_SLUG,
        base_url=VENDOR_BASE_URL,
        auth_type="api_key",
        auth_config={"header": "X-Api-Key", "value": "secret"},
        cache_ttl_seconds=0,
        rate_limit_rpm=100,
        is_active=True,
    )


@pytest.fixture()
def mock_adapter():
    adapter = MagicMock()
    adapter.prepare_request = AsyncMock(side_effect=lambda req: req)
    return adapter


@pytest.fixture()
def mock_registry(vendor_config, mock_adapter):
    reg = MagicMock(spec=VendorRegistry)
    reg.get.side_effect = lambda slug: vendor_config if slug == VENDOR_SLUG else None
    reg.get_adapter.side_effect = (
        lambda slug: mock_adapter if slug == VENDOR_SLUG else None
    )
    reg.reload_if_stale = AsyncMock()
    return reg


@pytest.fixture()
def mock_redis():
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.publish = AsyncMock(return_value=0)
    redis.pubsub = MagicMock()
    redis.aclose = AsyncMock()
    # Token bucket: eval returns 1 (allowed) by default
    redis.eval = AsyncMock(return_value=1)
    return redis


@pytest.fixture()
def mock_db():
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # no api key → no quota
    db.execute = AsyncMock(return_value=result)
    return db


def _build_app(mock_registry, mock_redis, mock_db, user_override=FIXED_USER):
    app = create_app()

    async def _mock_user():
        return user_override

    async def _mock_get_redis():
        yield mock_redis

    async def _mock_get_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = _mock_user
    app.dependency_overrides[get_redis] = _mock_get_redis
    app.dependency_overrides[get_db] = _mock_get_db

    return app


# ---------------------------------------------------------------------------
# Tests: middleware ordering and presence
# ---------------------------------------------------------------------------


class TestMiddlewareOrdering:
    """Verify the three middleware layers are installed in the correct order."""

    def test_middleware_stack_includes_tracing_logging_ratelimit(self):
        """All three middleware types must be registered on the app."""
        from gateway.middleware.logging import LoggingMiddleware
        from gateway.middleware.rate_limit import RateLimitMiddleware
        from gateway.middleware.tracing import TracingMiddleware

        app = create_app()
        # Starlette stores middleware on app.user_middleware in reverse order
        # (last-added = first in the list = outermost at runtime).
        middleware_classes = [m.cls for m in app.user_middleware]

        assert TracingMiddleware in middleware_classes, "TracingMiddleware not found"
        assert LoggingMiddleware in middleware_classes, "LoggingMiddleware not found"
        assert RateLimitMiddleware in middleware_classes, "RateLimitMiddleware not found"

    def test_tracing_is_outermost(self):
        """TracingMiddleware must be outermost (first in user_middleware list)."""
        from gateway.middleware.tracing import TracingMiddleware

        app = create_app()
        # user_middleware[0] is added last in code → outermost at runtime
        outermost = app.user_middleware[0].cls
        assert outermost is TracingMiddleware

    def test_rate_limit_is_innermost(self):
        """RateLimitMiddleware must be innermost (last in user_middleware list)."""
        from gateway.middleware.rate_limit import RateLimitMiddleware

        app = create_app()
        innermost = app.user_middleware[-1].cls
        assert innermost is RateLimitMiddleware


# ---------------------------------------------------------------------------
# Tests: rate limiting at middleware level (vendor scope)
# ---------------------------------------------------------------------------


class TestVendorRateLimitMiddleware:
    """RateLimitMiddleware enforces per-vendor RPM limits before routing."""

    @respx.mock
    def test_request_allowed_when_token_bucket_permits(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        mock_redis.eval = AsyncMock(return_value=1)  # bucket allows

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            # Inject the mock redis into RateLimitMiddleware via get_client patch
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 200

    def test_request_blocked_when_vendor_rate_limit_exceeded(
        self, mock_registry, mock_redis, mock_db
    ):
        mock_redis.eval = AsyncMock(return_value=0)  # bucket denies

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 429
        body = resp.json()
        assert body["error"] == "rate_limit_exceeded"
        assert body["scope"] == "vendor"
        assert "retry_after" in body
        assert "Retry-After" in resp.headers

    def test_rate_limit_evaluates_lua_script_for_vendor_path(
        self, mock_registry, mock_redis, mock_db
    ):
        """Middleware must run the token-bucket Lua script for vendor paths."""
        mock_redis.eval = AsyncMock(return_value=0)

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 429
        # eval must have been called — the Lua script ran
        mock_redis.eval.assert_awaited()

    def test_non_vendor_paths_bypass_rate_limit(self, mock_redis, mock_db):
        """Health endpoint is not vendor-scoped and must not go through rate limit."""
        mock_redis.eval = AsyncMock(return_value=0)  # would block if checked

        app = create_app()

        async def _mock_get_redis():
            yield mock_redis

        async def _mock_get_db():
            yield mock_db

        app.dependency_overrides[get_redis] = _mock_get_redis
        app.dependency_overrides[get_db] = _mock_get_db

        with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
            with TestClient(app) as client:
                resp = client.get("/health")

        # /health must succeed even when eval always returns 0
        assert resp.status_code == 200

    def test_rate_limit_fail_open_on_redis_error(
        self, mock_registry, mock_redis, mock_db
    ):
        """When Redis errors, the middleware must pass the request through."""
        mock_redis.eval = AsyncMock(side_effect=Exception("Redis unavailable"))

        with respx.mock:
            respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
                return_value=httpx.Response(200, json={"ok": True})
            )
            app = _build_app(mock_registry, mock_redis, mock_db)
            with patch("gateway.routes.proxy.registry", mock_registry):
                with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                    with TestClient(app) as client:
                        resp = client.get(
                            f"/vendors/{VENDOR_SLUG}/v1/data",
                            headers={"Authorization": "Bearer fake-token"},
                        )

        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Tests: logging middleware adds headers to response
# ---------------------------------------------------------------------------


class TestLoggingMiddleware:
    """LoggingMiddleware must not break the response pipeline."""

    @respx.mock
    def test_logging_middleware_does_not_alter_status(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(200, json={"data": 1})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 200

    @respx.mock
    def test_logging_middleware_present_on_error_path(
        self, mock_registry, mock_redis, mock_db
    ):
        """Logging middleware must not swallow 4xx/5xx responses."""
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(503, json={"error": "down"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# Tests: tracing middleware sets trace state
# ---------------------------------------------------------------------------


class TestTracingMiddleware:
    """TracingMiddleware must inject trace context into request.state."""

    @respx.mock
    def test_tracing_does_not_break_success_path(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(200, json={"traced": True})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 200
        assert resp.json() == {"traced": True}


# ---------------------------------------------------------------------------
# Tests: full pipeline integration
# ---------------------------------------------------------------------------


class TestFullPipeline:
    """Walk the complete request pipeline: middleware → auth → quota → cache → vendor."""

    @respx.mock
    def test_full_pipeline_success(self, mock_registry, mock_redis, mock_db):
        """Happy path: all middleware pass, auth ok, cache miss, vendor 200."""
        respx.get(f"{VENDOR_BASE_URL}/v1/resource").mock(
            return_value=httpx.Response(200, json={"id": "abc", "value": 42})
        )
        mock_redis.eval = AsyncMock(return_value=1)

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/resource",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 200
        assert resp.json() == {"id": "abc", "value": 42}

    def test_full_pipeline_vendor_rate_limited_before_auth(
        self, mock_registry, mock_redis, mock_db
    ):
        """When vendor is rate-limited at middleware, 429 is returned before auth runs."""
        mock_redis.eval = AsyncMock(return_value=0)

        # We do NOT override get_current_user — if auth ran it would verify the
        # (absent/invalid) JWT and might 401.  But rate limit fires first → 429.
        app = create_app()

        async def _mock_get_redis():
            yield mock_redis

        async def _mock_get_db():
            yield mock_db

        app.dependency_overrides[get_redis] = _mock_get_redis
        app.dependency_overrides[get_db] = _mock_get_db

        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        # No valid auth token — would normally 401
                        headers={"Authorization": "Bearer not-a-real-token"},
                    )

        # Rate limit fires at middleware level (before JWT verification)
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limit_exceeded"

    @respx.mock
    def test_full_pipeline_cache_hit_skips_vendor(
        self, mock_registry, mock_redis, mock_db
    ):
        """Cache hit in the full pipeline must skip vendor call."""
        from datetime import UTC, datetime

        from gateway.cache.response_cache import CachedResponse, _serialise

        cached = CachedResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=b'{"cached": true}',
            cached_at=datetime.now(tz=UTC),
        )

        def _get_side_effect(key, *args, **kwargs):
            if str(key).startswith("cache:"):
                return _serialise(cached)
            return None

        mock_redis.get = AsyncMock(side_effect=_get_side_effect)
        mock_redis.eval = AsyncMock(return_value=1)

        with respx.mock(assert_all_called=False) as transport:
            vendor_route = transport.get(f"{VENDOR_BASE_URL}/v1/resource").mock(
                return_value=httpx.Response(200, json={"should": "not_be_called"})
            )
            app = _build_app(mock_registry, mock_redis, mock_db)
            with patch("gateway.routes.proxy.registry", mock_registry):
                with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                    with TestClient(app) as client:
                        resp = client.get(
                            f"/vendors/{VENDOR_SLUG}/v1/resource",
                            headers={"Authorization": "Bearer fake-token"},
                        )

            assert not vendor_route.called

        assert resp.status_code == 200
        assert resp.headers.get("x-cache") == "HIT"

    def test_full_pipeline_unknown_vendor_404(
        self, mock_registry, mock_redis, mock_db
    ):
        """Unknown vendor slug must return 404 after passing middleware."""
        mock_redis.eval = AsyncMock(return_value=1)

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.middleware.rate_limit.get_client", return_value=mock_redis):
                with TestClient(app) as client:
                    resp = client.get(
                        "/vendors/no-such-vendor/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 404
