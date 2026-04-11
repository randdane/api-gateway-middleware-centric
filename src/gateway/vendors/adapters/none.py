import httpx

from gateway.vendors.adapters.base import VendorAdapter


class NoAuthAdapter(VendorAdapter):
    """Pass-through adapter for vendors that require no authentication."""

    def __init__(self, config: dict, **_) -> None:
        pass

    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        return request
