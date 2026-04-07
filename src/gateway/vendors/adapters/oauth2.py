import asyncio
import time

import httpx
import structlog

from gateway.vendors.adapters.base import VendorAdapter
from gateway.vendors.secrets import SecretsProvider, default_provider

logger = structlog.get_logger(__name__)

# Refresh the token this many seconds before it actually expires
_EXPIRY_BUFFER_SECONDS = 30


class OAuth2ClientCredentialsAdapter(VendorAdapter):
    """Manages the OAuth2 client_credentials token lifecycle.

    Fetches a token on first use, caches it in memory, and refreshes it
    proactively before expiry.

    Config fields:
        token_url:         Full URL of the token endpoint.
        client_id_ref:     Secret ref for the OAuth2 client ID.
        client_secret_ref: Secret ref for the OAuth2 client secret.
        scopes:            Optional list of scope strings.
    """

    def __init__(
        self,
        config: dict,
        secrets: SecretsProvider = default_provider,
    ) -> None:
        self._token_url: str = config["token_url"]
        self._client_id_ref: str = config["client_id_ref"]
        self._client_secret_ref: str = config["client_secret_ref"]
        self._scopes: list[str] = config.get("scopes", [])
        self._secrets = secrets

        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._lock = asyncio.Lock()

    def _is_expired(self) -> bool:
        return time.monotonic() >= (self._expires_at - _EXPIRY_BUFFER_SECONDS)

    async def _fetch_token(self) -> None:
        client_id = await self._secrets.get(self._client_id_ref)
        client_secret = await self._secrets.get(self._client_secret_ref)

        data: dict[str, str] = {
            "grant_type": "client_credentials",
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if self._scopes:
            data["scope"] = " ".join(self._scopes)

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(self._token_url, data=data)
            resp.raise_for_status()
            token_data = resp.json()

        self._access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in", 3600))
        self._expires_at = time.monotonic() + expires_in
        logger.info(
            "oauth2.token_fetched",
            token_url=self._token_url,
            expires_in=expires_in,
        )

    async def _ensure_token(self) -> str:
        if self._access_token is None or self._is_expired():
            async with self._lock:
                # Re-check after acquiring lock (another coroutine may have refreshed)
                if self._access_token is None or self._is_expired():
                    await self._fetch_token()
        assert self._access_token is not None
        return self._access_token

    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        token = await self._ensure_token()
        headers = dict(request.headers)
        headers["Authorization"] = f"Bearer {token}"
        return httpx.Request(
            request.method, request.url, headers=headers, content=request.content
        )

    async def refresh_credentials(self) -> None:
        """Force a token refresh regardless of expiry state."""
        async with self._lock:
            await self._fetch_token()
