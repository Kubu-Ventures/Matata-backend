"""Deterministic model surrogates for pipeline validation.

These providers exist **only** to exercise and measure the CrisisMap
*decision pipeline* — analyst-queue routing, the divergence flag, the
duplicate/merge-review path, the "insufficient quality" notification path,
and the moderation-before-storage guarantee — independently of any specific
production ML model's accuracy.

They are NOT models. Each returns a value that is a pure, deterministic
function of ``sha256(image_bytes)``, chosen so that a realistic spread of
outcomes lands in every routing branch. Because the mapping is deterministic,
a given synthetic image always produces the same result, so evaluation runs
are reproducible and the "correct" routing decision for every report can be
recomputed offline from the stored ``ai_*`` columns and compared to what the
pipeline actually did.

Activation (never a production default):
    VISION_PROVIDER=sim       → SimVisionProvider
    MODERATION_PROVIDER=sim   → SimModerationProvider

Target output distribution (SimVisionProvider), by construction:
    quality_flag:  ~84% usable, ~12% borderline, ~4% unusable
    ai_confidence: ~70% ≥ 0.80, ~20% 0.60–0.79, ~10% < 0.60
    divergence:    ~15% of usable/borderline images predict a severity that
                   differs from the reporter's classification

SimModerationProvider rejects ~3% of images (deterministically) and passes
the rest, so the "checked before stored" guarantee can be measured against a
non-zero rejection rate.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Literal

from app.services.moderation_service import ModerationResult
from app.services.vision_service import ImageAnalysisResult

_QualityFlag = Literal["usable", "borderline", "unusable"]

logger = logging.getLogger(__name__)

_SEVERITIES = ("minimal", "partial", "destroyed")


def _digest(image_bytes: bytes) -> bytes:
    return hashlib.sha256(image_bytes).digest()


def _u01(byte: int) -> float:
    """Map a byte to a float in [0, 1)."""
    return byte / 256.0


class SimVisionProvider:
    """Deterministic vision surrogate — see module docstring.

    Not a model. ``analyse_damage_image`` ignores image content beyond its
    hash and returns a value spread designed to hit every analyst-routing
    branch in ``app/workers/ai_tasks.py``.
    """

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        d = _digest(image_bytes)

        # ── quality ──────────────────────────────────────────────────────────
        q_roll = _u01(d[0])
        quality_flag: _QualityFlag
        if q_roll < 0.04:
            quality_score = 0.05 + _u01(d[1]) * 0.24  # 0.05–0.29  → unusable
            quality_flag = "unusable"
        elif q_roll < 0.16:
            quality_score = 0.30 + _u01(d[1]) * 0.29  # 0.30–0.59  → borderline
            quality_flag = "borderline"
        else:
            quality_score = 0.60 + _u01(d[1]) * 0.38  # 0.60–0.98  → usable
            quality_flag = "usable"

        # ── confidence ───────────────────────────────────────────────────────
        c_roll = _u01(d[2])
        if c_roll < 0.10:
            ai_confidence = 0.35 + _u01(d[3]) * 0.24  # 0.35–0.59  → critical band
        elif c_roll < 0.30:
            ai_confidence = 0.60 + _u01(d[3]) * 0.19  # 0.60–0.79  → high band
        else:
            ai_confidence = 0.80 + _u01(d[3]) * 0.19  # 0.80–0.99  → low band

        # ── severity prediction ──────────────────────────────────────────────
        reporter_severity = (
            reporter_severity if reporter_severity in _SEVERITIES else "partial"
        )
        if quality_flag == "unusable":
            # An unusable image cannot support a prediction; mirror the
            # reporter so the divergence flag is driven purely by the quality
            # branch in the router.
            ai_severity_prediction = reporter_severity
        elif _u01(d[4]) < 0.15:
            # Deterministically pick a *different* valid severity.
            others = [s for s in _SEVERITIES if s != reporter_severity]
            ai_severity_prediction = others[d[5] % len(others)]
        else:
            ai_severity_prediction = reporter_severity

        return ImageAnalysisResult(
            quality_score=round(quality_score, 4),
            quality_flag=quality_flag,
            ai_severity_prediction=ai_severity_prediction,
            ai_confidence=round(ai_confidence, 4),
            raw_response={"sim": True},
        )


class SimModerationProvider:
    """Deterministic moderation surrogate — rejects ~3% of images by hash."""

    _REJECT_CUTOFF = 8  # first digest byte < 8  → ~3.1% rejected

    async def moderate(self, image_bytes: bytes) -> ModerationResult:
        d = _digest(image_bytes)
        if d[0] < self._REJECT_CUTOFF:
            logger.debug("SimModerationProvider: deterministic rejection")
            return ModerationResult(
                passed=False,
                categories={"Simulated Prohibited Content": 0.97},
            )
        return ModerationResult(passed=True, categories={})
