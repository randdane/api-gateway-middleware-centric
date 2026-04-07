"""Unit tests for quota tracking and middleware.

All Redis and DB calls are mocked — no running infrastructure required.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.quota.models import QuotaExceededResponse, QuotaStatus
from gateway.quota.tracker import (
    DAILY_TTL,
    MONTHLY_TTL,
    check_quota,
    get_quota_usage,
    increment_quota,
    period_bucket,
    period_ttl,
    quota_key,
    sync_quota_to_db,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dt(year=2026, month=4, day=7, hour=12) -> datetime:
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_redis(get_return=None) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=get_return)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    return redis


# ===========================================================================
# period_bucket
# ===========================================================================


class TestPeriodBucket:
    def test_daily_returns_yyyy_mm_dd(self):
        dt = _dt(2026, 4, 7)
        assert period_bucket("daily", dt) == "2026-04-07"

    def test_monthly_returns_yyyy_mm(self):
        dt = _dt(2026, 4, 7)
        assert period_bucket("monthly", dt) == "2026-04"

    def test_daily_january(self):
        dt = _dt(2026, 1, 1)
        assert period_bucket("daily", dt) == "2026-01-01"

    def test_monthly_december(self):
        dt = _dt(2026, 12, 31)
        assert period_bucket("monthly", dt) == "2026-12"

    def test_unknown_period_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown quota period"):
            period_bucket("hourly", _dt())

    def test_daily_end_of_month(self):
        dt = _dt(2026, 3, 31)
        assert period_bucket("daily", dt) == "2026-03-31"


# ===========================================================================
# period_ttl
# ===========================================================================


class TestPeriodTtl:
    def test_daily_ttl(self):
        assert period_ttl("daily") == DAILY_TTL

    def test_monthly_ttl(self):
        assert period_ttl("monthly") == MONTHLY_TTL

    def test_daily_ttl_value(self):
        assert period_ttl("daily") == 86_400

    def test_monthly_ttl_value(self):
        assert period_ttl("monthly") == 2_678_400

    def test_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="Unknown quota period"):
            period_ttl("weekly")


# ===========================================================================
# quota_key
# ===========================================================================


class TestQuotaKey:
    def test_format(self):
        key = quota_key("vendor-1", "key-2", "2026-04-07")
        assert key == "quota:vendor-1:key-2:2026-04-07"

    def test_monthly_bucket(self):
        key = quota_key("v", "k", "2026-04")
        assert key == "quota:v:k:2026-04"

    def test_key_starts_with_quota_prefix(self):
        key = quota_key("a", "b", "2026-01-01")
        assert key.startswith("quota:")

    def test_key_contains_all_parts(self):
        vendor_id = "vendor-abc"
        key_id = "key-xyz"
        bucket = "2026-04-07"
        key = quota_key(vendor_id, key_id, bucket)
        assert vendor_id in key
        assert key_id in key
        assert bucket in key


# ===========================================================================
# get_quota_usage
# ===========================================================================


class TestGetQuotaUsage:
    async def test_returns_zero_when_key_missing(self):
        redis = _make_redis(get_return=None)
        result = await get_quota_usage(redis, "v", "k", "daily")
        assert result == 0

    async def test_returns_int_when_key_exists(self):
        redis = _make_redis(get_return="42")
        result = await get_quota_usage(redis, "v", "k", "daily")
        assert result == 42

    async def test_calls_redis_get(self):
        redis = _make_redis(get_return="5")
        await get_quota_usage(redis, "vendor-1", "key-1", "daily")
        redis.get.assert_called_once()

    async def test_redis_key_uses_current_date(self):
        redis = _make_redis(get_return=None)
        fixed_dt = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc)
        with patch("gateway.quota.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            await get_quota_usage(redis, "v", "k", "daily")
        called_key = redis.get.call_args[0][0]
        assert "2026-04-07" in called_key

    async def test_monthly_key_uses_year_month(self):
        redis = _make_redis(get_return=None)
        fixed_dt = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc)
        with patch("gateway.quota.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            await get_quota_usage(redis, "v", "k", "monthly")
        called_key = redis.get.call_args[0][0]
        assert "2026-04" in called_key
        assert "2026-04-07" not in called_key


# ===========================================================================
# check_quota
# ===========================================================================


class TestCheckQuota:
    async def test_allowed_when_under_limit(self):
        redis = _make_redis(get_return="50")
        allowed, count = await check_quota(redis, "v", "k", limit=100, period="daily")
        assert allowed is True
        assert count == 50

    async def test_denied_when_at_limit(self):
        redis = _make_redis(get_return="100")
        allowed, count = await check_quota(redis, "v", "k", limit=100, period="daily")
        assert allowed is False
        assert count == 100

    async def test_denied_when_over_limit(self):
        redis = _make_redis(get_return="150")
        allowed, count = await check_quota(redis, "v", "k", limit=100, period="daily")
        assert allowed is False
        assert count == 150

    async def test_allowed_when_zero_used(self):
        redis = _make_redis(get_return=None)
        allowed, count = await check_quota(redis, "v", "k", limit=10, period="daily")
        assert allowed is True
        assert count == 0

    async def test_returns_tuple(self):
        redis = _make_redis(get_return="1")
        result = await check_quota(redis, "v", "k", limit=10, period="daily")
        assert isinstance(result, tuple)
        assert len(result) == 2

    async def test_monthly_period(self):
        redis = _make_redis(get_return="999")
        allowed, count = await check_quota(redis, "v", "k", limit=1000, period="monthly")
        assert allowed is True
        assert count == 999


# ===========================================================================
# increment_quota
# ===========================================================================


class TestIncrementQuota:
    async def test_returns_new_count(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=5)
        result = await increment_quota(redis, "v", "k", "daily")
        assert result == 5

    async def test_calls_incr(self):
        redis = _make_redis()
        await increment_quota(redis, "v", "k", "daily")
        redis.incr.assert_called_once()

    async def test_sets_expire_on_first_increment(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=1)  # first write
        await increment_quota(redis, "v", "k", "daily")
        redis.expire.assert_called_once()

    async def test_does_not_set_expire_on_subsequent_increments(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=2)  # not first write
        await increment_quota(redis, "v", "k", "daily")
        redis.expire.assert_not_called()

    async def test_daily_expire_ttl(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=1)
        await increment_quota(redis, "v", "k", "daily")
        _, expire_args, _ = redis.expire.mock_calls[0]
        assert expire_args[1] == DAILY_TTL

    async def test_monthly_expire_ttl(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=1)
        await increment_quota(redis, "v", "k", "monthly")
        _, expire_args, _ = redis.expire.mock_calls[0]
        assert expire_args[1] == MONTHLY_TTL

    async def test_key_contains_bucket(self):
        redis = _make_redis()
        redis.incr = AsyncMock(return_value=1)
        fixed_dt = datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc)
        with patch("gateway.quota.tracker.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_dt
            await increment_quota(redis, "vendor-x", "key-y", "daily")
        incr_key = redis.incr.call_args[0][0]
        assert "2026-04-07" in incr_key
        assert "vendor-x" in incr_key
        assert "key-y" in incr_key


# ===========================================================================
# sync_quota_to_db (stub)
# ===========================================================================


class TestSyncQuotaToDb:
    async def test_runs_without_error(self):
        redis = _make_redis(get_return="10")
        session = AsyncMock()
        # Should not raise
        await sync_quota_to_db(session, redis, "v", "k", "daily")

    async def test_reads_current_usage(self):
        redis = _make_redis(get_return="77")
        session = AsyncMock()
        await sync_quota_to_db(session, redis, "v", "k", "daily")
        redis.get.assert_called_once()

    async def test_works_for_monthly(self):
        redis = _make_redis(get_return="500")
        session = AsyncMock()
        await sync_quota_to_db(session, redis, "v", "k", "monthly")
        redis.get.assert_called_once()


# ===========================================================================
# Pydantic models
# ===========================================================================


class TestQuotaExceededResponse:
    def test_default_error_field(self):
        resp = QuotaExceededResponse(
            vendor="acme",
            key="production",
            limit=10000,
            used=10000,
            period="daily",
            resets_at=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        assert resp.error == "quota_exceeded"

    def test_all_fields_present(self):
        resp = QuotaExceededResponse(
            vendor="acme",
            key="production",
            limit=10000,
            used=10000,
            period="daily",
            resets_at=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        data = resp.model_dump()
        assert "error" in data
        assert "vendor" in data
        assert "key" in data
        assert "limit" in data
        assert "used" in data
        assert "period" in data
        assert "resets_at" in data

    def test_json_serialisation(self):
        resp = QuotaExceededResponse(
            vendor="acme",
            key="production",
            limit=10000,
            used=10000,
            period="daily",
            resets_at=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        data = resp.model_dump(mode="json")
        assert data["error"] == "quota_exceeded"
        assert data["vendor"] == "acme"
        assert data["limit"] == 10000


class TestQuotaStatus:
    def test_remaining_field(self):
        status = QuotaStatus(
            vendor_id="v1",
            key_id="k1",
            period="daily",
            limit=1000,
            used=400,
            remaining=600,
            resets_at=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        assert status.remaining == 600

    def test_all_fields(self):
        s = QuotaStatus(
            vendor_id="v1",
            key_id="k1",
            period="monthly",
            limit=50000,
            used=12345,
            remaining=37655,
            resets_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        )
        assert s.period == "monthly"
        assert s.limit == 50000


# ===========================================================================
# Middleware dependency — check_quota_dependency
# ===========================================================================


class TestCheckQuotaDependency:
    """Unit tests for the FastAPI dependency; DB and Redis are fully mocked."""

    def _make_api_key(
        self,
        *,
        quota_limit: int | None = 100,
        quota_period: str | None = "daily",
        is_active: bool = True,
    ) -> MagicMock:
        key = MagicMock()
        key.quota_limit = quota_limit
        key.quota_period = quota_period
        key.is_active = is_active
        key.vendor_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        key.id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        return key

    def _make_vendor(self, slug: str = "acme") -> MagicMock:
        vendor = MagicMock()
        vendor.slug = slug
        return vendor

    async def _call_dep(
        self,
        *,
        api_key,
        vendor=None,
        redis_get_return=None,
    ):
        from gateway.middleware.quota import check_quota_dependency

        # Mock DB session: scalar_one_or_none returns api_key on first call,
        # then vendor on second (for the vendor name lookup in the 429 path).
        mock_result_key = MagicMock()
        mock_result_key.scalar_one_or_none.return_value = api_key

        mock_result_vendor = MagicMock()
        mock_result_vendor.scalar_one_or_none.return_value = vendor or self._make_vendor()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[mock_result_key, mock_result_vendor])

        redis = _make_redis(get_return=redis_get_return)

        await check_quota_dependency(
            vendor_slug="acme",
            key_name="production",
            db=db,
            redis=redis,
        )

    async def test_passes_when_no_quota_configured(self):
        api_key = self._make_api_key(quota_limit=None, quota_period=None)
        # Should not raise
        await self._call_dep(api_key=api_key)

    async def test_passes_when_under_limit(self):
        api_key = self._make_api_key(quota_limit=100, quota_period="daily")
        # 50 used, limit 100 → allowed
        await self._call_dep(api_key=api_key, redis_get_return="50")

    async def test_raises_429_when_quota_exhausted(self):
        from fastapi import HTTPException

        api_key = self._make_api_key(quota_limit=100, quota_period="daily")
        with pytest.raises(HTTPException) as exc_info:
            await self._call_dep(api_key=api_key, redis_get_return="100")
        assert exc_info.value.status_code == 429

    async def test_429_detail_has_correct_fields(self):
        from fastapi import HTTPException

        api_key = self._make_api_key(quota_limit=100, quota_period="daily")
        with pytest.raises(HTTPException) as exc_info:
            await self._call_dep(api_key=api_key, redis_get_return="100")

        detail = exc_info.value.detail
        assert detail["error"] == "quota_exceeded"
        assert detail["limit"] == 100
        assert detail["used"] == 100
        assert detail["period"] == "daily"
        assert "resets_at" in detail

    async def test_raises_404_when_api_key_not_found(self):
        from fastapi import HTTPException
        from gateway.middleware.quota import check_quota_dependency

        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None  # not found

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result)
        redis = _make_redis()

        with pytest.raises(HTTPException) as exc_info:
            await check_quota_dependency(
                vendor_slug="acme",
                key_name="nonexistent",
                db=db,
                redis=redis,
            )
        assert exc_info.value.status_code == 404

    async def test_fail_open_when_redis_error(self):
        """If Redis raises, the dependency should allow the request through."""
        from gateway.middleware.quota import check_quota_dependency

        api_key = self._make_api_key(quota_limit=100, quota_period="daily")

        mock_result_key = MagicMock()
        mock_result_key.scalar_one_or_none.return_value = api_key

        db = AsyncMock()
        db.execute = AsyncMock(return_value=mock_result_key)

        redis = AsyncMock()
        redis.get = AsyncMock(side_effect=ConnectionError("redis down"))

        # Should not raise
        await check_quota_dependency(
            vendor_slug="acme",
            key_name="production",
            db=db,
            redis=redis,
        )

    async def test_monthly_period_resets_at_first_of_next_month(self):
        from fastapi import HTTPException

        api_key = self._make_api_key(quota_limit=50, quota_period="monthly")
        with pytest.raises(HTTPException) as exc_info:
            await self._call_dep(api_key=api_key, redis_get_return="50")

        detail = exc_info.value.detail
        assert detail["period"] == "monthly"
        resets_at = detail["resets_at"]
        # Should be the first of the next month (day == 1)
        # resets_at is an ISO string in JSON mode
        assert "-01T" in resets_at or resets_at.endswith("-01")
