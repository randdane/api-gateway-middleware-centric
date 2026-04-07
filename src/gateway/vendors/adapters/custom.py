import httpx

from gateway.vendors.adapters.base import VendorAdapter
from gateway.vendors.secrets import SecretsProvider, default_provider


class CustomHeaderAdapter(VendorAdapter):
    """Injects arbitrary headers whose values are resolved via SecretsProvider.

    Config fields:
        headers: dict mapping header name → secret ref.
                 e.g. {"X-Tenant-Id": "TENANT_ID_REF", "X-Api-Token": "TOKEN_REF"}
    """

    def __init__(
        self,
        config: dict,
        secrets: SecretsProvider = default_provider,
    ) -> None:
        self._header_refs: dict[str, str] = config.get("headers", {})
        self._secrets = secrets

        if not self._header_refs:
            raise ValueError("CustomHeaderAdapter requires at least one entry in 'headers'")

    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        extra: dict[str, str] = {}
        for header_name, ref in self._header_refs.items():
            extra[header_name] = await self._secrets.get(ref)

        headers = dict(request.headers)
        headers.update(extra)
        return httpx.Request(
            request.method, request.url, headers=headers, content=request.content
        )
