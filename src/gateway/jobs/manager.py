"""Async job manager: creation, status, execution, and background polling."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.db.models import Job, VendorEndpoint
from gateway.db.session import AsyncSessionLocal
from gateway.vendors.client import VendorClient
from gateway.vendors.registry import registry

logger = structlog.get_logger(__name__)

# How often the background worker polls for pending jobs (seconds).
_POLL_INTERVAL = 5


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------


async def create_job(
    db: AsyncSession,
    *,
    vendor_id: uuid.UUID,
    endpoint_id: uuid.UUID,
    requested_by: str,
    request_payload: dict[str, Any] | None = None,
) -> Job:
    """Insert a new Job row with status=pending and return it."""
    job = Job(
        vendor_id=vendor_id,
        endpoint_id=endpoint_id,
        requested_by=requested_by,
        status="pending",
        request_payload=request_payload or {},
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)
    logger.info("job.created", job_id=str(job.id), vendor_id=str(vendor_id))
    return job


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """Return a Job by primary key, or None if not found."""
    result = await db.execute(select(Job).where(Job.id == job_id))
    return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# Job execution
# ---------------------------------------------------------------------------


async def run_job(db: AsyncSession, job: Job) -> None:
    """Execute a pending job:

    1. Mark in_progress.
    2. Call the vendor via the registry.
    3. Store result/error and mark completed/failed.
    4. Fire optional webhook if X-Callback-URL was present in the request.
    """
    # --- Mark in_progress ---------------------------------------------------
    job.status = "in_progress"
    await db.commit()
    await db.refresh(job)
    logger.info("job.in_progress", job_id=str(job.id))

    # --- Resolve vendor + endpoint ------------------------------------------
    endpoint_result = await db.execute(
        select(VendorEndpoint).where(VendorEndpoint.id == job.endpoint_id)
    )
    endpoint: VendorEndpoint | None = endpoint_result.scalar_one_or_none()
    if endpoint is None:
        job.status = "failed"
        job.error = f"VendorEndpoint {job.endpoint_id} not found"
        await db.commit()
        return

    vendor_config = registry.get_by_id(str(job.vendor_id))
    if vendor_config is None:
        job.status = "failed"
        job.error = f"Vendor {job.vendor_id} not found in registry"
        await db.commit()
        return

    adapter = registry.get_adapter_by_id(str(job.vendor_id))
    if adapter is None:
        job.status = "failed"
        job.error = f"No adapter for vendor {job.vendor_id}"
        await db.commit()
        return

    # --- Build call parameters from stored request_payload ------------------
    payload = job.request_payload or {}
    method: str = payload.get("method", endpoint.method or "GET")
    path: str = payload.get("path", endpoint.path)
    body: bytes | None = None
    raw_body = payload.get("body")
    if isinstance(raw_body, (bytes, bytearray)):
        body = bytes(raw_body)
    elif isinstance(raw_body, str):
        body = raw_body.encode()

    params: dict[str, str] | None = payload.get("params") or None
    forward_headers: dict[str, str] = payload.get("forward_headers") or {}

    # --- Call vendor --------------------------------------------------------
    client = VendorClient(vendor_config, adapter)
    try:
        vendor_response = await client.request(
            method,
            path,
            headers=forward_headers,
            content=body,
            params=params,
            timeout=float(endpoint.timeout_seconds),
        )
        if vendor_response.status_code >= 400:
            job.status = "failed"
            job.error = f"Vendor returned HTTP {vendor_response.status_code}"
            job.response_payload = {
                "status_code": vendor_response.status_code,
                "headers": dict(vendor_response.headers),
                "body": vendor_response.text,
            }
        else:
            job.status = "completed"
            job.response_payload = {
                "status_code": vendor_response.status_code,
                "headers": dict(vendor_response.headers),
                "body": vendor_response.text,
            }
        logger.info(
            "job.completed",
            job_id=str(job.id),
            status_code=vendor_response.status_code,
        )
    except Exception as exc:
        job.status = "failed"
        job.error = str(exc)
        logger.error("job.failed", job_id=str(job.id), error=str(exc))

    await db.commit()

    # --- Optional webhook ---------------------------------------------------
    callback_url: str | None = payload.get("headers", {}).get("x-callback-url")
    if callback_url:
        await _fire_webhook(callback_url, job)


async def _fire_webhook(callback_url: str, job: Job) -> None:
    """POST the job result to the caller-supplied callback URL."""
    payload = {
        "job_id": str(job.id),
        "status": job.status,
        "result": job.response_payload,
        "error": job.error,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(callback_url, json=payload)
        logger.info(
            "job.webhook_sent",
            job_id=str(job.id),
            callback_url=callback_url,
            status_code=resp.status_code,
        )
    except Exception as exc:
        logger.warning(
            "job.webhook_failed",
            job_id=str(job.id),
            callback_url=callback_url,
            error=str(exc),
        )


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------


async def _worker_loop() -> None:
    """Periodically pick up pending jobs and execute them."""
    logger.info("job.worker.started", poll_interval=_POLL_INTERVAL)
    while True:
        try:
            await _process_pending_jobs()
        except Exception:
            logger.exception("job.worker.error")
        await asyncio.sleep(_POLL_INTERVAL)


async def _process_pending_jobs() -> None:
    """Fetch all pending jobs and run each in sequence under its own session."""
    # NOTE: This query is safe only with a single worker. Multi-replica deployments
    # should use SELECT ... FOR UPDATE SKIP LOCKED to prevent duplicate processing.
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Job).where(Job.status == "pending").limit(50)
        )
        jobs = result.scalars().all()

    for job in jobs:
        # Each job gets a fresh session to avoid cross-job state leakage.
        async with AsyncSessionLocal() as db:
            # Re-fetch inside the new session
            fresh_result = await db.execute(select(Job).where(Job.id == job.id))
            fresh_job: Job | None = fresh_result.scalar_one_or_none()
            if fresh_job is None or fresh_job.status != "pending":
                continue
            await run_job(db, fresh_job)


def start_background_worker() -> asyncio.Task:
    """Schedule the worker loop as a background asyncio task.

    Call this from the FastAPI lifespan startup block.
    """
    return asyncio.create_task(_worker_loop(), name="job-background-worker")
