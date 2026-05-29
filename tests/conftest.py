# conftest.py  — must be at the project root (same level as app/)
import os

# ── Inject required env vars BEFORE any app module is imported ──────────────
# pydantic-settings reads os.environ at import time, so these must be set
# before `from app.core.config import settings` is ever called.
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("PHONE_HASH_SALT", "x" * 32)
os.environ.setdefault("SMS_GATEWAY", "console")
os.environ.setdefault("AFRICASTALKING_API_KEY", "")
os.environ.setdefault("AFRICASTALKING_USERNAME", "")

from unittest.mock import AsyncMock

# ── Now it is safe to import app modules ────────────────────────────────────
import pytest


@pytest.fixture
def anyio_backend():
    """Tell anyio/pytest-asyncio to use asyncio."""
    return "asyncio"


@pytest.fixture
def mock_redis():
    """Reusable async Redis mock for any test that needs it."""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)
    redis.exists = AsyncMock(return_value=0)
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock(return_value=True)
    redis.aclose = AsyncMock()
    return redis
