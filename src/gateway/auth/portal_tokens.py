"""Opaque portal-token validation with Redis caching.

When the gateway receives a bearer token that starts with "tok_", it is a
portal-issued opaque token rather than a JWT. This module handles validation
by calling the portal's /api/tokens/validate endpoint (authenticated with an
HMAC-SHA256 signature) and caching the result in Redis.

Cache strategy
--------------
Key:   portal_token:{sha256(plain_token)}
Value: JSON-encoded ValidateResponse {"valid", "user_id", "email", "role"}
TTL:   settings.portal_token_cache_ttl seconds (default 60)

The cache key uses the token hash, not the plaintext, so raw tokens never
sit in Redis. On a cache hit, the portal is not contacted. On revocation,
the stale cached entry may survive up to one TTL window (60 s by default)
before the gateway starts returning 401.

HMAC signing
------------
Every POST to the portal's validate endpoint is signed with a canonical
string over {timestamp, method, path, sha256(body)} using
HMAC-SHA256(PORTAL_SHARED_SECRET). The portal rejects requests outside a
30-second window and any whose recomputed signature does not match.
"""

import hashlib
import hmac
import json
import time

import httpx
import structlog
from redis.asyncio import Redis

from gateway.config import settings

logger = structlog.get_logger(__name__)

_VALIDATE_PATH = "/api/tokens/validate"
_CACHE_KEY_PREFIX = "portal_token:"
TIMESTAMP_HEADER = "X-Portal-Timestamp"
SIGNATURE_HEADER = "X-Portal-Signature"


def _cache_key(plain: str) -> str:
    token_hash = hashlib.sha256(plain.encode("utf-8")).hexdigest()
    return f"{_CACHE_KEY_PREFIX}{token_hash}"


def _canonical_string(timestamp: str, method: str, path: str, body: bytes) -> str:
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{timestamp}\n{method.upper()}\n{path}\n{body_hash}"


def _sign(secret: str, canonical: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        canonical.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _build_headers(body: bytes) -> dict[str, str]:
    ts = str(int(time.time()))
    canonical = _canonical_string(ts, "POST", _VALIDATE_PATH, body)
    signature = _sign(settings.portal_shared_secret, canonical)
    return {
        TIMESTAMP_HEADER: ts,
        SIGNATURE_HEADER: signature,
        "Content-Type": "application/json",
    }


class PortalTokenValidator:
    """Validates portal-issued opaque tokens with Redis caching.

    Args:
        redis: An async Redis client (connection borrowed from the pool).
        http_client: A shared async httpx client (created in app lifespan).
    """

    def __init__(self, redis: Redis, http_client: httpx.AsyncClient) -> None:
        self._redis = redis
        self._http = http_client

    async def validate(self, plain_token: str) -> dict | None:
        """Return the cached or freshly-fetched validation payload, or None.

        Returns a dict with keys {valid, user_id, email, role} if the token
        is valid, or None if it is invalid, revoked, expired, or if the
        portal call fails.
        """
        key = _cache_key(plain_token)

        # --- cache hit ---
        cached = await self._redis.get(key)
        if cached is not None:
            try:
                data = json.loads(cached)
                if data.get("valid"):
                    return data
            except json.JSONDecodeError:
                pass
            # invalid/revoked cached entry — no need to re-ask the portal
            return None

        # --- cache miss: call the portal ---
        body = json.dumps({"token": plain_token}).encode("utf-8")
        headers = _build_headers(body)
        url = f"{settings.portal_url.rstrip('/')}{_VALIDATE_PATH}"

        try:
            response = await self._http.post(url, content=body, headers=headers)
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as exc:
            logger.warning("portal_token.validate.http_error", error=str(exc))
            return None

        ttl = settings.portal_token_cache_ttl
        if data.get("valid"):
            await self._redis.set(key, json.dumps(data), ex=ttl)
            return data
        else:
            # Cache negative results briefly to avoid hammering the portal with
            # probes for invalid tokens.
            await self._redis.set(key, json.dumps({"valid": False}), ex=min(ttl, 10))
            return None
