"""End-to-end proxy route tests using respx to mock vendor HTTP calls."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.cache.redis import get_redis
from gateway.cache.response_cache import CachedResponse, _serialise
from gateway.db.models import VendorApiKey
from gateway.db.session import get_db
from gateway.main import create_app
from gateway.vendors.registry import VendorConfig, VendorRegistry

# ---------------------------------------------------------------------------
# Constants / shared data
# ---------------------------------------------------------------------------

VENDOR_SLUG = "test-vendor"
VENDOR_ID = str(uuid.uuid4())
VENDOR_BASE_URL = "https://api.test-vendor.example.com"
API_KEY_ID = str(uuid.uuid4())

FIXED_USER = UserIdentity(sub="user-test-123", email="test@example.com")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vendor_config() -> VendorConfig:
    return VendorConfig(
        id=VENDOR_ID,
        name="Test Vendor",
        slug=VENDOR_SLUG,
        base_url=VENDOR_BASE_URL,
        auth_type="api_key",
        auth_config={"header": "X-API-Key", "value": "secret"},
        cache_ttl_seconds=60,
        rate_limit_rpm=100,
        is_active=True,
    )


@pytest.fixture()
def mock_adapter():
    """VendorAdapter stub that returns the request unchanged."""
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
def vendor_api_key():
    key = MagicMock(spec=VendorApiKey)
    key.id = uuid.UUID(API_KEY_ID)
    key.vendor_id = uuid.UUID(VENDOR_ID)
    key.key_name = "default"
    key.quota_limit = 1000
    key.quota_period = "daily"
    key.is_active = True
    return key


@pytest.fixture()
def mock_redis():
    """Redis mock: cache miss, no quota issues by default."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.publish = AsyncMock(return_value=0)
    redis.pubsub = MagicMock()
    redis.aclose = AsyncMock()
    return redis


@pytest.fixture()
def mock_db(vendor_api_key):
    """AsyncSession mock that returns the vendor_api_key on execute."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = vendor_api_key
    db.execute = AsyncMock(return_value=result)
    return db


def _build_app(mock_registry, mock_redis, mock_db, user_override=FIXED_USER):
    app = create_app()

    # Override auth
    async def _mock_user():
        return user_override

    app.dependency_overrides[get_current_user] = _mock_user

    # Override redis
    async def _mock_get_redis():
        yield mock_redis

    app.dependency_overrides[get_redis] = _mock_get_redis

    # Override db
    async def _mock_get_db():
        yield mock_db

    app.dependency_overrides[get_db] = _mock_get_db

    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestProxySuccess:
    """Authenticated requests that proxy successfully to the vendor."""

    @respx.mock
    def test_get_request_proxied_successfully(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(200, json={"result": "ok"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 200
        assert resp.json() == {"result": "ok"}

    @respx.mock
    def test_post_request_proxied_successfully(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.post(f"{VENDOR_BASE_URL}/v1/submit").mock(
            return_value=httpx.Response(201, json={"id": "abc"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.post(
                    f"/vendors/{VENDOR_SLUG}/v1/submit",
                    json={"name": "test"},
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 201
        assert resp.json() == {"id": "abc"}

    @respx.mock
    def test_quota_incremented_on_success(
        self, mock_registry, mock_redis, mock_db, vendor_api_key
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(200, json={"result": "ok"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 200
        # incr should have been called (quota increment)
        mock_redis.incr.assert_awaited()


class TestCacheHit:
    """Cache hit should return cached response without calling vendor."""

    def test_cache_hit_skips_vendor_call(
        self, mock_registry, mock_redis, mock_db
    ):
        cached_body = b'{"cached": true}'
        cached_at = datetime.now(tz=UTC)
        cached = CachedResponse(
            status_code=200,
            headers={"content-type": "application/json"},
            body=cached_body,
            cached_at=cached_at,
        )
        serialised = _serialise(cached)

        # redis.get is called for:
        #   1. quota check  (quota:{vendor_id}:{key_id}:{bucket}) → None (no usage)
        #   2. cache check  (cache:{vendor_slug}:…)               → serialised response
        def _get_side_effect(key, *args, **kwargs):
            if str(key).startswith("cache:"):
                return serialised
            return None  # quota key → 0 usage

        mock_redis.get = AsyncMock(side_effect=_get_side_effect)

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            # assert_all_called=False because we expect the vendor is NOT called
            with respx.mock(assert_all_called=False) as mock_transport:
                vendor_route = mock_transport.get(f"{VENDOR_BASE_URL}/v1/data").mock(
                    return_value=httpx.Response(200, json={"should": "not_be_called"})
                )
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/data",
                        headers={"Authorization": "Bearer fake-token"},
                    )

                # Vendor route should NOT have been called
                assert not vendor_route.called

        assert resp.status_code == 200
        assert resp.headers.get("x-cache") == "HIT"
        assert resp.content == cached_body

    def test_cache_hit_returns_cached_status_code(
        self, mock_registry, mock_redis, mock_db
    ):
        cached = CachedResponse(
            status_code=206,
            headers={"content-type": "application/octet-stream"},
            body=b"partial content",
            cached_at=datetime.now(tz=UTC),
        )
        mock_redis.get = AsyncMock(return_value=_serialise(cached))

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 206
        assert resp.content == b"partial content"


class TestAuthErrors:
    """Unauthenticated requests should return 401."""

    def test_no_auth_header_returns_401(self, mock_registry, mock_redis, mock_db):
        app = create_app()

        async def _mock_get_redis():
            yield mock_redis

        async def _mock_get_db():
            yield mock_db

        app.dependency_overrides[get_redis] = _mock_get_redis
        app.dependency_overrides[get_db] = _mock_get_db
        # Note: do NOT override get_current_user — let real auth run

        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value={"keys": []})):
                with TestClient(app, raise_server_exceptions=False) as client:
                    resp = client.get(f"/vendors/{VENDOR_SLUG}/v1/data")

        assert resp.status_code == 401


class TestUnknownVendor:
    """Requests to unknown vendors should return 404."""

    def test_unknown_vendor_returns_404(self, mock_registry, mock_redis, mock_db):
        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    "/vendors/no-such-vendor/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 404
        assert "no-such-vendor" in resp.json()["detail"]


class TestVendorErrors:
    """Non-2xx from vendor should propagate; quota must NOT be incremented."""

    @respx.mock
    def test_vendor_500_propagated(self, mock_registry, mock_redis, mock_db):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(500, json={"error": "internal"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 500
        assert resp.json() == {"error": "internal"}

    @respx.mock
    def test_vendor_500_does_not_increment_quota(
        self, mock_registry, mock_redis, mock_db
    ):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(500, json={"error": "server error"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        # incr (quota increment) should NOT have been called
        mock_redis.incr.assert_not_awaited()

    @respx.mock
    def test_vendor_404_propagated(self, mock_registry, mock_redis, mock_db):
        respx.get(f"{VENDOR_BASE_URL}/v1/missing").mock(
            return_value=httpx.Response(404, json={"error": "not found"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/missing",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 404

    @respx.mock
    def test_vendor_500_does_not_cache(self, mock_registry, mock_redis, mock_db):
        respx.get(f"{VENDOR_BASE_URL}/v1/data").mock(
            return_value=httpx.Response(500, json={"error": "fail"})
        )

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        # set (cache store) should NOT have been called with a real payload
        # (redis.set may still be called for dedup lock; we only care it's not
        #  called with the response body under a cache: key)
        for call_args in mock_redis.set.call_args_list:
            key_arg = call_args[0][0] if call_args[0] else call_args[1].get("name", "")
            assert not str(key_arg).startswith("cache:"), (
                f"cache key should not be stored on 500, but got set({key_arg!r})"
            )


class TestDedupWaiter:
    """Dedup waiter path — another request holds the lock."""

    def test_dedup_waiter_receives_result_from_lock_holder(
        self, mock_registry, mock_redis, mock_db
    ):
        """Waiter should return the cached result published by the lock holder."""
        cached_response = CachedResponse(
            status_code=200,
            headers={},
            body=b'{"cached": true}',
            cached_at=datetime.now(UTC),
        )

        @asynccontextmanager
        async def _waiter_context(redis, key):
            yield False

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.routes.proxy.dedup_context", _waiter_context):
                with patch(
                    "gateway.routes.proxy.dedup_wait",
                    new=AsyncMock(return_value=cached_response),
                ):
                    with respx.mock(assert_all_called=False) as mock_transport:
                        vendor_route = mock_transport.get(
                            f"{VENDOR_BASE_URL}/v1/data"
                        ).mock(
                            return_value=httpx.Response(
                                200, json={"should": "not_be_called"}
                            )
                        )
                        with TestClient(app) as client:
                            resp = client.get(
                                f"/vendors/{VENDOR_SLUG}/v1/data",
                                headers={"Authorization": "Bearer fake-token"},
                            )

                        assert not vendor_route.called

        assert resp.status_code == 200
        mock_redis.incr.assert_not_awaited()

    def test_dedup_waiter_timeout_returns_504(
        self, mock_registry, mock_redis, mock_db
    ):
        """When dedup_wait times out (returns None), the waiter gets a 504."""

        @asynccontextmanager
        async def _waiter_context(redis, key):
            yield False

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with patch("gateway.routes.proxy.dedup_context", _waiter_context):
                with patch(
                    "gateway.routes.proxy.dedup_wait",
                    new=AsyncMock(return_value=None),
                ):
                    with TestClient(app) as client:
                        resp = client.get(
                            f"/vendors/{VENDOR_SLUG}/v1/data",
                            headers={"Authorization": "Bearer fake-token"},
                        )

        assert resp.status_code == 504


class TestQuotaExceeded:
    """When quota is exhausted the proxy must return 429."""

    def test_quota_exceeded_returns_429(
        self, mock_registry, mock_redis, mock_db, vendor_api_key
    ):
        # Simulate quota counter at the limit
        mock_redis.get = AsyncMock(return_value=b"1000")  # used == limit

        app = _build_app(mock_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 429
        body = resp.json()
        assert body["detail"]["error"] == "quota_exceeded"


class TestNoAdapter:
    """When registry returns no adapter for a known vendor, expect 503."""

    def test_no_adapter_returns_503(self, mock_registry, mock_redis, mock_db, vendor_config):
        # Registry recognises the vendor but has no adapter for it
        no_adapter_registry = MagicMock(spec=VendorRegistry)
        no_adapter_registry.get.side_effect = (
            lambda slug: vendor_config if slug == VENDOR_SLUG else None
        )
        no_adapter_registry.get_adapter.return_value = None
        no_adapter_registry.reload_if_stale = AsyncMock()

        app = _build_app(no_adapter_registry, mock_redis, mock_db)
        with patch("gateway.routes.proxy.registry", no_adapter_registry):
            with TestClient(app) as client:
                resp = client.get(
                    f"/vendors/{VENDOR_SLUG}/v1/data",
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 503
        assert "adapter" in resp.json()["detail"].lower()
