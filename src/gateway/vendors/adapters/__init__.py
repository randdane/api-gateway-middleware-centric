from gateway.vendors.adapters.api_key import ApiKeyAdapter
from gateway.vendors.adapters.base import VendorAdapter
from gateway.vendors.adapters.basic import BasicAuthAdapter
from gateway.vendors.adapters.custom import CustomHeaderAdapter
from gateway.vendors.adapters.none import NoAuthAdapter
from gateway.vendors.adapters.oauth2 import OAuth2ClientCredentialsAdapter
from gateway.vendors.secrets import SecretsProvider

_ADAPTER_MAP = {
    "api_key": ApiKeyAdapter,
    "oauth2": OAuth2ClientCredentialsAdapter,
    "basic": BasicAuthAdapter,
    "custom": CustomHeaderAdapter,
    "none": NoAuthAdapter,
}


def build_adapter(
    auth_type: str,
    auth_config: dict,
    secrets: SecretsProvider | None = None,
) -> VendorAdapter:
    """Instantiate the correct VendorAdapter for the given auth_type."""
    cls = _ADAPTER_MAP.get(auth_type)
    if cls is None:
        raise ValueError(f"Unknown auth_type: {auth_type!r}. Expected one of {list(_ADAPTER_MAP)}")
    kwargs: dict = {"config": auth_config}
    if secrets is not None:
        kwargs["secrets"] = secrets
    return cls(**kwargs)


__all__ = [
    "VendorAdapter",
    "ApiKeyAdapter",
    "BasicAuthAdapter",
    "CustomHeaderAdapter",
    "NoAuthAdapter",
    "OAuth2ClientCredentialsAdapter",
    "build_adapter",
]
