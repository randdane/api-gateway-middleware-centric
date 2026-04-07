"""Unit tests for gateway/auth/jwt.py — all JWKS calls are mocked."""

import time
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from gateway.auth.jwt import AuthError, JWKSCache, _cache, verify_token
from tests.conftest import make_token


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jwks_from_pem(public_pem: str, kid: str = "key-1") -> dict:
    """Build a minimal JWKS dict from a PEM public key."""
    from jose import jwk as jose_jwk
    key_dict = jose_jwk.construct(public_pem, algorithm="RS256").to_dict()
    key_dict["kid"] = kid
    key_dict["use"] = "sig"
    return {"keys": [key_dict]}


# ---------------------------------------------------------------------------
# JWKSCache unit tests
# ---------------------------------------------------------------------------

class TestJWKSCache:
    def test_is_stale_initially(self):
        cache = JWKSCache()
        assert cache.is_stale()

    def test_not_stale_after_update(self):
        cache = JWKSCache()
        cache.update({"keys": []})
        assert not cache.is_stale()

    def test_update_indexes_keys_by_kid(self, rsa_public_pem):
        cache = JWKSCache()
        jwks = _jwks_from_pem(rsa_public_pem, kid="k1")
        cache.update(jwks)
        assert "k1" in cache.keys

    def test_update_ignores_keys_without_kid(self):
        cache = JWKSCache()
        cache.update({"keys": [{"kty": "RSA", "use": "sig"}]})
        assert cache.keys == {}


# ---------------------------------------------------------------------------
# verify_token — happy path
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verify_valid_token(rsa_private_pem, rsa_public_pem):
    jwks = _jwks_from_pem(rsa_public_pem, kid="key-1")
    token = make_token(rsa_private_pem, sub="alice", roles=["user"])

    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value=jwks)):
        # Force cache stale so fetch is triggered
        _cache.fetched_at = 0
        claims = await verify_token(token)

    assert claims["sub"] == "alice"
    assert claims["roles"] == ["user"]


@pytest.mark.asyncio
async def test_verify_uses_cached_keys(rsa_private_pem, rsa_public_pem):
    jwks = _jwks_from_pem(rsa_public_pem, kid="key-1")
    token = make_token(rsa_private_pem, sub="bob")

    fetch_mock = AsyncMock(return_value=jwks)
    with patch("gateway.auth.jwt._fetch_jwks", new=fetch_mock):
        _cache.fetched_at = 0
        await verify_token(token)
        call_count_after_first = fetch_mock.call_count

        # Second call — cache is warm, no extra fetch
        await verify_token(token)

    assert fetch_mock.call_count == call_count_after_first


# ---------------------------------------------------------------------------
# verify_token — failure cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_expired_token_raises(rsa_private_pem, rsa_public_pem):
    jwks = _jwks_from_pem(rsa_public_pem, kid="key-1")
    token = make_token(rsa_private_pem, expired=True)

    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value=jwks)):
        _cache.fetched_at = 0
        with pytest.raises(AuthError, match="expired"):
            await verify_token(token)


@pytest.mark.asyncio
async def test_invalid_signature_raises(rsa_private_pem, rsa_public_pem):
    # Sign with one key, verify with a different public key
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization
    other_public_pem = other_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()

    token = make_token(rsa_private_pem, kid="key-1")
    wrong_jwks = _jwks_from_pem(other_public_pem, kid="key-1")

    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value=wrong_jwks)):
        _cache.fetched_at = 0
        with pytest.raises(AuthError, match="verification failed"):
            await verify_token(token)


@pytest.mark.asyncio
async def test_malformed_token_raises():
    with pytest.raises(AuthError, match="Malformed"):
        await verify_token("not.a.jwt")


@pytest.mark.asyncio
async def test_unknown_kid_retries_then_raises(rsa_private_pem, rsa_public_pem):
    # JWKS has "key-2" but token requests "key-1"
    jwks_wrong_kid = _jwks_from_pem(rsa_public_pem, kid="key-2")
    token = make_token(rsa_private_pem, kid="key-1")

    fetch_mock = AsyncMock(return_value=jwks_wrong_kid)
    with patch("gateway.auth.jwt._fetch_jwks", new=fetch_mock):
        _cache.fetched_at = 0
        with pytest.raises(AuthError, match="Unknown signing key"):
            await verify_token(token)

    # Should have fetched twice: initial load + forced retry
    assert fetch_mock.call_count == 2


@pytest.mark.asyncio
async def test_empty_jwks_raises(rsa_private_pem):
    token = make_token(rsa_private_pem)
    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value={"keys": []})):
        _cache.fetched_at = 0
        _cache.keys = {}
        with pytest.raises(AuthError, match="no keys"):
            await verify_token(token)


@pytest.mark.asyncio
async def test_token_without_kid_uses_first_key(rsa_private_pem, rsa_public_pem):
    """Tokens with no kid header should fall back to the first available key."""
    from jose import jwt as jose_jwt

    claims = {"sub": "svc", "roles": [], "iat": int(time.time()), "exp": int(time.time()) + 3600}
    # Encode without kid in header
    token = jose_jwt.encode(claims, rsa_private_pem, algorithm="RS256")

    jwks = _jwks_from_pem(rsa_public_pem, kid="only-key")

    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value=jwks)):
        _cache.fetched_at = 0
        result = await verify_token(token)

    assert result["sub"] == "svc"
