"""Request deduplication for the API gateway.

Prevents thundering-herd to the same vendor endpoint by serialising identical
in-flight requests behind a single Redis lock.  A request that wins the lock
makes the vendor call and publishes the result; all other requests subscribe
to that pub/sub channel and receive the result without making a redundant
vendor call.

Key format: ``dedup:{sha256(vendor + ":" + endpoint + ":" + sorted_params + body)}``

Usage
-----
::

    async with dedup_context(redis, dedup_key) as acquired:
        if acquired:
            result = await vendor_client.request(...)
            await dedup_publish(redis, dedup_key, result)
            return result
        else:
            return await dedup_wait(redis, dedup_key, timeout=30)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator

from redis.asyncio import Redis

from gateway.cache.response_cache import CachedResponse

_KEY_PREFIX = "dedup"
_LOCK_TTL_SECONDS = 30
_RESULT_TTL_SECONDS = 60


def _result_key(key: str) -> str:
    """Return the Redis key used to store a published result for *key*."""
    return f"{key}:result"


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def make_dedup_key(
    vendor_slug: str,
    endpoint_path: str,
    params: dict[str, str] | None,
    body: bytes | str | None,
) -> str:
    """Return the Redis dedup key for a request.

    The fingerprint is ``sha256(vendor ":" normalised_path ":"
    sorted_params_json body_bytes)`` so that identical requests always map to
    the same key regardless of parameter ordering.
    """
    params_str = json.dumps(sorted((params or {}).items()), separators=(",", ":"))

    if isinstance(body, str):
        body_bytes = body.encode()
    elif body is None:
        body_bytes = b""
    else:
        body_bytes = body

    path = endpoint_path.strip("/")

    fingerprint = hashlib.sha256(
        f"{vendor_slug}:{path}:".encode() + params_str.encode() + body_bytes
    ).hexdigest()

    return f"{_KEY_PREFIX}:{fingerprint}"


# ---------------------------------------------------------------------------
# Serialisation helpers  (reuse CachedResponse as the wire format)
# ---------------------------------------------------------------------------


def _serialise_result(response: CachedResponse) -> str:
    return json.dumps(
        {
            "status_code": response.status_code,
            "headers": response.headers,
            "body": response.body.hex(),
            "cached_at": response.cached_at.isoformat(),
        }
    )


def _deserialise_result(raw: str) -> CachedResponse:
    data = json.loads(raw)
    return CachedResponse(
        status_code=data["status_code"],
        headers=data["headers"],
        body=bytes.fromhex(data["body"]),
        cached_at=datetime.fromisoformat(data["cached_at"]),
    )


# ---------------------------------------------------------------------------
# Lock acquisition
# ---------------------------------------------------------------------------


async def _acquire_lock(redis: Redis, key: str) -> bool:
    """Try to set the dedup lock using SET NX EX.

    Returns ``True`` if the lock was acquired, ``False`` if it already exists.
    """
    result = await redis.set(key, "1", nx=True, ex=_LOCK_TTL_SECONDS)
    return result is not None


async def _release_lock(redis: Redis, key: str) -> None:
    """Release the dedup lock."""
    await redis.delete(key)


# ---------------------------------------------------------------------------
# Pub/sub helpers
# ---------------------------------------------------------------------------


async def dedup_publish(redis: Redis, key: str, response: CachedResponse) -> None:
    """Publish *response* on the pub/sub channel for *key*.

    Call this after a successful vendor call when you own the dedup lock.
    The channel name is the dedup key itself.

    The serialised result is also stored in Redis under ``dedup:result:{hash}``
    with a short TTL so that waiters which subscribe *after* the message is
    published can still retrieve the result without hanging until their timeout.
    """
    payload = _serialise_result(response)
    # Store first so any subscriber that wakes up after the publish can also
    # find the result via GET (store-and-notify pattern).
    await redis.set(_result_key(key), payload, ex=_RESULT_TTL_SECONDS)
    await redis.publish(key, payload)


async def dedup_wait(
    redis: Redis,
    key: str,
    timeout: float = 30.0,
) -> CachedResponse | None:
    """Subscribe to the pub/sub channel for *key* and wait for a result.

    Returns the ``CachedResponse`` published by the lock holder, or ``None``
    if the timeout expires before a message arrives.

    To close the race where the lock holder completes and calls
    :func:`dedup_publish` between when this waiter checks the lock and when
    it subscribes to the channel, we subscribe *first* and then check for a
    pre-stored result key (written by :func:`dedup_publish` before it
    publishes).  If found, we return immediately without polling.

    If the lock holder fails without calling :func:`dedup_publish`, waiters
    will receive ``None`` after *timeout* seconds.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe(key)
    try:
        # Check stored result AFTER subscribing (race-safe).
        stored = await redis.get(_result_key(key))
        if stored is not None:
            return _deserialise_result(stored if isinstance(stored, str) else stored.decode())
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return None
            # Poll with a short sleep to avoid busy-waiting
            message = await pubsub.get_message(
                ignore_subscribe_messages=True,
                timeout=min(remaining, 0.1),
            )
            if message is not None and message.get("type") == "message":
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                return _deserialise_result(data)
            # Short yield to allow other coroutines to run
            await asyncio.sleep(0.01)
    finally:
        await pubsub.unsubscribe(key)
        await pubsub.aclose()


# ---------------------------------------------------------------------------
# High-level context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def dedup_context(
    redis: Redis,
    key: str,
) -> AsyncIterator[bool]:
    """Async context manager that acquires the dedup lock for *key*.

    Yields ``True`` if this caller acquired the lock (i.e. should make the
    vendor call and then call :func:`dedup_publish`), or ``False`` if another
    request already holds the lock (the caller should call
    :func:`dedup_wait` instead).

    The lock is always released when the ``acquired=True`` block exits (even
    on exception) so waiting subscribers are not left hanging.

    Example::

        async with dedup_context(redis, dedup_key) as acquired:
            if acquired:
                result = await vendor_client.request(...)
                await dedup_publish(redis, dedup_key, result)
                return result
            else:
                return await dedup_wait(redis, dedup_key, timeout=30)
    """
    acquired = await _acquire_lock(redis, key)
    try:
        yield acquired
    finally:
        if acquired:
            await _release_lock(redis, key)
