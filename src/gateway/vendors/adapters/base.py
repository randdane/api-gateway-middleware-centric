from abc import ABC, abstractmethod

import httpx


class VendorAdapter(ABC):
    """Abstract base for all vendor authentication adapters.

    Each concrete adapter knows how to inject credentials into an outgoing
    httpx.Request for one authentication pattern (API key, OAuth2, etc.).
    """

    @abstractmethod
    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        """Inject vendor-specific auth into the outgoing request.

        Must return the (potentially mutated) request object.
        """
        ...

    async def refresh_credentials(self) -> None:
        """Refresh tokens or credentials if needed.

        Called proactively before credential expiry (e.g., OAuth2 access
        tokens). Default implementation is a no-op for static-credential
        adapters.
        """
