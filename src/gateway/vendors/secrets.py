import os
from abc import ABC, abstractmethod


class SecretsProvider(ABC):
    """Abstract interface for retrieving secrets by reference key."""

    @abstractmethod
    async def get(self, ref: str) -> str:
        """Return the secret value for the given reference key.

        Raises KeyError if the ref cannot be resolved.
        """
        ...


class EnvSecretsProvider(SecretsProvider):
    """Resolves secret refs directly from environment variables.

    Intended for local development only. In production, swap this for a
    Vault or AWS Secrets Manager implementation.
    """

    async def get(self, ref: str) -> str:
        value = os.environ.get(ref)
        if value is None:
            raise KeyError(f"Secret not found in environment: {ref!r}")
        return value


# Default provider used by adapters when none is explicitly injected.
default_provider: SecretsProvider = EnvSecretsProvider()
