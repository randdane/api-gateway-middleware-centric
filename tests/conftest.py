"""Shared test fixtures."""

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt


@pytest.fixture(scope="session")
def rsa_private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def rsa_public_key(rsa_private_key):
    return rsa_private_key.public_key()


@pytest.fixture(scope="session")
def rsa_private_pem(rsa_private_key):
    return rsa_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()


@pytest.fixture(scope="session")
def rsa_public_pem(rsa_public_key):
    return rsa_public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def make_token(
    private_pem: str,
    *,
    sub: str = "user-123",
    roles: list[str] | None = None,
    kid: str = "key-1",
    expired: bool = False,
    extra_claims: dict | None = None,
) -> str:
    now = int(time.time())
    claims = {
        "sub": sub,
        "email": f"{sub}@example.com",
        "roles": roles or [],
        "iat": now,
        "exp": now - 60 if expired else now + 3600,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture(scope="session")
def token_factory(rsa_private_pem):
    """Return a callable that mints tokens signed with the session RSA key."""
    def _factory(**kwargs) -> str:
        return make_token(rsa_private_pem, **kwargs)
    return _factory
