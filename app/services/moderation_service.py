"""Content moderation service.

Defines the ``ModerationProvider`` Protocol that all moderation back-ends must
satisfy, and provides two concrete implementations:

* ``MockModerationProvider``  — always passes; used in development and CI so
  no AWS credentials are required.
* ``RekognitionModerationProvider`` — production moderation via AWS Rekognition
  DetectModerationLabels, used when ``MODERATION_PROVIDER=rekognition``.

A factory ``get_moderation_provider()`` selects the correct implementation from
the ``MODERATION_PROVIDER`` environment variable, keeping all selection logic
in one place.

Design notes
------------
* ``ModerationProvider`` is a ``typing.Protocol`` (structural subtyping).  Any
  object that implements ``moderate(image_bytes)`` satisfies the interface
  without inheritance, making it trivial to add a third provider (e.g., Google
  SafeSearch) by creating a new class and registering it in the factory —
  zero changes to existing code (Open/Closed Principle).

* ``moderate`` is ``async`` throughout.  The Rekognition client uses
  ``aiobotocore`` for non-blocking I/O; the mock runs entirely in memory.
  Keeping everything async means callers (``submission_service``) are never
  forced to run blocking code in a thread pool.

* The spec mandates **check-then-store**: the result of ``moderate`` is
  evaluated by the caller *before* any write to object storage.  This service
  has no knowledge of storage — it is purely concerned with moderation.

AWS Free Tier note
------------------
AWS Rekognition DetectModerationLabels is included in the **AWS Free Tier**:
* 5,000 images per month for the first 12 months.
* After free tier: ~$0.001 per image.

For a challenge prototype this is effectively free.  To use the free tier:
1. Create a free AWS account at https://aws.amazon.com/free/
2. Set ``MODERATION_PROVIDER=rekognition`` in ``.env``
3. Set ``AWS_REGION``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``

For local development and CI, ``MODERATION_PROVIDER=mock`` (the default) is
recommended — it requires no AWS account and costs nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from app.core.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModerationResult:
    """Immutable result returned by every ``ModerationProvider.moderate`` call.

    Attributes:
        passed:     ``True`` if the image cleared all moderation thresholds.
        categories: Mapping of detected label name → confidence score (0–1).
                    Empty when no labels are detected.
    """

    passed: bool
    categories: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Protocol — the interface every provider must satisfy
# ---------------------------------------------------------------------------


@runtime_checkable
class ModerationProvider(Protocol):
    """Structural interface for content moderation back-ends.

    Any object that implements ``moderate`` with this exact signature is a valid
    ``ModerationProvider`` — no inheritance required.
    """

    async def moderate(self, image_bytes: bytes) -> ModerationResult:
        """Evaluate *image_bytes* for harmful content.

        Args:
            image_bytes: Raw image binary (JPEG or PNG).

        Returns:
            ``ModerationResult`` with ``passed=True`` if no prohibited content
            exceeds its threshold, ``passed=False`` otherwise.

        Raises:
            ModerationError: If the downstream service is unavailable or
                             returns an unexpected error.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class ModerationError(RuntimeError):
    """Raised when a moderation provider fails to evaluate an image."""


# ---------------------------------------------------------------------------
# MockModerationProvider — development / CI
# ---------------------------------------------------------------------------


class MockModerationProvider:
    """Development and CI provider that unconditionally passes all images.

    No network calls are made.  The ``reject_next`` flag exists exclusively for
    unit tests that need to simulate a moderation rejection without patching.

    Usage in tests::

        provider = MockModerationProvider()
        provider.reject_next = True          # next call will fail
        result = await provider.moderate(b"...")
        assert not result.passed
    """

    def __init__(self) -> None:
        # When True the next ``moderate`` call returns a rejection result.
        # Reset to False after use so each test is independent.
        self.reject_next: bool = False

    async def moderate(self, image_bytes: bytes) -> ModerationResult:  # noqa: ARG002
        """Return a passing result, or a failing result if ``reject_next`` is set."""
        if self.reject_next:
            self.reject_next = False
            logger.debug("MockModerationProvider: simulated rejection")
            return ModerationResult(
                passed=False,
                categories={"Explicit Nudity": 0.99},
            )
        logger.debug("MockModerationProvider: image passed (mock)")
        return ModerationResult(passed=True, categories={})


# ---------------------------------------------------------------------------
# RekognitionModerationProvider — production (AWS Free Tier eligible)
# ---------------------------------------------------------------------------

# Moderation label categories that cause an automatic rejection when their
# confidence score meets or exceeds the threshold.
#
# Conservative defaults aligned with humanitarian field use:
# violence and explicit content should never appear in a damage-report dataset.
_REJECTION_THRESHOLDS: dict[str, float] = {
    "Explicit Nudity": 0.50,
    "Nudity": 0.50,
    "Graphic Male Nudity": 0.50,
    "Graphic Female Nudity": 0.50,
    "Sexual Activity": 0.50,
    "Illustrated Explicit Nudity": 0.50,
    "Adult Toys": 0.75,
    "Violence": 0.70,
    "Graphic Violence Or Gore": 0.60,
    "Physical Violence": 0.65,
    "Weapon Violence": 0.60,
    "Weapons": 0.80,
    "Self Injury": 0.60,
    "Hate Symbols": 0.70,
    "Nazi Party": 0.70,
    "White Supremacy": 0.70,
    "Extremist": 0.70,
}


class RekognitionModerationProvider:
    """Production content moderation backed by AWS Rekognition.

    Uses ``aiobotocore`` for fully async I/O so the FastAPI event loop is
    never blocked.

    Requirements:
        ``pip install aiobotocore``

    Required environment variables:
        ``AWS_REGION``, ``AWS_ACCESS_KEY_ID``, ``AWS_SECRET_ACCESS_KEY``

    AWS Free Tier:
        5,000 images/month for 12 months at no cost.  See module docstring.

    Raises:
        ModerationError: On any Rekognition or network error.
    """

    # Minimum Rekognition confidence score to consider a label detected.
    # Labels returned below this value are discarded as noise.
    _MIN_CONFIDENCE: float = 50.0

    async def moderate(self, image_bytes: bytes) -> ModerationResult:
        """Send *image_bytes* to Rekognition DetectModerationLabels.

        Args:
            image_bytes: Raw image bytes (JPEG recommended).

        Returns:
            ``ModerationResult`` indicating pass/fail and detected label scores.

        Raises:
            ModerationError: On Rekognition API or network error.
        """
        try:
            import aiobotocore.session  # type: ignore[import]
        except ImportError as exc:
            raise ModerationError(
                "aiobotocore is required for MODERATION_PROVIDER=rekognition. "
                "Install it with: pip install aiobotocore"
            ) from exc

        session = aiobotocore.session.get_session()
        try:
            async with session.create_client(
                "rekognition",
                region_name=settings.AWS_REGION,
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            ) as client:
                response = await client.detect_moderation_labels(
                    Image={"Bytes": image_bytes},
                    MinConfidence=self._MIN_CONFIDENCE,
                )
        except Exception as exc:
            logger.error("Rekognition moderation error: %s", type(exc).__name__)
            raise ModerationError(
                f"AWS Rekognition moderation failed: {type(exc).__name__}"
            ) from exc

        # Build a flat label → confidence mapping from the nested response.
        categories: dict[str, float] = {
            label["Name"]: label["Confidence"] / 100.0
            for label in response.get("ModerationLabels", [])
        }

        # Evaluate against thresholds.
        for label, confidence in categories.items():
            threshold = _REJECTION_THRESHOLDS.get(label, 1.0)
            if confidence >= threshold:
                logger.info(
                    "Moderation rejection — label: %s, confidence: %.2f",
                    label,
                    confidence,
                )
                return ModerationResult(passed=False, categories=categories)

        logger.debug(
            "Rekognition moderation passed (%d labels below threshold)",
            len(categories),
        )
        return ModerationResult(passed=True, categories=categories)


# ---------------------------------------------------------------------------
# Provider factory
# ---------------------------------------------------------------------------


def get_moderation_provider() -> ModerationProvider:
    """Return the moderation provider selected by ``MODERATION_PROVIDER``.

    Supported values:
    * ``mock``        — ``MockModerationProvider`` (default, development/CI)
    * ``rekognition`` — ``RekognitionModerationProvider`` (production, AWS Free Tier)

    Returns:
        An object satisfying the ``ModerationProvider`` Protocol.

    Raises:
        ValueError: If ``MODERATION_PROVIDER`` is set to an unknown value.
    """
    provider_name = settings.MODERATION_PROVIDER.lower()

    if provider_name == "mock":
        return MockModerationProvider()
    if provider_name == "rekognition":
        return RekognitionModerationProvider()

    raise ValueError(
        f"Unknown MODERATION_PROVIDER value: '{provider_name}'. "
        "Supported options: 'mock', 'rekognition'."
    )
