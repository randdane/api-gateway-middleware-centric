"""Integration tests for gateway.cache.dedup — real Redis via testcontainers.

All tests are skipped automatically when Docker is unavailable so CI without
a Docker daemon does not fail.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from gateway.cache.dedup import (
    _acquire_lock,
    _release_lock,
    dedup_context,
    dedup_publish,
    dedup_wait,
    make_dedup_key,
)
from gateway.cache.response_cache import CachedResponse

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
) -> CachedResponse:
    return CachedResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=body,
        cached_at=datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# make_dedup_key
# ---------------------------------------------------------------------------


def test_dedup_key_format():
    key = make_dedup_key("stripe", "/v1/charges", {"limit": "10"}, b"")
    assert key.startswith("dedup:")
    assert len(key.split(":")[1]) == 64


# ---------------------------------------------------------------------------
# Lock acquire / release
# ---------------------------------------------------------------------------


async def test_acquire_lock_succeeds_when_key_absent(redis_client):
    key = make_dedup_key("stripe", "/v1/charges", {}, b"")
    acquired = await _acquire_lock(redis_client, key)
    assert acquired is True


async def test_acquire_lock_fails_when_key_present(redis_client):
    key = make_dedup_key("stripe", "/v1/charges", {}, b"test-conflict")
    acquired_first = await _acquire_lock(redis_client, key)
    acquired_second = await _acquire_lock(redis_client, key)
    assert acquired_first is True
    assert acquired_second is False


async def test_lock_ttl_is_set(redis_client):
    key = make_dedup_key("stripe", "/v1/ttl", {}, b"")
    await _acquire_lock(redis_client, key)
    ttl = await redis_client.ttl(key)
    # TTL should be ≤ 30 and > 0
    assert 0 < ttl <= 30


async def test_release_lock_removes_key(redis_client):
    key = make_dedup_key("stripe", "/v1/release", {}, b"")
    await _acquire_lock(redis_client, key)
    assert await redis_client.exists(key) == 1

    await _release_lock(redis_client, key)
    assert await redis_client.exists(key) == 0


async def test_release_lock_idempotent(redis_client):
    """Releasing a non-existent lock should not raise."""
    key = make_dedup_key("stripe", "/v1/idempotent", {}, b"")
    await _release_lock(redis_client, key)  # should not raise


# ---------------------------------------------------------------------------
# dedup_publish + dedup_wait  (end-to-end pub/sub)
# ---------------------------------------------------------------------------


async def test_publish_and_wait_round_trip(redis_url):
    """The waiter receives exactly the response published by the lock holder."""
    import redis.asyncio as aioredis

    # Use two separate clients: one for publishing, one for subscribing.
    # decode_responses=False on the subscriber so we get raw bytes/str as
    # redis.asyncio pub/sub naturally returns.
    publisher = aioredis.from_url(redis_url, decode_responses=True)
    subscriber = aioredis.from_url(redis_url, decode_responses=True)

    await publisher.flushdb()

    response = _make_response(status_code=200, body=b'{"data": "hello"}')
    key = make_dedup_key("vendor", "/path", {"q": "1"}, b"")

    async def _waiter():
        return await dedup_wait(subscriber, key, timeout=5.0)

    async def _publisher():
        # Small delay to ensure subscriber is subscribed before publish
        await asyncio.sleep(0.1)
        await dedup_publish(publisher, key, response)

    waiter_task = asyncio.create_task(_waiter())
    publisher_task = asyncio.create_task(_publisher())

    received, _ = await asyncio.gather(waiter_task, publisher_task)

    assert received is not None
    assert received.status_code == 200
    assert received.body == b'{"data": "hello"}'
    assert received.headers == response.headers

    await publisher.aclose()
    await subscriber.aclose()


async def test_wait_returns_none_on_timeout(redis_url):
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_url, decode_responses=True)
    await client.flushdb()

    key = make_dedup_key("vendor", "/timeout-path", {}, b"")
    result = await dedup_wait(client, key, timeout=0.2)
    assert result is None

    await client.aclose()


# ---------------------------------------------------------------------------
# dedup_context
# ---------------------------------------------------------------------------


async def test_context_manager_acquires_and_releases(redis_client):
    key = make_dedup_key("vendor", "/ctx", {}, b"")

    async with dedup_context(redis_client, key) as acquired:
        assert acquired is True
        # Lock should exist during the block
        assert await redis_client.exists(key) == 1

    # Lock should be released after the block
    assert await redis_client.exists(key) == 0


async def test_context_manager_second_caller_sees_false(redis_client):
    key = make_dedup_key("vendor", "/ctx-second", {}, b"")

    async with dedup_context(redis_client, key) as first:
        assert first is True
        async with dedup_context(redis_client, key) as second:
            assert second is False


async def test_context_manager_releases_lock_on_exception(redis_client):
    key = make_dedup_key("vendor", "/ctx-exc", {}, b"")

    with pytest.raises(RuntimeError):
        async with dedup_context(redis_client, key) as acquired:
            assert acquired is True
            raise RuntimeError("vendor error")

    # Lock must be released even after exception
    assert await redis_client.exists(key) == 0


async def test_full_dedup_flow_with_concurrent_requests(redis_url):
    """Two concurrent requests for the same key: only one makes the vendor call."""
    import redis.asyncio as aioredis

    client_a = aioredis.from_url(redis_url, decode_responses=True)
    client_b = aioredis.from_url(redis_url, decode_responses=True)
    client_pub = aioredis.from_url(redis_url, decode_responses=True)

    await client_a.flushdb()

    key = make_dedup_key("vendor", "/concurrent", {"id": "42"}, b"")
    response = _make_response(body=b'{"id": 42}')

    lock_holders = []
    results = []

    async def request_a():
        async with dedup_context(client_a, key) as acquired:
            lock_holders.append(acquired)
            if acquired:
                # Simulate vendor call latency then publish
                await asyncio.sleep(0.15)
                await dedup_publish(client_pub, key, response)
                results.append(response)
            else:
                result = await dedup_wait(client_a, key, timeout=5.0)
                results.append(result)

    async def request_b():
        # Small delay so A gets the lock first
        await asyncio.sleep(0.05)
        async with dedup_context(client_b, key) as acquired:
            lock_holders.append(acquired)
            if acquired:
                await asyncio.sleep(0.15)
                await dedup_publish(client_pub, key, response)
                results.append(response)
            else:
                result = await dedup_wait(client_b, key, timeout=5.0)
                results.append(result)

    await asyncio.gather(request_a(), request_b())

    # Exactly one caller should have acquired the lock
    assert lock_holders.count(True) == 1
    assert lock_holders.count(False) == 1

    # Both should have a result
    assert len(results) == 2
    for r in results:
        assert r is not None
        assert r.body == b'{"id": 42}'

    await client_a.aclose()
    await client_b.aclose()
    await client_pub.aclose()
