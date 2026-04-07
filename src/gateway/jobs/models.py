"""Pydantic models for the async job API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel


class JobCreatedResponse(BaseModel):
    """Returned immediately (HTTP 202) when an async job is enqueued."""

    job_id: uuid.UUID
    status: str
    poll_url: str


class JobStatusResponse(BaseModel):
    """Returned by GET /jobs/{job_id}."""

    job_id: uuid.UUID
    status: str  # pending | in_progress | completed | failed
    result: Any | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
