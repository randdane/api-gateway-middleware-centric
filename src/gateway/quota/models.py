"""Pydantic models for quota responses."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class QuotaExceededResponse(BaseModel):
    """Response body for HTTP 429 quota exhaustion."""

    error: str = "quota_exceeded"
    vendor: str
    key: str
    limit: int
    used: int
    period: str  # "daily" | "monthly"
    resets_at: datetime


class QuotaStatus(BaseModel):
    """Current quota usage info for a vendor API key."""

    vendor_id: str
    key_id: str
    period: str  # "daily" | "monthly"
    limit: int
    used: int
    remaining: int
    resets_at: datetime
