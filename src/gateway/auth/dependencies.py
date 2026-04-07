from dataclasses import dataclass, field

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from gateway.auth.jwt import AuthError, verify_token

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
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> UserIdentity:
    """Extract and verify the Bearer JWT, returning the caller's identity.

    Raises 401 if the token is missing or invalid.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        claims = await verify_token(credentials.credentials)
    except AuthError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    # Extract roles — support both "roles" and "realm_access.roles" claim shapes
    roles: list[str] = claims.get("roles", [])
    if not roles:
        roles = claims.get("realm_access", {}).get("roles", [])

    # Heuristic: service accounts often lack an email claim
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
