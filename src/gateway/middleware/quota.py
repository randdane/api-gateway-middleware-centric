"""Quota check FastAPI dependency.

Usage (in route handler):
    from gateway.middleware.quota import check_quota_dependency

    @router.get("/vendors/{vendor_slug}/keys/{key_name}/proxy")
    async def proxy(
        vendor_slug: str,
        key_name: str,
        _: None = Depends(check_quota_dependency),
    ):
        ...

The dependency performs the *pre-request* quota check only.  The caller
(Phase 5.1 proxy route) is responsible for calling ``increment_quota()`` after
a successful upstream response.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.cache.redis import get_redis
from gateway.db.models import Vendor, VendorApiKey
from gateway.db.session import get_db
from gateway.quota.models import QuotaExceededResponse
from gateway.quota.tracker import check_quota, period_bucket

logger = structlog.get_logger(__name__)


def _resets_at(period: str) -> datetime:
    """Return the UTC datetime at which the current period resets.

    - daily:   start of next UTC day
    - monthly: start of next UTC month (1st at 00:00:00 UTC)
    """
    now = datetime.now(tz=timezone.utc)
    if period == "daily":
        tomorrow = now.date() + timedelta(days=1)
        return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=timezone.utc)
    if period == "monthly":
        # Advance to first day of next month
        if now.month == 12:
            return datetime(now.year + 1, 1, 1, tzinfo=timezone.utc)
        return datetime(now.year, now.month + 1, 1, tzinfo=timezone.utc)
    raise ValueError(f"Unknown quota period: {period!r}")


async def check_quota_dependency(
    vendor_slug: str,
    key_name: str,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> None:
    """FastAPI dependency: pre-request quota check.

    Path parameters ``vendor_slug`` and ``key_name`` must be present in the
    route that declares this dependency (they are resolved automatically by
    FastAPI from the path).

    Raises:
        HTTPException(404): if the vendor or API key is not found / inactive.
        HTTPException(429): if the quota for the current period is exhausted.
    """
    # ------------------------------------------------------------------
    # 1. Load the VendorApiKey record (join through Vendor for the slug)
    # ------------------------------------------------------------------
    stmt = (
        select(VendorApiKey)
        .join(Vendor, VendorApiKey.vendor_id == Vendor.id)
        .where(
            Vendor.slug == vendor_slug,
            VendorApiKey.key_name == key_name,
            VendorApiKey.is_active.is_(True),
            Vendor.is_active.is_(True),
        )
    )
    result = await db.execute(stmt)
    api_key: VendorApiKey | None = result.scalar_one_or_none()

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"API key '{key_name}' not found for vendor '{vendor_slug}'.",
        )

    # ------------------------------------------------------------------
    # 2. Skip quota check if no limit is configured
    # ------------------------------------------------------------------
    if api_key.quota_limit is None or api_key.quota_period is None:
        return

    limit: int = api_key.quota_limit
    period: str = api_key.quota_period
    vendor_id = str(api_key.vendor_id)
    key_id = str(api_key.id)

    # ------------------------------------------------------------------
    # 3. Check the quota counter in Redis
    # ------------------------------------------------------------------
    try:
        allowed, current_count = await check_quota(
            redis, vendor_id, key_id, limit, period
        )
    except Exception:
        # Fail-open: if Redis is unavailable, allow the request through
        # and log the error so ops can investigate.
        logger.exception(
            "quota.redis_error",
            vendor_slug=vendor_slug,
            key_name=key_name,
            vendor_id=vendor_id,
            key_id=key_id,
        )
        return

    if allowed:
        return

    # ------------------------------------------------------------------
    # 4. Quota exceeded — build 429 response body
    # ------------------------------------------------------------------
    resets_at = _resets_at(period)

    # Load vendor name for the response body (already joined above, but we
    # need the slug/name from the Vendor row).
    vendor_result = await db.execute(
        select(Vendor).where(Vendor.slug == vendor_slug)
    )
    vendor: Vendor | None = vendor_result.scalar_one_or_none()
    vendor_name = vendor.slug if vendor else vendor_slug

    body = QuotaExceededResponse(
        error="quota_exceeded",
        vendor=vendor_name,
        key=key_name,
        limit=limit,
        used=current_count,
        period=period,
        resets_at=resets_at,
    )

    logger.warning(
        "quota.exceeded",
        vendor_slug=vendor_slug,
        key_name=key_name,
        limit=limit,
        used=current_count,
        period=period,
        resets_at=resets_at.isoformat(),
    )

    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=body.model_dump(mode="json"),
    )
