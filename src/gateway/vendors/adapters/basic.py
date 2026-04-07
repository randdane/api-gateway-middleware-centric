import base64

import httpx

from gateway.vendors.adapters.base import VendorAdapter
from gateway.vendors.secrets import SecretsProvider, default_provider


class BasicAuthAdapter(VendorAdapter):
    """Injects HTTP Basic Auth credentials into the Authorization header.

    Config fields:
        username_ref: Secret ref for the username.
        password_ref: Secret ref for the password.
    """

    def __init__(
        self,
        config: dict,
        secrets: SecretsProvider = default_provider,
    ) -> None:
        self._username_ref: str = config["username_ref"]
        self._password_ref: str = config["password_ref"]
        self._secrets = secrets

    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        username = await self._secrets.get(self._username_ref)
        password = await self._secrets.get(self._password_ref)

        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        headers = dict(request.headers)
        headers["Authorization"] = f"Basic {credentials}"
        return httpx.Request(
            request.method, request.url, headers=headers, content=request.content
        )
