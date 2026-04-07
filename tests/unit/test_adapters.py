"""Unit tests for all VendorAdapter implementations."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from gateway.vendors.adapters import build_adapter
from gateway.vendors.adapters.api_key import ApiKeyAdapter
from gateway.vendors.adapters.basic import BasicAuthAdapter
from gateway.vendors.adapters.custom import CustomHeaderAdapter
from gateway.vendors.adapters.oauth2 import OAuth2ClientCredentialsAdapter
from gateway.vendors.secrets import SecretsProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class DictSecretsProvider(SecretsProvider):
    def __init__(self, values: dict[str, str]) -> None:
        self._values = values

    async def get(self, ref: str) -> str:
        if ref not in self._values:
            raise KeyError(ref)
        return self._values[ref]


def _request(url: str = "https://vendor.example.com/api/data") -> httpx.Request:
    return httpx.Request("GET", url)


# ---------------------------------------------------------------------------
# ApiKeyAdapter
# ---------------------------------------------------------------------------

class TestApiKeyAdapter:
    @pytest.mark.asyncio
    async def test_injects_header(self):
        secrets = DictSecretsProvider({"MY_KEY": "secret-value"})
        adapter = ApiKeyAdapter(
            {"header_name": "X-Api-Key", "key_reference": "MY_KEY"}, secrets
        )
        result = await adapter.prepare_request(_request())
        assert result.headers["X-Api-Key"] == "secret-value"

    @pytest.mark.asyncio
    async def test_injects_query_param(self):
        secrets = DictSecretsProvider({"MY_KEY": "secret-value"})
        adapter = ApiKeyAdapter(
            {"query_param": "api_key", "key_reference": "MY_KEY"}, secrets
        )
        result = await adapter.prepare_request(_request())
        assert result.url.params["api_key"] == "secret-value"

    @pytest.mark.asyncio
    async def test_preserves_existing_query_params(self):
        secrets = DictSecretsProvider({"MY_KEY": "k"})
        adapter = ApiKeyAdapter(
            {"query_param": "api_key", "key_reference": "MY_KEY"}, secrets
        )
        result = await adapter.prepare_request(_request("https://v.example.com/?foo=bar"))
        assert result.url.params["foo"] == "bar"
        assert result.url.params["api_key"] == "k"

    def test_missing_header_and_query_raises(self):
        with pytest.raises(ValueError, match="header_name.*query_param"):
            ApiKeyAdapter({"key_reference": "K"})

    @pytest.mark.asyncio
    async def test_missing_secret_raises(self):
        secrets = DictSecretsProvider({})
        adapter = ApiKeyAdapter({"header_name": "X-Key", "key_reference": "MISSING"}, secrets)
        with pytest.raises(KeyError):
            await adapter.prepare_request(_request())


# ---------------------------------------------------------------------------
# BasicAuthAdapter
# ---------------------------------------------------------------------------

class TestBasicAuthAdapter:
    @pytest.mark.asyncio
    async def test_injects_basic_auth_header(self):
        import base64
        secrets = DictSecretsProvider({"U": "alice", "P": "s3cr3t"})
        adapter = BasicAuthAdapter({"username_ref": "U", "password_ref": "P"}, secrets)
        result = await adapter.prepare_request(_request())
        auth = result.headers["Authorization"]
        assert auth.startswith("Basic ")
        decoded = base64.b64decode(auth[6:]).decode()
        assert decoded == "alice:s3cr3t"

    @pytest.mark.asyncio
    async def test_missing_secret_raises(self):
        secrets = DictSecretsProvider({"U": "alice"})
        adapter = BasicAuthAdapter({"username_ref": "U", "password_ref": "MISSING"}, secrets)
        with pytest.raises(KeyError):
            await adapter.prepare_request(_request())


# ---------------------------------------------------------------------------
# CustomHeaderAdapter
# ---------------------------------------------------------------------------

class TestCustomHeaderAdapter:
    @pytest.mark.asyncio
    async def test_injects_multiple_headers(self):
        secrets = DictSecretsProvider({"T_REF": "tok123", "TENANT_REF": "acme"})
        adapter = CustomHeaderAdapter(
            {"headers": {"X-Token": "T_REF", "X-Tenant": "TENANT_REF"}}, secrets
        )
        result = await adapter.prepare_request(_request())
        assert result.headers["X-Token"] == "tok123"
        assert result.headers["X-Tenant"] == "acme"

    def test_empty_headers_raises(self):
        with pytest.raises(ValueError, match="at least one entry"):
            CustomHeaderAdapter({"headers": {}})

    @pytest.mark.asyncio
    async def test_missing_secret_raises(self):
        secrets = DictSecretsProvider({})
        adapter = CustomHeaderAdapter({"headers": {"X-Key": "MISSING_REF"}}, secrets)
        with pytest.raises(KeyError):
            await adapter.prepare_request(_request())


# ---------------------------------------------------------------------------
# OAuth2ClientCredentialsAdapter
# ---------------------------------------------------------------------------

class TestOAuth2Adapter:
    def _adapter(self, secrets=None) -> OAuth2ClientCredentialsAdapter:
        if secrets is None:
            secrets = DictSecretsProvider({"CID": "client-id", "CSEC": "client-secret"})
        return OAuth2ClientCredentialsAdapter(
            {
                "token_url": "https://auth.example.com/token",
                "client_id_ref": "CID",
                "client_secret_ref": "CSEC",
                "scopes": ["read", "write"],
            },
            secrets,
        )

    def _mock_token_response(self, access_token="tok", expires_in=3600):
        resp = MagicMock(spec=httpx.Response)
        resp.json.return_value = {"access_token": access_token, "expires_in": expires_in}
        resp.raise_for_status = MagicMock()
        return resp

    @pytest.mark.asyncio
    async def test_injects_bearer_token(self):
        adapter = self._adapter()
        with patch("gateway.vendors.adapters.oauth2.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post = AsyncMock(return_value=self._mock_token_response("my-token"))

            result = await adapter.prepare_request(_request())

        assert result.headers["Authorization"] == "Bearer my-token"

    @pytest.mark.asyncio
    async def test_token_cached_on_second_call(self):
        adapter = self._adapter()
        with patch("gateway.vendors.adapters.oauth2.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post = AsyncMock(return_value=self._mock_token_response("tok1"))

            await adapter.prepare_request(_request())
            await adapter.prepare_request(_request())

        # Token fetch should only happen once
        assert mock_http.post.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_token_is_refreshed(self):
        adapter = self._adapter()
        with patch("gateway.vendors.adapters.oauth2.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post = AsyncMock(return_value=self._mock_token_response("tok1", expires_in=1))

            await adapter.prepare_request(_request())
            # Force expiry
            adapter._expires_at = time.monotonic() - 1

            mock_http.post.return_value = self._mock_token_response("tok2")
            result = await adapter.prepare_request(_request())

        assert result.headers["Authorization"] == "Bearer tok2"
        assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_refresh_credentials_forces_fetch(self):
        adapter = self._adapter()
        with patch("gateway.vendors.adapters.oauth2.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post = AsyncMock(return_value=self._mock_token_response("tok"))

            # Pre-warm cache
            await adapter.prepare_request(_request())
            assert mock_http.post.call_count == 1

            # Force refresh
            await adapter.refresh_credentials()
            assert mock_http.post.call_count == 2

    @pytest.mark.asyncio
    async def test_scopes_sent_in_token_request(self):
        adapter = self._adapter()
        with patch("gateway.vendors.adapters.oauth2.httpx.AsyncClient") as mock_cls:
            mock_http = AsyncMock()
            mock_cls.return_value.__aenter__.return_value = mock_http
            mock_http.post = AsyncMock(return_value=self._mock_token_response())

            await adapter.prepare_request(_request())

        call_kwargs = mock_http.post.call_args
        sent_data = call_kwargs[1]["data"]
        assert sent_data["scope"] == "read write"
        assert sent_data["grant_type"] == "client_credentials"


# ---------------------------------------------------------------------------
# build_adapter factory
# ---------------------------------------------------------------------------

class TestBuildAdapter:
    def test_builds_api_key(self):
        a = build_adapter("api_key", {"header_name": "X-Key", "key_reference": "R"})
        assert isinstance(a, ApiKeyAdapter)

    def test_builds_basic(self):
        a = build_adapter("basic", {"username_ref": "U", "password_ref": "P"})
        assert isinstance(a, BasicAuthAdapter)

    def test_builds_oauth2(self):
        a = build_adapter("oauth2", {
            "token_url": "https://x.com/token",
            "client_id_ref": "C",
            "client_secret_ref": "S",
        })
        assert isinstance(a, OAuth2ClientCredentialsAdapter)

    def test_builds_custom(self):
        a = build_adapter("custom", {"headers": {"X-H": "REF"}})
        assert isinstance(a, CustomHeaderAdapter)

    def test_unknown_type_raises(self):
        with pytest.raises(ValueError, match="Unknown auth_type"):
            build_adapter("magic", {})
