import httpx
import structlog

from gateway.vendors.adapters import VendorAdapter
from gateway.vendors.registry import VendorConfig

logger = structlog.get_logger(__name__)


class VendorClient:
    """Thin httpx AsyncClient wrapper that injects vendor auth via the adapter.

    One VendorClient is created per request; it does not own the adapter
    (the registry manages adapter lifecycle).
    """

    def __init__(self, config: VendorConfig, adapter: VendorAdapter) -> None:
        self._config = config
        self._adapter = adapter

    async def request(
        self,
        method: str,
        path: str,
        *,
        timeout: float | None = None,
        **kwargs,
    ) -> httpx.Response:
        """Make an authenticated request to the vendor.

        Args:
            method:  HTTP method string (GET, POST, …).
            path:    Path relative to the vendor's base_url.
            timeout: Per-request timeout override (seconds).
            **kwargs: Passed through to httpx.AsyncClient.request().
        """
        url = self._config.base_url.rstrip("/") + "/" + path.lstrip("/")
        effective_timeout = timeout or 30.0

        async with httpx.AsyncClient(timeout=effective_timeout) as http:
            # Build an unsigned request so the adapter can mutate it
            request = http.build_request(method, url, **kwargs)
            request = await self._adapter.prepare_request(request)

            logger.debug(
                "vendor.request",
                vendor=self._config.slug,
                method=method,
                url=str(request.url),
            )

            response = await http.send(request)

        logger.info(
            "vendor.response",
            vendor=self._config.slug,
            method=method,
            status=response.status_code,
            url=str(response.url),
        )

        return response
