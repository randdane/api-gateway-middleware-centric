"""Structlog configuration for the API gateway.

Call ``configure_logging()`` once at application startup (in ``create_app()``).
"""

from __future__ import annotations

import logging
import sys

import structlog

from gateway.config import settings


def configure_logging() -> None:
    """Configure structlog with JSON or console output based on environment.

    - Production / staging: JSON output (machine-readable).
    - Development: ConsoleRenderer (human-readable, coloured).

    Both modes include: timestamp, log level, logger name.
    """
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)
    is_development = settings.environment == "development"

    # ------------------------------------------------------------------
    # Shared pre-chain processors (run before the final renderer)
    # ------------------------------------------------------------------
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    if is_development:
        # Human-readable output for local development
        renderer: structlog.types.Processor = structlog.dev.ConsoleRenderer()
    else:
        # Machine-readable JSON for production/staging
        shared_processors.append(structlog.processors.format_exc_info)
        renderer = structlog.processors.JSONRenderer()

    structlog.configure(
        processors=[
            *shared_processors,
            # Prepare event dict for the final renderer
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # ------------------------------------------------------------------
    # Also configure the standard library logging so that any stdlib
    # loggers (uvicorn, SQLAlchemy, etc.) flow through structlog.
    # ------------------------------------------------------------------
    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
        foreign_pre_chain=shared_processors,
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)
