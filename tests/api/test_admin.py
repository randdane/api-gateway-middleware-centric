"""Tests for the Admin API (Phase 6.1).

Covers all 12 endpoints:
- Auth: 401 on no token, 403 on non-admin token
- Vendor CRUD: list, create, get, update, deactivate
- Quota: view config + current usage, adjust limits
- Usage: stub response
- Cache: per-vendor flush, global flush
- Config reload
- Health check
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.exc import IntegrityError

import pytest
from fastapi.testclient import TestClient

from gateway.admin.routes import router as admin_router
from gateway.auth.dependencies import UserIdentity, get_current_user, require_admin
from gateway.cache.redis import get_redis
from gateway.db.models import Vendor, VendorApiKey
from gateway.db.session import get_db
from gateway.main import create_app

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VENDOR_ID = uuid.uuid4()
VENDOR_ID_STR = str(VENDOR_ID)
KEY_ID = uuid.uuid4()
KEY_ID_STR = str(KEY_ID)

ADMIN_USER = UserIdentity(sub="admin-001", email="admin@example.com", roles=["admin"])
PLAIN_USER = UserIdentity(sub="user-001", email="user@example.com", roles=["user"])

NOW = datetime(2026, 4, 6, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers to build mock objects
# ---------------------------------------------------------------------------


def _make_vendor(
    *,
    id: uuid.UUID = VENDOR_ID,
    name: str = "Test Vendor",
    slug: str = "test-vendor",
    base_url: str = "https://api.test.example.com",
    auth_type: str = "api_key",
    auth_config: dict | None = None,
    cache_ttl_seconds: int = 60,
    rate_limit_rpm: int = 100,
    is_active: bool = True,
) -> MagicMock:
    vendor = MagicMock(spec=Vendor)
    vendor.id = id
    vendor.name = name
    vendor.slug = slug
    vendor.base_url = base_url
    vendor.auth_type = auth_type
    vendor.auth_config = auth_config or {}
    vendor.cache_ttl_seconds = cache_ttl_seconds
    vendor.rate_limit_rpm = rate_limit_rpm
    vendor.is_active = is_active
    vendor.created_at = NOW
    vendor.updated_at = NOW
    return vendor


def _make_api_key(
    *,
    id: uuid.UUID = KEY_ID,
    vendor_id: uuid.UUID = VENDOR_ID,
    key_name: str = "default",
    quota_limit: int | None = 1000,
    quota_period: str | None = "daily",
    is_active: bool = True,
) -> MagicMock:
    key = MagicMock(spec=VendorApiKey)
    key.id = id
    key.vendor_id = vendor_id
    key.key_name = key_name
    key.quota_limit = quota_limit
    key.quota_period = quota_period
    key.is_active = is_active
    return key


# ---------------------------------------------------------------------------
# App builder
# ---------------------------------------------------------------------------


def _build_app(
    mock_db: AsyncMock,
    mock_redis: AsyncMock,
    *,
    user: UserIdentity = ADMIN_USER,
) -> TestClient:
    app = create_app()

    async def _override_user():
        return user

    async def _override_redis():
        yield mock_redis

    async def _override_db():
        yield mock_db

    app.dependency_overrides[get_current_user] = _override_user
    app.dependency_overrides[get_redis] = _override_redis
    app.dependency_overrides[get_db] = _override_db

    return TestClient(app, raise_server_exceptions=True)


def _make_mock_db(vendor: MagicMock | None = None, vendors: list | None = None) -> AsyncMock:
    """Build a minimal AsyncSession mock."""
    db = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    single_result = MagicMock()
    single_result.scalar_one_or_none.return_value = vendor
    single_result.scalars.return_value.all.return_value = vendors or (
        [vendor] if vendor else []
    )

    db.execute = AsyncMock(return_value=single_result)
    db.add = MagicMock()
    return db


def _make_mock_redis() -> AsyncMock:
    redis = AsyncMock()
    redis.ping = AsyncMock(return_value=True)
    redis.get = AsyncMock(return_value=None)
    redis.scan = AsyncMock(return_value=(0, []))
    redis.delete = AsyncMock(return_value=0)
    redis.aclose = AsyncMock()
    return redis


# ---------------------------------------------------------------------------
# Auth tests (shared across all endpoints)
# ---------------------------------------------------------------------------


class TestAdminAuth:
    """All admin endpoints must require authentication and admin role."""

    def test_list_vendors_no_token_returns_401(self):
        app = create_app()
        # No dependency overrides — real auth
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/vendors")
        assert resp.status_code == 401

    def test_list_vendors_non_admin_returns_403(self):
        mock_db = _make_mock_db()
        mock_redis = _make_mock_redis()

        app = create_app()

        async def _non_admin():
            return PLAIN_USER

        async def _override_db():
            yield mock_db

        async def _override_redis():
            yield mock_redis

        app.dependency_overrides[get_current_user] = _non_admin
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_redis] = _override_redis

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/vendors")
        assert resp.status_code == 403

    def test_health_no_token_returns_401(self):
        app = create_app()
        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/health")
        assert resp.status_code == 401

    def test_health_non_admin_returns_403(self):
        app = create_app()

        async def _non_admin():
            return PLAIN_USER

        app.dependency_overrides[get_current_user] = _non_admin

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.get("/admin/health")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Vendor list / create
# ---------------------------------------------------------------------------


class TestVendorList:
    def test_list_vendors_empty(self):
        mock_db = _make_mock_db(vendors=[])
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get("/admin/vendors")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_vendors_returns_all(self):
        v1 = _make_vendor(id=uuid.uuid4(), slug="vendor-a")
        v2 = _make_vendor(id=uuid.uuid4(), slug="vendor-b", is_active=False)
        mock_db = _make_mock_db(vendors=[v1, v2])
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get("/admin/vendors")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        slugs = {d["slug"] for d in data}
        assert slugs == {"vendor-a", "vendor-b"}


class TestVendorCreate:
    def test_create_vendor_success(self):
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock()

        async def _refresh(obj):
            # Simulate DB-assigned defaults on the real Vendor object
            obj.id = VENDOR_ID
            obj.is_active = True
            obj.created_at = NOW
            obj.updated_at = NOW

        mock_db.refresh = _refresh
        mock_db.execute = AsyncMock()  # not used for create

        mock_redis = _make_mock_redis()

        app = create_app()

        async def _override_user():
            return ADMIN_USER

        async def _override_redis():
            yield mock_redis

        async def _override_db():
            yield mock_db

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_redis] = _override_redis
        app.dependency_overrides[get_db] = _override_db

        payload = {
            "name": "New Vendor",
            "slug": "new-vendor",
            "base_url": "https://new.example.com",
            "auth_type": "api_key",
            "auth_config": {"header": "X-Key", "value": "secret"},
            "cache_ttl_seconds": 30,
            "rate_limit_rpm": 50,
        }

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/admin/vendors", json=payload)

        assert resp.status_code == 201
        data = resp.json()
        assert data["slug"] == "new-vendor"
        assert data["name"] == "New Vendor"
        # db.add was called once
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called_once()


    def test_create_vendor_duplicate_slug_returns_409(self):
        mock_db = AsyncMock()
        mock_db.add = MagicMock()
        mock_db.commit = AsyncMock(side_effect=IntegrityError("duplicate", {}, None))
        mock_db.rollback = AsyncMock()
        mock_db.execute = AsyncMock()

        mock_redis = _make_mock_redis()

        app = create_app()

        async def _override_user():
            return ADMIN_USER

        async def _override_redis():
            yield mock_redis

        async def _override_db():
            yield mock_db

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_redis] = _override_redis
        app.dependency_overrides[get_db] = _override_db

        payload = {
            "name": "Duplicate Vendor",
            "slug": "existing-slug",
            "base_url": "https://dup.example.com",
            "auth_type": "api_key",
        }

        with TestClient(app, raise_server_exceptions=True) as client:
            resp = client.post("/admin/vendors", json=payload)

        assert resp.status_code == 409
        data = resp.json()
        assert "existing-slug" in data["detail"]
        mock_db.rollback.assert_called_once()


# ---------------------------------------------------------------------------
# Vendor get / update / deactivate
# ---------------------------------------------------------------------------


class TestVendorGet:
    def test_get_vendor_success(self):
        vendor = _make_vendor()
        mock_db = _make_mock_db(vendor=vendor)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get(f"/admin/vendors/{VENDOR_ID_STR}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == VENDOR_ID_STR
        assert data["slug"] == "test-vendor"

    def test_get_vendor_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get(f"/admin/vendors/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_get_vendor_invalid_uuid(self):
        mock_db = _make_mock_db()
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get("/admin/vendors/not-a-uuid")
        assert resp.status_code == 422


class TestVendorUpdate:
    def test_update_vendor_success(self):
        vendor = _make_vendor()
        mock_db = _make_mock_db(vendor=vendor)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)

        resp = client.put(
            f"/admin/vendors/{VENDOR_ID_STR}",
            json={"name": "Updated Name", "rate_limit_rpm": 200},
        )
        assert resp.status_code == 200
        mock_db.commit.assert_called_once()

    def test_update_vendor_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.put(
            f"/admin/vendors/{uuid.uuid4()}",
            json={"name": "X"},
        )
        assert resp.status_code == 404


class TestVendorDeactivate:
    def test_deactivate_vendor_success(self):
        vendor = _make_vendor(is_active=True)
        mock_db = _make_mock_db(vendor=vendor)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)

        resp = client.delete(f"/admin/vendors/{VENDOR_ID_STR}")
        assert resp.status_code == 200
        # is_active should have been set to False
        assert vendor.is_active is False
        mock_db.commit.assert_called_once()

    def test_deactivate_vendor_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.delete(f"/admin/vendors/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


class TestVendorQuota:
    def test_get_quota_returns_key_info(self):
        vendor = _make_vendor()
        api_key = _make_api_key(quota_limit=500, quota_period="monthly")

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        # First execute → vendor lookup; second → keys lookup
        vendor_result = MagicMock()
        vendor_result.scalar_one_or_none.return_value = vendor

        keys_result = MagicMock()
        keys_result.scalars.return_value.all.return_value = [api_key]

        mock_db.execute = AsyncMock(side_effect=[vendor_result, keys_result])

        mock_redis = _make_mock_redis()
        mock_redis.get = AsyncMock(return_value="42")  # current usage = 42

        client = _build_app(mock_db, mock_redis)
        resp = client.get(f"/admin/vendors/{VENDOR_ID_STR}/quota")

        assert resp.status_code == 200
        data = resp.json()
        assert data["vendor_id"] == VENDOR_ID_STR
        assert len(data["keys"]) == 1
        key_data = data["keys"][0]
        assert key_data["quota_limit"] == 500
        assert key_data["quota_period"] == "monthly"
        assert key_data["current_usage"] == 42

    def test_get_quota_vendor_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get(f"/admin/vendors/{uuid.uuid4()}/quota")
        assert resp.status_code == 404

    def test_update_quota_success(self):
        vendor = _make_vendor()
        api_key = _make_api_key(quota_limit=1000, quota_period="daily")

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        vendor_result = MagicMock()
        vendor_result.scalar_one_or_none.return_value = vendor

        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = api_key

        all_keys_result = MagicMock()
        all_keys_result.scalars.return_value.all.return_value = [api_key]

        mock_db.execute = AsyncMock(
            side_effect=[vendor_result, key_result, all_keys_result]
        )

        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)

        resp = client.put(
            f"/admin/vendors/{VENDOR_ID_STR}/quota",
            json={"key_id": KEY_ID_STR, "quota_limit": 2000},
        )
        assert resp.status_code == 200
        assert api_key.quota_limit == 2000
        mock_db.commit.assert_called_once()

    def test_update_quota_key_not_found(self):
        vendor = _make_vendor()

        mock_db = AsyncMock()
        mock_db.commit = AsyncMock()
        mock_db.refresh = AsyncMock()

        vendor_result = MagicMock()
        vendor_result.scalar_one_or_none.return_value = vendor

        key_result = MagicMock()
        key_result.scalar_one_or_none.return_value = None

        mock_db.execute = AsyncMock(side_effect=[vendor_result, key_result])

        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)

        resp = client.put(
            f"/admin/vendors/{VENDOR_ID_STR}/quota",
            json={"key_id": str(uuid.uuid4()), "quota_limit": 5000},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Usage stub
# ---------------------------------------------------------------------------


class TestVendorUsage:
    def test_usage_returns_stub(self):
        vendor = _make_vendor()
        mock_db = _make_mock_db(vendor=vendor)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)

        resp = client.get(f"/admin/vendors/{VENDOR_ID_STR}/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert "message" in data
        assert "not yet implemented" in data["message"].lower()

    def test_usage_vendor_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.get(f"/admin/vendors/{uuid.uuid4()}/usage")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Cache flush
# ---------------------------------------------------------------------------


class TestCacheFlush:
    def test_flush_vendor_cache(self):
        vendor = _make_vendor()
        mock_db = _make_mock_db(vendor=vendor)
        mock_redis = _make_mock_redis()
        mock_redis.scan = AsyncMock(return_value=(0, ["cache:test-vendor:key1"]))
        mock_redis.delete = AsyncMock(return_value=1)

        client = _build_app(mock_db, mock_redis)
        resp = client.delete(f"/admin/vendors/{VENDOR_ID_STR}/cache")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 1
        assert data["vendor_slug"] == "test-vendor"

    def test_flush_vendor_cache_not_found(self):
        mock_db = _make_mock_db(vendor=None)
        mock_redis = _make_mock_redis()
        client = _build_app(mock_db, mock_redis)
        resp = client.delete(f"/admin/vendors/{uuid.uuid4()}/cache")
        assert resp.status_code == 404

    def test_flush_all_caches(self):
        mock_db = _make_mock_db()
        mock_redis = _make_mock_redis()
        mock_redis.scan = AsyncMock(return_value=(0, ["cache:v1:k1", "cache:v2:k2"]))
        mock_redis.delete = AsyncMock(return_value=2)

        client = _build_app(mock_db, mock_redis)
        resp = client.delete("/admin/cache")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 2
        assert data["vendor_slug"] is None

    def test_flush_all_no_keys(self):
        mock_db = _make_mock_db()
        mock_redis = _make_mock_redis()
        # scan returns empty set → no keys to delete
        mock_redis.scan = AsyncMock(return_value=(0, []))

        client = _build_app(mock_db, mock_redis)
        resp = client.delete("/admin/cache")

        assert resp.status_code == 200
        assert resp.json()["deleted"] == 0


# ---------------------------------------------------------------------------
# Config reload
# ---------------------------------------------------------------------------


class TestConfigReload:
    def test_reload_config_success(self):
        mock_db = _make_mock_db()
        mock_redis = _make_mock_redis()

        mock_registry = MagicMock()
        mock_registry.load = AsyncMock()
        mock_registry.all_vendors.return_value = [_make_vendor(), _make_vendor(id=uuid.uuid4(), slug="v2")]

        client = _build_app(mock_db, mock_redis)

        with patch("gateway.admin.routes.registry", mock_registry):
            resp = client.post("/admin/config/reload")

        assert resp.status_code == 200
        data = resp.json()
        assert data["reloaded"] is True
        assert data["vendor_count"] == 2
        mock_registry.load.assert_called_once()
        mock_registry.invalidate.assert_not_called()

    def test_reload_config_requires_admin(self):
        app = create_app()

        async def _non_admin():
            return PLAIN_USER

        app.dependency_overrides[get_current_user] = _non_admin

        with TestClient(app, raise_server_exceptions=False) as client:
            resp = client.post("/admin/config/reload")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


class TestAdminHealth:
    def test_health_all_ok(self):
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()  # SELECT 1 succeeds

        mock_redis = _make_mock_redis()

        mock_registry = MagicMock()
        mock_registry.all_vendors.return_value = [_make_vendor()]

        app = create_app()

        async def _override_user():
            return ADMIN_USER

        async def _override_db():
            yield mock_db

        async def _override_redis():
            yield mock_redis

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_redis] = _override_redis

        with patch("gateway.admin.routes.registry", mock_registry):
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get("/admin/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["services"]["redis"]["status"] == "ok"
        assert data["services"]["postgres"]["status"] == "ok"
        assert data["vendor_count"] == 1

    def test_health_redis_down(self):
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock()

        mock_redis = _make_mock_redis()
        mock_redis.ping = AsyncMock(side_effect=ConnectionError("Redis unreachable"))

        mock_registry = MagicMock()
        mock_registry.all_vendors.return_value = []

        app = create_app()

        async def _override_user():
            return ADMIN_USER

        async def _override_db():
            yield mock_db

        async def _override_redis():
            yield mock_redis

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_redis] = _override_redis

        with patch("gateway.admin.routes.registry", mock_registry):
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get("/admin/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["services"]["redis"]["status"] == "error"
        assert data["services"]["postgres"]["status"] == "ok"

    def test_health_postgres_down(self):
        mock_db = AsyncMock()
        mock_db.execute = AsyncMock(side_effect=Exception("Connection refused"))

        mock_redis = _make_mock_redis()

        mock_registry = MagicMock()
        mock_registry.all_vendors.return_value = []

        app = create_app()

        async def _override_user():
            return ADMIN_USER

        async def _override_db():
            yield mock_db

        async def _override_redis():
            yield mock_redis

        app.dependency_overrides[get_current_user] = _override_user
        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_redis] = _override_redis

        with patch("gateway.admin.routes.registry", mock_registry):
            with TestClient(app, raise_server_exceptions=True) as client:
                resp = client.get("/admin/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["services"]["postgres"]["status"] == "error"
