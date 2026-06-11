"""Prometheus metrics registry for CrisisMap.

All custom metrics are defined here as module-level singletons so that
any module in the application can import and update them without risking
duplicate-registration errors.

Metrics
-------
crisismap_reports_submitted_total
    Counter. Labelled by ``crisis_type`` and ``submission_mode``
    (``online`` / ``offline``).

crisismap_moderation_rejections_total
    Counter. Incremented each time Stage 2 moderation rejects an image.

crisismap_ai_queue_depth
    Gauge. Updated on each task enqueue / dequeue in the AI worker.

crisismap_gis_match_confidence
    Histogram. Labelled by ``match_method``
    (``point_in_polygon`` / ``nearest_neighbour`` / ``landmark`` /
    ``unmatched``). Records the spatial confidence score (0-1).

crisismap_export_duration_seconds
    Histogram. Labelled by ``format`` (``geojson`` / ``csv`` /
    ``shapefile``). Records wall-clock seconds for export generation.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Report submission
# ---------------------------------------------------------------------------

reports_submitted_total = Counter(
    "crisismap_reports_submitted_total",
    "Total number of damage reports submitted.",
    labelnames=["crisis_type", "submission_mode"],
)

# ---------------------------------------------------------------------------
# Content moderation
# ---------------------------------------------------------------------------

moderation_rejections_total = Counter(
    "crisismap_moderation_rejections_total",
    "Total number of images rejected by the content moderation stage.",
)

# ---------------------------------------------------------------------------
# AI processing queue
# ---------------------------------------------------------------------------

ai_queue_depth = Gauge(
    "crisismap_ai_queue_depth",
    "Current depth of the AI processing task queue.",
)

# ---------------------------------------------------------------------------
# GIS building footprint matching
# ---------------------------------------------------------------------------

gis_match_confidence = Histogram(
    "crisismap_gis_match_confidence",
    "Spatial confidence score for GIS building footprint matches (0-1).",
    labelnames=["match_method"],
    buckets=(0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0),
)

# ---------------------------------------------------------------------------
# Export duration
# ---------------------------------------------------------------------------

export_duration_seconds = Histogram(
    "crisismap_export_duration_seconds",
    "Wall-clock seconds for structured data export generation.",
    labelnames=["format"],
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)
