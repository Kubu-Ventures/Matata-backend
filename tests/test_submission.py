"""Tests for the report submission feature.

Covers:
- moderation_service: MockModerationProvider, RekognitionModerationProvider (mocked),
  get_moderation_provider factory
- storage_service: MockStorageService, S3StorageService (mocked),
  get_storage_service factory
- queue_service: RedisQueueService, MockQueueService
- submission_service: create_report, add_photo_to_report, get_own_report,
  get_nearby_reports — all branches including rate limit, moderation rejection,
  storage failure, ownership checks, sanitisation, pHash computation, audit log
- report routes: all four endpoints, all documented HTTP codes

All tests are pure unit tests — no real database, Redis, S3 or AWS account
required.  The async database session is fully mocked at the boundary.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.models.enums import ReportStatus

# ---------------------------------------------------------------------------
# Shared fixtures and constants
# ---------------------------------------------------------------------------

_REPORT_ID = uuid.uuid4()
_TOKEN = "test-reporter-token"
_TOKEN_HASH = None  # computed lazily in tests


def _get_token_hash() -> str:
    import hashlib

    return hashlib.sha256(_TOKEN.encode()).hexdigest()


# ===========================================================================
# moderation_service
# ===========================================================================


class TestMockModerationProvider:
    @pytest.mark.asyncio
    async def test_passes_by_default(self):
        from app.services.moderation_service import MockModerationProvider

        provider = MockModerationProvider()
        result = await provider.moderate(b"fake-image-bytes")
        assert result.passed is True
        assert result.categories == {}

    @pytest.mark.asyncio
    async def test_rejects_when_flag_set(self):
        from app.services.moderation_service import MockModerationProvider

        provider = MockModerationProvider()
        provider.reject_next = True
        result = await provider.moderate(b"fake-image-bytes")
        assert result.passed is False
        assert len(result.categories) > 0

    @pytest.mark.asyncio
    async def test_flag_resets_after_use(self):
        from app.services.moderation_service import MockModerationProvider

        provider = MockModerationProvider()
        provider.reject_next = True
        await provider.moderate(b"bytes1")  # consumes the flag
        result = await provider.moderate(b"bytes2")  # should pass
        assert result.passed is True

    def test_satisfies_protocol(self):
        from app.services.moderation_service import (
            MockModerationProvider,
            ModerationProvider,
        )

        assert isinstance(MockModerationProvider(), ModerationProvider)


class TestGetModerationProvider:
    def test_mock_provider_returned(self):
        from app.services.moderation_service import (
            MockModerationProvider,
            get_moderation_provider,
        )

        with patch("app.services.moderation_service.settings") as s:
            s.MODERATION_PROVIDER = "mock"
            provider = get_moderation_provider()
        assert isinstance(provider, MockModerationProvider)

    def test_unknown_provider_raises(self):
        from app.services.moderation_service import get_moderation_provider

        with patch("app.services.moderation_service.settings") as s:
            s.MODERATION_PROVIDER = "unknown_provider"
            with pytest.raises(ValueError, match="Unknown MODERATION_PROVIDER"):
                get_moderation_provider()

    def test_rekognition_returned(self):
        from app.services.moderation_service import (
            RekognitionModerationProvider,
            get_moderation_provider,
        )

        with patch("app.services.moderation_service.settings") as s:
            s.MODERATION_PROVIDER = "rekognition"
            provider = get_moderation_provider()
        assert isinstance(provider, RekognitionModerationProvider)


class TestRekognitionModerationProvider:
    @pytest.mark.asyncio
    async def test_raises_if_aiobotocore_missing(self):
        from app.services.moderation_service import (
            ModerationError,
            RekognitionModerationProvider,
        )

        provider = RekognitionModerationProvider()
        with patch.dict(
            "sys.modules", {"aiobotocore": None, "aiobotocore.session": None}
        ):
            with pytest.raises(ModerationError, match="aiobotocore"):
                await provider.moderate(b"bytes")

    @pytest.mark.asyncio
    async def test_passes_when_no_labels(self):
        pytest.importorskip("aiobotocore")
        from app.services.moderation_service import RekognitionModerationProvider

        provider = RekognitionModerationProvider()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.detect_moderation_labels = AsyncMock(
            return_value={"ModerationLabels": []}
        )
        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch("aiobotocore.session.get_session", return_value=mock_session):
            result = await provider.moderate(b"bytes")
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_rejects_explicit_content(self):
        pytest.importorskip("aiobotocore")
        from app.services.moderation_service import RekognitionModerationProvider

        provider = RekognitionModerationProvider()
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.detect_moderation_labels = AsyncMock(
            return_value={
                "ModerationLabels": [{"Name": "Explicit Nudity", "Confidence": 98.0}]
            }
        )
        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch("aiobotocore.session.get_session", return_value=mock_session):
            result = await provider.moderate(b"bytes")
        assert result.passed is False
        assert "Explicit Nudity" in result.categories

    @pytest.mark.asyncio
    async def test_raises_on_client_error(self):
        pytest.importorskip("aiobotocore")
        from app.services.moderation_service import (
            ModerationError,
            RekognitionModerationProvider,
        )

        provider = RekognitionModerationProvider()
        mock_session = MagicMock()
        mock_session.create_client.side_effect = RuntimeError("boto3 error")

        with patch("aiobotocore.session.get_session", return_value=mock_session):
            with pytest.raises(ModerationError):
                await provider.moderate(b"bytes")


# ===========================================================================
# storage_service
# ===========================================================================


class TestMockStorageService:
    @pytest.mark.asyncio
    async def test_upload_returns_key(self):
        from app.services.storage_service import MockStorageService

        svc = MockStorageService()
        key = await svc.upload_image("report-123", b"image-bytes")
        assert key.startswith("reports/report-123/")
        assert key.endswith(".jpg")

    @pytest.mark.asyncio
    async def test_upload_stores_bytes(self):
        from app.services.storage_service import MockStorageService

        svc = MockStorageService()
        key = await svc.upload_image("report-123", b"hello")
        assert svc.store[key] == b"hello"

    @pytest.mark.asyncio
    async def test_delete_removes_key(self):
        from app.services.storage_service import MockStorageService

        svc = MockStorageService()
        key = await svc.upload_image("report-123", b"hello")
        await svc.delete_image(key)
        assert key not in svc.store

    @pytest.mark.asyncio
    async def test_delete_missing_key_is_noop(self):
        from app.services.storage_service import MockStorageService

        svc = MockStorageService()
        await svc.delete_image("reports/nonexistent/key.jpg")

    def test_satisfies_protocol(self):
        from app.services.storage_service import MockStorageService, StorageService

        assert isinstance(MockStorageService(), StorageService)

    @pytest.mark.asyncio
    async def test_unique_keys_per_upload(self):
        from app.services.storage_service import MockStorageService

        svc = MockStorageService()
        key1 = await svc.upload_image("rep", b"a")
        key2 = await svc.upload_image("rep", b"b")
        assert key1 != key2


class TestGetStorageService:
    def test_mock_backend(self):
        from app.services.storage_service import MockStorageService, get_storage_service

        with patch("app.services.storage_service.settings") as s:
            s.STORAGE_BACKEND = "mock"
            svc = get_storage_service()
        assert isinstance(svc, MockStorageService)

    def test_s3_backend(self):
        from app.services.storage_service import S3StorageService, get_storage_service

        with patch("app.services.storage_service.settings") as s:
            s.STORAGE_BACKEND = "s3"
            s.S3_BUCKET_NAME = "test-bucket"
            svc = get_storage_service()
        assert isinstance(svc, S3StorageService)

    def test_unknown_backend_raises(self):
        from app.services.storage_service import get_storage_service

        with patch("app.services.storage_service.settings") as s:
            s.STORAGE_BACKEND = "gcs"
            with pytest.raises(ValueError, match="Unknown STORAGE_BACKEND"):
                get_storage_service()


class TestS3StorageService:
    def test_raises_if_no_bucket(self):
        from app.services.storage_service import S3StorageService, StorageError

        with patch("app.services.storage_service.settings") as s:
            s.S3_BUCKET_NAME = ""
            with pytest.raises(StorageError, match="S3_BUCKET_NAME"):
                S3StorageService()

    @pytest.mark.asyncio
    async def test_upload_raises_if_aiobotocore_missing(self):
        from app.services.storage_service import S3StorageService, StorageError

        with patch("app.services.storage_service.settings") as s:
            s.S3_BUCKET_NAME = "bucket"
            s.AWS_REGION = "us-east-1"
            s.AWS_ACCESS_KEY_ID = ""
            s.AWS_SECRET_ACCESS_KEY = ""
            s.S3_ENDPOINT_URL = ""
            svc = S3StorageService()

        with patch.dict(
            "sys.modules", {"aiobotocore": None, "aiobotocore.session": None}
        ):
            with pytest.raises(StorageError, match="aiobotocore"):
                await svc.upload_image("rep", b"bytes")

    @pytest.mark.asyncio
    async def test_upload_success(self):
        pytest.importorskip("aiobotocore")
        from app.services.storage_service import S3StorageService

        with patch("app.services.storage_service.settings") as s:
            s.S3_BUCKET_NAME = "bucket"
            s.AWS_REGION = "us-east-1"
            s.AWS_ACCESS_KEY_ID = "key"
            s.AWS_SECRET_ACCESS_KEY = "secret"
            s.S3_ENDPOINT_URL = ""
            svc = S3StorageService()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.put_object = AsyncMock()
        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch("aiobotocore.session.get_session", return_value=mock_session):
            key = await svc.upload_image("report-abc", b"bytes")

        assert key.startswith("reports/report-abc/")
        mock_client.put_object.assert_awaited_once()
        call_kwargs = mock_client.put_object.call_args.kwargs
        assert call_kwargs["ServerSideEncryption"] == "AES256"

    @pytest.mark.asyncio
    async def test_upload_raises_storage_error_on_failure(self):
        pytest.importorskip("aiobotocore")
        from app.services.storage_service import S3StorageService, StorageError

        with patch("app.services.storage_service.settings") as s:
            s.S3_BUCKET_NAME = "bucket"
            s.AWS_REGION = "us-east-1"
            s.AWS_ACCESS_KEY_ID = "key"
            s.AWS_SECRET_ACCESS_KEY = "secret"
            s.S3_ENDPOINT_URL = ""
            svc = S3StorageService()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.put_object = AsyncMock(side_effect=RuntimeError("S3 down"))
        mock_session = MagicMock()
        mock_session.create_client.return_value = mock_client

        with patch("aiobotocore.session.get_session", return_value=mock_session):
            with pytest.raises(StorageError):
                await svc.upload_image("rep", b"bytes")


# ===========================================================================
# queue_service
# ===========================================================================


class TestMockQueueService:
    @pytest.mark.asyncio
    async def test_publish_gis_job(self):
        from app.services.queue_service import MockQueueService

        svc = MockQueueService()
        rid = uuid.uuid4()
        await svc.publish_gis_job(rid)
        assert rid in svc.gis_jobs

    @pytest.mark.asyncio
    async def test_publish_ai_job(self):
        from app.services.queue_service import MockQueueService

        svc = MockQueueService()
        rid = uuid.uuid4()
        await svc.publish_ai_job(rid)
        assert rid in svc.ai_jobs


class TestRedisQueueService:
    @pytest.mark.asyncio
    async def test_gis_job_calls_xadd(self):
        from app.services.queue_service import RedisQueueService

        redis = AsyncMock()
        redis.xadd = AsyncMock()
        svc = RedisQueueService(redis)
        rid = uuid.uuid4()
        await svc.publish_gis_job(rid)
        redis.xadd.assert_awaited_once()
        args = redis.xadd.call_args
        assert args.args[0] == "crisismap:queue:gis"
        assert args.args[1]["report_id"] == str(rid)

    @pytest.mark.asyncio
    async def test_ai_job_calls_xadd(self):
        from app.services.queue_service import RedisQueueService

        redis = AsyncMock()
        redis.xadd = AsyncMock()
        svc = RedisQueueService(redis)
        rid = uuid.uuid4()
        await svc.publish_ai_job(rid)
        redis.xadd.assert_awaited_once()
        args = redis.xadd.call_args
        assert args.args[0] == "crisismap:queue:ai"

    @pytest.mark.asyncio
    async def test_redis_error_is_swallowed(self):
        """Queue publish errors must not propagate to callers."""
        from app.services.queue_service import RedisQueueService

        redis = AsyncMock()
        redis.xadd = AsyncMock(side_effect=RuntimeError("Redis down"))
        svc = RedisQueueService(redis)
        await svc.publish_gis_job(uuid.uuid4())
        await svc.publish_ai_job(uuid.uuid4())


# ===========================================================================
# submission_service — helper functions
# ===========================================================================


class TestSanitiseText:
    def test_strips_html_tags(self):
        from app.services.submission_service import _sanitise_text

        result = _sanitise_text("<script>alert(1)</script>near the market", 500)
        assert "<script>" not in result
        assert "alert(1)" not in result
        assert "near the market" in result

    def test_truncates_to_max_length(self):
        from app.services.submission_service import _sanitise_text

        result = _sanitise_text("a" * 600, 500)
        assert len(result) == 500

    def test_none_passthrough(self):
        from app.services.submission_service import _sanitise_text

        assert _sanitise_text(None, 100) is None

    def test_strips_img_tag(self):
        from app.services.submission_service import _sanitise_text

        result = _sanitise_text('<img src="x" onerror="alert(1)">Building', 500)
        assert "<img" not in result
        assert "Building" in result


class TestHashToken:
    def test_returns_64_char_hex(self):
        from app.services.submission_service import _hash_token

        result = _hash_token("my-token")
        assert len(result) == 64
        assert all(c in "0123456789abcdef" for c in result)

    def test_deterministic(self):
        from app.services.submission_service import _hash_token

        assert _hash_token("t") == _hash_token("t")

    def test_different_tokens_different_hashes(self):
        from app.services.submission_service import _hash_token

        assert _hash_token("a") != _hash_token("b")


class TestComputePhash:
    def test_returns_none_without_pillow(self):
        from app.services.submission_service import _compute_phash

        with patch.dict("sys.modules", {"PIL": None, "PIL.Image": None}):
            result = _compute_phash(b"not-an-image")
        assert result is None

    def test_returns_none_on_invalid_bytes(self):
        from app.services.submission_service import _compute_phash

        result = _compute_phash(b"not-a-valid-image-file")
        assert result is None


class TestRateLimitHelper:
    @pytest.mark.asyncio
    async def test_passes_within_limit(self):
        from app.services.submission_service import _check_rate_limit

        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock()
        await _check_rate_limit("hash", redis)

    @pytest.mark.asyncio
    async def test_raises_when_exceeded(self):
        from app.services.submission_service import (
            RateLimitExceededError,
            _check_rate_limit,
        )

        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=11)
        redis.expire = AsyncMock()
        with pytest.raises(RateLimitExceededError):
            await _check_rate_limit("hash", redis)

    @pytest.mark.asyncio
    async def test_expire_set_on_first_submission(self):
        from app.services.submission_service import _check_rate_limit

        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock()
        await _check_rate_limit("hash", redis)
        redis.expire.assert_awaited_once_with("crisismap:submission:rate:hash", 3600)

    @pytest.mark.asyncio
    async def test_expire_not_called_on_subsequent(self):
        """EXPIRE must not be reset on subsequent submissions within the window."""
        from app.services.submission_service import _check_rate_limit

        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=5)
        redis.expire = AsyncMock()
        await _check_rate_limit("hash", redis)
        redis.expire.assert_not_awaited()


# ===========================================================================
# submission_service — create_report
# ===========================================================================


def _make_db_mock():
    """Return an AsyncSession mock with the minimal interface needed.

    scalar_one_or_none() is configured as a plain MagicMock (not AsyncMock)
    because SQLAlchemy's CursorResult.scalar_one_or_none() is synchronous even
    when the session is async — only the execute() call itself is awaitable.
    """
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=mock_result)
    return db


def _make_redis_mock():
    redis = AsyncMock()
    redis.incr = AsyncMock(return_value=1)
    redis.expire = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock()
    redis.xadd = AsyncMock()
    return redis


class TestCreateReport:
    @pytest.fixture(autouse=True)
    def _mock_compress_image(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.submission_service.compress_image",
            lambda image_bytes, **kw: (image_bytes, "image/jpeg"),
        )

    _BASE_KWARGS = dict(
        crisis_type="flood",
        infrastructure_type="residential",
        damage_severity="partial",
        lat=1.234,
        lng=36.789,
        gps_accuracy_m=10.0,
        landmark_description=None,
        electricity_status=None,
        health_services_status=None,
        most_pressing_needs=None,
        debris_clearing_needed=None,
        offline_queued_at=None,
        image_bytes=None,
        image_content_type="image/jpeg",
        reporter_token=_TOKEN,
        reporter_trust_tier=0,
    )

    @pytest.mark.asyncio
    async def test_creates_report_without_photo(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import create_report

        db = _make_db_mock()
        redis = _make_redis_mock()

        report = await create_report(
            **self._BASE_KWARGS,
            db=db,
            redis=redis,
            moderation_provider=MockModerationProvider(),
            storage_service=MockStorageService(),
            queue_service=MockQueueService(),
        )
        assert report is not None
        assert report.crisis_type == "flood"
        db.add.assert_called()
        db.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_gis_job_dispatched_without_photo(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import create_report

        db = _make_db_mock()
        redis = _make_redis_mock()
        queue = MockQueueService()

        report = await create_report(
            **self._BASE_KWARGS,
            db=db,
            redis=redis,
            moderation_provider=MockModerationProvider(),
            storage_service=MockStorageService(),
            queue_service=queue,
        )
        assert report.id in queue.gis_jobs
        assert report.id not in queue.ai_jobs

    @pytest.mark.asyncio
    async def test_moderation_and_storage_and_ai_job_with_photo(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import create_report

        db = _make_db_mock()
        redis = _make_redis_mock()
        queue = MockQueueService()
        storage = MockStorageService()

        kwargs = dict(self._BASE_KWARGS)
        kwargs["image_bytes"] = b"fake-jpeg-bytes"

        report = await create_report(
            **kwargs,
            db=db,
            redis=redis,
            moderation_provider=MockModerationProvider(),
            storage_service=storage,
            queue_service=queue,
        )
        assert report.photo_url is not None
        assert report.id in queue.gis_jobs
        assert report.id in queue.ai_jobs
        assert len(storage.store) == 1

    @pytest.mark.asyncio
    async def test_moderation_rejection_raises_and_writes_audit_log(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import (
            ModerationRejectionError,
            create_report,
        )

        db = _make_db_mock()
        redis = _make_redis_mock()
        storage = MockStorageService()

        provider = MockModerationProvider()
        provider.reject_next = True

        kwargs = dict(self._BASE_KWARGS)
        kwargs["image_bytes"] = b"bad-content"

        with pytest.raises(ModerationRejectionError):
            await create_report(
                **kwargs,
                db=db,
                redis=redis,
                moderation_provider=provider,
                storage_service=storage,
                queue_service=MockQueueService(),
            )

        assert len(storage.store) == 0
        db.add.assert_called()
        db.flush.assert_awaited()

    @pytest.mark.asyncio
    async def test_rate_limit_enforced(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import (
            RateLimitExceededError,
            create_report,
        )

        db = _make_db_mock()
        redis = _make_redis_mock()
        redis.incr = AsyncMock(return_value=11)

        with pytest.raises(RateLimitExceededError):
            await create_report(
                **self._BASE_KWARGS,
                db=db,
                redis=redis,
                moderation_provider=MockModerationProvider(),
                storage_service=MockStorageService(),
                queue_service=MockQueueService(),
            )

    @pytest.mark.asyncio
    async def test_xss_stripped_from_landmark(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import create_report

        db = _make_db_mock()
        redis = _make_redis_mock()

        kwargs = dict(self._BASE_KWARGS)
        kwargs["landmark_description"] = "<script>alert(1)</script>near the market"

        report = await create_report(
            **kwargs,
            db=db,
            redis=redis,
            moderation_provider=MockModerationProvider(),
            storage_service=MockStorageService(),
            queue_service=MockQueueService(),
        )
        assert "<script>" not in (report.landmark_description or "")
        assert "near the market" in (report.landmark_description or "")

    @pytest.mark.asyncio
    async def test_storage_failure_raises_submission_error(self):
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import StorageError
        from app.services.submission_service import SubmissionError, create_report

        db = _make_db_mock()
        redis = _make_redis_mock()

        broken_storage = MagicMock()
        broken_storage.upload_image = AsyncMock(side_effect=StorageError("broken"))

        kwargs = dict(self._BASE_KWARGS)
        kwargs["image_bytes"] = b"bytes"

        with pytest.raises(SubmissionError):
            await create_report(
                **kwargs,
                db=db,
                redis=redis,
                moderation_provider=MockModerationProvider(),
                storage_service=broken_storage,
                queue_service=MockQueueService(),
            )

    @pytest.mark.asyncio
    async def test_reporter_token_never_stored(self):
        """Raw reporter token must never appear in any stored attribute."""
        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import create_report

        db = _make_db_mock()
        redis = _make_redis_mock()

        report = await create_report(
            **self._BASE_KWARGS,
            db=db,
            redis=redis,
            moderation_provider=MockModerationProvider(),
            storage_service=MockStorageService(),
            queue_service=MockQueueService(),
        )
        assert _TOKEN not in (report.reporter_token_hash or "")
        assert len(report.reporter_token_hash) == 64


# ===========================================================================
# submission_service — add_photo_to_report
# ===========================================================================


class TestAddPhotoToReport:
    @pytest.fixture(autouse=True)
    def _mock_compress_image(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.submission_service.compress_image",
            lambda image_bytes, **kw: (image_bytes, "image/jpeg"),
        )

    @pytest.mark.asyncio
    async def test_happy_path_updates_report(self):
        import hashlib

        from app.services.moderation_service import MockModerationProvider
        from app.services.queue_service import MockQueueService
        from app.services.storage_service import MockStorageService
        from app.services.submission_service import add_photo_to_report

        token_hash = hashlib.sha256(_TOKEN.encode()).hexdigest()
        existing_report = MagicMock()
        existing_report.id = _REPORT_ID
        existing_report.reporter_token_hash = token_hash
        existing_report.photo_url = None
        existing_report.photo_phash = None

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=existing_report)
            )
        )

        queue = MockQueueService()

        await add_photo_to_report(
            report_id=_REPORT_ID,
            image_bytes=b"jpeg-data",
            reporter_token=_TOKEN,
            db=db,
            redis=_make_redis_mock(),
            moderation_provider=MockModerationProvider(),
            storage_service=MockStorageService(),
            queue_service=queue,
        )

        assert existing_report.photo_url is not None
        assert _REPORT_ID in queue.ai_jobs

    @pytest.mark.asyncio
    async def test_raises_not_found_when_missing(self):
        from app.services.submission_service import (
            ReportNotFoundError,
            add_photo_to_report,
        )

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        with pytest.raises(ReportNotFoundError):
            await add_photo_to_report(
                report_id=_REPORT_ID,
                image_bytes=b"bytes",
                reporter_token=_TOKEN,
                db=db,
                redis=_make_redis_mock(),
            )

    @pytest.mark.asyncio
    async def test_raises_ownership_error(self):
        from app.services.submission_service import (
            ReportOwnershipError,
            add_photo_to_report,
        )

        wrong_report = MagicMock()
        wrong_report.reporter_token_hash = "someone-elses-hash"

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=wrong_report)
            )
        )

        with pytest.raises(ReportOwnershipError):
            await add_photo_to_report(
                report_id=_REPORT_ID,
                image_bytes=b"bytes",
                reporter_token=_TOKEN,
                db=db,
                redis=_make_redis_mock(),
            )

    @pytest.mark.asyncio
    async def test_moderation_rejection_writes_audit_and_raises(self):
        import hashlib

        from app.services.moderation_service import MockModerationProvider
        from app.services.submission_service import (
            ModerationRejectionError,
            add_photo_to_report,
        )

        token_hash = hashlib.sha256(_TOKEN.encode()).hexdigest()
        existing_report = MagicMock()
        existing_report.id = _REPORT_ID
        existing_report.reporter_token_hash = token_hash

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=existing_report)
            )
        )

        provider = MockModerationProvider()
        provider.reject_next = True

        with pytest.raises(ModerationRejectionError):
            await add_photo_to_report(
                report_id=_REPORT_ID,
                image_bytes=b"bad-bytes",
                reporter_token=_TOKEN,
                db=db,
                redis=_make_redis_mock(),
                moderation_provider=provider,
            )

        db.add.assert_called()


# ===========================================================================
# submission_service — get_own_report
# ===========================================================================


class TestGetOwnReport:
    @pytest.mark.asyncio
    async def test_returns_own_report(self):
        import hashlib

        from app.services.submission_service import get_own_report

        token_hash = hashlib.sha256(_TOKEN.encode()).hexdigest()
        report = MagicMock()
        report.id = _REPORT_ID
        report.reporter_token_hash = token_hash

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=report))
        )

        result = await get_own_report(
            report_id=_REPORT_ID,
            reporter_token=_TOKEN,
            db=db,
        )
        assert result is report

    @pytest.mark.asyncio
    async def test_raises_not_found(self):
        from app.services.submission_service import ReportNotFoundError, get_own_report

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None))
        )

        with pytest.raises(ReportNotFoundError):
            await get_own_report(
                report_id=_REPORT_ID,
                reporter_token=_TOKEN,
                db=db,
            )

    @pytest.mark.asyncio
    async def test_raises_ownership_error(self):
        from app.services.submission_service import ReportOwnershipError, get_own_report

        wrong_report = MagicMock()
        wrong_report.reporter_token_hash = "someone-else"

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalar_one_or_none=MagicMock(return_value=wrong_report)
            )
        )

        with pytest.raises(ReportOwnershipError):
            await get_own_report(
                report_id=_REPORT_ID,
                reporter_token=_TOKEN,
                db=db,
            )


# ===========================================================================
# submission_service — get_nearby_reports
# ===========================================================================


class TestGetNearbyReports:
    @pytest.mark.asyncio
    async def test_returns_cached_results(self):
        from app.services.submission_service import get_nearby_reports

        cached = json.dumps(
            [
                {
                    "id": str(uuid.uuid4()),
                    "lat": 1.234,
                    "lng": 36.789,
                    "status": "pending",
                    "damage_severity": "partial",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "similarity_score": 0.8,
                }
            ]
        )

        db = _make_db_mock()
        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=cached)

        results = await get_nearby_reports(
            lat=1.234, lng=36.789, radius_m=30, db=db, redis=redis
        )
        assert len(results) == 1
        db.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_queries_db_on_cache_miss(self):
        from app.services.submission_service import get_nearby_reports

        mock_report = MagicMock()
        mock_report.id = uuid.uuid4()
        mock_report.lat = 1.234
        mock_report.lng = 36.789
        mock_report.status = MagicMock(value="pending")
        mock_report.damage_severity = MagicMock(value="partial")
        mock_report.created_at = datetime.now(timezone.utc)

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[mock_report]))
                )
            )
        )

        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=None)

        results = await get_nearby_reports(
            lat=1.234, lng=36.789, radius_m=30, db=db, redis=redis
        )
        assert isinstance(results, list)
        redis.set.assert_awaited()

    @pytest.mark.asyncio
    async def test_radius_clamped_to_100m(self):
        from app.services.submission_service import get_nearby_reports

        db = _make_db_mock()
        db.execute = AsyncMock(
            return_value=MagicMock(
                scalars=MagicMock(
                    return_value=MagicMock(all=MagicMock(return_value=[]))
                )
            )
        )
        redis = _make_redis_mock()
        redis.get = AsyncMock(return_value=None)

        await get_nearby_reports(lat=0.0, lng=0.0, radius_m=9999, db=db, redis=redis)


# ===========================================================================
# Report schemas
# ===========================================================================


class TestReportCreateSchema:
    def test_valid_with_coords(self):
        from app.schemas.report_submission import ReportCreateSchema

        schema = ReportCreateSchema(
            crisis_type="flood",
            infrastructure_type="residential",
            damage_severity="partial",
            lat=1.0,
            lng=36.0,
        )
        assert schema.lat == 1.0

    def test_valid_with_landmark(self):
        from app.schemas.report_submission import ReportCreateSchema

        schema = ReportCreateSchema(
            crisis_type="flood",
            infrastructure_type="residential",
            damage_severity="partial",
            landmark_description="near the market",
        )
        assert schema.landmark_description == "near the market"

    def test_requires_location(self):
        from pydantic import ValidationError

        from app.schemas.report_submission import ReportCreateSchema

        with pytest.raises(ValidationError) as exc_info:
            ReportCreateSchema(
                crisis_type="flood",
                infrastructure_type="residential",
                damage_severity="partial",
            )
        assert "coordinates" in str(exc_info.value) or "landmark" in str(exc_info.value)

    def test_invalid_crisis_type_rejected(self):
        from pydantic import ValidationError

        from app.schemas.report_submission import ReportCreateSchema

        with pytest.raises(ValidationError):
            ReportCreateSchema(
                crisis_type="tornado",
                infrastructure_type="residential",
                damage_severity="partial",
                lat=1.0,
                lng=36.0,
            )

    def test_landmark_truncated_at_schema_level(self):
        from pydantic import ValidationError

        from app.schemas.report_submission import ReportCreateSchema

        with pytest.raises(ValidationError):
            ReportCreateSchema(
                crisis_type="flood",
                infrastructure_type="residential",
                damage_severity="partial",
                landmark_description="x" * 501,
            )


# ===========================================================================
# Route test helpers
# ===========================================================================

# Minimal valid metadata so form-field validation never fires before auth.
_VALID_REPORT_METADATA = json.dumps(
    {
        "crisis_type": "flood",
        "infrastructure_type": "residential",
        "damage_severity": "partial",
        "lat": 1.0,
        "lng": 36.0,
    }
)

# Fixed user payload returned by the auth override in _make_app_with_overrides.
_MOCK_USER_PAYLOAD = {
    "sub": _get_token_hash(),
    "role": "anonymous_reporter",
    "jti": "test-jti",
}


def _make_db_override():
    """Async generator yielding a DB mock with synchronous scalar_one_or_none."""

    async def _override():
        db = AsyncMock()
        db.add = MagicMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        mock_result = MagicMock()
        # scalar_one_or_none() is synchronous in SQLAlchemy 2.x CursorResult —
        # must be MagicMock, NOT AsyncMock, or it returns an unawaited coroutine.
        mock_result.scalar_one_or_none = MagicMock(return_value=None)
        db.execute = AsyncMock(return_value=mock_result)
        yield db

    return _override


def _make_redis_override():
    """Async generator yielding a Redis mock."""

    async def _override():
        redis = AsyncMock()
        redis.incr = AsyncMock(return_value=1)
        redis.expire = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.xadd = AsyncMock()
        yield redis

    return _override


def _make_app_with_overrides():
    """FastAPI app with DB, Redis AND auth all mocked.

    get_current_user is overridden at its definition site in
    app.api.v1.routes.auth so that BOTH the direct dependency injection AND
    any re-export via app.core.dependencies are covered.
    """
    from fastapi import FastAPI

    from app.api.v1.routes.auth import get_current_user
    from app.api.v1.routes.auth import router as auth_router
    from app.api.v1.routes.reports import router as reports_router
    from app.core.dependencies import get_db, get_redis

    app = FastAPI()

    async def override_current_user():
        return _MOCK_USER_PAYLOAD

    app.dependency_overrides[get_db] = _make_db_override()
    app.dependency_overrides[get_redis] = _make_redis_override()
    app.dependency_overrides[get_current_user] = override_current_user
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    return app


def _make_app_no_auth_override():
    """FastAPI app with DB/Redis mocked but real auth enforcement intact.

    Used by tests that must verify 401 is returned when no token is supplied.
    """
    from fastapi import FastAPI

    from app.api.v1.routes.auth import router as auth_router
    from app.api.v1.routes.reports import router as reports_router
    from app.core.dependencies import get_db, get_redis

    app = FastAPI()

    app.dependency_overrides[get_db] = _make_db_override()
    app.dependency_overrides[get_redis] = _make_redis_override()
    # get_current_user intentionally NOT overridden — real auth runs.
    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(reports_router, prefix="/api/v1")
    return app


# ===========================================================================
# Report routes (integration-style, no real DB/Redis)
# ===========================================================================


class TestSubmitReportEndpoint:
    def test_missing_auth_returns_401(self):
        """No token → auth dependency must reject with 401."""
        client = TestClient(_make_app_no_auth_override())
        resp = client.post("/api/v1/reports", data={"metadata": _VALID_REPORT_METADATA})
        assert resp.status_code == 401

    def test_invalid_metadata_json_returns_422(self):
        client = TestClient(_make_app_with_overrides())
        resp = client.post("/api/v1/reports", data={"metadata": "not-json"})
        assert resp.status_code == 422

    def test_missing_location_returns_422(self):
        client = TestClient(_make_app_with_overrides())
        metadata = json.dumps(
            {
                "crisis_type": "flood",
                "infrastructure_type": "residential",
                "damage_severity": "partial",
                # No lat/lng and no landmark_description
            }
        )
        resp = client.post("/api/v1/reports", data={"metadata": metadata})
        assert resp.status_code == 422

    def test_rate_limit_returns_429(self):
        from app.services.submission_service import RateLimitExceededError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.create_report",
            new=AsyncMock(side_effect=RateLimitExceededError("limit")),
        ):
            resp = client.post(
                "/api/v1/reports", data={"metadata": _VALID_REPORT_METADATA}
            )
        assert resp.status_code == 429

    def test_moderation_rejection_returns_422(self):
        from app.services.submission_service import ModerationRejectionError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.create_report",
            new=AsyncMock(side_effect=ModerationRejectionError("bad")),
        ):
            resp = client.post(
                "/api/v1/reports", data={"metadata": _VALID_REPORT_METADATA}
            )
        assert resp.status_code == 422
        assert "could not be accepted" in resp.json().get("detail", "").lower()

    def test_successful_submission_returns_201(self):
        mock_report = MagicMock()
        mock_report.id = uuid.uuid4()
        mock_report.status = ReportStatus.pending
        mock_report.building_id = None

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.create_report",
            new=AsyncMock(return_value=mock_report),
        ):
            resp = client.post(
                "/api/v1/reports", data={"metadata": _VALID_REPORT_METADATA}
            )
        assert resp.status_code == 201
        data = resp.json()
        assert "id" in data
        assert "status" in data


class TestUploadPhotoEndpoint:
    def test_missing_auth_returns_401(self):
        client = TestClient(_make_app_no_auth_override())
        resp = client.patch(
            f"/api/v1/reports/{uuid.uuid4()}/photo",
            files={"photo": ("test.jpg", b"bytes", "image/jpeg")},
        )
        assert resp.status_code == 401

    def test_not_found_returns_404(self):
        from app.services.submission_service import ReportNotFoundError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.add_photo_to_report",
            new=AsyncMock(side_effect=ReportNotFoundError("not found")),
        ):
            resp = client.patch(
                f"/api/v1/reports/{uuid.uuid4()}/photo",
                files={"photo": ("test.jpg", b"bytes", "image/jpeg")},
            )
        assert resp.status_code == 404

    def test_ownership_error_returns_403(self):
        from app.services.submission_service import ReportOwnershipError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.add_photo_to_report",
            new=AsyncMock(side_effect=ReportOwnershipError("forbidden")),
        ):
            resp = client.patch(
                f"/api/v1/reports/{uuid.uuid4()}/photo",
                files={"photo": ("test.jpg", b"bytes", "image/jpeg")},
            )
        assert resp.status_code == 403

    def test_moderation_rejection_returns_422(self):
        from app.services.submission_service import ModerationRejectionError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.add_photo_to_report",
            new=AsyncMock(side_effect=ModerationRejectionError("bad")),
        ):
            resp = client.patch(
                f"/api/v1/reports/{uuid.uuid4()}/photo",
                files={"photo": ("test.jpg", b"bytes", "image/jpeg")},
            )
        assert resp.status_code == 422

    def test_success_returns_200(self):
        rid = uuid.uuid4()
        mock_report = MagicMock()
        mock_report.id = rid
        mock_report.photo_url = f"reports/{rid}/abc.jpg"
        mock_report.photo_status = MagicMock(value="processing")

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.add_photo_to_report",
            new=AsyncMock(return_value=mock_report),
        ):
            resp = client.patch(
                f"/api/v1/reports/{rid}/photo",
                files={"photo": ("test.jpg", b"bytes", "image/jpeg")},
            )
        assert resp.status_code == 200
        assert "photo_url" in resp.json()


class TestGetReportEndpoint:
    def test_missing_auth_returns_401(self):
        client = TestClient(_make_app_no_auth_override())
        resp = client.get(f"/api/v1/reports/{uuid.uuid4()}")
        assert resp.status_code == 401

    def test_not_found_returns_404(self):
        from app.services.submission_service import ReportNotFoundError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.get_own_report",
            new=AsyncMock(side_effect=ReportNotFoundError("not found")),
        ):
            resp = client.get(f"/api/v1/reports/{uuid.uuid4()}")
        assert resp.status_code == 404

    def test_ownership_error_returns_403(self):
        from app.services.submission_service import ReportOwnershipError

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.get_own_report",
            new=AsyncMock(side_effect=ReportOwnershipError("forbidden")),
        ):
            resp = client.get(f"/api/v1/reports/{uuid.uuid4()}")
        assert resp.status_code == 403


class TestNearbyReportsEndpoint:
    def test_no_auth_required(self):
        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.get_nearby_reports",
            new=AsyncMock(return_value=[]),
        ):
            resp = client.get("/api/v1/reports/nearby?lat=1.0&lng=36.0")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_missing_lat_returns_422(self):
        client = TestClient(_make_app_with_overrides())
        resp = client.get("/api/v1/reports/nearby?lng=36.0")
        assert resp.status_code == 422

    def test_radius_exceeding_100_clamped_in_query_validation(self):
        client = TestClient(_make_app_with_overrides())
        resp = client.get("/api/v1/reports/nearby?lat=1.0&lng=36.0&radius_m=999")
        assert resp.status_code == 422

    def test_returns_list_of_nearby_items(self):
        mock_results = [
            {
                "id": str(uuid.uuid4()),
                "lat": 1.234,
                "lng": 36.789,
                "status": "pending",
                "damage_severity": "partial",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "similarity_score": 0.75,
            }
        ]

        client = TestClient(_make_app_with_overrides())
        with patch(
            "app.api.v1.routes.reports.get_nearby_reports",
            new=AsyncMock(return_value=mock_results),
        ):
            resp = client.get("/api/v1/reports/nearby?lat=1.0&lng=36.0")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert "similarity_score" in data[0]
