"""Pydantic models for Admin API request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Vendor schemas
# ---------------------------------------------------------------------------


class VendorCreate(BaseModel):
    name: str
    slug: str
    base_url: str
    auth_type: Literal["api_key", "oauth2", "basic", "custom"]
    auth_config: dict = Field(default_factory=dict)
    cache_ttl_seconds: int = 0
    rate_limit_rpm: int = 0


class VendorUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    auth_type: Literal["api_key", "oauth2", "basic", "custom"] | None = None
    auth_config: dict | None = None
    cache_ttl_seconds: int | None = None
    rate_limit_rpm: int | None = None
    is_active: bool | None = None


class VendorResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    base_url: str
    auth_type: str
    auth_config: dict
    cache_ttl_seconds: int
    rate_limit_rpm: int
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Quota schemas
# ---------------------------------------------------------------------------


class ApiKeyQuotaUsage(BaseModel):
    key_id: uuid.UUID
    key_name: str
    quota_limit: int | None
    quota_period: str | None
    current_usage: int
    is_active: bool


class VendorQuotaResponse(BaseModel):
    vendor_id: uuid.UUID
    vendor_slug: str
    keys: list[ApiKeyQuotaUsage]


class QuotaUpdate(BaseModel):
    key_id: uuid.UUID
    quota_limit: int | None = None
    quota_period: Literal["daily", "monthly"] | None = None


# ---------------------------------------------------------------------------
# Cache flush schemas
# ---------------------------------------------------------------------------


class CacheFlushResponse(BaseModel):
    deleted: int
    vendor_slug: str | None = None


# ---------------------------------------------------------------------------
# Config reload schemas
# ---------------------------------------------------------------------------


class ConfigReloadResponse(BaseModel):
    reloaded: bool
    vendor_count: int
    message: str


# ---------------------------------------------------------------------------
# Health schemas
# ---------------------------------------------------------------------------


class ServiceHealth(BaseModel):
    status: str  # ok | error
    detail: str | None = None


class HealthResponse(BaseModel):
    status: str  # ok | degraded
    services: dict[str, ServiceHealth]
    vendor_count: int


# ---------------------------------------------------------------------------
# Usage stub schema
# ---------------------------------------------------------------------------


class UsageStubResponse(BaseModel):
    message: str
