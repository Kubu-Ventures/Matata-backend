"""Controlled vocabularies for all CrisisMap domain entities.

All enum classes are defined here and imported from this module in both
SQLAlchemy models and Pydantic schemas to guarantee a single source of truth
across the entire codebase.
"""

import enum


class BuildingSource(str, enum.Enum):
    microsoft_africa = "microsoft_africa"
    osm = "osm"
    manual = "manual"


class DamageSeverity(str, enum.Enum):
    none = "none"
    minimal = "minimal"
    partial = "partial"
    destroyed = "destroyed"


class CrisisType(str, enum.Enum):
    flood = "flood"
    earthquake = "earthquake"
    conflict = "conflict"
    wildfire = "wildfire"
    other = "other"


class InfrastructureType(str, enum.Enum):
    residential = "residential"
    commercial = "commercial"
    government = "government"
    utilities = "utilities"
    transport = "transport"
    community = "community"


class ReportDamageSeverity(str, enum.Enum):
    minimal = "minimal"
    partial = "partial"
    destroyed = "destroyed"


class ElectricityStatus(str, enum.Enum):
    functional = "functional"
    non_functional = "non_functional"
    unknown = "unknown"


class HealthServicesStatus(str, enum.Enum):
    accessible = "accessible"
    inaccessible = "inaccessible"
    unknown = "unknown"


class PhotoStatus(str, enum.Enum):
    pending = "pending"
    processing = "processing"
    accepted = "accepted"
    rejected = "rejected"
    insufficient_quality = "insufficient_quality"
    ai_processing_failed = "ai_processing_failed"


class ReportStatus(str, enum.Enum):
    pending = "pending"
    verified = "verified"
    rejected = "rejected"
    duplicate = "duplicate"
    pending_merge_review = "pending_merge_review"


class ReviewPriority(str, enum.Enum):
    """Analyst queue priority assigned by the AI worker after image processing.

    Routing logic (thresholds from config):
      critical — ai_confidence < 0.60 OR quality_score < 0.30 OR AI failed.
                 Human review is mandatory before any action propagates.
      high     — 0.60 ≤ ai_confidence < 0.80 OR ai_divergence is True.
                 Analyst should review; something needs a second look.
      normal   — Default before AI processes the report.
      low      — ai_confidence ≥ 0.80, no divergence, quality ≥ 0.60.
                 Safe to defer; AI is confident and agrees with reporter.
    """

    critical = "critical"
    high = "high"
    normal = "normal"
    low = "low"


class NotificationType(str, enum.Enum):
    analyst_alert = "analyst_alert"
    reporter_photo_request = "reporter_photo_request"


class NotificationStatus(str, enum.Enum):
    pending = "pending"
    sent = "sent"
    failed = "failed"
