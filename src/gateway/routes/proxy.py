"""Proxy endpoints — catch-all route that pipes requests through to vendors.

Pipeline per request:
    auth → quota check → cache check → dedup → vendor call → cache store
    → quota increment → return response
"""

from __future__ import annotations

import uuid as _uuid
import httpx
import structlog
from datetime import UTC, datetime
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.cache.dedup import dedup_context, dedup_publish, dedup_wait, make_dedup_key
from gateway.cache.redis import get_redis
from gateway.cache.response_cache import (
    CachedResponse,
    get_cached,
    make_cache_key,
    resolve_ttl,
    set_cached,
)
from gateway.db.models import VendorApiKey, VendorEndpoint
from gateway.db.session import get_db
from gateway.jobs.manager import create_job
from gateway.jobs.models import JobCreatedResponse
from gateway.quota.tracker import check_quota, increment_quota, resets_at
from gateway.vendors.client import VendorClient
from gateway.vendors.registry import VendorConfig, registry

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/vendors", tags=["proxy"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HOP_BY_HOP = frozenset(
    [
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    ]
)


def _filter_response_headers(headers: dict[str, str]) -> dict[str, str]:
    """Strip hop-by-hop headers that must not be forwarded."""
    return {k: v for k, v in headers.items() if k.lower() not in _HOP_BY_HOP}


def _cached_to_response(cached: CachedResponse) -> Response:
    headers = _filter_response_headers(cached.headers)
    headers["X-Cache"] = "HIT"
    return Response(
        content=cached.body,
        status_code=cached.status_code,
        headers=headers,
    )


async def _load_active_api_key(
    db: AsyncSession, vendor_config: VendorConfig
) -> VendorApiKey | None:
    """Return the first active VendorApiKey for a vendor, or None."""
    result = await db.execute(
        select(VendorApiKey)
        .where(
            VendorApiKey.vendor_id == vendor_config.id,
            VendorApiKey.is_active.is_(True),
        )
        .limit(1)
    )
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.api_route(
    "/{vendor_slug}/{endpoint_path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
)
async def proxy(
    vendor_slug: str,
    endpoint_path: str,
    request: Request,
    user: UserIdentity = Depends(get_current_user),
    redis: Redis = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Catch-all proxy that forwards requests to the configured vendor."""

    # ------------------------------------------------------------------
    # 0. Ensure registry is fresh
    # ------------------------------------------------------------------
    await registry.reload_if_stale(db)

    # ------------------------------------------------------------------
    # 1. Resolve vendor
    # ------------------------------------------------------------------
    vendor_config: VendorConfig | None = registry.get(vendor_slug)
    if vendor_config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Vendor '{vendor_slug}' not found.",
        )

    adapter = registry.get_adapter(vendor_slug)
    if adapter is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Vendor '{vendor_slug}' has no configured adapter.",
        )

    # ------------------------------------------------------------------
    # 2. Check if the endpoint is configured as an async job
    # ------------------------------------------------------------------
    endpoint_record: VendorEndpoint | None = None
    if vendor_config.id:
        ep_result = await db.execute(
            select(VendorEndpoint).where(
                VendorEndpoint.vendor_id == _uuid.UUID(vendor_config.id),
                VendorEndpoint.path == endpoint_path,
                VendorEndpoint.method == request.method.upper(),
            )
        )
        endpoint_record = ep_result.scalar_one_or_none()

    if isinstance(endpoint_record, VendorEndpoint) and endpoint_record.is_async_job:
        # Read body now so we can store it
        body_bytes: bytes = await request.body()
        forward_hdrs: dict[str, str] = {
            k.lower(): v
            for k, v in request.headers.items()
        }
        stored_headers = {k: v for k, v in forward_hdrs.items() if k.lower() != "authorization"}
        request_payload = {
            "method": request.method,
            "path": endpoint_path,
            "params": dict(request.query_params),
            "body": body_bytes.decode(errors="replace") if body_bytes else None,
            "forward_headers": {
                k: v
                for k, v in forward_hdrs.items()
                if k not in ("host", "authorization")
            },
            "headers": stored_headers,
        }
        job = await create_job(
            db,
            vendor_id=_uuid.UUID(vendor_config.id),
            endpoint_id=endpoint_record.id,
            requested_by=user.sub,
            request_payload=request_payload,
        )
        return Response(
            content=JobCreatedResponse(
                job_id=job.id,
                status=job.status,
                poll_url=f"/jobs/{job.id}",
            ).model_dump_json(),
            status_code=status.HTTP_202_ACCEPTED,
            media_type="application/json",
        )

    # ------------------------------------------------------------------
    # 3. Quota pre-check (manual, so we keep the api_key for increment)
    # ------------------------------------------------------------------
    api_key = await _load_active_api_key(db, vendor_config)
    quota_applicable = (
        api_key is not None
        and api_key.quota_limit is not None
        and api_key.quota_period is not None
    )

    if quota_applicable:
        if api_key is None:
            raise HTTPException(status_code=500, detail="Quota check inconsistency")
        try:
            allowed, current_count = await check_quota(
                redis,
                str(api_key.vendor_id),
                str(api_key.id),
                api_key.quota_limit,  # type: ignore[arg-type]
                api_key.quota_period,  # type: ignore[arg-type]
            )
        except Exception:
            logger.exception(
                "proxy.quota_check_error",
                vendor_slug=vendor_slug,
                user_sub=user.sub,
            )
            allowed = True  # fail-open

        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail={
                    "error": "quota_exceeded",
                    "vendor": vendor_slug,
                    "key": api_key.key_name,
                    "limit": api_key.quota_limit,
                    "used": current_count,
                    "period": api_key.quota_period,
                    "resets_at": resets_at(api_key.quota_period).isoformat(),
                },
            )

    # ------------------------------------------------------------------
    # 3. Read request body (needed for cache / dedup keys)
    # ------------------------------------------------------------------
    body: bytes = await request.body()
    params: dict[str, str] = dict(request.query_params)

    # ------------------------------------------------------------------
    # 4. Cache check
    # ------------------------------------------------------------------
    cache_key = make_cache_key(vendor_slug, endpoint_path, params, body)
    cached = await get_cached(redis, cache_key)
    if cached is not None:
        logger.debug("proxy.cache_hit", vendor_slug=vendor_slug, path=endpoint_path)
        return _cached_to_response(cached)

    # ------------------------------------------------------------------
    # 5. Dedup + vendor call
    # ------------------------------------------------------------------
    dedup_key = make_dedup_key(vendor_slug, endpoint_path, params, body)
    client = VendorClient(vendor_config, adapter)

    async with dedup_context(redis, dedup_key) as acquired:
        if not acquired:
            # Another request is in flight for the same payload; wait for it.
            logger.debug(
                "proxy.dedup_wait", vendor_slug=vendor_slug, path=endpoint_path
            )
            deduped = await dedup_wait(redis, dedup_key)
            if deduped is None:
                raise HTTPException(
                    status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                    detail="Upstream request timed out (dedup wait).",
                )
            return _cached_to_response(deduped)

        # We hold the lock — make the vendor call.
        logger.debug(
            "proxy.vendor_call",
            vendor_slug=vendor_slug,
            method=request.method,
            path=endpoint_path,
        )

        # Forward relevant request headers (strip hop-by-hop + host)
        forward_headers: dict[str, str] = {
            k: v
            for k, v in request.headers.items()
            if k.lower() not in _HOP_BY_HOP
            and k.lower() not in ("host", "authorization")
        }

        try:
            vendor_response = await client.request(
                request.method,
                endpoint_path,
                headers=forward_headers,
                content=body if body is not None else None,
                params=params if params else None,
            )
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="Vendor request timed out")
        except httpx.ConnectError:
            raise HTTPException(status_code=502, detail="Could not connect to vendor")

        response_headers = dict(vendor_response.headers)
        response_body: bytes = vendor_response.content

        # ------------------------------------------------------------------
        # 6. On 2xx: cache, publish dedup result, increment quota
        # ------------------------------------------------------------------
        if 200 <= vendor_response.status_code < 300:
            cached_response = CachedResponse(
                status_code=vendor_response.status_code,
                headers=_filter_response_headers(response_headers),
                body=response_body,
                cached_at=datetime.now(tz=UTC),
            )

            ttl = resolve_ttl(vendor_config.cache_ttl_seconds, endpoint_record.cache_ttl_override if isinstance(endpoint_record, VendorEndpoint) else None)

            await set_cached(redis, cache_key, cached_response, ttl)
            await dedup_publish(redis, dedup_key, cached_response)

            if quota_applicable and api_key is not None:
                try:
                    await increment_quota(
                        redis,
                        str(api_key.vendor_id),
                        str(api_key.id),
                        api_key.quota_period,  # type: ignore[arg-type]
                    )
                except Exception:
                    logger.exception(
                        "proxy.quota_increment_error",
                        vendor_slug=vendor_slug,
                        user_sub=user.sub,
                    )

            return Response(
                content=response_body,
                status_code=vendor_response.status_code,
                headers=_filter_response_headers(response_headers),
            )

        # ------------------------------------------------------------------
        # 7. Non-2xx: propagate without caching or incrementing quota
        # ------------------------------------------------------------------
        # Non-2xx: propagate without caching. dedup_publish is intentionally skipped —
        # concurrent waiters on the same dedup key will time out with 504 rather than
        # receiving the error response.
        logger.warning(
            "proxy.vendor_error",
            vendor_slug=vendor_slug,
            path=endpoint_path,
            status=vendor_response.status_code,
        )
        return Response(
            content=response_body,
            status_code=vendor_response.status_code,
            headers=_filter_response_headers(response_headers),
        )
