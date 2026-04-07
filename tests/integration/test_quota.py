"""Integration tests for quota tracking — real Redis via testcontainers.

All tests are skipped automatically when Docker is unavailable so CI without
a Docker daemon does not fail.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from gateway.quota.tracker import (
    check_quota,
    get_quota_usage,
    increment_quota,
    quota_key,
    period_bucket,
    period_ttl,
)


# ---------------------------------------------------------------------------
# Docker / testcontainers availability guard
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    try:
        import docker  # type: ignore[import-untyped]

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon not available — skipping integration tests",
)


# ---------------------------------------------------------------------------
# Session-scoped Redis container + client
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_container():
    from testcontainers.redis import RedisContainer  # type: ignore[import-untyped]

    with RedisContainer() as container:
        yield container


@pytest.fixture(scope="session")
def redis_url(redis_container):
    host = redis_container.get_container_host_ip()
    port = redis_container.get_exposed_port(6379)
    return f"redis://{host}:{port}"


@pytest.fixture
async def redis_client(redis_url):
    """Yield a connected Redis client and flush the DB between tests."""
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    try:
        yield client
    finally:
        await client.aclose()


# ---------------------------------------------------------------------------
# get_quota_usage
# ---------------------------------------------------------------------------


async def test_get_usage_returns_zero_for_missing_key(redis_client):
    result = await get_quota_usage(redis_client, "vendor-1", "key-1", "daily")
    assert result == 0


async def test_get_usage_returns_stored_value(redis_client):
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("daily", now)
    key = quota_key("vendor-1", "key-1", bucket)
    await redis_client.set(key, "42")
    result = await get_quota_usage(redis_client, "vendor-1", "key-1", "daily")
    assert result == 42


async def test_get_usage_monthly(redis_client):
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("monthly", now)
    key = quota_key("vendor-2", "key-2", bucket)
    await redis_client.set(key, "999")
    result = await get_quota_usage(redis_client, "vendor-2", "key-2", "monthly")
    assert result == 999


# ---------------------------------------------------------------------------
# check_quota
# ---------------------------------------------------------------------------


async def test_check_quota_allowed_when_under_limit(redis_client):
    allowed, count = await check_quota(
        redis_client, "vendor-1", "key-1", limit=100, period="daily"
    )
    assert allowed is True
    assert count == 0


async def test_check_quota_denied_when_at_limit(redis_client):
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("daily", now)
    key = quota_key("vendor-1", "key-1", bucket)
    await redis_client.set(key, "100")

    allowed, count = await check_quota(
        redis_client, "vendor-1", "key-1", limit=100, period="daily"
    )
    assert allowed is False
    assert count == 100


async def test_check_quota_allowed_just_under_limit(redis_client):
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("daily", now)
    key = quota_key("vendor-1", "key-1", bucket)
    await redis_client.set(key, "99")

    allowed, count = await check_quota(
        redis_client, "vendor-1", "key-1", limit=100, period="daily"
    )
    assert allowed is True
    assert count == 99


# ---------------------------------------------------------------------------
# increment_quota
# ---------------------------------------------------------------------------


async def test_increment_quota_returns_1_on_first_call(redis_client):
    count = await increment_quota(redis_client, "vendor-1", "key-1", "daily")
    assert count == 1


async def test_increment_quota_increments_each_call(redis_client):
    for expected in range(1, 6):
        count = await increment_quota(redis_client, "vendor-1", "key-1", "daily")
        assert count == expected


async def test_increment_quota_sets_ttl_on_first_call(redis_client):
    await increment_quota(redis_client, "vendor-1", "key-1", "daily")
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("daily", now)
    key = quota_key("vendor-1", "key-1", bucket)
    ttl = await redis_client.ttl(key)
    assert ttl > 0
    assert ttl <= period_ttl("daily")


async def test_increment_quota_daily_ttl_approx(redis_client):
    await increment_quota(redis_client, "vendor-a", "key-a", "daily")
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("daily", now)
    key = quota_key("vendor-a", "key-a", bucket)
    ttl = await redis_client.ttl(key)
    # Allow up to 5 s of drift
    assert ttl >= period_ttl("daily") - 5


async def test_increment_quota_monthly_ttl_approx(redis_client):
    await increment_quota(redis_client, "vendor-b", "key-b", "monthly")
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket("monthly", now)
    key = quota_key("vendor-b", "key-b", bucket)
    ttl = await redis_client.ttl(key)
    assert ttl >= period_ttl("monthly") - 5


async def test_increment_does_not_reset_ttl_on_subsequent_calls(redis_client):
    """Second increment should not reset TTL (it should remain <= first TTL)."""
    v, k, p = "vendor-c", "key-c", "daily"
    await increment_quota(redis_client, v, k, p)

    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket(p, now)
    redis_key = quota_key(v, k, bucket)
    ttl_after_first = await redis_client.ttl(redis_key)

    await increment_quota(redis_client, v, k, p)
    ttl_after_second = await redis_client.ttl(redis_key)

    # TTL should not be reset (second call should not extend it)
    assert ttl_after_second <= ttl_after_first


# ---------------------------------------------------------------------------
# Key isolation
# ---------------------------------------------------------------------------


async def test_different_vendor_ids_are_independent(redis_client):
    await increment_quota(redis_client, "vendor-x", "key-1", "daily")
    # vendor-y is untouched
    usage = await get_quota_usage(redis_client, "vendor-y", "key-1", "daily")
    assert usage == 0


async def test_different_key_ids_are_independent(redis_client):
    await increment_quota(redis_client, "vendor-1", "key-alpha", "daily")
    usage = await get_quota_usage(redis_client, "vendor-1", "key-beta", "daily")
    assert usage == 0


async def test_daily_and_monthly_counters_are_independent(redis_client):
    await increment_quota(redis_client, "vendor-1", "key-1", "daily")
    usage_monthly = await get_quota_usage(redis_client, "vendor-1", "key-1", "monthly")
    assert usage_monthly == 0


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_increments_are_atomic(redis_client):
    """N concurrent increments should yield a final count of exactly N."""
    n = 20
    results = await asyncio.gather(
        *[increment_quota(redis_client, "vendor-1", "key-1", "daily") for _ in range(n)]
    )
    # Final count (max returned value) should equal n
    assert max(results) == n
    # All counts should be unique (1..n)
    assert sorted(results) == list(range(1, n + 1))


async def test_concurrent_check_then_increment(redis_client):
    """check_quota + increment_quota reflects correct counts under concurrency."""
    limit = 5
    n = 10
    v, k, p = "vendor-1", "key-1", "daily"

    # Allow all checks, then increment (simulates sequential request flow)
    for i in range(n):
        allowed, count = await check_quota(redis_client, v, k, limit=limit, period=p)
        if allowed:
            await increment_quota(redis_client, v, k, p)

    final = await get_quota_usage(redis_client, v, k, p)
    assert final == limit
