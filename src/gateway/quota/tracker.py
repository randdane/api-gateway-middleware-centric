"""Quota tracking: Redis counters with periodic sync to Postgres.

Counter key format:  quota:{vendor_id}:{key_id}:{period_bucket}

Period buckets:
  - daily:   "2026-04-07"   (YYYY-MM-DD)
  - monthly: "2026-04"      (YYYY-MM)

TTLs:
  - daily:   86400 s  (24 h)
  - monthly: 2678400 s (~31 days)

Enforcement flow:
  1. Pre-request:  check_quota()  → if used >= limit, caller raises 429
  2. Post-request: increment_quota() on success
  3. Background:   sync_quota_to_db() writes Redis counts to Postgres

NOTE (Phase 6 TODO): sync_quota_to_db currently only logs the sync attempt.
A dedicated ``quota_usage`` table will be added in Phase 6 (Admin API) once
the usage-query endpoints are designed, at which point this stub should be
replaced with a real upsert.
"""

from __future__ import annotations

from datetime import datetime, timezone

import structlog
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# TTL constants
# ---------------------------------------------------------------------------

DAILY_TTL: int = 86_400       # 24 hours in seconds
MONTHLY_TTL: int = 2_678_400  # ~31 days in seconds


# ---------------------------------------------------------------------------
# Period helpers
# ---------------------------------------------------------------------------


def period_bucket(period: str, dt: datetime) -> str:
    """Return the period bucket string for *dt*.

    Args:
        period: "daily" or "monthly"
        dt:     The datetime to bucket (should be UTC-aware or naive UTC).

    Returns:
        "YYYY-MM-DD" for daily, "YYYY-MM" for monthly.

    Raises:
        ValueError: if *period* is not "daily" or "monthly".
    """
    if period == "daily":
        return dt.strftime("%Y-%m-%d")
    if period == "monthly":
        return dt.strftime("%Y-%m")
    raise ValueError(f"Unknown quota period: {period!r}. Expected 'daily' or 'monthly'.")


def period_ttl(period: str) -> int:
    """Return the Redis TTL in seconds for the given period.

    Args:
        period: "daily" or "monthly"

    Returns:
        86400 for daily, 2678400 for monthly.

    Raises:
        ValueError: if *period* is unknown.
    """
    if period == "daily":
        return DAILY_TTL
    if period == "monthly":
        return MONTHLY_TTL
    raise ValueError(f"Unknown quota period: {period!r}. Expected 'daily' or 'monthly'.")


def quota_key(vendor_id: str, key_id: str, bucket: str) -> str:
    """Build the Redis key for a quota counter.

    Format: ``quota:{vendor_id}:{key_id}:{bucket}``
    """
    return f"quota:{vendor_id}:{key_id}:{bucket}"


# ---------------------------------------------------------------------------
# Redis operations
# ---------------------------------------------------------------------------


async def get_quota_usage(
    redis: Redis,
    vendor_id: str,
    key_id: str,
    period: str,
) -> int:
    """Return the current counter value for the given vendor/key/period.

    Returns 0 if the key does not exist in Redis.
    """
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket(period, now)
    key = quota_key(vendor_id, key_id, bucket)
    value = await redis.get(key)
    if value is None:
        return 0
    return int(value)


async def check_quota(
    redis: Redis,
    vendor_id: str,
    key_id: str,
    limit: int,
    period: str,
) -> tuple[bool, int]:
    """Check whether the quota allows another request.

    Args:
        redis:     Async Redis client.
        vendor_id: Vendor UUID string.
        key_id:    API key UUID string.
        limit:     Maximum allowed requests for the period.
        period:    "daily" or "monthly".

    Returns:
        ``(allowed, current_count)`` where *allowed* is ``True`` when
        ``current_count < limit``.
    """
    current = await get_quota_usage(redis, vendor_id, key_id, period)
    allowed = current < limit
    return allowed, current


async def increment_quota(
    redis: Redis,
    vendor_id: str,
    key_id: str,
    period: str,
) -> int:
    """Increment the quota counter and set TTL on first write.

    Uses INCR (atomic) followed by EXPIRE only when the counter was just
    created (value == 1 after increment), so the TTL is set once and not
    reset on every request.

    Args:
        redis:     Async Redis client.
        vendor_id: Vendor UUID string.
        key_id:    API key UUID string.
        period:    "daily" or "monthly".

    Returns:
        The new counter value after incrementing.
    """
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket(period, now)
    key = quota_key(vendor_id, key_id, bucket)

    new_count: int = await redis.incr(key)

    # Set expiry only on first write to avoid resetting the window.
    if new_count == 1:
        ttl = period_ttl(period)
        await redis.expire(key, ttl)
        logger.debug(
            "quota.counter_created",
            vendor_id=vendor_id,
            key_id=key_id,
            period=period,
            bucket=bucket,
            ttl=ttl,
        )

    return new_count


# ---------------------------------------------------------------------------
# Periodic sync to Postgres
# ---------------------------------------------------------------------------


async def sync_quota_to_db(
    session: AsyncSession,
    redis: Redis,
    vendor_id: str,
    key_id: str,
    period: str,
) -> None:
    """Read the current Redis quota counter and persist it to Postgres.

    TODO (Phase 6 — Admin API): Replace this stub with a real upsert into a
    ``quota_usage`` table once the schema is finalised.  The table should have
    columns: vendor_id, key_id, period, bucket, used_count, synced_at.

    For now this function reads the counter and logs the sync attempt so that
    the periodic background task has a well-defined hook to call.
    """
    current = await get_quota_usage(redis, vendor_id, key_id, period)
    now = datetime.now(tz=timezone.utc)
    bucket = period_bucket(period, now)

    logger.info(
        "quota.sync_to_db.stub",
        vendor_id=vendor_id,
        key_id=key_id,
        period=period,
        bucket=bucket,
        used_count=current,
        note="TODO Phase 6: upsert into quota_usage table",
    )

    # ``session`` is accepted so the signature is stable for Phase 6; suppress
    # the "unused variable" linter warning intentionally.
    _ = session
