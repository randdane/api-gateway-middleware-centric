"""Integration tests for gateway.cache.response_cache — real Redis via testcontainers.

All tests are skipped automatically when Docker is unavailable so CI without
a Docker daemon does not fail.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from gateway.cache.response_cache import (
    CachedResponse,
    flush_all,
    flush_vendor,
    get_cached,
    make_cache_key,
    set_cached,
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
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    body: bytes = b'{"result": "ok"}',
    headers: dict[str, str] | None = None,
) -> CachedResponse:
    return CachedResponse(
        status_code=status_code,
        headers=headers or {"content-type": "application/json"},
        body=body,
        cached_at=datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Basic write / read
# ---------------------------------------------------------------------------


async def test_set_and_get_cached_response(redis_client):
    key = make_cache_key("stripe", "/v1/charges", {"limit": "10"}, b"")
    response = _make_response()

    await set_cached(redis_client, key, response, ttl_seconds=60)
    result = await get_cached(redis_client, key)

    assert result is not None
    assert result.status_code == 200
    assert result.body == response.body
    assert result.headers == response.headers
    assert result.cached_at == response.cached_at


async def test_get_returns_none_on_miss(redis_client):
    key = make_cache_key("stripe", "/v1/missing", {}, b"")
    result = await get_cached(redis_client, key)
    assert result is None


async def test_set_does_not_store_non_2xx(redis_client):
    key = make_cache_key("stripe", "/v1/err", {}, b"")
    await set_cached(redis_client, key, _make_response(status_code=404), ttl_seconds=60)
    assert await get_cached(redis_client, key) is None


async def test_set_does_not_store_with_zero_ttl(redis_client):
    key = make_cache_key("stripe", "/v1/charges", {}, b"")
    await set_cached(redis_client, key, _make_response(), ttl_seconds=0)
    assert await get_cached(redis_client, key) is None


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


async def test_entry_expires_after_ttl(redis_client):
    import asyncio

    key = make_cache_key("stripe", "/v1/expire", {}, b"")
    await set_cached(redis_client, key, _make_response(), ttl_seconds=1)

    # Should be present immediately
    assert await get_cached(redis_client, key) is not None

    # After TTL expires it should be gone
    await asyncio.sleep(1.1)
    assert await get_cached(redis_client, key) is None


# ---------------------------------------------------------------------------
# flush_vendor
# ---------------------------------------------------------------------------


async def test_flush_vendor_removes_only_vendor_keys(redis_client):
    key_a = make_cache_key("stripe", "/v1/charges", {}, b"")
    key_b = make_cache_key("stripe", "/v1/customers", {}, b"")
    key_other = make_cache_key("twilio", "/messages", {}, b"")

    for k in (key_a, key_b, key_other):
        await set_cached(redis_client, k, _make_response(), ttl_seconds=300)

    deleted = await flush_vendor(redis_client, "stripe")

    assert deleted == 2
    assert await get_cached(redis_client, key_a) is None
    assert await get_cached(redis_client, key_b) is None
    # Other vendor untouched
    assert await get_cached(redis_client, key_other) is not None


async def test_flush_vendor_returns_zero_when_no_keys(redis_client):
    count = await flush_vendor(redis_client, "nonexistent-vendor")
    assert count == 0


# ---------------------------------------------------------------------------
# flush_all
# ---------------------------------------------------------------------------


async def test_flush_all_removes_all_cache_keys(redis_client):
    keys = [
        make_cache_key("stripe", "/v1/a", {}, b""),
        make_cache_key("twilio", "/messages", {}, b""),
        make_cache_key("sendgrid", "/mail/send", {}, b""),
    ]
    for k in keys:
        await set_cached(redis_client, k, _make_response(), ttl_seconds=300)

    deleted = await flush_all(redis_client)

    assert deleted == 3
    for k in keys:
        assert await get_cached(redis_client, k) is None


async def test_flush_all_returns_zero_on_empty_cache(redis_client):
    count = await flush_all(redis_client)
    assert count == 0


async def test_flush_all_does_not_remove_non_cache_keys(redis_client):
    """Keys not using the 'cache:' prefix must be untouched."""
    await redis_client.set("other:key", "value")
    await set_cached(
        redis_client,
        make_cache_key("stripe", "/v1/x", {}, b""),
        _make_response(),
        ttl_seconds=300,
    )

    await flush_all(redis_client)

    assert await redis_client.get("other:key") == "value"


# ---------------------------------------------------------------------------
# Key uniqueness
# ---------------------------------------------------------------------------


async def test_different_params_cached_independently(redis_client):
    key1 = make_cache_key("stripe", "/v1/charges", {"limit": "10"}, b"")
    key2 = make_cache_key("stripe", "/v1/charges", {"limit": "20"}, b"")

    resp1 = _make_response(body=b'{"limit":10}')
    resp2 = _make_response(body=b'{"limit":20}')

    await set_cached(redis_client, key1, resp1, ttl_seconds=60)
    await set_cached(redis_client, key2, resp2, ttl_seconds=60)

    assert (await get_cached(redis_client, key1)).body == b'{"limit":10}'
    assert (await get_cached(redis_client, key2)).body == b'{"limit":20}'
