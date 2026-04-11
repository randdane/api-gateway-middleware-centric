from dataclasses import dataclass, field

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth.jwt import AuthError, verify_token
from gateway.auth.portal_tokens import PortalTokenValidator
from gateway.auth.tokens import is_portal_token
from gateway.cache.redis import get_client

_bearer = HTTPBearer(auto_error=False)


@dataclass
class UserIdentity:
    sub: str                          # subject — user or service account ID
    email: str | None = None
    roles: list[str] = field(default_factory=list)
    is_service_account: bool = False
    raw_claims: dict = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.email or self.sub


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserIdentity:
    """Extract and verify the Bearer token, returning the caller's identity.

    Two authentication paths:
    - Tokens starting with "tok_" are portal-issued opaque tokens. They are
      validated by calling the portal's /api/tokens/validate endpoint (with
      the result cached in Redis for portal_token_cache_ttl seconds).
    - All other tokens are treated as JWTs and verified against the JWKS
      endpoint (existing service-account flow — unchanged).

    Raises 401 if the token is missing or invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials

    if is_portal_token(token):
        return await _validate_portal_token(request, token)

    return await _validate_jwt(token)


async def _validate_portal_token(request: Request, token: str) -> UserIdentity:
    """Validate a portal-issued opaque token and build a UserIdentity."""
    http_client: httpx.AsyncClient | None = getattr(
        request.app.state, "http_client", None
    )
    if http_client is None:
        # Fallback: create a one-shot client (less efficient, for robustness)
        http_client = httpx.AsyncClient(timeout=5.0)

    redis_client = get_client()
    try:
        validator = PortalTokenValidator(redis_client, http_client)
        result = await validator.validate(token)
    finally:
        await redis_client.aclose()

    if result is None or not result.get("valid"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked portal token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    role = result.get("role", "user")
    return UserIdentity(
        sub=result["user_id"],
        email=result.get("email"),
        roles=[role],
        is_service_account=False,
        raw_claims=result,
    )


async def _validate_jwt(token: str) -> UserIdentity:
    """Validate a JWT via JWKS — the existing service-account path."""
    try:
        claims = await verify_token(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    roles: list[str] = claims.get("roles", [])
    if not roles:
        roles = claims.get("realm_access", {}).get("roles", [])

    is_service_account = "email" not in claims

    return UserIdentity(
        sub=claims["sub"],
        email=claims.get("email"),
        roles=roles,
        is_service_account=is_service_account,
        raw_claims=claims,
    )


async def require_admin(
    user: UserIdentity = Depends(get_current_user),
) -> UserIdentity:
    """Dependency that requires the caller to hold the 'admin' role.

    Raises 403 if the role is absent.
    """
    if "admin" not in user.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return user
