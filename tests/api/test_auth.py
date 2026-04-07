"""Integration tests for auth dependencies via FastAPI TestClient."""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from gateway.auth.dependencies import UserIdentity, get_current_user, require_admin
from gateway.auth.jwt import _cache
from gateway.main import create_app
from tests.unit.test_jwt import _jwks_from_pem


# ---------------------------------------------------------------------------
# App with test routes wired up
# ---------------------------------------------------------------------------

def build_test_app(rsa_public_pem: str):
    app = create_app()

    @app.get("/test/me")
    async def me(user: UserIdentity = Depends(get_current_user)):
        return {"sub": user.sub, "roles": user.roles, "email": user.email}

    @app.get("/test/admin")
    async def admin_only(user: UserIdentity = Depends(require_admin)):
        return {"sub": user.sub}

    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def jwks(rsa_public_pem):
    return _jwks_from_pem(rsa_public_pem, kid="key-1")


@pytest.fixture()
def client(rsa_public_pem, jwks):
    app = build_test_app(rsa_public_pem)
    with patch("gateway.auth.jwt._fetch_jwks", new=AsyncMock(return_value=jwks)):
        _cache.fetched_at = 0
        with TestClient(app, raise_server_exceptions=True) as c:
            yield c


# ---------------------------------------------------------------------------
# get_current_user tests
# ---------------------------------------------------------------------------

def test_valid_token_returns_identity(client, token_factory):
    token = token_factory(sub="alice", roles=["user"])
    resp = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["sub"] == "alice"
    assert data["roles"] == ["user"]
    assert data["email"] == "alice@example.com"


def test_missing_auth_header_returns_401(client):
    resp = client.get("/test/me")
    assert resp.status_code == 401


def test_malformed_token_returns_401(client):
    resp = client.get("/test/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_expired_token_returns_401(client, token_factory):
    token = token_factory(expired=True)
    resp = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()


def test_invalid_signature_returns_401(client, rsa_private_pem, rsa_public_pem):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa as _rsa
    from tests.conftest import make_token

    other_key = _rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    token = make_token(other_pem, kid="key-1")
    resp = client.get("/test/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# require_admin tests
# ---------------------------------------------------------------------------

def test_admin_role_grants_access(client, token_factory):
    token = token_factory(sub="admin-user", roles=["admin", "user"])
    resp = client.get("/test/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["sub"] == "admin-user"


def test_missing_admin_role_returns_403(client, token_factory):
    token = token_factory(sub="plain-user", roles=["user"])
    resp = client.get("/test/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403
    assert "Admin role required" in resp.json()["detail"]


def test_unauthenticated_admin_route_returns_401(client):
    resp = client.get("/test/admin")
    assert resp.status_code == 401


def test_realm_access_roles_supported(client, rsa_private_pem, rsa_public_pem):
    """Keycloak-style realm_access.roles claim should be recognised."""
    from tests.conftest import make_token
    token = make_token(
        rsa_private_pem,
        sub="kc-admin",
        roles=[],
        extra_claims={"realm_access": {"roles": ["admin"]}},
    )
    resp = client.get("/test/admin", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
