"""Object storage service.

Defines the ``StorageService`` Protocol and provides two implementations:

* ``MockStorageService``  — stores uploads in memory; used in development and
  CI so no S3 credentials are needed.
* ``S3StorageService``    — production storage backed by any S3-compatible
  provider (AWS S3, MinIO, Cloudflare R2) via ``aiobotocore``.

A factory ``get_storage_service()`` selects the correct implementation from
the ``STORAGE_BACKEND`` environment variable.

AWS Free Tier note
------------------
AWS S3 is included in the **AWS Free Tier**:
* 5 GB storage
* 20,000 GET requests / month
* 2,000 PUT requests / month
* For the first 12 months.

For an early-stage deployment this comfortably covers development and demo
use.  For local development and CI, ``STORAGE_BACKEND=mock`` (the default) is
recommended — it requires no credentials and costs nothing.

MinIO alternative (completely free, self-hosted)
------------------------------------------------
If you prefer to avoid AWS entirely, MinIO is an open-source, S3-compatible
object store you can run locally via Docker:

    docker run -p 9000:9000 -p 9001:9001 \\
        -e "MINIO_ROOT_USER=minioadmin" \\
        -e "MINIO_ROOT_PASSWORD=minioadmin" \\
        quay.io/minio/minio server /data --console-address ":9001"

Set these variables in ``.env``:
    STORAGE_BACKEND=s3
    S3_ENDPOINT_URL=http://localhost:9000
    S3_BUCKET_NAME=crisismap
    AWS_ACCESS_KEY_ID=minioadmin
    AWS_SECRET_ACCESS_KEY=minioadmin
    AWS_REGION=us-east-1

MinIO is fully compatible with the S3 API — no code changes required to
switch between MinIO (local/self-hosted) and AWS S3 (cloud).

Object key format
-----------------
Every uploaded image is stored under:
    reports/{report_id}/{uuid4()}.jpg

The original filename is **never** used — this prevents path traversal attacks,
filename enumeration, and metadata leakage.
"""

from __future__ import annotations

import logging
import uuid
from typing import Protocol, runtime_checkable

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class StorageError(RuntimeError):
    """Raised when an object storage operation fails."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class StorageService(Protocol):
    """Structural interface for object storage back-ends."""

    async def upload_image(
        self,
        report_id: str,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> str:
        """Upload *image_bytes* and return the object key.

        Args:
            report_id:    UUID string of the parent Report record.  Used as the
                          first path component of the object key.
            image_bytes:  Raw image binary.
            content_type: MIME type (default ``image/jpeg``).

        Returns:
            Object key (e.g. ``reports/<report_id>/<uuid>.jpg``).

        Raises:
            StorageError: If the upload fails.
        """
        ...  # pragma: no cover

    async def delete_image(self, object_key: str) -> None:
        """Delete the object identified by *object_key*.

        Used during rollback / cleanup paths only.  A missing key is silently
        ignored so delete is safe to call idempotently.

        Args:
            object_key: Full object key as returned by ``upload_image``.

        Raises:
            StorageError: If deletion fails for a reason other than a missing key.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# MockStorageService — development / CI
# ---------------------------------------------------------------------------


class MockStorageService:
    """In-memory storage service for development and automated tests.

    Uploaded bytes are stored in a dict keyed by object key.  This keeps
    tests hermetic — no filesystem access, no network calls.

    Usage in tests::

        service = MockStorageService()
        key = await service.upload_image("report-uuid", b"...")
        assert key.startswith("reports/report-uuid/")
        assert key in service.store
    """

    def __init__(self) -> None:
        # Maps object_key → raw bytes.
        self.store: dict[str, bytes] = {}

    async def upload_image(
        self,
        report_id: str,
        image_bytes: bytes,
        content_type: str = "image/jpeg",  # noqa: ARG002
    ) -> str:
        """Store bytes in memory and return the generated key."""
        object_key = f"reports/{report_id}/{uuid.uuid4()}.jpg"
        self.store[object_key] = image_bytes
        logger.debug(
            "MockStorageService: stored %d bytes at %s", len(image_bytes), object_key
        )
        return object_key

    async def delete_image(self, object_key: str) -> None:
        """Remove key from the in-memory store (no-op if absent)."""
        self.store.pop(object_key, None)
        logger.debug("MockStorageService: deleted key %s", object_key)


# ---------------------------------------------------------------------------
# S3StorageService — production (AWS S3 / MinIO / Cloudflare R2)
# ---------------------------------------------------------------------------


class S3StorageService:
    """Production object storage backed by any S3-compatible provider.

    Supports:
    * AWS S3     (set ``S3_ENDPOINT_URL`` to empty)
    * MinIO      (set ``S3_ENDPOINT_URL=http://localhost:9000``)
    * Cloudflare R2 (set endpoint to your R2 account URL)

    All uploads are encrypted at rest using AES-256 server-side encryption
    (``ServerSideEncryption="AES256"``), as required by the specification §14.2.

    Requires ``aiobotocore`` (``pip install aiobotocore``).
    """

    def __init__(self) -> None:
        if not settings.S3_BUCKET_NAME:
            raise StorageError("S3_BUCKET_NAME must be set when STORAGE_BACKEND=s3.")

    def _make_key(self, report_id: str) -> str:
        """Generate a determinism-free object key for a report photo.

        Format: ``reports/<report_id>/<uuid4>.jpg``
        The original filename is deliberately discarded.
        """
        return f"reports/{report_id}/{uuid.uuid4()}.jpg"

    def _client_kwargs(self) -> dict:
        """Build keyword arguments for the aiobotocore client factory."""
        kwargs: dict = {
            "region_name": settings.AWS_REGION or "us-east-1",
        }
        if settings.AWS_ACCESS_KEY_ID:
            kwargs["aws_access_key_id"] = settings.AWS_ACCESS_KEY_ID
        if settings.AWS_SECRET_ACCESS_KEY:
            kwargs["aws_secret_access_key"] = settings.AWS_SECRET_ACCESS_KEY
        if settings.S3_ENDPOINT_URL:
            # MinIO / Cloudflare R2 / other S3-compatible endpoints.
            kwargs["endpoint_url"] = settings.S3_ENDPOINT_URL
        return kwargs

    async def upload_image(
        self,
        report_id: str,
        image_bytes: bytes,
        content_type: str = "image/jpeg",
    ) -> str:
        """Upload *image_bytes* to S3-compatible storage with AES-256 encryption.

        Args:
            report_id:    UUID string of the owning Report.
            image_bytes:  Raw image binary.
            content_type: MIME type.

        Returns:
            Object key (``reports/<report_id>/<uuid>.jpg``).

        Raises:
            StorageError: On S3 or network error.
        """
        try:
            import aiobotocore.session  # type: ignore[import]
        except ImportError as exc:
            raise StorageError(
                "aiobotocore is required for STORAGE_BACKEND=s3. "
                "Install it with: pip install aiobotocore"
            ) from exc

        object_key = self._make_key(report_id)
        session = aiobotocore.session.get_session()
        try:
            async with session.create_client("s3", **self._client_kwargs()) as client:
                await client.put_object(
                    Bucket=settings.S3_BUCKET_NAME,
                    Key=object_key,
                    Body=image_bytes,
                    ContentType=content_type,
                    # Spec §14.2 — all stored images encrypted at rest with AES-256.
                    ServerSideEncryption="AES256",
                )
        except Exception as exc:
            logger.error(
                "S3 upload error for key %s: %s", object_key, type(exc).__name__
            )
            raise StorageError(
                f"Object storage upload failed: {type(exc).__name__}"
            ) from exc

        logger.info("Uploaded image to storage key: %s", object_key)
        return object_key

    async def delete_image(self, object_key: str) -> None:
        """Delete *object_key* from S3-compatible storage.

        A ``NoSuchKey`` response is silently ignored so this is safe to call
        in cleanup paths even when a prior upload may not have completed.

        Raises:
            StorageError: On unexpected S3 or network error.
        """
        try:
            import aiobotocore.session  # type: ignore[import]
        except ImportError as exc:
            raise StorageError(
                "aiobotocore is required for STORAGE_BACKEND=s3."
            ) from exc

        session = aiobotocore.session.get_session()
        try:
            async with session.create_client("s3", **self._client_kwargs()) as client:
                await client.delete_object(
                    Bucket=settings.S3_BUCKET_NAME,
                    Key=object_key,
                )
        except Exception as exc:
            # Swallow NoSuchKey — anything else should surface.
            exc_name = type(exc).__name__
            if "NoSuchKey" in exc_name or "NoSuchKey" in str(exc):
                logger.debug("delete_image: key %s already absent", object_key)
                return
            logger.error("S3 delete error for key %s: %s", object_key, exc_name)
            raise StorageError(f"Object storage delete failed: {exc_name}") from exc

        logger.info("Deleted storage key: %s", object_key)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_storage_service() -> StorageService:
    """Return the storage service selected by ``STORAGE_BACKEND``.

    Supported values:
    * ``mock`` — ``MockStorageService`` (default, development/CI)
    * ``s3``   — ``S3StorageService`` (AWS S3, MinIO, Cloudflare R2)

    Returns:
        An object satisfying the ``StorageService`` Protocol.

    Raises:
        ValueError: If ``STORAGE_BACKEND`` is set to an unknown value.
    """
    backend = settings.STORAGE_BACKEND.lower()

    if backend == "mock":
        return MockStorageService()
    if backend == "s3":
        return S3StorageService()

    raise ValueError(
        f"Unknown STORAGE_BACKEND value: '{backend}'. "
        "Supported options: 'mock', 's3'."
    )
