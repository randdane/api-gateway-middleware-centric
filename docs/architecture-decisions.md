# Architecture Decision Record — API Gateway

This document captures every design choice made during the planning of this project,
including the options that were considered but not chosen. It exists so you can rebuild
this app with different choices and compare outcomes.

## Source References

| Artifact | Location |
|----------|----------|
| Planning conversation (full session) | `~/.claude/projects/-Users-r/ba62bf87-4484-41e8-ad89-e8f8f8f54e02.jsonl` |
| Product spec | `docs/superpowers/specs/2026-04-06-api-gateway-design.md` |
| Implementation plan | `~/.claude/plans/memoized-imagining-pebble.md` |

---

## Decisions With Multiple Options Considered

### 1. Overall Architecture Pattern

Three options were evaluated:

**Option A — Middleware-Centric Gateway** ✅ *chosen*
- FastAPI middleware stack + `Depends()` dependency injection
- Middleware handles cross-cutting concerns in order: tracing → logging → rate limiting
- JWT auth, quota, cache, and dedup live as FastAPI dependencies or inside route handlers
- Vendor adapters are pluggable classes; routes are thin pass-throughs
- Pros: FastAPI-idiomatic, clean separation, each layer independently testable
- Cons: Middleware ordering matters and can be tricky to debug

**Option B — Service Layer Architecture**
- An explicit `GatewayService` class orchestrates the full request lifecycle:
  authenticate → rate limit → quota → cache → proxy → cache response → track usage
- Routes call `GatewayService.handle()` and return the result
- Cross-cutting concerns live in service methods, not middleware
- Pros: Explicit control flow, easier to debug step-by-step, clear error handling per stage
- Cons: More boilerplate per route, less idiomatic, risk of the service class becoming a god object

**Option C — Event-Driven Pipeline**
- Each request flows through a chain of independent async stage functions
  (auth, rate limit, quota, cache, proxy)
- A pipeline runner chains stages; any stage can short-circuit early
- Per-vendor stage customisation is straightforward
- Pros: Very flexible, stages trivially unit-testable, easy to reorder
- Cons: More abstract, harder for new developers to follow, composition adds complexity

**Why A was chosen:** Most FastAPI-idiomatic; keeps routes thin; the middleware/DI pattern
is well-understood in the ecosystem. At ~50 vendors the pipeline abstraction adds complexity
without clear benefit, and the explicit service layer adds boilerplate that FastAPI's DI
already eliminates.

---

### 2. Primary Database

**PostgreSQL** ✅ *chosen (no alternatives formally evaluated)*

Recommended directly: async-capable via `asyncpg`, relational integrity for vendor config,
quota limits, and job state, JSONB support for the adapter `auth_config` column.
SQLAlchemy 2.0 async engine integrates cleanly with FastAPI.

---

## Decisions Made Without Alternatives (Architect Recommendations Accepted)

These were presented as single recommendations that were approved without debate.
They are the most likely candidates to vary in a rebuild.

| # | Decision | What was chosen | Alternative to try |
|---|----------|----------------|--------------------|
| 3 | Ephemeral state store | Redis (caching, rate limiting, dedup, quota counters) | Valkey, Memcached, in-process (single-replica only) |
| 4 | Secrets backend | Pluggable `SecretsProvider`; env vars for local dev | Wire to HashiCorp Vault, AWS Secrets Manager, or GCP Secret Manager |
| 5 | Internal auth | Enterprise OAuth2/JWT verified via JWKS endpoint | API key header, mTLS, or a simpler shared-secret scheme |
| 6 | Rate limit algorithm | Token bucket via Redis Lua script (atomic) | Fixed window, sliding window log, leaky bucket |
| 7 | Rate limit scopes | Per-user, per-vendor, per-user-per-vendor | Per-endpoint, per-team, per-IP |
| 8 | Quota enforcement | Hard block (429) at limit | Soft limit (warn but allow), degraded-mode throttle |
| 9 | Quota config location | `vendor_api_keys` table in Postgres | Separate `quotas` table, config file, Redis only |
| 10 | Response caching | TTL-based, Redis, key = `cache:{vendor}:{path}:{sha256(params+body)}` | No caching, CDN layer, per-user cache keys |
| 11 | Request deduplication | Redis lock + pub/sub; waiters subscribe before lock-holder publishes | Singleflight in-process (single-replica only), no dedup |
| 12 | Auth layer placement | JWT auth as a `Depends()` dependency (not middleware) so user identity is accessible in handlers | Middleware (user identity then injected via `request.state`) |
| 13 | Rate limiting placement | Middleware (runs before routing, before user identity is known) | Dependency (runs after auth, allows per-user limits in one place) |
| 14 | Cache/dedup placement | Inside route handler (cache key needs vendor+endpoint context) | Middleware (coarser keys, hits before auth) |
| 15 | Quota decrement timing | After successful vendor call only (failures don't consume quota) | Pre-decrement with refund on failure, or always-decrement |
| 16 | Async job pattern | POST → 202 + job ID; background worker polls DB; optional `X-Callback-URL` webhook | Celery/RQ task queue, WebSocket push, SSE, long-poll |
| 17 | Background worker | Single asyncio task polling every 5 s; `SELECT ... LIMIT 50` | `SELECT ... FOR UPDATE SKIP LOCKED` (multi-replica safe), dedicated worker process |
| 18 | Vendor auth adapters | `api_key`, `oauth2_client_credentials`, `basic`, `custom_headers` | Add `oauth2_pkce`, `aws_sigv4`, `hmac_signature` |
| 19 | Vendor auth config storage | JSONB column in `vendors` table; secrets stored as *references*, not values | Separate `vendor_auth_configs` table, external config map |
| 20 | Vendor registry | In-memory, loaded from Postgres at startup, refreshed on interval or admin trigger | Per-request DB lookup (no cache), file-based config, etcd/Consul |
| 21 | Observability stack | OTel tracing → Jaeger/Zipkin; Prometheus metrics; structlog JSON logs | Datadog APM, AWS X-Ray, plain `logging` module, statsd |
| 22 | Testing layers | Unit (mocked I/O) + integration (testcontainers: real Redis + Postgres) + API (TestClient + respx) | Contract tests, load tests, skip integration layer |
| 23 | Python version in Docker | 3.12-slim | 3.11, 3.13, or alpine base |
| 24 | Package manager | `uv` | `pip`, `poetry`, `pdm` |

---

## Key Constraints That Drove Decisions

- ~20 internal users + service accounts (not public-facing scale)
- ~50 vendor APIs with diverse auth patterns
- Single-replica deployment assumed (the background worker's `SELECT LIMIT 50` is not safe for multiple replicas — see decision 17)
- Secrets must never be stored in the database (all `*_ref` fields are references to a secrets provider)
