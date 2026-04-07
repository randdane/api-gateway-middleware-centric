"""Admin API routes — vendor CRUD, quota management, cache control, config reload, health.

All endpoints require a JWT with the 'admin' role via the `require_admin` dependency.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.admin.models import (
    ApiKeyQuotaUsage,
    CacheFlushResponse,
    ConfigReloadResponse,
    HealthResponse,
    QuotaUpdate,
    ServiceHealth,
    UsageStubResponse,
    VendorCreate,
    VendorQuotaResponse,
    VendorResponse,
    VendorUpdate,
)
from gateway.auth.dependencies import UserIdentity, require_admin
from gateway.cache.redis import get_redis
from gateway.cache.response_cache import flush_all, flush_vendor
from gateway.db.models import Vendor, VendorApiKey
from gateway.db.session import get_db
from gateway.quota.tracker import get_quota_usage
from gateway.vendors.registry import registry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


async def _get_vendor_or_404(db: AsyncSession, vendor_id: uuid.UUID) -> Vendor:
    result = await db.execute(select(Vendor).where(Vendor.id == vendor_id))
    vendor = result.scalar_one_or_none()
    if vendor is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vendor {vendor_id} not found",
        )
    return vendor


# ---------------------------------------------------------------------------
# Vendor CRUD
# ---------------------------------------------------------------------------


@router.get("/vendors", response_model=list[VendorResponse])
async def list_vendors(
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> list[VendorResponse]:
    """List all vendors (active and inactive)."""
    result = await db.execute(select(Vendor).order_by(Vendor.created_at))
    vendors = result.scalars().all()
    return [VendorResponse.model_validate(v) for v in vendors]


@router.post("/vendors", response_model=VendorResponse, status_code=status.HTTP_201_CREATED)
async def create_vendor(
    body: VendorCreate,
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorResponse:
    """Create a new vendor."""
    vendor = Vendor(
        name=body.name,
        slug=body.slug,
        base_url=body.base_url,
        auth_type=body.auth_type,
        auth_config=body.auth_config,
        cache_ttl_seconds=body.cache_ttl_seconds,
        rate_limit_rpm=body.rate_limit_rpm,
    )
    db.add(vendor)
    await db.commit()
    await db.refresh(vendor)
    logger.info("admin.vendor.created", vendor_id=str(vendor.id), slug=vendor.slug)
    return VendorResponse.model_validate(vendor)


@router.get("/vendors/{vendor_id}", response_model=VendorResponse)
async def get_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorResponse:
    """Get vendor details by ID."""
    vendor = await _get_vendor_or_404(db, vendor_id)
    return VendorResponse.model_validate(vendor)


@router.put("/vendors/{vendor_id}", response_model=VendorResponse)
async def update_vendor(
    vendor_id: uuid.UUID,
    body: VendorUpdate,
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorResponse:
    """Update vendor configuration."""
    vendor = await _get_vendor_or_404(db, vendor_id)

    update_data = body.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(vendor, field, value)

    await db.commit()
    await db.refresh(vendor)
    logger.info("admin.vendor.updated", vendor_id=str(vendor_id))
    return VendorResponse.model_validate(vendor)


@router.delete("/vendors/{vendor_id}", response_model=VendorResponse)
async def deactivate_vendor(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorResponse:
    """Deactivate a vendor (soft delete — sets is_active=False)."""
    vendor = await _get_vendor_or_404(db, vendor_id)
    vendor.is_active = False
    await db.commit()
    await db.refresh(vendor)
    logger.info("admin.vendor.deactivated", vendor_id=str(vendor_id), slug=vendor.slug)
    return VendorResponse.model_validate(vendor)


# ---------------------------------------------------------------------------
# Quota
# ---------------------------------------------------------------------------


@router.get("/vendors/{vendor_id}/quota", response_model=VendorQuotaResponse)
async def get_vendor_quota(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorQuotaResponse:
    """View quota config and current usage for all API keys of a vendor."""
    vendor = await _get_vendor_or_404(db, vendor_id)

    result = await db.execute(
        select(VendorApiKey).where(VendorApiKey.vendor_id == vendor_id)
    )
    api_keys = result.scalars().all()

    key_usages: list[ApiKeyQuotaUsage] = []
    for key in api_keys:
        current_usage = 0
        if key.quota_limit is not None and key.quota_period is not None:
            current_usage = await get_quota_usage(
                redis,
                str(vendor_id),
                str(key.id),
                key.quota_period,
            )
        key_usages.append(
            ApiKeyQuotaUsage(
                key_id=key.id,
                key_name=key.key_name,
                quota_limit=key.quota_limit,
                quota_period=key.quota_period,
                current_usage=current_usage,
                is_active=key.is_active,
            )
        )

    return VendorQuotaResponse(
        vendor_id=vendor_id,
        vendor_slug=vendor.slug,
        keys=key_usages,
    )


@router.put("/vendors/{vendor_id}/quota", response_model=VendorQuotaResponse)
async def update_vendor_quota(
    vendor_id: uuid.UUID,
    body: QuotaUpdate,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin: UserIdentity = Depends(require_admin),
) -> VendorQuotaResponse:
    """Adjust quota limits for a specific API key of a vendor."""
    vendor = await _get_vendor_or_404(db, vendor_id)

    key_result = await db.execute(
        select(VendorApiKey).where(
            VendorApiKey.id == body.key_id,
            VendorApiKey.vendor_id == vendor_id,
        )
    )
    api_key = key_result.scalar_one_or_none()
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key {body.key_id} not found for vendor {vendor_id}",
        )

    if body.quota_limit is not None:
        api_key.quota_limit = body.quota_limit
    if body.quota_period is not None:
        api_key.quota_period = body.quota_period

    await db.commit()
    await db.refresh(api_key)
    logger.info(
        "admin.vendor.quota_updated",
        vendor_id=str(vendor_id),
        key_id=str(body.key_id),
    )

    # Return the full quota view after update
    all_keys_result = await db.execute(
        select(VendorApiKey).where(VendorApiKey.vendor_id == vendor_id)
    )
    all_keys = all_keys_result.scalars().all()

    key_usages: list[ApiKeyQuotaUsage] = []
    for key in all_keys:
        current_usage = 0
        if key.quota_limit is not None and key.quota_period is not None:
            current_usage = await get_quota_usage(
                redis,
                str(vendor_id),
                str(key.id),
                key.quota_period,
            )
        key_usages.append(
            ApiKeyQuotaUsage(
                key_id=key.id,
                key_name=key.key_name,
                quota_limit=key.quota_limit,
                quota_period=key.quota_period,
                current_usage=current_usage,
                is_active=key.is_active,
            )
        )

    return VendorQuotaResponse(
        vendor_id=vendor_id,
        vendor_slug=vendor.slug,
        keys=key_usages,
    )


# ---------------------------------------------------------------------------
# Usage (stub)
# ---------------------------------------------------------------------------


@router.get("/vendors/{vendor_id}/usage", response_model=UsageStubResponse)
async def get_vendor_usage(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> UsageStubResponse:
    """Usage stats — stub until a metrics table is available.

    TODO: Replace with real metrics once a dedicated metrics/events table is
    added.  Will expose requests, errors, latency percentiles per vendor.
    """
    # Verify vendor exists so we return 404 for unknown IDs
    await _get_vendor_or_404(db, vendor_id)
    return UsageStubResponse(
        message="Usage stats not yet implemented — will be available once metrics are collected"
    )


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


@router.delete("/vendors/{vendor_id}/cache", response_model=CacheFlushResponse)
async def flush_vendor_cache(
    vendor_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin: UserIdentity = Depends(require_admin),
) -> CacheFlushResponse:
    """Flush the response cache for a specific vendor."""
    vendor = await _get_vendor_or_404(db, vendor_id)
    deleted = await flush_vendor(redis, vendor.slug)
    logger.info(
        "admin.cache.flushed_vendor",
        vendor_id=str(vendor_id),
        slug=vendor.slug,
        deleted=deleted,
    )
    return CacheFlushResponse(deleted=deleted, vendor_slug=vendor.slug)


@router.delete("/cache", response_model=CacheFlushResponse)
async def flush_all_caches(
    redis: Redis = Depends(get_redis),
    _admin: UserIdentity = Depends(require_admin),
) -> CacheFlushResponse:
    """Flush the entire response cache across all vendors."""
    deleted = await flush_all(redis)
    logger.info("admin.cache.flushed_all", deleted=deleted)
    return CacheFlushResponse(deleted=deleted)


# ---------------------------------------------------------------------------
# Config reload
# ---------------------------------------------------------------------------


@router.post("/config/reload", response_model=ConfigReloadResponse)
async def reload_config(
    db: AsyncSession = Depends(get_db),
    _admin: UserIdentity = Depends(require_admin),
) -> ConfigReloadResponse:
    """Reload vendor registry from DB and invalidate adapter cache."""
    await registry.load(db)
    registry.invalidate()
    vendor_count = len(registry.all_vendors())
    logger.info("admin.config.reloaded", vendor_count=vendor_count)
    return ConfigReloadResponse(
        reloaded=True,
        vendor_count=vendor_count,
        message=f"Registry reloaded — {vendor_count} active vendor(s) loaded",
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@router.get("/health", response_model=HealthResponse)
async def admin_health(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
    _admin: UserIdentity = Depends(require_admin),
) -> HealthResponse:
    """Detailed health check: Redis, Postgres, and vendor registry status."""
    services: dict[str, ServiceHealth] = {}
    overall = "ok"

    # Redis ping
    try:
        await redis.ping()
        services["redis"] = ServiceHealth(status="ok")
    except Exception as exc:
        services["redis"] = ServiceHealth(status="error", detail=str(exc))
        overall = "degraded"

    # Postgres — execute a trivial query
    try:
        await db.execute(text("SELECT 1"))
        services["postgres"] = ServiceHealth(status="ok")
    except Exception as exc:
        services["postgres"] = ServiceHealth(status="error", detail=str(exc))
        overall = "degraded"

    vendor_count = len(registry.all_vendors())

    return HealthResponse(
        status=overall,
        services=services,
        vendor_count=vendor_count,
    )
