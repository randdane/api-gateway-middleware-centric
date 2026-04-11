"""Token-format helpers for the gateway auth path."""

_PORTAL_TOKEN_PREFIX = "tok_"


def is_portal_token(token: str) -> bool:
    """Return True if the token is a portal-issued opaque token.

    Portal tokens are identified by the "tok_" prefix. All other bearer
    tokens are treated as JWTs and routed through the existing JWKS path.
    """
    return token.startswith(_PORTAL_TOKEN_PREFIX)
