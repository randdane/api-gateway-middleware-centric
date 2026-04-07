"""Unit tests for gateway.middleware.rate_limit.

All Redis calls are mocked — no running Redis required.
"""

from __future__ import annotations

import math
import re
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.responses import Response

from gateway.middleware.rate_limit import (
    RateLimitMiddleware,
    _TOKEN_BUCKET_LUA,
    _extract_vendor_slug,
    _rate_limit_response,
    check_rate_limit,
    check_user_rate_limit,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis(eval_return: int = 1) -> AsyncMock:
    """Return an AsyncMock Redis client whose ``eval`` returns *eval_return*."""
    redis = AsyncMock()
    redis.eval = AsyncMock(return_value=eval_return)
    return redis


# ---------------------------------------------------------------------------
# _extract_vendor_slug
# ---------------------------------------------------------------------------


class TestExtractVendorSlug:
    def test_vendors_prefix(self):
        assert _extract_vendor_slug("/vendors/stripe/charges") == "stripe"

    def test_v1_prefix(self):
        assert _extract_vendor_slug("/v1/stripe/charges") == "stripe"

    def test_slug_only(self):
        assert _extract_vendor_slug("/vendors/acme") == "acme"

    def test_no_match_health(self):
        assert _extract_vendor_slug("/health") is None

    def test_no_match_root(self):
        assert _extract_vendor_slug("/") is None

    def test_no_match_docs(self):
        assert _extract_vendor_slug("/docs") is None

    def test_slug_with_hyphens(self):
        assert _extract_vendor_slug("/vendors/my-vendor/endpoint") == "my-vendor"

    def test_slug_with_underscores(self):
        assert _extract_vendor_slug("/v1/my_vendor/path") == "my_vendor"

    def test_empty_path(self):
        assert _extract_vendor_slug("") is None


# ---------------------------------------------------------------------------
# check_rate_limit
# ---------------------------------------------------------------------------


class TestCheckRateLimit:
    async def test_allowed_when_lua_returns_1(self):
        redis = _make_redis(eval_return=1)
        allowed, retry_after = await check_rate_limit(redis, "rl:vendor:x", 600, "vendor")
        assert allowed is True
        assert retry_after == 0

    async def test_denied_when_lua_returns_0(self):
        redis = _make_redis(eval_return=0)
        allowed, retry_after = await check_rate_limit(redis, "rl:vendor:x", 600, "vendor")
        assert allowed is False
        assert retry_after > 0

    async def test_retry_after_is_ceil_of_one_over_refill_rate(self):
        redis = _make_redis(eval_return=0)
        capacity_rpm = 60  # 1 token/s → retry_after = 1
        _, retry_after = await check_rate_limit(redis, "rl:k", capacity_rpm, "vendor")
        expected = math.ceil(1.0 / (capacity_rpm / 60.0))
        assert retry_after == expected

    async def test_retry_after_rounds_up(self):
        redis = _make_redis(eval_return=0)
        # 600 rpm → 10 tokens/s → 1/10 = 0.1 → ceil = 1
        _, retry_after = await check_rate_limit(redis, "rl:k", 600, "vendor")
        assert retry_after == 1

    async def test_lua_called_with_correct_args(self):
        redis = _make_redis(eval_return=1)
        key = "rl:vendor:stripe"
        capacity_rpm = 1000

        with patch("gateway.middleware.rate_limit.time") as mock_time:
            mock_time.time.return_value = 1234567890.5
            await check_rate_limit(redis, key, capacity_rpm, "vendor")

        redis.eval.assert_called_once()
        call_args = redis.eval.call_args
        # positional: (script, num_keys, key, capacity, refill_rate, now, tokens_requested)
        args = call_args[0]
        assert args[0] == _TOKEN_BUCKET_LUA  # script
        assert args[1] == 1                  # num_keys
        assert args[2] == key                # KEYS[1]
        assert args[3] == float(capacity_rpm)
        assert abs(args[4] - capacity_rpm / 60.0) < 1e-9  # refill_rate
        assert args[5] == 1234567890.5       # now
        assert args[6] == 1                  # tokens_requested

    async def test_capacity_passed_as_float(self):
        redis = _make_redis(eval_return=1)
        await check_rate_limit(redis, "rl:k", 300, "vendor")
        args = redis.eval.call_args[0]
        assert isinstance(args[3], float)

    async def test_different_scopes_use_same_logic(self):
        for scope in ("vendor", "user", "user_vendor"):
            redis = _make_redis(eval_return=1)
            allowed, _ = await check_rate_limit(redis, "rl:k", 60, scope)
            assert allowed is True


# ---------------------------------------------------------------------------
# Lua script content sanity checks
# ---------------------------------------------------------------------------


class TestLuaScript:
    def test_script_references_hmget(self):
        assert "HMGET" in _TOKEN_BUCKET_LUA

    def test_script_references_hmset(self):
        assert "HMSET" in _TOKEN_BUCKET_LUA

    def test_script_references_expire(self):
        assert "EXPIRE" in _TOKEN_BUCKET_LUA

    def test_script_returns_1_on_allow(self):
        assert "return 1" in _TOKEN_BUCKET_LUA

    def test_script_returns_0_on_deny(self):
        assert "return 0" in _TOKEN_BUCKET_LUA

    def test_script_uses_min_for_refill_cap(self):
        assert "math.min" in _TOKEN_BUCKET_LUA


# ---------------------------------------------------------------------------
# _rate_limit_response
# ---------------------------------------------------------------------------


class TestRateLimitResponse:
    def test_status_code_429(self):
        resp = _rate_limit_response(scope="vendor", retry_after=5)
        assert resp.status_code == 429

    def test_retry_after_header(self):
        resp = _rate_limit_response(scope="user", retry_after=10)
        assert resp.headers["Retry-After"] == "10"

    def test_json_body_contains_scope(self):
        import json

        resp = _rate_limit_response(scope="vendor", retry_after=3)
        body = json.loads(resp.body)
        assert body["scope"] == "vendor"
        assert body["error"] == "rate_limit_exceeded"
        assert body["retry_after"] == 3

    def test_all_scopes(self):
        for scope in ("vendor", "user", "user_vendor"):
            resp = _rate_limit_response(scope=scope, retry_after=1)
            import json

            body = json.loads(resp.body)
            assert body["scope"] == scope


# ---------------------------------------------------------------------------
# RateLimitMiddleware
# ---------------------------------------------------------------------------


def _make_app(redis: AsyncMock) -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.get("/vendors/{slug}/endpoint")
    async def vendor_endpoint(slug: str):
        return {"slug": slug}

    @app.get("/v1/{slug}/resource")
    async def v1_endpoint(slug: str):
        return {"slug": slug}

    app.add_middleware(RateLimitMiddleware, redis=redis)
    return app


class TestRateLimitMiddleware:
    def test_health_path_passes_through(self):
        redis = _make_redis(eval_return=0)  # would deny if checked
        app = _make_app(redis)
        client = TestClient(app, raise_server_exceptions=True)
        resp = client.get("/health")
        assert resp.status_code == 200
        redis.eval.assert_not_called()

    def test_vendor_path_allowed(self):
        redis = _make_redis(eval_return=1)
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/vendors/stripe/endpoint")
        assert resp.status_code == 200

    def test_vendor_path_denied_returns_429(self):
        redis = _make_redis(eval_return=0)
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/vendors/stripe/endpoint")
        assert resp.status_code == 429

    def test_429_response_has_retry_after_header(self):
        redis = _make_redis(eval_return=0)
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/vendors/stripe/endpoint")
        assert "Retry-After" in resp.headers

    def test_429_body_has_scope_vendor(self):
        redis = _make_redis(eval_return=0)
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/vendors/stripe/endpoint")
        body = resp.json()
        assert body["scope"] == "vendor"
        assert body["error"] == "rate_limit_exceeded"

    def test_v1_path_checked(self):
        redis = _make_redis(eval_return=0)
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/v1/stripe/resource")
        assert resp.status_code == 429

    def test_redis_key_contains_vendor_slug(self):
        redis = _make_redis(eval_return=1)
        app = _make_app(redis)
        client = TestClient(app)
        client.get("/vendors/my-vendor/endpoint")
        redis.eval.assert_called_once()
        key_arg = redis.eval.call_args[0][2]  # KEYS[1]
        assert "my-vendor" in key_arg

    def test_redis_key_format(self):
        redis = _make_redis(eval_return=1)
        app = _make_app(redis)
        client = TestClient(app)
        client.get("/vendors/stripe/endpoint")
        key_arg = redis.eval.call_args[0][2]
        assert key_arg == "rl:vendor:stripe"

    def test_redis_error_passes_through(self):
        """When Redis raises, the request should still be served (fail-open)."""
        redis = AsyncMock()
        redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
        app = _make_app(redis)
        client = TestClient(app)
        resp = client.get("/vendors/stripe/endpoint")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# check_user_rate_limit (dependency)
# ---------------------------------------------------------------------------


class TestCheckUserRateLimitDependency:
    async def test_raises_429_when_user_limit_exceeded(self):
        from gateway.auth.dependencies import UserIdentity

        redis = _make_redis(eval_return=0)  # deny
        user = UserIdentity(sub="user-abc")

        mock_request = MagicMock()
        mock_request.url.path = "/vendors/stripe/charges"

        with pytest.raises(Exception) as exc_info:
            await check_user_rate_limit(request=mock_request, user=user, redis=redis)

        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["scope"] == "user"

    async def test_does_not_raise_when_allowed(self):
        from gateway.auth.dependencies import UserIdentity

        redis = _make_redis(eval_return=1)  # allow
        user = UserIdentity(sub="user-abc")

        mock_request = MagicMock()
        mock_request.url.path = "/vendors/stripe/charges"

        # Should not raise
        await check_user_rate_limit(request=mock_request, user=user, redis=redis)

    async def test_user_key_format(self):
        from gateway.auth.dependencies import UserIdentity

        redis = _make_redis(eval_return=1)
        user = UserIdentity(sub="user-xyz")

        mock_request = MagicMock()
        mock_request.url.path = "/health"  # no vendor slug

        await check_user_rate_limit(request=mock_request, user=user, redis=redis)

        # Only one eval call (no vendor slug in path)
        redis.eval.assert_called_once()
        key_arg = redis.eval.call_args[0][2]
        assert key_arg == "rl:user:user-xyz"

    async def test_user_vendor_key_checked_when_slug_present(self):
        from gateway.auth.dependencies import UserIdentity

        redis = _make_redis(eval_return=1)
        user = UserIdentity(sub="user-123")

        mock_request = MagicMock()
        mock_request.url.path = "/vendors/stripe/charges"

        await check_user_rate_limit(request=mock_request, user=user, redis=redis)

        assert redis.eval.call_count == 2
        keys = [call[0][2] for call in redis.eval.call_args_list]
        assert "rl:user:user-123" in keys
        assert "rl:user:user-123:vendor:stripe" in keys

    async def test_user_vendor_scope_in_429_detail(self):
        from gateway.auth.dependencies import UserIdentity

        call_count = 0

        async def _eval_side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            # Allow user check (first call), deny user_vendor check (second)
            return 1 if call_count == 1 else 0

        redis = AsyncMock()
        redis.eval = AsyncMock(side_effect=_eval_side_effect)
        user = UserIdentity(sub="user-123")

        mock_request = MagicMock()
        mock_request.url.path = "/vendors/stripe/charges"

        with pytest.raises(Exception) as exc_info:
            await check_user_rate_limit(request=mock_request, user=user, redis=redis)

        assert exc_info.value.status_code == 429
        assert exc_info.value.detail["scope"] == "user_vendor"

    async def test_retry_after_header_in_exception(self):
        from gateway.auth.dependencies import UserIdentity

        redis = _make_redis(eval_return=0)
        user = UserIdentity(sub="u")

        mock_request = MagicMock()
        mock_request.url.path = "/health"

        with pytest.raises(Exception) as exc_info:
            await check_user_rate_limit(request=mock_request, user=user, redis=redis)

        assert "Retry-After" in exc_info.value.headers

    async def test_redis_error_is_fail_open(self):
        """When Redis raises during user check, the dependency returns without error."""
        from gateway.auth.dependencies import UserIdentity

        redis = AsyncMock()
        redis.eval = AsyncMock(side_effect=ConnectionError("redis down"))
        user = UserIdentity(sub="u")

        mock_request = MagicMock()
        mock_request.url.path = "/vendors/stripe/charges"

        # Should not raise
        await check_user_rate_limit(request=mock_request, user=user, redis=redis)
