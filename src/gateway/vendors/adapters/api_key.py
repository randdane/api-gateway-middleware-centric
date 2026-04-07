import httpx

from gateway.vendors.adapters.base import VendorAdapter
from gateway.vendors.secrets import SecretsProvider, default_provider


class ApiKeyAdapter(VendorAdapter):
    """Injects a static API key into a request header or query parameter.

    Config fields:
        header_name:   Name of the HTTP header to set (e.g. "X-Api-Key").
                       Mutually exclusive with query_param.
        query_param:   Name of the query parameter to set (e.g. "api_key").
                       Mutually exclusive with header_name.
        key_reference: Secret ref resolved via SecretsProvider.
    """

    def __init__(
        self,
        config: dict,
        secrets: SecretsProvider = default_provider,
    ) -> None:
        self._header_name: str | None = config.get("header_name")
        self._query_param: str | None = config.get("query_param")
        self._key_ref: str = config["key_reference"]
        self._secrets = secrets

        if not self._header_name and not self._query_param:
            raise ValueError("ApiKeyAdapter requires 'header_name' or 'query_param' in config")

    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        key = await self._secrets.get(self._key_ref)

        if self._header_name:
            headers = dict(request.headers)
            headers[self._header_name] = key
            return httpx.Request(
                request.method, request.url, headers=headers, content=request.content
            )

        # query_param path — merge into existing URL params
        params = dict(request.url.params)
        params[self._query_param] = key  # type: ignore[index]
        url = request.url.copy_with(params=params)
        return httpx.Request(
            request.method, url, headers=request.headers, content=request.content
        )
