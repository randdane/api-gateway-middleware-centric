"""Job status polling endpoint: GET /jobs/{job_id}."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from gateway.auth.dependencies import UserIdentity, get_current_user
from gateway.db.session import get_db
from gateway.jobs.manager import get_job
from gateway.jobs.models import JobStatusResponse

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}", response_model=JobStatusResponse)
async def poll_job(
    job_id: uuid.UUID,
    user: UserIdentity = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    """Return the current status (and result when complete) for a job."""
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job '{job_id}' not found.",
        )

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        result=job.response_payload,
        error=job.error,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
