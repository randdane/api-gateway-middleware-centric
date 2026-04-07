"""Tests for the async job system (Phase 5.2).

Covers:
- POST /vendors/{slug}/{path} → 202 for is_async_job=True endpoint
- GET /jobs/{id} → returns current status
- Job transitions: pending → in_progress → completed
- Webhook callback on completion
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.cache.redis import get_redis
from gateway.db.models import Job, VendorEndpoint
from gateway.db.session import get_db
from gateway.jobs.manager import create_job, get_job, run_job
from gateway.jobs.models import JobCreatedResponse, JobStatusResponse
from gateway.main import create_app
from gateway.vendors.registry import VendorConfig, VendorRegistry

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

VENDOR_SLUG = "async-vendor"
VENDOR_ID = str(uuid.uuid4())
ENDPOINT_ID = str(uuid.uuid4())
VENDOR_BASE_URL = "https://api.async-vendor.example.com"

FIXED_USER = UserIdentity(sub="user-async-test", email="async@example.com")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def vendor_config() -> VendorConfig:
    return VendorConfig(
        id=VENDOR_ID,
        name="Async Vendor",
        slug=VENDOR_SLUG,
        base_url=VENDOR_BASE_URL,
        auth_type="api_key",
        auth_config={"header": "X-API-Key", "value": "secret"},
        cache_ttl_seconds=0,
        rate_limit_rpm=0,
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
    reg.get_by_id.side_effect = (
        lambda vid: vendor_config if vid == VENDOR_ID else None
    )
    reg.get_adapter_by_id.side_effect = (
        lambda vid: mock_adapter if vid == VENDOR_ID else None
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
    return redis


def _make_async_endpoint() -> MagicMock:
    ep = MagicMock(spec=VendorEndpoint)
    ep.id = uuid.UUID(ENDPOINT_ID)
    ep.vendor_id = uuid.UUID(VENDOR_ID)
    ep.path = "v1/async-op"
    ep.method = "POST"
    ep.is_async_job = True
    ep.timeout_seconds = 30
    return ep


def _make_sync_endpoint() -> MagicMock:
    ep = MagicMock(spec=VendorEndpoint)
    ep.id = uuid.uuid4()
    ep.vendor_id = uuid.UUID(VENDOR_ID)
    ep.path = "v1/sync-op"
    ep.method = "GET"
    ep.is_async_job = False
    ep.timeout_seconds = 30
    return ep


def _make_job(status: str = "pending") -> Job:
    job = Job(
        vendor_id=uuid.UUID(VENDOR_ID),
        endpoint_id=uuid.UUID(ENDPOINT_ID),
        requested_by="user-async-test",
        status=status,
        request_payload={},
    )
    job.id = uuid.uuid4()
    job.created_at = datetime.now(UTC)
    job.updated_at = datetime.now(UTC)
    return job


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
# Unit tests for manager functions
# ---------------------------------------------------------------------------


class TestCreateJob:
    async def test_create_job_returns_pending_job(self):
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        job_id = uuid.uuid4()

        async def _refresh(obj):
            obj.id = job_id
            obj.created_at = datetime.now(UTC)
            obj.updated_at = datetime.now(UTC)

        db.refresh = AsyncMock(side_effect=_refresh)

        vid = uuid.uuid4()
        eid = uuid.uuid4()
        job = await create_job(
            db,
            vendor_id=vid,
            endpoint_id=eid,
            requested_by="user-1",
            request_payload={"method": "POST"},
        )

        assert job.status == "pending"
        assert job.vendor_id == vid
        assert job.endpoint_id == eid
        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_get_job_returns_none_when_missing(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        found = await get_job(db, uuid.uuid4())
        assert found is None

    async def test_get_job_returns_job_when_present(self):
        job = _make_job()
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)

        found = await get_job(db, job.id)
        assert found is job


# ---------------------------------------------------------------------------
# Unit tests for run_job
# ---------------------------------------------------------------------------


class TestRunJob:
    async def test_run_job_marks_completed_on_success(self, mock_adapter):
        job = _make_job("pending")

        endpoint = _make_async_endpoint()
        vendor_cfg = VendorConfig(
            id=VENDOR_ID,
            name="AV",
            slug=VENDOR_SLUG,
            base_url=VENDOR_BASE_URL,
            auth_type="api_key",
            auth_config={"header": "X-API-Key", "value": "s"},
            cache_ttl_seconds=0,
            rate_limit_rpm=0,
            is_active=True,
        )

        db = AsyncMock()
        db.commit = AsyncMock()

        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = endpoint
        db.execute = AsyncMock(return_value=ep_result)

        with patch("gateway.jobs.manager.registry") as mock_reg:
            mock_reg.get_by_id.return_value = vendor_cfg
            mock_reg.get_adapter_by_id.return_value = mock_adapter

            with respx.mock:
                respx.post(f"{VENDOR_BASE_URL}/v1/async-op").mock(
                    return_value=httpx.Response(200, json={"done": True})
                )
                await run_job(db, job)

        assert job.status == "completed"
        assert job.response_payload is not None
        assert job.response_payload["status_code"] == 200
        assert job.error is None

    async def test_run_job_marks_failed_on_vendor_error(self, mock_adapter):
        job = _make_job("pending")
        endpoint = _make_async_endpoint()
        vendor_cfg = VendorConfig(
            id=VENDOR_ID,
            name="AV",
            slug=VENDOR_SLUG,
            base_url=VENDOR_BASE_URL,
            auth_type="api_key",
            auth_config={"header": "X-API-Key", "value": "s"},
            cache_ttl_seconds=0,
            rate_limit_rpm=0,
            is_active=True,
        )

        db = AsyncMock()
        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = endpoint
        db.execute = AsyncMock(return_value=ep_result)

        with patch("gateway.jobs.manager.registry") as mock_reg:
            mock_reg.get_by_id.return_value = vendor_cfg
            mock_reg.get_adapter_by_id.return_value = mock_adapter

            with respx.mock:
                respx.post(f"{VENDOR_BASE_URL}/v1/async-op").mock(
                    side_effect=httpx.ConnectError("connection refused")
                )
                await run_job(db, job)

        assert job.status == "failed"
        assert job.error is not None

    async def test_run_job_fires_webhook_on_completion(self, mock_adapter):
        job = _make_job("pending")
        job.request_payload = {
            "method": "POST",
            "path": "v1/async-op",
            "headers": {"x-callback-url": "https://callback.example.com/hook"},
            "forward_headers": {},
        }

        endpoint = _make_async_endpoint()
        vendor_cfg = VendorConfig(
            id=VENDOR_ID,
            name="AV",
            slug=VENDOR_SLUG,
            base_url=VENDOR_BASE_URL,
            auth_type="api_key",
            auth_config={"header": "X-API-Key", "value": "s"},
            cache_ttl_seconds=0,
            rate_limit_rpm=0,
            is_active=True,
        )

        db = AsyncMock()
        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = endpoint
        db.execute = AsyncMock(return_value=ep_result)

        with patch("gateway.jobs.manager.registry") as mock_reg:
            mock_reg.get_by_id.return_value = vendor_cfg
            mock_reg.get_adapter_by_id.return_value = mock_adapter

            with respx.mock:
                respx.post(f"{VENDOR_BASE_URL}/v1/async-op").mock(
                    return_value=httpx.Response(200, json={"done": True})
                )
                respx.post("https://callback.example.com/hook").mock(
                    return_value=httpx.Response(200)
                )
                await run_job(db, job)

        assert job.status == "completed"

    async def test_run_job_marks_failed_when_endpoint_missing(self):
        job = _make_job("pending")

        db = AsyncMock()
        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=ep_result)

        with patch("gateway.jobs.manager.registry"):
            await run_job(db, job)

        assert job.status == "failed"
        assert "not found" in (job.error or "")


# ---------------------------------------------------------------------------
# Integration-style tests via TestClient
# ---------------------------------------------------------------------------


class TestAsyncJobCreation:
    """POST to an async endpoint should return 202 with job metadata."""

    def _mock_db_for_async_endpoint(self):
        """DB that returns an async VendorEndpoint on execute, then handles job creation."""
        db = AsyncMock()

        async_ep = _make_async_endpoint()
        job_obj = _make_job("pending")

        call_count = {"n": 0}

        async def _execute(stmt):
            result = MagicMock()
            if call_count["n"] == 0:
                # First call: VendorEndpoint lookup
                result.scalar_one_or_none.return_value = async_ep
            else:
                # Subsequent calls
                result.scalar_one_or_none.return_value = None
            call_count["n"] += 1
            return result

        db.execute = AsyncMock(side_effect=_execute)
        db.add = MagicMock()
        db.commit = AsyncMock()

        async def _refresh(obj):
            obj.id = job_obj.id
            obj.created_at = job_obj.created_at
            obj.updated_at = job_obj.updated_at

        db.refresh = AsyncMock(side_effect=_refresh)
        return db, job_obj

    def test_post_async_endpoint_returns_202(self, mock_registry, mock_redis):
        mock_db, expected_job = self._mock_db_for_async_endpoint()
        app = _build_app(mock_registry, mock_redis, mock_db)

        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.post(
                    f"/vendors/{VENDOR_SLUG}/v1/async-op",
                    json={"input": "data"},
                    headers={"Authorization": "Bearer fake-token"},
                )

        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert "job_id" in body
        assert body["poll_url"].startswith("/jobs/")

    def test_post_async_endpoint_returns_job_id(self, mock_registry, mock_redis):
        mock_db, expected_job = self._mock_db_for_async_endpoint()
        app = _build_app(mock_registry, mock_redis, mock_db)

        with patch("gateway.routes.proxy.registry", mock_registry):
            with TestClient(app) as client:
                resp = client.post(
                    f"/vendors/{VENDOR_SLUG}/v1/async-op",
                    json={"input": "data"},
                    headers={"Authorization": "Bearer fake-token"},
                )

        body = resp.json()
        job_id = body["job_id"]
        # Should be a valid UUID
        uuid.UUID(job_id)
        assert body["poll_url"] == f"/jobs/{job_id}"

    def test_post_sync_endpoint_proxies_normally(
        self, mock_registry, mock_redis
    ):
        """Non-async endpoints should still go through the sync proxy path."""
        sync_ep = _make_sync_endpoint()
        db = AsyncMock()
        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = sync_ep

        api_key_result = MagicMock()
        api_key_result.scalar_one_or_none.return_value = None  # no quota

        call_count = {"n": 0}

        async def _execute(stmt):
            if call_count["n"] == 0:
                call_count["n"] += 1
                return ep_result
            return api_key_result

        db.execute = AsyncMock(side_effect=_execute)

        app = _build_app(mock_registry, mock_redis, db)

        with patch("gateway.routes.proxy.registry", mock_registry):
            with respx.mock:
                respx.get(f"{VENDOR_BASE_URL}/v1/sync-op").mock(
                    return_value=httpx.Response(200, json={"sync": True})
                )
                with TestClient(app) as client:
                    resp = client.get(
                        f"/vendors/{VENDOR_SLUG}/v1/sync-op",
                        headers={"Authorization": "Bearer fake-token"},
                    )

        assert resp.status_code == 200
        assert resp.json() == {"sync": True}


class TestJobPolling:
    """GET /jobs/{id} should return job status."""

    def _db_with_job(self, job: Job):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = job
        db.execute = AsyncMock(return_value=result)
        return db

    def test_get_pending_job_returns_pending(self, mock_registry, mock_redis):
        job = _make_job("pending")
        mock_db = self._db_with_job(job)
        app = _build_app(mock_registry, mock_redis, mock_db)

        with TestClient(app) as client:
            resp = client.get(
                f"/jobs/{job.id}",
                headers={"Authorization": "Bearer fake-token"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "pending"
        assert body["job_id"] == str(job.id)
        assert body["result"] is None
        assert body["error"] is None

    def test_get_completed_job_returns_result(self, mock_registry, mock_redis):
        job = _make_job("completed")
        job.response_payload = {"status_code": 200, "body": {"done": True}}

        mock_db = self._db_with_job(job)
        app = _build_app(mock_registry, mock_redis, mock_db)

        with TestClient(app) as client:
            resp = client.get(
                f"/jobs/{job.id}",
                headers={"Authorization": "Bearer fake-token"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "completed"
        assert body["result"] == {"status_code": 200, "body": {"done": True}}

    def test_get_failed_job_returns_error(self, mock_registry, mock_redis):
        job = _make_job("failed")
        job.error = "Connection refused"

        mock_db = self._db_with_job(job)
        app = _build_app(mock_registry, mock_redis, mock_db)

        with TestClient(app) as client:
            resp = client.get(
                f"/jobs/{job.id}",
                headers={"Authorization": "Bearer fake-token"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "failed"
        assert body["error"] == "Connection refused"

    def test_get_nonexistent_job_returns_404(self, mock_registry, mock_redis):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=result)

        app = _build_app(mock_registry, mock_redis, db)

        missing_id = uuid.uuid4()
        with TestClient(app) as client:
            resp = client.get(
                f"/jobs/{missing_id}",
                headers={"Authorization": "Bearer fake-token"},
            )

        assert resp.status_code == 404

    def test_get_in_progress_job(self, mock_registry, mock_redis):
        job = _make_job("in_progress")
        mock_db = self._db_with_job(job)
        app = _build_app(mock_registry, mock_redis, mock_db)

        with TestClient(app) as client:
            resp = client.get(
                f"/jobs/{job.id}",
                headers={"Authorization": "Bearer fake-token"},
            )

        assert resp.status_code == 200
        assert resp.json()["status"] == "in_progress"


class TestJobTransitions:
    """Verify the full pending → in_progress → completed lifecycle."""

    async def test_full_job_lifecycle(self, mock_adapter):
        """run_job should transition pending → in_progress → completed."""
        job = _make_job("pending")
        endpoint = _make_async_endpoint()
        vendor_cfg = VendorConfig(
            id=VENDOR_ID,
            name="AV",
            slug=VENDOR_SLUG,
            base_url=VENDOR_BASE_URL,
            auth_type="api_key",
            auth_config={"header": "X-API-Key", "value": "s"},
            cache_ttl_seconds=0,
            rate_limit_rpm=0,
            is_active=True,
        )

        status_sequence: list[str] = []

        db = AsyncMock()

        ep_result = MagicMock()
        ep_result.scalar_one_or_none.return_value = endpoint
        db.execute = AsyncMock(return_value=ep_result)

        original_commit = db.commit

        async def _commit_and_track():
            status_sequence.append(job.status)

        db.commit = AsyncMock(side_effect=_commit_and_track)

        with patch("gateway.jobs.manager.registry") as mock_reg:
            mock_reg.get_by_id.return_value = vendor_cfg
            mock_reg.get_adapter_by_id.return_value = mock_adapter

            with respx.mock:
                respx.post(f"{VENDOR_BASE_URL}/v1/async-op").mock(
                    return_value=httpx.Response(200, json={"result": "ok"})
                )
                await run_job(db, job)

        # The commits should have captured in_progress then completed
        assert "in_progress" in status_sequence
        assert "completed" in status_sequence
        assert status_sequence.index("in_progress") < status_sequence.index("completed")
        assert job.status == "completed"


class TestPydanticModels:
    """Smoke-test the Pydantic models."""

    def test_job_created_response_serializes(self):
        job_id = uuid.uuid4()
        r = JobCreatedResponse(
            job_id=job_id,
            status="pending",
            poll_url=f"/jobs/{job_id}",
        )
        d = r.model_dump()
        assert d["job_id"] == job_id
        assert d["status"] == "pending"

    def test_job_status_response_with_result(self):
        job_id = uuid.uuid4()
        now = datetime.now(UTC)
        r = JobStatusResponse(
            job_id=job_id,
            status="completed",
            result={"status_code": 200, "body": {}},
            error=None,
            created_at=now,
            updated_at=now,
        )
        assert r.status == "completed"
        assert r.result == {"status_code": 200, "body": {}}

    def test_job_status_response_optional_fields_default_none(self):
        job_id = uuid.uuid4()
        now = datetime.now(UTC)
        r = JobStatusResponse(
            job_id=job_id,
            status="pending",
            created_at=now,
            updated_at=now,
        )
        assert r.result is None
        assert r.error is None
