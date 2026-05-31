# conftest.py  — must be at the project root (same level as app/)
#
# E402 NOTE: os.environ must be populated before any app module is imported
# because pydantic-settings reads the environment at class-definition time.
# All other imports (pytest, AsyncMock) are placed AFTER the os.environ block
# so that standard import-order linters see a clear separation of concerns:
# first the environment is prepared, then modules that depend on it are loaded.
#
# flake8: noqa: E402  (module-level import not at top of file — intentional)
import os

# ── Inject required env vars BEFORE any app module is imported ──────────────
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")
os.environ.setdefault("JWT_SECRET_KEY", "x" * 64)
os.environ.setdefault("PHONE_HASH_SALT", "x" * 32)
os.environ.setdefault("SMS_GATEWAY", "console")
os.environ.setdefault("AFRICASTALKING_API_KEY", "")
os.environ.setdefault("AFRICASTALKING_USERNAME", "")
# ── New env vars added by submission feature ─────────────────────────────────
os.environ.setdefault("MODERATION_PROVIDER", "mock")
os.environ.setdefault("STORAGE_BACKEND", "mock")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("S3_BUCKET_NAME", "")
os.environ.setdefault("S3_ENDPOINT_URL", "")

from unittest.mock import AsyncMock  # noqa: E402

# ── Standard imports (safe now that env is ready) ────────────────────────────
import pytest  # noqa: E402


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
    redis.xadd = AsyncMock(return_value=True)
    redis.aclose = AsyncMock()
    return redis
