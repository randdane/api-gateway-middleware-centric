from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from redis.asyncio import Redis

from gateway.config import settings

_pool: aioredis.ConnectionPool | None = None


def create_pool() -> aioredis.ConnectionPool:
    return aioredis.ConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        decode_responses=True,
    )


def get_pool() -> aioredis.ConnectionPool:
    if _pool is None:
        raise RuntimeError("Redis pool not initialized — call init_redis() first")
    return _pool


def init_redis() -> aioredis.ConnectionPool:
    global _pool
    _pool = create_pool()
    return _pool


async def close_redis() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


def get_client() -> Redis:
    return aioredis.Redis(connection_pool=get_pool())


async def get_redis() -> AsyncGenerator[Redis, None]:
    """FastAPI dependency that yields a Redis client."""
    client = get_client()
    try:
        yield client
    finally:
        await client.aclose()
