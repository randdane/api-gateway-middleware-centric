from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from prometheus_client import make_asgi_app

from gateway.admin.routes import router as admin_router
from gateway.cache.redis import close_redis, get_client, init_redis
from gateway.config import settings
from gateway.db.session import engine
from gateway.jobs.manager import start_background_worker
from gateway.logging_config import configure_logging
from gateway.middleware.logging import LoggingMiddleware
from gateway.middleware.tracing import TracingMiddleware
from gateway.observability.tracing import setup_tracing
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
    configure_logging()
    setup_tracing()

    app = FastAPI(
        title="API Gateway",
        description="Internal API gateway for vendor API access",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url="/redoc" if settings.debug else None,
    )

    # Middleware order (outermost first): tracing → logging → rate limiting.
    # add_middleware() wraps in reverse order, so TracingMiddleware is added
    # last here so it ends up as the outermost layer.
    app.add_middleware(LoggingMiddleware)
    app.add_middleware(TracingMiddleware)

    app.include_router(proxy_router)
    app.include_router(jobs_router)
    app.include_router(admin_router)
    _register_routes(app)

    # Prometheus scrape endpoint — mounted *after* routers so the ASGI sub-app
    # takes precedence over any route with the same prefix.
    if settings.metrics_enabled:
        app.mount("/metrics", make_asgi_app())

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
