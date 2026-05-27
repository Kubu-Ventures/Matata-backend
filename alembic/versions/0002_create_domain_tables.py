"""Create all CrisisMap domain tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Enum definitions — (name, values) pairs.
# ---------------------------------------------------------------------------
_ENUMS = [
    ("building_source_enum", ["microsoft_africa", "osm", "manual"]),
    ("damage_severity_enum", ["none", "minimal", "partial", "destroyed"]),
    ("crisis_type_enum", ["flood", "earthquake", "conflict", "wildfire", "other"]),
    (
        "infrastructure_type_enum",
        ["residential", "commercial", "government", "utilities", "transport", "community"],
    ),
    ("report_damage_severity_enum", ["minimal", "partial", "destroyed"]),
    ("electricity_status_enum", ["functional", "non_functional", "unknown"]),
    ("health_services_status_enum", ["accessible", "inaccessible", "unknown"]),
    (
        "photo_status_enum",
        [
            "pending",
            "processing",
            "accepted",
            "rejected",
            "insufficient_quality",
            "ai_processing_failed",
        ],
    ),
    ("report_status_enum", ["pending", "verified", "rejected", "duplicate"]),
    ("notification_type_enum", ["analyst_alert", "reporter_photo_request"]),
    ("notification_status_enum", ["pending", "sent", "failed"]),
]


def _create_enums() -> None:
    """Create all enums idempotently via a PL/pgSQL existence check.

    CREATE TYPE has no IF NOT EXISTS clause (unlike CREATE TABLE), so we
    use a DO block that queries pg_type before issuing the CREATE. This is
    safe across repeated CI runs and partial-migration rollback scenarios.
    """
    for name, values in _ENUMS:
        quoted = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_type WHERE typname = '{name}'
                ) THEN
                    CREATE TYPE {name} AS ENUM ({quoted});
                END IF;
            END$$;
            """
        )


def _drop_enums() -> None:
    """Drop all enums in reverse creation order, ignoring missing types."""
    for name, _values in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")


def upgrade() -> None:
    _create_enums()

    # ── building ─────────────────────────────────────────────────────────────
    op.create_table(
        "building",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "footprint",
            Geometry("POLYGON", srid=4326),
            nullable=False,
        ),
        sa.Column(
            "centroid",
            Geometry("POINT", srid=4326),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.Enum(name="building_source_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("external_id", sa.String, nullable=False),
        sa.Column(
            "current_severity",
            sa.Enum(name="damage_severity_enum", create_type=False),
            nullable=False,
            server_default="none",
        ),
        sa.Column("last_report_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("external_id", name="uq_building_external_id"),
    )
    op.create_index(
        "ix_building_footprint", "building", ["footprint"], postgresql_using="gist"
    )
    op.create_index(
        "ix_building_centroid", "building", ["centroid"], postgresql_using="gist"
    )

    # ── report ────────────────────────────────────────────────────────────────
    op.create_table(
        "report",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "building_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("building.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "crisis_type",
            sa.Enum(name="crisis_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "infrastructure_type",
            sa.Enum(name="infrastructure_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "damage_severity",
            sa.Enum(name="report_damage_severity_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lng", sa.Float, nullable=False),
        sa.Column("gps_accuracy_m", sa.Float, nullable=True),
        sa.Column("landmark_description", sa.Text, nullable=True),
        sa.Column(
            "electricity_status",
            sa.Enum(name="electricity_status_enum", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "health_services_status",
            sa.Enum(name="health_services_status_enum", create_type=False),
            nullable=True,
        ),
        sa.Column("most_pressing_needs", sa.Text, nullable=True),
        sa.Column("debris_clearing_needed", sa.Boolean, nullable=True),
        sa.Column("photo_url", sa.String, nullable=True),
        sa.Column("photo_phash", sa.String(64), nullable=True),
        sa.Column(
            "photo_status",
            sa.Enum(name="photo_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "status",
            sa.Enum(name="report_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("reporter_token_hash", sa.String, nullable=False),
        sa.Column(
            "reporter_trust_tier", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("offline_queued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "ai_severity_prediction",
            sa.Enum(name="report_damage_severity_enum", create_type=False),
            nullable=True,
        ),
        sa.Column("ai_confidence", sa.Float, nullable=True),
        sa.Column("ai_quality_score", sa.Float, nullable=True),
        sa.Column("ai_divergence", sa.Boolean, nullable=True),
        sa.Column("footprint_match_confidence", sa.Float, nullable=True),
        sa.Column("duplicate_of_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("possible_duplicate_of_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_score", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_foreign_key(
        "fk_report_duplicate_of",
        "report", "report",
        ["duplicate_of_id"], ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_report_possible_duplicate_of",
        "report", "report",
        ["possible_duplicate_of_id"], ["id"],
        ondelete="SET NULL",
    )

    op.create_index("ix_report_building_id", "report", ["building_id"])
    op.create_index("ix_report_status", "report", ["status"])
    op.create_index(
        "ix_report_created_at",
        "report",
        [sa.text("created_at DESC")],
    )
    op.create_index("ix_report_lat_lng", "report", ["lat", "lng"])

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("operation", sa.String, nullable=False),
        sa.Column("actor_id_hash", sa.String, nullable=False),
        sa.Column("record_id", sa.UUID(as_uuid=True), nullable=False),
        sa.Column("before_state", JSONB, nullable=True),
        sa.Column("after_state", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.execute("REVOKE DELETE ON audit_log FROM app_user")

    # ── analyst_note ──────────────────────────────────────────────────────────
    op.create_table(
        "analyst_note",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "report_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("analyst_id_hash", sa.String, nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_analyst_note_report_id", "analyst_note", ["report_id"])

    # ── notification ──────────────────────────────────────────────────────────
    op.create_table(
        "notification",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "type",
            sa.Enum(name="notification_type_enum", create_type=False),
            nullable=False,
        ),
        sa.Column("recipient_hash", sa.String, nullable=False),
        sa.Column(
            "report_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(name="notification_status_enum", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_notification_report_id", "notification", ["report_id"])


def downgrade() -> None:
    op.execute("GRANT DELETE ON audit_log TO app_user")

    op.drop_table("notification")
    op.drop_table("analyst_note")
    op.drop_table("audit_log")

    op.drop_constraint("fk_report_duplicate_of", "report", type_="foreignkey")
    op.drop_constraint("fk_report_possible_duplicate_of", "report", type_="foreignkey")
    op.drop_table("report")
    op.drop_table("building")

    _drop_enums()
