"""FastAPI dependencies shared across route modules.

Provides reusable dependency-injected resources (database session, Redis
client) so that route handlers never construct infrastructure objects directly.

All generator-style dependencies follow the yield pattern so resources are
always cleaned up after each request, even on exception.
"""

from __future__ import annotations

from typing import AsyncGenerator

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings

# ---------------------------------------------------------------------------
# Async database engine and session factory
# ---------------------------------------------------------------------------
# The engine and session factory are module-level singletons — created once at
# import time and reused across all requests.  Disposing the engine is handled
# by the FastAPI lifespan context manager in ``app/main.py``.

_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.ENVIRONMENT == "development",
    future=True,
)

_async_session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
    bind=_engine,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Yield an async database session per request.

    The session is created fresh for each request and closed in the
    ``finally`` block.  Callers are responsible for committing or rolling back
    before the generator resumes.

    Usage::

        @router.post("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(MyModel))
            ...
    """
    async with _async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


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
