"""Token bucket rate limiting middleware and dependency.

Architecture:
- ``RateLimitMiddleware`` — Starlette middleware; runs *before* routing.
  Handles per-vendor rate limiting only (user identity is not yet available
  at middleware time because JWT auth is a FastAPI dependency).
- ``check_user_rate_limit`` — async FastAPI dependency; runs *after* JWT auth
  resolves the caller's identity.  Handles per-user and per-user-per-vendor
  checks.

Token bucket algorithm is implemented as a Redis Lua script for atomicity.
"""

from __future__ import annotations

import math
import re
import time

import structlog
from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.cache.redis import get_client
from gateway.config import settings
from gateway.vendors.registry import VendorRegistry, registry as default_registry

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Lua script — token bucket (atomic read-modify-write in Redis)
# ---------------------------------------------------------------------------

_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill_rate = tonumber(ARGV[2])  -- tokens per second
local now = tonumber(ARGV[3])
local tokens_requested = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'last_refill')
local tokens = tonumber(state[1]) or capacity
local last_refill = tonumber(state[2]) or now

-- Refill tokens based on elapsed time
local elapsed = now - last_refill
local new_tokens = math.min(capacity, tokens + elapsed * refill_rate)

if new_tokens >= tokens_requested then
    new_tokens = new_tokens - tokens_requested
    redis.call('HSET', key, 'tokens', new_tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 1)
    return 1  -- allowed
else
    redis.call('HSET', key, 'tokens', new_tokens, 'last_refill', now)
    redis.call('EXPIRE', key, math.ceil(capacity / refill_rate) + 1)
    return 0  -- denied
end
"""

# ---------------------------------------------------------------------------
# URL pattern for extracting vendor slug
# ---------------------------------------------------------------------------

# Matches /vendors/{slug}/... or /v1/{slug}/... style paths.
_VENDOR_SLUG_RE = re.compile(r"^/(?:vendors|v1)/([^/]+)")


def _extract_vendor_slug(path: str) -> str | None:
    """Return the vendor slug embedded in the URL path, or None."""
    m = _VENDOR_SLUG_RE.match(path)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Core check function
# ---------------------------------------------------------------------------


async def check_rate_limit(
    redis: Redis,
    key: str,
    capacity_rpm: int,
    scope: str,
) -> tuple[bool, int]:
    """Run the token bucket Lua script and return ``(allowed, retry_after_seconds)``.

    ``retry_after_seconds`` is 0 when ``allowed`` is True; it is an estimate of
    how many seconds until one token is available when ``allowed`` is False.
    """
    capacity = float(capacity_rpm)
    refill_rate = capacity_rpm / 60.0  # tokens per second
    now = time.time()

    result = await redis.eval(  # type: ignore[attr-defined]
        _TOKEN_BUCKET_LUA,
        1,  # number of keys
        key,
        capacity,
        refill_rate,
        now,
        1,  # tokens requested
    )

    allowed = bool(result)
    if allowed:
        return True, 0

    # Estimate seconds until 1 token refills
    retry_after = math.ceil(1.0 / refill_rate)
    return False, retry_after


# ---------------------------------------------------------------------------
# Middleware — per-vendor rate limiting
# ---------------------------------------------------------------------------


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Apply per-vendor rate limits at middleware time.

    User identity is not yet available here (JWT auth is a FastAPI dependency
    that runs after middleware), so only vendor-level checks are performed.

    Pass-through:
    - Requests without a recognisable vendor slug in the URL path are passed
      through unchanged (e.g. /health, /docs).
    - If Redis is unavailable the request is passed through (fail-open) and an
      error is logged.
    """

    def __init__(
        self,
        app: ASGIApp,
        redis: Redis | None = None,
        registry: VendorRegistry = default_registry,
    ) -> None:
        super().__init__(app)
        # Allow injecting a Redis client (useful for tests); fall back to the
        # module-level pool-backed client at request time.
        self._redis = redis
        self._registry = registry

    def _get_redis(self) -> Redis:
        return self._redis if self._redis is not None else get_client()

    async def dispatch(self, request: Request, call_next) -> Response:
        slug = _extract_vendor_slug(request.url.path)
        if slug is None:
            return await call_next(request)

        redis = self._get_redis()
        key = f"rl:vendor:{slug}"

        vendor_config = self._registry.get(slug)
        vendor_rpm = vendor_config.rate_limit_rpm if vendor_config else settings.rate_limit_vendor_rpm

        try:
            allowed, retry_after = await check_rate_limit(
                redis,
                key,
                vendor_rpm,
                scope="vendor",
            )
        except Exception:
            logger.exception(
                "rate_limit.redis_error",
                scope="vendor",
                slug=slug,
            )
            return await call_next(request)

        if not allowed:
            return _rate_limit_response(scope="vendor", retry_after=retry_after)

        return await call_next(request)


# ---------------------------------------------------------------------------
# FastAPI dependency — per-user (and per-user-per-vendor) rate limiting
# ---------------------------------------------------------------------------


async def check_user_rate_limit(
    request: Request,
    user: UserIdentity = Depends(get_current_user),
    redis: Redis = Depends(get_client),
) -> None:
    """FastAPI dependency that enforces per-user and per-user-per-vendor limits.

    Raises ``HTTPException(429)`` if either limit is exceeded.

    Call after ``get_current_user`` — typically via ``Depends(check_user_rate_limit)``.
    """
    user_id = user.sub
    slug = _extract_vendor_slug(request.url.path)

    # Per-user global check
    user_key = f"rl:user:{user_id}"
    try:
        allowed, retry_after = await check_rate_limit(
            redis,
            user_key,
            settings.rate_limit_user_rpm,
            scope="user",
        )
    except Exception:
        logger.exception("rate_limit.redis_error", scope="user", user_id=user_id)
        return  # fail-open

    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "rate_limit_exceeded",
                "scope": "user",
                "retry_after": retry_after,
            },
            headers={"Retry-After": str(retry_after)},
        )

    # Per-user-per-vendor check (only when a vendor slug is present)
    if slug is not None:
        uv_key = f"rl:user:{user_id}:vendor:{slug}"
        try:
            uv_allowed, uv_retry = await check_rate_limit(
                redis,
                uv_key,
                settings.rate_limit_user_rpm,  # same RPM cap; can be overridden later
                scope="user_vendor",
            )
        except Exception:
            logger.exception(
                "rate_limit.redis_error",
                scope="user_vendor",
                user_id=user_id,
                slug=slug,
            )
            return  # fail-open

        if not uv_allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "rate_limit_exceeded",
                    "scope": "user_vendor",
                    "retry_after": uv_retry,
                },
                headers={"Retry-After": str(uv_retry)},
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rate_limit_response(*, scope: str, retry_after: int) -> JSONResponse:
    body = {
        "error": "rate_limit_exceeded",
        "scope": scope,
        "retry_after": retry_after,
    }
    return JSONResponse(
        content=body,
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )
