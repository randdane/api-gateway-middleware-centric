# API Gateway — Product Requirements Document

## Context

Internal teams (~20 users and service accounts) need to call ~50 external vendor APIs. Today, each consumer manages their own vendor credentials, rate limiting, and error handling — leading to duplicated effort, inconsistent quota tracking, and credential sprawl. This API gateway centralizes vendor access behind a single authenticated API surface, handling auth, caching, rate limiting, quota enforcement, and observability so consumers don't have to.

## Overview

A FastAPI-based API gateway that:

- Authenticates internal callers via enterprise OAuth2/JWT (JWKS verification)
- Proxies requests to ~50 vendor APIs with diverse auth patterns
- Manages all vendor credentials and auth lifecycle
- Caches responses (TTL-based) and deduplicates in-flight requests
- Enforces rate limits per-user, per-vendor, and per-user-per-vendor
- Tracks and enforces quotas per-vendor and per-API-key (hard block at limit)
- Supports both synchronous request-response and async job patterns
- Provides full observability: structured logging, Prometheus metrics, OpenTelemetry tracing

## Architecture

**Approach:** Middleware-centric with FastAPI dependency injection.

- Middleware stack handles cross-cutting concerns (tracing, logging, rate limiting)
- Dependency injection provides user identity, vendor config, and quota state to route handlers
- Pluggable vendor adapters abstract diverse auth patterns
- Routes are thin pass-through handlers that delegate to the vendor adapter + httpx client

### Tech Stack

| Component | Technology |
|-----------|-----------|
| Framework | FastAPI + Uvicorn |
| HTTP Client | httpx (async) |
| Cache / Rate Limit / Dedup | Redis (redis.asyncio + hiredis) |
| Database | PostgreSQL (SQLAlchemy 2.0 async + asyncpg) |
| Migrations | Alembic |
| Configuration | pydantic-settings |
| JWT Verification | python-jose[cryptography] |
| Logging | structlog (JSON) |
| Metrics | OpenTelemetry → Prometheus |
| Tracing | OpenTelemetry (Jaeger/Zipkin/Collector) |
| Testing | pytest, pytest-asyncio, respx, testcontainers |
| Packaging | uv, pyproject.toml |
| Deployment | Docker, docker-compose |

### Project Structure

```
api-gateway/
├── src/
│   └── gateway/
│       ├── __init__.py
│       ├── main.py                 # App factory, middleware registration
│       ├── config.py               # Settings via pydantic-settings
│       ├── middleware/
│       │   ├── tracing.py          # OTel span creation
│       │   ├── logging.py          # Structured request/response logging
│       │   ├── rate_limit.py       # Token bucket via Redis
│       │   └── quota.py            # Quota enforcement
│       ├── auth/
│       │   ├── jwt.py              # JWKS-based JWT verification
│       │   └── dependencies.py     # get_current_user Depends()
│       ├── vendors/
│       │   ├── registry.py         # Vendor config registry (loads from DB)
│       │   ├── adapters/
│       │   │   ├── base.py         # Abstract VendorAdapter
│       │   │   ├── api_key.py
│       │   │   ├── oauth2.py
│       │   │   ├── basic.py
│       │   │   └── custom.py
│       │   └── client.py           # httpx AsyncClient wrapper
│       ├── cache/
│       │   ├── response_cache.py   # TTL-based response caching
│       │   └── dedup.py            # In-flight request deduplication
│       ├── quota/
│       │   ├── tracker.py          # Quota state (Redis counters, DB config)
│       │   └── models.py           # Quota DB models
│       ├── jobs/
│       │   ├── manager.py          # Async job lifecycle
│       │   └── models.py           # Job DB models
│       ├── admin/
│       │   └── routes.py           # Admin API endpoints
│       ├── routes/
│       │   └── proxy.py            # Gateway proxy endpoints
│       ├── db/
│       │   ├── session.py          # Async SQLAlchemy engine/session
│       │   └── models.py           # All SQLAlchemy models
│       └── observability/
│           ├── metrics.py          # Prometheus metric definitions
│           └── tracing.py          # OTel tracer configuration
├── tests/
│   ├── unit/
│   ├── integration/
│   └── api/
├── alembic/
├── docker-compose.yml
├── Dockerfile
└── pyproject.toml
```

## Request Flow

```
Client Request
    │
    ▼
┌─────────────────────────┐
│ OTel Tracing Middleware  │  Create span, inject trace context
├─────────────────────────┤
│ Logging Middleware       │  Log request metadata, start timer
├─────────────────────────┤
│ JWT Auth (Depends)       │  Verify JWT via JWKS, extract user identity
├─────────────────────────┤
│ Rate Limit Middleware    │  Token bucket check in Redis
├─────────────────────────┤
│ Quota Check (Depends)    │  Pre-check: will this exceed quota?
├─────────────────────────┤
│ Cache Check (in handler) │  Hash request → Redis lookup
│   ├─ HIT → return cached │
│   └─ MISS → continue     │
├─────────────────────────┤
│ Dedup Check (in handler) │  Identical request in-flight?
│   ├─ YES → await result  │
│   └─ NO  → continue      │
├─────────────────────────┤
│ Route Handler            │
│   ├─ Sync: adapter →     │  Resolve vendor adapter, inject auth,
│   │   httpx → respond    │  call vendor via httpx, return response
│   └─ Async: create job → │  Store job in DB, return 202 + job ID,
│       background worker   │  background task calls vendor
├─────────────────────────┤
│ Post-processing          │  Cache response, decrement quota,
│                          │  log response, close span
└─────────────────────────┘
```

**Key decisions:**

- Auth is a dependency (not middleware) so user identity is accessible in handlers and quota checks
- Cache and dedup happen inside the route handler because cache keys depend on vendor/endpoint specifics resolved at route time
- Quota is decremented after successful vendor call (failed calls don't consume quota)
- Rate limiting is middleware because it applies globally before route matching

## Vendor Adapter System

### Adapter Interface

```python
class VendorAdapter(ABC):
    async def prepare_request(self, request: httpx.Request) -> httpx.Request:
        """Inject vendor-specific auth into the outgoing request."""
        ...

    async def refresh_credentials(self) -> None:
        """Refresh tokens/credentials if needed (e.g., OAuth2 token expiry)."""
        ...
```

### Concrete Adapters

| Adapter | Auth Pattern | Config Fields |
|---------|-------------|---------------|
| `ApiKeyAdapter` | Static key in header or query param | header_name, key_reference |
| `OAuth2ClientCredentialsAdapter` | Token lifecycle (fetch, cache, refresh) | token_url, client_id_ref, client_secret_ref, scopes |
| `BasicAuthAdapter` | username:password in Authorization header | username_ref, password_ref |
| `CustomHeaderAdapter` | Arbitrary headers from config | headers (dict of name → value_ref) |

All `*_ref` fields are references to the secrets manager — actual secrets are never stored in the database. The secrets manager integration is abstracted behind a `SecretsProvider` interface so the implementation can be swapped (e.g., HashiCorp Vault, AWS Secrets Manager, environment variables for local dev).

### Database Schema

**vendors**

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| name | VARCHAR | Human-readable name |
| slug | VARCHAR (unique) | URL-safe identifier |
| base_url | VARCHAR | Vendor API base URL |
| auth_type | VARCHAR | Adapter type: api_key, oauth2, basic, custom |
| auth_config | JSONB | Adapter-specific config (with secret refs, not secrets) |
| cache_ttl_seconds | INTEGER | Default cache TTL for this vendor (0 = no cache) |
| rate_limit_rpm | INTEGER | Requests per minute limit for this vendor |
| is_active | BOOLEAN | Soft delete / disable |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

**vendor_api_keys**

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| vendor_id | UUID (FK) | References vendors.id |
| key_name | VARCHAR | Identifier for this API key |
| description | TEXT | |
| quota_limit | INTEGER | Max requests per period |
| quota_period | VARCHAR | "daily" or "monthly" |
| is_active | BOOLEAN | |
| created_at | TIMESTAMPTZ | |

**vendor_endpoints**

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| vendor_id | UUID (FK) | References vendors.id |
| path | VARCHAR | Endpoint path (relative to vendor base_url) |
| method | VARCHAR | HTTP method |
| description | TEXT | |
| cache_ttl_override | INTEGER | Override vendor-level cache TTL (NULL = use vendor default) |
| rate_limit_override | INTEGER | Override vendor-level rate limit |
| is_async_job | BOOLEAN | Whether this endpoint uses the async job pattern |
| timeout_seconds | INTEGER | Request timeout for this endpoint |

**jobs**

| Column | Type | Description |
|--------|------|-------------|
| id | UUID | Primary key |
| vendor_id | UUID (FK) | |
| endpoint_id | UUID (FK) | |
| requested_by | VARCHAR | User/service identity from JWT |
| status | VARCHAR | pending, in_progress, completed, failed |
| request_payload | JSONB | Original request |
| response_payload | JSONB | Vendor response (when complete) |
| error | TEXT | Error message (when failed) |
| created_at | TIMESTAMPTZ | |
| updated_at | TIMESTAMPTZ | |

## Caching

### Response Cache

- **Key format:** `cache:{vendor_slug}:{endpoint_path}:{sha256(sorted_params + body)}`
- **TTL:** From vendor config, with per-endpoint override
- **Stored data:** status code, response headers, response body, cached_at timestamp
- **Only cache 2xx responses**
- **Admin flush:** per-vendor (`DELETE /admin/vendors/{id}/cache`) or global (`DELETE /admin/cache`)

### Request Deduplication

- **Key format:** `dedup:{sha256(vendor + endpoint + params + body)}`
- Acquire Redis lock (30s TTL) using the dedup key
- If lock acquired: proceed with vendor call, store result, publish via Redis pub/sub
- If lock exists: subscribe to pub/sub channel, await result from the in-flight request
- Prevents thundering herd to the same vendor endpoint

## Rate Limiting

Token bucket algorithm implemented in Redis (Lua script for atomicity).

**Three scopes:**

| Scope | Key Format | Config Source |
|-------|-----------|---------------|
| Per-user (global) | `rl:user:{user_id}` | App-level config |
| Per-vendor | `rl:vendor:{vendor_slug}` | vendors.rate_limit_rpm |
| Per-user-per-vendor | `rl:user:{user_id}:vendor:{vendor_slug}` | Optional per-vendor config |

**Response on limit:** `429 Too Many Requests` with `Retry-After` header and JSON body indicating which limit was hit.

## Quota Tracking

**Config:** Lives in Postgres via `vendor_api_keys` table (limit + period per key).

**Counters:** Redis for speed.

- Key: `quota:{vendor_id}:{key_id}:{period_bucket}` (e.g., `quota:abc:def:2026-04-06` for daily)
- TTL: auto-expire at period end (daily keys expire after 24h, monthly after ~31d)

**Enforcement:**

1. Pre-request: `GET` counter → if >= limit, return `429` with quota details
2. Post-request (on success): `INCR` counter
3. Periodic sync: background task writes Redis counters to Postgres for durability and audit

**429 response body for quota exhaustion:**

```json
{
  "error": "quota_exceeded",
  "vendor": "acme",
  "key": "production",
  "limit": 10000,
  "used": 10000,
  "period": "daily",
  "resets_at": "2026-04-07T00:00:00Z"
}
```

## Async Job Pattern

For vendor endpoints marked `is_async_job = true`:

1. **POST /vendors/{slug}/{endpoint}** — Gateway creates a job record (status: pending), returns `202 Accepted`:
   ```json
   {"job_id": "uuid", "status": "pending", "poll_url": "/jobs/uuid"}
   ```

2. **Background worker** picks up the job, calls the vendor, updates job status + response in Postgres.

3. **GET /jobs/{id}** — Returns current job status and result (when complete).

4. **Optional webhook:** If the caller provides a `X-Callback-URL` header, the gateway POSTs the result to that URL when complete.

## Observability

### Structured Logging (structlog, JSON output)

Every request log includes: trace_id, span_id, user_id, service_account, vendor_slug, endpoint, method, status_code, latency_ms, cache_hit, quota_remaining.

### Metrics (OpenTelemetry → Prometheus)

| Metric | Type | Labels |
|--------|------|--------|
| `gateway_requests_total` | Counter | vendor, endpoint, status, user |
| `gateway_request_duration_seconds` | Histogram | vendor, endpoint |
| `gateway_cache_hits_total` | Counter | vendor |
| `gateway_cache_misses_total` | Counter | vendor |
| `gateway_quota_remaining` | Gauge | vendor, key |
| `gateway_rate_limit_rejections_total` | Counter | vendor, scope |
| `gateway_vendor_errors_total` | Counter | vendor, error_type |

Prometheus scrape endpoint at `/metrics`.

### Distributed Tracing (OpenTelemetry)

Spans for: inbound request, JWT verification, cache lookup, vendor HTTP call, cache write, quota update. Trace context propagated to vendor calls where supported. Exportable to Jaeger, Zipkin, or OTel Collector.

## Admin API

All admin endpoints require JWT with an admin role claim.

| Method | Path | Description |
|--------|------|-------------|
| GET | /admin/vendors | List all vendors |
| POST | /admin/vendors | Create vendor |
| GET | /admin/vendors/{id} | Get vendor details |
| PUT | /admin/vendors/{id} | Update vendor config |
| DELETE | /admin/vendors/{id} | Deactivate vendor |
| GET | /admin/vendors/{id}/quota | View quota config and current usage |
| PUT | /admin/vendors/{id}/quota | Adjust quota limits |
| GET | /admin/vendors/{id}/usage | Usage stats (requests, errors, latency) |
| DELETE | /admin/vendors/{id}/cache | Flush vendor cache |
| DELETE | /admin/cache | Flush all caches |
| POST | /admin/config/reload | Reload vendor registry from DB |
| GET | /admin/health | Detailed health (Redis, Postgres, vendor connectivity) |

## Internal Auth

- Callers obtain a JWT from the enterprise OAuth2 service
- JWT passed in `Authorization: Bearer <token>` header
- Gateway verifies JWT signature using signing keys from the enterprise JWKS endpoint
- JWKS keys are cached in-memory with periodic refresh
- JWT claims provide: user identity, service account identity, roles (including admin)

## Testing Strategy

### Unit Tests
- Vendor adapters (each auth pattern in isolation with mocked secrets)
- Cache key generation and TTL logic
- Rate limit token bucket algorithm
- Quota calculation and period bucketing
- JWT verification with mock JWKS
- Request deduplication logic

### Integration Tests (real Redis + Postgres via testcontainers)
- Cache write/read/invalidation cycles
- Rate limit enforcement across multiple requests
- Quota tracking across request sequences
- Vendor registry load/refresh from Postgres
- Job creation, status updates, result retrieval
- Quota counter sync between Redis and Postgres

### API Tests (FastAPI TestClient + respx for vendor mocking)
- End-to-end proxy: auth → vendor call → response
- Cache hit vs cache miss paths
- Rate limit returns 429 with Retry-After
- Quota exhaustion returns 429 with quota info
- Async job: POST → 202 → poll → result
- Admin API CRUD operations
- Auth edge cases: valid, expired, invalid, missing JWT
- Vendor error handling: timeout, 5xx, connection refused

### Test Infrastructure
- pytest + pytest-asyncio
- respx for mocking httpx vendor calls
- testcontainers-python for Redis/Postgres in integration tests
- Fixtures for: authenticated client, vendor configs, pre-seeded DB

## Deployment

- **Dockerfile:** Multi-stage build, uv for dependency installation
- **docker-compose.yml:** Gateway app + Redis + PostgreSQL (dev/test)
- **Health check:** `GET /health` (basic) and `GET /admin/health` (detailed)
- **Graceful shutdown:** Drain in-flight requests, close DB/Redis connections

## Verification

To verify the implementation end-to-end:

1. `docker-compose up` — starts gateway, Redis, Postgres
2. Run Alembic migrations
3. Seed a test vendor via admin API
4. Send an authenticated request to a vendor endpoint (mock vendor with respx or a local test server)
5. Verify: response returned, cache populated, quota decremented, metrics exposed at `/metrics`, structured log emitted
6. Send requests until rate limit triggers → verify 429
7. Send requests until quota exhausts → verify 429 with quota details
8. Test async job: POST → 202 → poll until complete
9. `pytest` — full test suite passes
