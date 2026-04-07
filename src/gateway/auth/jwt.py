import time
from dataclasses import dataclass, field

import httpx
import structlog
from jose import JWTError, jwk, jwt
from jose.exceptions import ExpiredSignatureError, JWTClaimsError

from gateway.config import settings

logger = structlog.get_logger(__name__)


class AuthError(Exception):
    """Raised when JWT verification fails."""

    def __init__(self, message: str, status_code: int = 401) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass
class JWKSCache:
    keys: dict = field(default_factory=dict)  # kid → JWK key object
    fetched_at: float = 0.0

    def is_stale(self) -> bool:
        return (time.monotonic() - self.fetched_at) > settings.jwks_cache_ttl_seconds

    def update(self, jwks: dict) -> None:
        self.keys = {}
        for key_data in jwks.get("keys", []):
            kid = key_data.get("kid")
            if kid:
                self.keys[kid] = jwk.construct(key_data)
        self.fetched_at = time.monotonic()
        logger.info("jwks.refreshed", key_count=len(self.keys))


_cache = JWKSCache()


async def _fetch_jwks() -> dict:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(settings.jwks_url)
        resp.raise_for_status()
        return resp.json()


async def get_signing_key(kid: str | None) -> object:
    """Return the JWK key object for the given kid, refreshing cache if needed."""
    if _cache.is_stale():
        jwks = await _fetch_jwks()
        _cache.update(jwks)

    if not _cache.keys:
        raise AuthError("JWKS endpoint returned no keys")

    if kid is None:
        # No kid in token — use first available key (single-key issuers)
        return next(iter(_cache.keys.values()))

    key = _cache.keys.get(kid)
    if key is None:
        # kid not in cache — try a forced refresh once
        jwks = await _fetch_jwks()
        _cache.update(jwks)
        key = _cache.keys.get(kid)
        if key is None:
            raise AuthError(f"Unknown signing key: kid={kid!r}")

    return key


async def verify_token(token: str) -> dict:
    """Verify a JWT and return its claims dict.

    Raises AuthError on any verification failure.
    """
    try:
        # Peek at the header to get kid without full verification
        unverified_header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise AuthError(f"Malformed token header: {exc}") from exc

    kid = unverified_header.get("kid")
    key = await get_signing_key(kid)

    decode_options: dict = {}
    if settings.jwt_audience:
        decode_options["audience"] = settings.jwt_audience
    if settings.jwt_issuer:
        decode_options["issuer"] = settings.jwt_issuer

    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=settings.jwt_algorithms,
            options=decode_options,
        )
    except ExpiredSignatureError as exc:
        raise AuthError("Token has expired") from exc
    except JWTClaimsError as exc:
        raise AuthError(f"Invalid token claims: {exc}") from exc
    except JWTError as exc:
        raise AuthError(f"Token verification failed: {exc}") from exc

    return claims
