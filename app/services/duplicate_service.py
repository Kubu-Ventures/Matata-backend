"""Duplicate detection service — spec §10.

``DuplicateScorer`` computes a weighted composite similarity score between a
newly submitted report and a pool of candidate reports, then recommends the
appropriate action based on configured thresholds.

Scoring signals (spec §10.1)
-----------------------------
| Signal              | Weight | Method                                      |
|---------------------|--------|---------------------------------------------|
| Building footprint  |  40 %  | Exact match on ``building_id``              |
| GPS proximity       |  30 %  | Haversine distance, linear decay 0 → 50 m   |
| Image similarity    |  20 %  | pHash Hamming distance, decay 10 → 30 bits  |
| Damage category     |  10 %  | crisis_type + infrastructure_type agreement |

Composite = 0.4 × building + 0.3 × gps + 0.2 × image + 0.1 × category

Thresholds and actions (spec §10.2)
-------------------------------------
≥ 0.9  → AUTO_MERGE  — new report marked duplicate; primary photo updated.
0.6–0.9 → FLAG        — stored independently with ``possible_duplicate_of_id``.
< 0.6  → INDEPENDENT  — stored as a new unique incident.

Design notes
------------
* ``DuplicateScorer`` is a pure computation class: it receives data, returns
  results, and performs no I/O.  All database and audit-log writes are the
  responsibility of the Celery task (``duplicate_tasks.score_report``).
* pHash values are stored as hex strings (16 chars = 64-bit hash).  The
  Hamming distance is computed directly from the integer XOR of the two hashes.
* ``mypy --strict`` compatible: all public callables are fully annotated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from uuid import UUID

from app.utils.geo import haversine_distance_m

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants — threshold values from spec §10.1 and §10.2
# ---------------------------------------------------------------------------

# Composite score thresholds
_THRESHOLD_AUTO_MERGE: float = 0.9
_THRESHOLD_ANALYST_FLAG: float = 0.6

# GPS proximity decay: 1.0 at 0 m, 0.0 at GPS_DECAY_MAX_M
_GPS_DECAY_MAX_M: float = 50.0

# pHash Hamming distance: score 1.0 below _PHASH_PERFECT, 0.0 at _PHASH_ZERO
_PHASH_PERFECT: int = 10
_PHASH_ZERO: int = 30

# Composite signal weights — must sum to 1.0
_W_BUILDING: float = 0.4
_W_GPS: float = 0.3
_W_IMAGE: float = 0.2
_W_CATEGORY: float = 0.1


# ---------------------------------------------------------------------------
# Public enumerations and data classes
# ---------------------------------------------------------------------------


class DuplicateAction(str, Enum):
    """Action to take based on the composite duplicate score."""

    AUTO_MERGE = "auto_merge"  # score ≥ 0.9
    ANALYST_FLAG = "analyst_flag"  # 0.6 ≤ score < 0.9
    INDEPENDENT = "independent"  # score < 0.6


@dataclass(frozen=True)
class CandidateReport:
    """Snapshot of an existing report used as a scoring candidate.

    All fields that are ``None`` simply contribute a zero score for the
    relevant signal — the scorer degrades gracefully for partial data.

    Attributes:
        id:                  UUID of the existing report.
        building_id:         Matched building UUID, or ``None`` if unresolved.
        lat:                 WGS84 latitude, or ``None``.
        lng:                 WGS84 longitude, or ``None``.
        photo_phash:         64-bit perceptual hash as a 16-char hex string,
                             or ``None`` if no photo or hash not yet computed.
        crisis_type:         Controlled vocabulary value (e.g. ``"flood"``).
        infrastructure_type: Controlled vocabulary value (e.g. ``"residential"``).
    """

    id: UUID
    building_id: Optional[UUID]
    lat: Optional[float]
    lng: Optional[float]
    photo_phash: Optional[str]
    crisis_type: str
    infrastructure_type: str


@dataclass
class ScoredCandidate:
    """Result of scoring one candidate against the incoming report.

    Attributes:
        candidate:        The candidate report that was scored.
        composite_score:  Weighted composite in [0.0, 1.0].
        building_score:   Raw building signal (0 or 1).
        gps_score:        Raw GPS proximity signal [0.0, 1.0].
        image_score:      Raw image similarity signal [0.0, 1.0].
        category_score:   Raw category agreement signal (0, 0.5, or 1.0).
    """

    candidate: CandidateReport
    composite_score: float
    building_score: float = 0.0
    gps_score: float = 0.0
    image_score: float = 0.0
    category_score: float = 0.0


@dataclass
class DuplicateResult:
    """Output of ``DuplicateScorer.score``.

    Attributes:
        action:          Recommended action based on the best score found.
        best_score:      Highest composite score across all candidates
                         (0.0 when no candidates were evaluated).
        best_candidate:  The candidate with the highest score, or ``None``
                         when ``action == INDEPENDENT``.
        all_scores:      Full list of ``ScoredCandidate`` objects, sorted
                         descending by ``composite_score``.
    """

    action: DuplicateAction
    best_score: float
    best_candidate: Optional[ScoredCandidate]
    all_scores: list[ScoredCandidate] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Internal signal helpers
# ---------------------------------------------------------------------------


def _building_signal(
    incoming_building_id: Optional[UUID],
    candidate_building_id: Optional[UUID],
) -> float:
    """Return 1.0 if both reports share a resolved building ID, else 0.0."""
    if incoming_building_id is None or candidate_building_id is None:
        return 0.0
    return 1.0 if incoming_building_id == candidate_building_id else 0.0


def _gps_signal(
    lat1: Optional[float],
    lng1: Optional[float],
    lat2: Optional[float],
    lng2: Optional[float],
) -> float:
    """GPS proximity signal: 1.0 at 0 m, linear decay to 0.0 at 50 m."""
    if lat1 is None or lng1 is None or lat2 is None or lng2 is None:
        return 0.0
    distance_m = haversine_distance_m(lat1, lng1, lat2, lng2)
    return max(0.0, 1.0 - distance_m / _GPS_DECAY_MAX_M)


def _hamming_distance(hex_a: str, hex_b: str) -> int:
    """Return the bit-level Hamming distance between two hex-encoded hashes.

    Args:
        hex_a: First hash as a hex string (any even length).
        hex_b: Second hash as a hex string (same length as ``hex_a``).

    Returns:
        Number of differing bits (≥ 0).  Returns the maximum possible distance
        if the strings have different lengths (graceful degradation).
    """
    if len(hex_a) != len(hex_b):
        logger.warning(
            "pHash length mismatch (%d vs %d) — treating as maximum distance",
            len(hex_a),
            len(hex_b),
        )
        return len(hex_a) * 4  # Each hex char = 4 bits

    xor_val = int(hex_a, 16) ^ int(hex_b, 16)
    return bin(xor_val).count("1")


def _image_signal(phash_a: Optional[str], phash_b: Optional[str]) -> float:
    """Image similarity signal based on pHash Hamming distance.

    Score = 1.0 when Hamming distance < 10 (visually identical).
    Score = 0.0 when Hamming distance ≥ 30 (visually dissimilar).
    Linear interpolation in between.

    If either hash is absent, returns 0.0 (no signal — do not penalise).
    """
    if not phash_a or not phash_b:
        return 0.0
    try:
        distance = _hamming_distance(phash_a, phash_b)
    except ValueError:
        logger.warning("Invalid pHash value — skipping image signal")
        return 0.0

    if distance < _PHASH_PERFECT:
        return 1.0
    if distance >= _PHASH_ZERO:
        return 0.0
    # Linear decay between _PHASH_PERFECT and _PHASH_ZERO
    return 1.0 - (distance - _PHASH_PERFECT) / (_PHASH_ZERO - _PHASH_PERFECT)


def _category_signal(
    crisis_type_a: str,
    infra_type_a: str,
    crisis_type_b: str,
    infra_type_b: str,
) -> float:
    """Damage category agreement signal.

    Returns:
        1.0 if both ``crisis_type`` AND ``infrastructure_type`` match.
        0.5 if exactly one matches.
        0.0 if neither matches.
    """
    crisis_match = crisis_type_a == crisis_type_b
    infra_match = infra_type_a == infra_type_b
    matches = int(crisis_match) + int(infra_match)
    return matches * 0.5


# ---------------------------------------------------------------------------
# DuplicateScorer
# ---------------------------------------------------------------------------


class DuplicateScorer:
    """Stateless scorer that evaluates incoming reports against candidates.

    Usage::

        scorer = DuplicateScorer()
        result = scorer.score(
            incoming_building_id=UUID("..."),
            incoming_lat=-1.29,
            incoming_lng=36.82,
            incoming_phash="a1b2c3d4e5f60708",
            incoming_crisis_type="flood",
            incoming_infrastructure_type="residential",
            candidates=[...],
        )
        if result.action == DuplicateAction.AUTO_MERGE:
            # merge into result.best_candidate.candidate.id
            ...
    """

    def score(
        self,
        *,
        incoming_building_id: Optional[UUID],
        incoming_lat: Optional[float],
        incoming_lng: Optional[float],
        incoming_phash: Optional[str],
        incoming_crisis_type: str,
        incoming_infrastructure_type: str,
        candidates: list[CandidateReport],
    ) -> DuplicateResult:
        """Score the incoming report against each candidate.

        Args:
            incoming_building_id:          Resolved building UUID (may be None).
            incoming_lat:                  WGS84 latitude (may be None).
            incoming_lng:                  WGS84 longitude (may be None).
            incoming_phash:                64-bit perceptual hash hex string
                                           (may be None).
            incoming_crisis_type:          Controlled vocabulary value.
            incoming_infrastructure_type:  Controlled vocabulary value.
            candidates:                    Pool of existing reports to score
                                           against.  May be empty.

        Returns:
            ``DuplicateResult`` with the recommended action and full score list.
        """
        if not candidates:
            logger.debug("No candidates — treating report as independent")
            return DuplicateResult(
                action=DuplicateAction.INDEPENDENT,
                best_score=0.0,
                best_candidate=None,
                all_scores=[],
            )

        scored: list[ScoredCandidate] = []

        for candidate in candidates:
            b = _building_signal(incoming_building_id, candidate.building_id)
            g = _gps_signal(incoming_lat, incoming_lng, candidate.lat, candidate.lng)
            i = _image_signal(incoming_phash, candidate.photo_phash)
            c = _category_signal(
                incoming_crisis_type,
                incoming_infrastructure_type,
                candidate.crisis_type,
                candidate.infrastructure_type,
            )

            composite = _W_BUILDING * b + _W_GPS * g + _W_IMAGE * i + _W_CATEGORY * c

            scored.append(
                ScoredCandidate(
                    candidate=candidate,
                    composite_score=round(composite, 6),
                    building_score=b,
                    gps_score=round(g, 6),
                    image_score=round(i, 6),
                    category_score=c,
                )
            )

        # Sort descending by composite score
        scored.sort(key=lambda s: s.composite_score, reverse=True)
        best = scored[0]

        logger.debug(
            "Duplicate scoring: %d candidates evaluated, best=%.4f (candidate=%s)",
            len(scored),
            best.composite_score,
            best.candidate.id,
        )

        if best.composite_score >= _THRESHOLD_AUTO_MERGE:
            action = DuplicateAction.AUTO_MERGE
        elif best.composite_score >= _THRESHOLD_ANALYST_FLAG:
            action = DuplicateAction.ANALYST_FLAG
        else:
            action = DuplicateAction.INDEPENDENT

        return DuplicateResult(
            action=action,
            best_score=best.composite_score,
            best_candidate=best if action != DuplicateAction.INDEPENDENT else None,
            all_scores=scored,
        )
