"""Unit tests for gateway.cache.dedup.

All Redis calls are mocked — no running Redis required.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.cache.dedup import (
    _acquire_lock,
    _deserialise_result,
    _release_lock,
    _result_key,
    _serialise_result,
    dedup_context,
    dedup_publish,
    dedup_wait,
    make_dedup_key,
)
from gateway.cache.response_cache import CachedResponse


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_response(
    status_code: int = 200,
    body: bytes = b'{"ok": true}',
    headers: dict[str, str] | None = None,
) -> CachedResponse:
    return CachedResponse(
        status_code=status_code,
        headers=headers or {"content-type": "application/json"},
        body=body,
        cached_at=datetime(2026, 4, 6, 12, 0, 0, tzinfo=UTC),
    )


def _make_redis(**overrides) -> AsyncMock:
    redis = AsyncMock()
    # Default: no stored result key found (fast-path in dedup_wait is skipped).
    redis.get = AsyncMock(return_value=None)
    for name, val in overrides.items():
        setattr(redis, name, val)
    return redis


# ---------------------------------------------------------------------------
# make_dedup_key
# ---------------------------------------------------------------------------


class TestMakeDedupKey:
    def test_basic_format(self):
        key = make_dedup_key("stripe", "/v1/charges", {}, b"")
        assert key.startswith("dedup:")
        parts = key.split(":")
        assert len(parts) == 2
        # fingerprint is a sha256 hex string (64 chars)
        assert len(parts[1]) == 64

    def test_leading_trailing_slashes_normalised(self):
        key1 = make_dedup_key("stripe", "/v1/charges/", {}, b"")
        key2 = make_dedup_key("stripe", "v1/charges", {}, b"")
        assert key1 == key2

    def test_params_order_independent(self):
        key1 = make_dedup_key("v", "/ep", {"b": "2", "a": "1"}, b"")
        key2 = make_dedup_key("v", "/ep", {"a": "1", "b": "2"}, b"")
        assert key1 == key2

    def test_different_params_give_different_keys(self):
        key1 = make_dedup_key("v", "/ep", {"a": "1"}, b"")
        key2 = make_dedup_key("v", "/ep", {"a": "2"}, b"")
        assert key1 != key2

    def test_different_body_gives_different_keys(self):
        key1 = make_dedup_key("v", "/ep", {}, b"body-a")
        key2 = make_dedup_key("v", "/ep", {}, b"body-b")
        assert key1 != key2

    def test_none_params_treated_as_empty(self):
        key1 = make_dedup_key("v", "/ep", None, b"")
        key2 = make_dedup_key("v", "/ep", {}, b"")
        assert key1 == key2

    def test_none_body_treated_as_empty(self):
        key1 = make_dedup_key("v", "/ep", {}, None)
        key2 = make_dedup_key("v", "/ep", {}, b"")
        assert key1 == key2

    def test_string_body_same_as_bytes(self):
        key1 = make_dedup_key("v", "/ep", {}, "hello")
        key2 = make_dedup_key("v", "/ep", {}, b"hello")
        assert key1 == key2

    def test_different_vendor_gives_different_keys(self):
        key1 = make_dedup_key("vendor-a", "/ep", {}, b"")
        key2 = make_dedup_key("vendor-b", "/ep", {}, b"")
        assert key1 != key2

    def test_different_path_gives_different_keys(self):
        key1 = make_dedup_key("v", "/ep/a", {}, b"")
        key2 = make_dedup_key("v", "/ep/b", {}, b"")
        assert key1 != key2

    def test_deterministic(self):
        """Same inputs always produce the same key."""
        key1 = make_dedup_key("stripe", "/charges", {"limit": "5"}, b"body")
        key2 = make_dedup_key("stripe", "/charges", {"limit": "5"}, b"body")
        assert key1 == key2


# ---------------------------------------------------------------------------
# Serialisation round-trip
# ---------------------------------------------------------------------------


class TestSerialisation:
    def test_round_trip(self):
        original = _make_response()
        restored = _deserialise_result(_serialise_result(original))
        assert restored.status_code == original.status_code
        assert restored.headers == original.headers
        assert restored.body == original.body
        assert restored.cached_at == original.cached_at

    def test_binary_body_preserved(self):
        binary_body = bytes(range(256))
        resp = _make_response(body=binary_body)
        assert _deserialise_result(_serialise_result(resp)).body == binary_body


# ---------------------------------------------------------------------------
# _acquire_lock
# ---------------------------------------------------------------------------


class TestAcquireLock:
    async def test_returns_true_when_lock_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)
        result = await _acquire_lock(redis, "dedup:abc")
        assert result is True

    async def test_returns_false_when_lock_exists(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=None)
        result = await _acquire_lock(redis, "dedup:abc")
        assert result is False

    async def test_uses_nx_and_ex_flags(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)
        await _acquire_lock(redis, "dedup:abc")
        redis.set.assert_called_once_with("dedup:abc", "1", nx=True, ex=30)


# ---------------------------------------------------------------------------
# _release_lock
# ---------------------------------------------------------------------------


class TestReleaseLock:
    async def test_deletes_key(self):
        redis = _make_redis()
        await _release_lock(redis, "dedup:abc")
        redis.delete.assert_called_once_with("dedup:abc")


# ---------------------------------------------------------------------------
# dedup_publish
# ---------------------------------------------------------------------------


class TestDedupPublish:
    async def test_publishes_to_channel_named_after_key(self):
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc123"

        await dedup_publish(redis, key, response)

        redis.publish.assert_called_once()
        call_args = redis.publish.call_args
        assert call_args[0][0] == key

    async def test_published_payload_is_valid_json(self):
        redis = _make_redis()
        response = _make_response(status_code=201, body=b"hello")
        await dedup_publish(redis, "dedup:key", response)

        payload = redis.publish.call_args[0][1]
        data = json.loads(payload)
        assert data["status_code"] == 201
        assert bytes.fromhex(data["body"]) == b"hello"

    async def test_published_payload_includes_headers(self):
        redis = _make_redis()
        response = _make_response(headers={"x-custom": "value"})
        await dedup_publish(redis, "dedup:key", response)

        payload = redis.publish.call_args[0][1]
        data = json.loads(payload)
        assert data["headers"]["x-custom"] == "value"

    async def test_stores_result_in_redis_before_publishing(self):
        """dedup_publish must SET the result key before calling PUBLISH."""
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc123"

        call_order: list[str] = []
        redis.set = AsyncMock(side_effect=lambda *a, **kw: call_order.append("set"))
        redis.publish = AsyncMock(side_effect=lambda *a, **kw: call_order.append("publish"))

        await dedup_publish(redis, key, response)

        assert call_order == ["set", "publish"], (
            "SET must happen before PUBLISH to avoid the race condition"
        )

    async def test_stores_result_with_correct_key_and_ttl(self):
        """Result key is ``<key>:result`` with EX=60."""
        redis = _make_redis()
        response = _make_response(status_code=202, body=b"stored")
        key = "dedup:abc123"

        await dedup_publish(redis, key, response)

        redis.set.assert_called_once()
        set_args, set_kwargs = redis.set.call_args
        assert set_args[0] == _result_key(key)
        stored_data = json.loads(set_args[1])
        assert stored_data["status_code"] == 202
        assert bytes.fromhex(stored_data["body"]) == b"stored"
        assert set_kwargs.get("ex") == 60

    async def test_publish_and_set_use_same_payload(self):
        """The payload written to Redis and sent on pub/sub must be identical."""
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc123"

        await dedup_publish(redis, key, response)

        set_payload = redis.set.call_args[0][1]
        publish_payload = redis.publish.call_args[0][1]
        assert set_payload == publish_payload


# ---------------------------------------------------------------------------
# dedup_wait
# ---------------------------------------------------------------------------


def _make_pubsub_message(key: str, response: CachedResponse) -> dict:
    return {
        "type": "message",
        "channel": key,
        "data": _serialise_result(response),
    }


class TestDedupWait:
    async def test_returns_response_when_message_arrives(self):
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            side_effect=[
                None,  # First poll: no message yet
                _make_pubsub_message(key, response),  # Second poll: got it
            ]
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        result = await dedup_wait(redis, key, timeout=5.0)

        assert result is not None
        assert result.status_code == response.status_code
        assert result.body == response.body

    async def test_subscribes_to_correct_channel(self):
        redis = _make_redis()
        response = _make_response()
        key = "dedup:mykey"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            return_value=_make_pubsub_message(key, response)
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        await dedup_wait(redis, key, timeout=5.0)

        pubsub.subscribe.assert_called_once_with(key)

    async def test_unsubscribes_and_closes_on_success(self):
        redis = _make_redis()
        response = _make_response()
        key = "dedup:mykey"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            return_value=_make_pubsub_message(key, response)
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        await dedup_wait(redis, key, timeout=5.0)

        pubsub.unsubscribe.assert_called_once_with(key)
        pubsub.aclose.assert_called_once()

    async def test_returns_none_on_timeout(self):
        redis = _make_redis()
        key = "dedup:mykey"

        pubsub = AsyncMock()
        # Always return no message
        pubsub.get_message = AsyncMock(return_value=None)
        redis.pubsub = MagicMock(return_value=pubsub)

        result = await dedup_wait(redis, key, timeout=0.05)

        assert result is None

    async def test_unsubscribes_on_timeout(self):
        redis = _make_redis()
        key = "dedup:mykey"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(return_value=None)
        redis.pubsub = MagicMock(return_value=pubsub)

        await dedup_wait(redis, key, timeout=0.05)

        pubsub.unsubscribe.assert_called_once_with(key)
        pubsub.aclose.assert_called_once()

    async def test_ignores_non_message_type(self):
        """Subscribe confirmation messages (type='subscribe') are ignored."""
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            side_effect=[
                {"type": "subscribe", "channel": key, "data": 1},
                _make_pubsub_message(key, response),
            ]
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        result = await dedup_wait(redis, key, timeout=5.0)
        assert result is not None
        assert result.body == response.body

    async def test_handles_bytes_data(self):
        """pub/sub data may arrive as bytes when decode_responses=False."""
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc"

        payload_bytes = _serialise_result(response).encode()

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            return_value={"type": "message", "channel": key, "data": payload_bytes}
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        result = await dedup_wait(redis, key, timeout=5.0)
        assert result is not None
        assert result.body == response.body

    # ------------------------------------------------------------------
    # Fast-path: stored result already present (store-and-notify pattern)
    # ------------------------------------------------------------------

    async def test_returns_immediately_when_stored_result_found(self):
        """If the result key already exists in Redis, return without subscribing."""
        response = _make_response(status_code=200, body=b"fast")
        key = "dedup:abc"
        payload = _serialise_result(response)

        redis = _make_redis()
        redis.get = AsyncMock(return_value=payload)

        result = await dedup_wait(redis, key, timeout=5.0)

        assert result is not None
        assert result.body == b"fast"
        assert result.status_code == 200
        # pubsub should never have been used
        redis.pubsub.assert_not_called()

    async def test_stored_result_as_bytes_is_decoded(self):
        """Stored value may be raw bytes (decode_responses=False)."""
        response = _make_response(body=b"bytes-fast")
        key = "dedup:abc"
        payload_bytes = _serialise_result(response).encode()

        redis = _make_redis()
        redis.get = AsyncMock(return_value=payload_bytes)

        result = await dedup_wait(redis, key, timeout=5.0)

        assert result is not None
        assert result.body == b"bytes-fast"
        redis.pubsub.assert_not_called()

    async def test_checks_stored_result_key_with_correct_key(self):
        """GET must be issued against ``<key>:result``."""
        key = "dedup:abc"
        redis = _make_redis()
        # Return None so we fall through to pubsub
        response = _make_response()
        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            return_value=_make_pubsub_message(key, response)
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        await dedup_wait(redis, key, timeout=5.0)

        redis.get.assert_called_once_with(_result_key(key))

    async def test_falls_through_to_pubsub_when_no_stored_result(self):
        """When GET returns None, the normal pub/sub path is followed."""
        redis = _make_redis()
        response = _make_response()
        key = "dedup:abc"

        pubsub = AsyncMock()
        pubsub.get_message = AsyncMock(
            return_value=_make_pubsub_message(key, response)
        )
        redis.pubsub = MagicMock(return_value=pubsub)

        result = await dedup_wait(redis, key, timeout=5.0)

        assert result is not None
        assert result.body == response.body
        # pubsub was used because stored key was absent
        redis.pubsub.assert_called_once()


# ---------------------------------------------------------------------------
# dedup_context
# ---------------------------------------------------------------------------


class TestDedupContext:
    async def test_yields_true_when_lock_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)

        async with dedup_context(redis, "dedup:key") as acquired:
            assert acquired is True

    async def test_yields_false_when_lock_not_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=None)

        async with dedup_context(redis, "dedup:key") as acquired:
            assert acquired is False

    async def test_releases_lock_on_exit_when_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)

        async with dedup_context(redis, "dedup:key"):
            pass

        redis.delete.assert_called_once_with("dedup:key")

    async def test_does_not_release_lock_when_not_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=None)

        async with dedup_context(redis, "dedup:key"):
            pass

        redis.delete.assert_not_called()

    async def test_releases_lock_even_on_exception(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)

        with pytest.raises(ValueError):
            async with dedup_context(redis, "dedup:key"):
                raise ValueError("boom")

        redis.delete.assert_called_once_with("dedup:key")

    async def test_does_not_release_lock_on_exception_when_not_acquired(self):
        redis = _make_redis()
        redis.set = AsyncMock(return_value=None)

        with pytest.raises(ValueError):
            async with dedup_context(redis, "dedup:key"):
                raise ValueError("boom")

        redis.delete.assert_not_called()

    async def test_full_flow_lock_holder(self):
        """Simulate the lock-holder path: acquire -> publish -> release."""
        redis = _make_redis()
        redis.set = AsyncMock(return_value=True)
        response = _make_response()

        async with dedup_context(redis, "dedup:key") as acquired:
            assert acquired is True
            await dedup_publish(redis, "dedup:key", response)

        redis.publish.assert_called_once()
        redis.delete.assert_called_once_with("dedup:key")
