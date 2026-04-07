# CLAUDE.md

Use Python `uv` package for this project.

## Commands

```bash
# Start the app locally (requires Redis + Postgres running)
uv run uvicorn gateway.main:app

# Start all services (requires Docker)
docker compose up -d

# Run database migrations
DATABASE_URL=postgresql+asyncpg://gateway:gateway@localhost:5432/gateway uv run alembic upgrade head

# Generate a new migration (requires a running Postgres)
PYTHONPATH=src uv run alembic revision --autogenerate -m "description"
# If no running Postgres, create an empty revision and write upgrade/downgrade manually:
PYTHONPATH=src uv run alembic revision -m "description"
```

## Architecture

The gateway is **middleware-centric with FastAPI dependency injection**. Cross-cutting concerns (tracing, logging, rate limiting) live in middleware; user identity, vendor config, and quota state flow into route handlers via `Depends()`.

### Request flow (outermost → innermost)

```
OTel Tracing Middleware → Logging Middleware → Rate Limit Middleware
    → JWT Auth (Depends) → Quota Check (Depends)
        → Cache check → Dedup check → Route handler
            → Adapter → httpx → vendor
        → Cache write, quota decrement, span close
```

Auth is a dependency (not middleware) so `UserIdentity` is accessible in handlers and quota checks. Cache and dedup are inside handlers because cache keys depend on vendor/endpoint context resolved at route time.

### Key design decisions

- **`httpx.Request` mutation**: `httpx.Request` has no `copy_with()`. Adapters construct a new `httpx.Request(method, url, headers=..., content=request.content)` to inject auth.
- **Secrets are never stored in DB**: `auth_config` in the `vendors` table holds *references* (env var names or Vault paths), not actual secrets. The `SecretsProvider` interface resolves them. Use `EnvSecretsProvider` locally.
- **Module-level singletons**: `gateway/vendors/registry.py` exports `registry`, `gateway/cache/redis.py` exports pool helpers — all initialized in the app lifespan in `main.py`.
- **Alembic is async**: `alembic/env.py` uses `async_engine_from_config` + `asyncpg`. The `DATABASE_URL` env var overrides `alembic.ini` at runtime.

### Vendor adapter system

`build_adapter(auth_type, auth_config, secrets)` in `gateway/vendors/adapters/__init__.py` is the entry point. Four types: `api_key`, `oauth2`, `basic`, `custom`. The `OAuth2ClientCredentialsAdapter` manages the full token lifecycle (lazy fetch, memory cache with 30s expiry buffer, asyncio lock for concurrent requests).

The `VendorRegistry` (slug-keyed) loads from Postgres, builds adapters lazily, and caches them. Call `registry.invalidate()` to force a reload.

### Database models

Four tables in `gateway/db/models.py`: `vendors`, `vendor_api_keys`, `vendor_endpoints`, `jobs`. All use async SQLAlchemy 2.0 with `asyncpg`. UUIDs as primary keys throughout. `auth_config` is JSONB.

### Testing conventions

- **Unit tests** (`tests/unit/`): mock all I/O. DB sessions are `AsyncMock`; httpx calls use `unittest.mock.patch`. No live services required.
- **Integration tests** (`tests/integration/`): use `testcontainers` for real Redis/Postgres.
- **API tests** (`tests/api/`): use FastAPI `TestClient` + `respx` to mock vendor HTTP calls.
- `tests/conftest.py` provides session-scoped RSA keypairs and a `token_factory` fixture for signing JWTs in tests.
- `pytest-asyncio` is configured with `asyncio_mode = "auto"` — no `@pytest.mark.asyncio` needed (though it is harmless if present).
- The module-level `_cache` in `gateway/auth/jwt.py` is shared across tests; reset `_cache.fetched_at = 0` and `_cache.keys = {}` in tests that need a clean state.

### Implementation status

Phases completed: 1 (scaffold), 2 (JWT auth), 3 (vendor adapters + registry).
Remaining: Phase 4 (caching, dedup, rate limiting, quota), Phase 5 (proxy routes + async jobs), Phase 6 (admin API), Phase 7 (observability), Phase 8 (middleware assembly + deployment).
Full plan: `.claude/plans/memoized-imagining-pebble.md`. Spec: `docs/superpowers/specs/2026-04-06-api-gateway-design.md`.
