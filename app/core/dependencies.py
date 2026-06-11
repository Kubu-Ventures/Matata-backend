"""FastAPI dependencies shared across route modules.

Provides reusable dependency-injected resources (database session, Redis
client) so that route handlers never construct infrastructure objects directly.

All generator-style dependencies follow the yield pattern so resources are
always cleaned up after each request, even on exception.

Two session flavours are provided:
* ``get_db``       — async ``AsyncSession`` for all normal FastAPI route handlers.
* ``get_sync_db``  — synchronous ``Session`` for the GIS route, which calls
                     ``GISService`` (sync SQLAlchemy, shared with Celery workers).

The sync engine is created lazily on first use rather than at module import
time.  This prevents a psycopg2 import from being triggered when the module
is loaded in the async app process, which only has asyncpg installed.
psycopg2-binary is required in requirements.txt for the Celery worker path.
"""

from __future__ import annotations

from typing import AsyncGenerator, Generator

from redis.asyncio.client import Redis
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings

# ---------------------------------------------------------------------------
# Async database engine and session factory
# ---------------------------------------------------------------------------
# Module-level singleton — safe because create_async_engine uses asyncpg,
# which is always present in both the app and worker containers.

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


# ---------------------------------------------------------------------------
# Synchronous database engine and session factory (lazy)
# ---------------------------------------------------------------------------
# NOT created at import time — only on the first request that actually needs
# it.  This means importing dependencies.py in the async app process never
# triggers a psycopg2 import, avoiding ModuleNotFoundError in containers
# where only asyncpg is installed.
#
# The engine is created exactly once (thread-safety is not a concern here
# because FastAPI's startup is single-threaded) and reused for all subsequent
# requests.

_sync_engine = None
_sync_session_factory = None


def _get_sync_session_factory() -> sessionmaker:
    """Return the sync session factory, initialising it on first call."""
    global _sync_engine, _sync_session_factory

    if _sync_session_factory is None:
        _sync_url = settings.DATABASE_URL.replace("+asyncpg", "").replace(
            "+aiosqlite", ""
        )
        _sync_engine = create_engine(
            _sync_url,
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=10,
        )
        _sync_session_factory = sessionmaker(
            bind=_sync_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
        )

    return _sync_session_factory


def get_sync_db() -> Generator[Session, None, None]:
    """Yield a synchronous SQLAlchemy session for the GIS service.

    Used by ``GET /gis/building/match`` via ``Depends(get_sync_db)``.
    ``GISService`` accepts a plain ``Session`` so that the same class can be
    used from both FastAPI route handlers and Celery workers.

    The underlying sync engine is created on the first call, so importing
    this module never requires psycopg2 to be available.

    Usage::

        @router.get("/gis/building/match")
        async def match(db: Session = Depends(get_sync_db)):
            svc = GISService(db)
            ...
    """
    factory = _get_sync_session_factory()
    db = factory()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------


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
