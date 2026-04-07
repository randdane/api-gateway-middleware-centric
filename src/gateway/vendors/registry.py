import asyncio
import time
from dataclasses import dataclass, field

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.models import Vendor
from gateway.vendors.adapters import VendorAdapter, build_adapter
from gateway.vendors.secrets import SecretsProvider, default_provider

logger = structlog.get_logger(__name__)

_DEFAULT_REFRESH_INTERVAL = 60  # seconds


@dataclass
class VendorConfig:
    id: str
    name: str
    slug: str
    base_url: str
    auth_type: str
    auth_config: dict
    cache_ttl_seconds: int
    rate_limit_rpm: int
    is_active: bool


@dataclass
class VendorRegistry:
    """In-memory cache of active vendor configs, loaded from Postgres.

    Adapters are built lazily and cached; the whole registry is refreshed
    on a configurable interval.
    """

    refresh_interval: float = _DEFAULT_REFRESH_INTERVAL
    secrets: SecretsProvider = field(default_factory=lambda: default_provider)

    _vendors: dict[str, VendorConfig] = field(default_factory=dict)  # slug → config
    _adapters: dict[str, VendorAdapter] = field(default_factory=dict)  # slug → adapter
    _last_loaded: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _is_stale(self) -> bool:
        return (time.monotonic() - self._last_loaded) > self.refresh_interval

    async def load(self, session: AsyncSession) -> None:
        """(Re)load all active vendors from the database."""
        async with self._lock:
            result = await session.execute(
                select(Vendor).where(Vendor.is_active.is_(True))
            )
            vendors = result.scalars().all()

            new_configs: dict[str, VendorConfig] = {}
            for v in vendors:
                new_configs[v.slug] = VendorConfig(
                    id=str(v.id),
                    name=v.name,
                    slug=v.slug,
                    base_url=v.base_url,
                    auth_type=v.auth_type,
                    auth_config=v.auth_config,
                    cache_ttl_seconds=v.cache_ttl_seconds,
                    rate_limit_rpm=v.rate_limit_rpm,
                    is_active=v.is_active,
                )

            self._vendors = new_configs
            # Invalidate adapter cache for changed/removed vendors
            self._adapters = {}
            self._last_loaded = time.monotonic()
            logger.info("vendor_registry.loaded", count=len(self._vendors))

    async def reload_if_stale(self, session: AsyncSession) -> None:
        if self._is_stale():
            await self.load(session)

    def get(self, slug: str) -> VendorConfig | None:
        return self._vendors.get(slug)

    def get_adapter(self, slug: str) -> VendorAdapter | None:
        if slug not in self._vendors:
            return None
        if slug not in self._adapters:
            config = self._vendors[slug]
            self._adapters[slug] = build_adapter(
                config.auth_type, config.auth_config, self.secrets
            )
        return self._adapters[slug]

    def get_by_id(self, vendor_id: str) -> VendorConfig | None:
        """Return a VendorConfig by its UUID string, or None."""
        for config in self._vendors.values():
            if config.id == vendor_id:
                return config
        return None

    def get_adapter_by_id(self, vendor_id: str):
        """Return the adapter for the vendor with the given UUID string, or None."""
        config = self.get_by_id(vendor_id)
        if config is None:
            return None
        return self.get_adapter(config.slug)

    def all_vendors(self) -> list[VendorConfig]:
        return list(self._vendors.values())

    def invalidate(self, slug: str | None = None) -> None:
        """Evict one vendor (or all) from the adapter cache to force rebuild."""
        if slug is None:
            self._adapters.clear()
            self._last_loaded = 0.0
        else:
            self._adapters.pop(slug, None)
            self._last_loaded = 0.0


# Module-level singleton — initialized in app lifespan
registry = VendorRegistry()
