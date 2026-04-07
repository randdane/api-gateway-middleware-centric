"""Unit tests for gateway.cache.response_cache.

All Redis calls are mocked — no running Redis required.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.cache.response_cache import (
    CachedResponse,
    _deserialise,
    _serialise,
    flush_all,
    flush_vendor,
    get_cached,
    make_cache_key,
    resolve_ttl,
    set_cached,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_response(status_code: int = 200, body: bytes = b'{"ok": true}') -> CachedResponse:
    return CachedResponse(
        status_code=status_code,
        headers={"content-type": "application/json"},
        body=body,
        cached_at=datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC),
    )


def _make_redis(**method_overrides) -> AsyncMock:
    """Return a mock Redis client with sensible defaults."""
    redis = AsyncMock()
    for name, val in method_overrides.items():
        setattr(redis, name, val)
    return redis


# ---------------------------------------------------------------------------
# make_cache_key
# ---------------------------------------------------------------------------


class TestMakeCacheKey:
    def test_basic_format(self):
        key = make_cache_key("stripe", "/v1/charges", {}, b"")
        assert key.startswith("cache:stripe:v1/charges:")
        # fingerprint is a sha256 hex string (64 chars)
        parts = key.split(":")
        assert len(parts) == 4
        assert len(parts[3]) == 64

    def test_leading_trailing_slashes_normalised(self):
        key1 = make_cache_key("stripe", "/v1/charges/", {}, b"")
        key2 = make_cache_key("stripe", "v1/charges", {}, b"")
        assert key1 == key2

    def test_params_order_independent(self):
        key1 = make_cache_key("v", "/ep", {"b": "2", "a": "1"}, b"")
        key2 = make_cache_key("v", "/ep", {"a": "1", "b": "2"}, b"")
        assert key1 == key2

    def test_different_params_give_different_keys(self):
        key1 = make_cache_key("v", "/ep", {"a": "1"}, b"")
        key2 = make_cache_key("v", "/ep", {"a": "2"}, b"")
        assert key1 != key2

    def test_different_body_gives_different_keys(self):
        key1 = make_cache_key("v", "/ep", {}, b"body-a")
        key2 = make_cache_key("v", "/ep", {}, b"body-b")
        assert key1 != key2

    def test_none_params_treated_as_empty(self):
        key1 = make_cache_key("v", "/ep", None, b"")
        key2 = make_cache_key("v", "/ep", {}, b"")
        assert key1 == key2

    def test_none_body_treated_as_empty(self):
        key1 = make_cache_key("v", "/ep", {}, None)
        key2 = make_cache_key("v", "/ep", {}, b"")
        assert key1 == key2

    def test_string_body_same_as_bytes(self):
        key1 = make_cache_key("v", "/ep", {}, "hello")
        key2 = make_cache_key("v", "/ep", {}, b"hello")
        assert key1 == key2

    def test_different_vendor_gives_different_keys(self):
        key1 = make_cache_key("vendor-a", "/ep", {}, b"")
        key2 = make_cache_key("vendor-b", "/ep", {}, b"")
        assert key1 != key2

    def test_different_path_gives_different_keys(self):
        key1 = make_cache_key("v", "/ep/a", {}, b"")
        key2 = make_cache_key("v", "/ep/b", {}, b"")
        assert key1 != key2


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_round_trip(self):
        original = _make_response()
        restored = _deserialise(_serialise(original))
        assert restored.status_code == original.status_code
        assert restored.headers == original.headers
        assert restored.body == original.body
        assert restored.cached_at == original.cached_at

    def test_binary_body_preserved(self):
        binary_body = bytes(range(256))
        resp = _make_response(body=binary_body)
        assert _deserialise(_serialise(resp)).body == binary_body


# ---------------------------------------------------------------------------
# get_cached
# ---------------------------------------------------------------------------


class TestGetCached:
    async def test_returns_none_on_cache_miss(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value=None)
        result = await get_cached(redis, "cache:v:ep:abc")
        assert result is None

    async def test_returns_cached_response_on_hit(self):
        response = _make_response()
        redis = _make_redis()
        redis.get = AsyncMock(return_value=_serialise(response))

        result = await get_cached(redis, "cache:v:ep:abc")
        assert result is not None
        assert result.status_code == 200
        assert result.body == response.body

    async def test_passes_key_to_redis(self):
        redis = _make_redis()
        redis.get = AsyncMock(return_value=None)
        key = "cache:stripe:v1/charges:deadbeef"
        await get_cached(redis, key)
        redis.get.assert_called_once_with(key)


# ---------------------------------------------------------------------------
# set_cached
# ---------------------------------------------------------------------------


class TestSetCached:
    async def test_stores_2xx_response(self):
        redis = _make_redis()
        response = _make_response(status_code=200)
        await set_cached(redis, "k", response, ttl_seconds=60)
        redis.set.assert_called_once()
        assert redis.set.call_args[1]["ex"] == 60

    async def test_stores_201_response(self):
        redis = _make_redis()
        response = _make_response(status_code=201)
        await set_cached(redis, "k", response, ttl_seconds=30)
        redis.set.assert_called_once()

    async def test_does_not_store_4xx(self):
        redis = _make_redis()
        response = _make_response(status_code=404)
        await set_cached(redis, "k", response, ttl_seconds=60)
        redis.set.assert_not_called()

    async def test_does_not_store_5xx(self):
        redis = _make_redis()
        response = _make_response(status_code=500)
        await set_cached(redis, "k", response, ttl_seconds=60)
        redis.set.assert_not_called()

    async def test_does_not_store_when_ttl_zero(self):
        redis = _make_redis()
        response = _make_response(status_code=200)
        await set_cached(redis, "k", response, ttl_seconds=0)
        redis.set.assert_not_called()

    async def test_does_not_store_when_ttl_negative(self):
        redis = _make_redis()
        response = _make_response(status_code=200)
        await set_cached(redis, "k", response, ttl_seconds=-1)
        redis.set.assert_not_called()

    async def test_uses_correct_ttl(self):
        redis = _make_redis()
        response = _make_response()
        await set_cached(redis, "k", response, ttl_seconds=120)
        call_kwargs = redis.set.call_args[1]
        assert call_kwargs["ex"] == 120


# ---------------------------------------------------------------------------
# flush_vendor
# ---------------------------------------------------------------------------


class TestFlushVendor:
    async def test_deletes_vendor_keys_and_returns_count(self):
        redis = _make_redis()
        # First scan returns 2 keys, cursor=0 means done
        redis.scan = AsyncMock(return_value=(0, ["cache:stripe:a:1", "cache:stripe:b:2"]))
        redis.delete = AsyncMock(return_value=2)

        count = await flush_vendor(redis, "stripe")
        assert count == 2
        redis.delete.assert_called_once_with("cache:stripe:a:1", "cache:stripe:b:2")

    async def test_uses_vendor_pattern(self):
        redis = _make_redis()
        redis.scan = AsyncMock(return_value=(0, []))
        redis.delete = AsyncMock(return_value=0)

        await flush_vendor(redis, "acme")
        scan_call = redis.scan.call_args
        assert scan_call[1]["match"] == "cache:acme:*"

    async def test_returns_zero_when_no_keys(self):
        redis = _make_redis()
        redis.scan = AsyncMock(return_value=(0, []))
        redis.delete = AsyncMock(return_value=0)

        count = await flush_vendor(redis, "empty-vendor")
        assert count == 0
        redis.delete.assert_not_called()

    async def test_handles_multiple_scan_pages(self):
        redis = _make_redis()
        # Two pages: cursor=5 then cursor=0 (done)
        redis.scan = AsyncMock(
            side_effect=[
                (5, ["cache:v:a:1", "cache:v:b:2"]),
                (0, ["cache:v:c:3"]),
            ]
        )
        redis.delete = AsyncMock(return_value=2)

        count = await flush_vendor(redis, "v")
        assert redis.scan.call_count == 2
        assert redis.delete.call_count == 2
        assert count == 4


# ---------------------------------------------------------------------------
# flush_all
# ---------------------------------------------------------------------------


class TestFlushAll:
    async def test_uses_global_pattern(self):
        redis = _make_redis()
        redis.scan = AsyncMock(return_value=(0, []))
        redis.delete = AsyncMock(return_value=0)

        await flush_all(redis)
        scan_call = redis.scan.call_args
        assert scan_call[1]["match"] == "cache:*"

    async def test_returns_deleted_count(self):
        redis = _make_redis()
        redis.scan = AsyncMock(return_value=(0, ["cache:a:b:1", "cache:c:d:2"]))
        redis.delete = AsyncMock(return_value=2)

        count = await flush_all(redis)
        assert count == 2


# ---------------------------------------------------------------------------
# resolve_ttl
# ---------------------------------------------------------------------------


class TestResolveTtl:
    def test_uses_endpoint_override_when_set(self):
        assert resolve_ttl(vendor_ttl=60, endpoint_ttl_override=30) == 30

    def test_falls_back_to_vendor_ttl_when_override_is_none(self):
        assert resolve_ttl(vendor_ttl=60, endpoint_ttl_override=None) == 60

    def test_zero_override_disables_caching(self):
        assert resolve_ttl(vendor_ttl=60, endpoint_ttl_override=0) == 0

    def test_zero_vendor_ttl_with_no_override(self):
        assert resolve_ttl(vendor_ttl=0, endpoint_ttl_override=None) == 0
