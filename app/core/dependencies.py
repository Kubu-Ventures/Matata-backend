"""FastAPI dependencies shared across route modules.

Provides reusable dependency-injected resources (Redis client, etc.)
so that route handlers never construct infrastructure objects directly.
"""

from __future__ import annotations

from typing import AsyncGenerator

from redis.asyncio import Redis

from app.core.config import settings


async def get_redis() -> AsyncGenerator[Redis, None]:
    """Yield an async Redis client and ensure it is closed after the request.

    Usage::

        @router.get("/example")
        async def example(redis: Redis = Depends(get_redis)):
            ...
    """
    redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        yield redis
    finally:
        await redis.aclose()
