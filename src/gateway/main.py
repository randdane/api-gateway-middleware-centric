from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from gateway.cache.redis import close_redis, get_client, init_redis
from gateway.config import settings
from gateway.db.session import engine
from gateway.jobs.manager import start_background_worker
from gateway.routes.jobs import router as jobs_router
from gateway.routes.proxy import router as proxy_router

logger = structlog.get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("gateway.starting", environment=settings.environment)

    init_redis()
    logger.info("gateway.redis.connected")

    worker_task = start_background_worker()
    logger.info("gateway.job_worker.started")

    yield

    # Shutdown
    worker_task.cancel()
    await close_redis()
    await engine.dispose()
    logger.info("gateway.stopped")


def create_app() -> FastAPI:
    app = FastAPI(
        title="API Gateway",
        description="Internal API gateway for vendor API access",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )

    # Middleware will be added in Phase 8 (Step 8.1) once each is implemented.
    # Order (outermost first): tracing → logging → rate limiting

    app.include_router(proxy_router)
    app.include_router(jobs_router)
    _register_routes(app)

    return app


def _register_routes(app: FastAPI) -> None:
    @app.get("/health", include_in_schema=False)
    async def health() -> JSONResponse:
        status: dict = {"status": "ok", "services": {}}

        # Redis ping
        try:
            client = get_client()
            await client.ping()
            await client.aclose()
            status["services"]["redis"] = "ok"
        except Exception as exc:
            status["services"]["redis"] = f"error: {exc}"
            status["status"] = "degraded"

        return JSONResponse(status)


app = create_app()
