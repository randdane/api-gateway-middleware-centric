"""Response caching for the API gateway.

Key format: cache:{vendor_slug}:{endpoint_path}:{sha256(sorted_params + body)}

Only 2xx responses are cached. TTL comes from VendorConfig.cache_ttl_seconds with
an optional per-endpoint override.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

_KEY_PREFIX = "cache"


# ---------------------------------------------------------------------------
# Key generation
# ---------------------------------------------------------------------------


def make_cache_key(
    vendor_slug: str,
    endpoint_path: str,
    params: dict[str, str] | None,
    body: bytes | str | None,
) -> str:
    """Return the Redis key for a cached response.

    The fingerprint is sha256 over the sorted query-parameter pairs concatenated
    with the raw request body so that identical requests always map to the same
    key, regardless of parameter ordering.
    """
    params_str = json.dumps(sorted((params or {}).items()), separators=(",", ":"))

    if isinstance(body, str):
        body_bytes = body.encode()
    elif body is None:
        body_bytes = b""
    else:
        body_bytes = body

    fingerprint = hashlib.sha256(params_str.encode() + body_bytes).hexdigest()

    # Normalise path so leading/trailing slashes don't create different keys
    path = endpoint_path.strip("/")

    return f"{_KEY_PREFIX}:{vendor_slug}:{path}:{fingerprint}"


# ---------------------------------------------------------------------------
# Stored data model
# ---------------------------------------------------------------------------


@dataclass
class CachedResponse:
    status_code: int
    headers: dict[str, str]
    body: bytes
    cached_at: datetime


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def _serialise(response: CachedResponse) -> str:
    return json.dumps(
        {
            "status_code": response.status_code,
            "headers": response.headers,
            "body": response.body.hex(),
            "cached_at": response.cached_at.isoformat(),
        }
    )


def _deserialise(raw: str) -> CachedResponse:
    data = json.loads(raw)
    return CachedResponse(
        status_code=data["status_code"],
        headers=data["headers"],
        body=bytes.fromhex(data["body"]),
        cached_at=datetime.fromisoformat(data["cached_at"]),
    )


# ---------------------------------------------------------------------------
# Store / retrieve
# ---------------------------------------------------------------------------


async def get_cached(redis: Redis, key: str) -> CachedResponse | None:
    """Return the cached response for *key*, or ``None`` if not present."""
    raw = await redis.get(key)
    if raw is None:
        return None
    return _deserialise(raw)


async def set_cached(
    redis: Redis,
    key: str,
    response: CachedResponse,
    ttl_seconds: int,
) -> None:
    """Store *response* under *key* with the given TTL.

    Only stores if:
    - ``ttl_seconds > 0``
    - the status code is 2xx
    """
    if ttl_seconds <= 0:
        return
    if not (200 <= response.status_code < 300):
        return

    await redis.set(key, _serialise(response), ex=ttl_seconds)


# ---------------------------------------------------------------------------
# Cache flush helpers
# ---------------------------------------------------------------------------


async def flush_vendor(redis: Redis, vendor_slug: str) -> int:
    """Delete all cached responses for *vendor_slug*.

    Returns the number of keys deleted.
    """
    pattern = f"{_KEY_PREFIX}:{vendor_slug}:*"
    return await _delete_by_pattern(redis, pattern)


async def flush_all(redis: Redis) -> int:
    """Delete every response-cache key managed by this module.

    Returns the number of keys deleted.
    """
    pattern = f"{_KEY_PREFIX}:*"
    return await _delete_by_pattern(redis, pattern)


async def _delete_by_pattern(redis: Redis, pattern: str) -> int:
    """Scan and delete all keys matching *pattern*; return count deleted."""
    deleted = 0
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        if keys:
            deleted += await redis.delete(*keys)
        if cursor == 0:
            break
    return deleted


# ---------------------------------------------------------------------------
# TTL resolution helper
# ---------------------------------------------------------------------------


def resolve_ttl(vendor_ttl: int, endpoint_ttl_override: int | None) -> int:
    """Return the effective TTL in seconds.

    ``endpoint_ttl_override`` (if not None) wins over the vendor-level default.
    """
    if endpoint_ttl_override is not None:
        return endpoint_ttl_override
    return vendor_ttl
