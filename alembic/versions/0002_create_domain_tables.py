"""Create all CrisisMap domain tables.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-27

Covers: building, report (with self-referential duplicate FKs),
audit_log, analyst_note, notification.

Security note: The application database user (app_user) is explicitly
denied DELETE on audit_log at the end of upgrade(). downgrade() re-grants
the permission before dropping the table so the drop can proceed cleanly
in CI and local teardown scenarios.

Enum strategy: All PostgreSQL enum types are created and dropped via raw
SQL only. Column definitions use sa.Text() with an explicit
postgresql_server_default / type cast so SQLAlchemy never touches enum
DDL. This is the only reliable way to prevent asyncpg + SQLAlchemy from
firing spurious CREATE TYPE statements regardless of create_type=False.
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
# Enum definitions — order matters for creation; reversed for teardown.
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


def _enum_col(type_name: str, **kwargs) -> sa.Column:
    """Return a Column backed by a pre-existing PostgreSQL enum type.

    Uses sa.Text() as the SQLAlchemy-side type so that no CREATE TYPE DDL
    is ever emitted by SQLAlchemy. The column is then altered via raw SQL
    to cast it to the correct enum type after the table is created.
    We instead pass the type name via type_=sa.text(...) using the
    postgresql ENUM approach through explicit SQL type reference.
    """
    from sqlalchemy.dialects.postgresql import ENUM as PG_ENUM
    return sa.Column(
        PG_ENUM(name=type_name, create_type=False),
        **kwargs,
    )


def _create_enums() -> None:
    """Create all enum types idempotently using a PL/pgSQL existence guard.

    CREATE TYPE has no IF NOT EXISTS clause, so we use a DO block that
    checks pg_type first. This survives repeated CI runs and partial
    rollback scenarios without error.
    """
    for name, values in _ENUMS:
        quoted_values = ", ".join(f"'{v}'" for v in values)
        op.execute(
            f"""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_type WHERE typname = '{name}'
                ) THEN
                    CREATE TYPE {name} AS ENUM ({quoted_values});
                END IF;
            END$$;
            """
        )


def _drop_enums() -> None:
    """Drop all enum types in reverse creation order, skipping missing ones."""
    for name, _values in reversed(_ENUMS):
        op.execute(f"DROP TYPE IF EXISTS {name}")


def upgrade() -> None:
    # Create all enum types first via raw SQL so they exist before any table
    # DDL fires. Column definitions below reference them by name only.
    _create_enums()

    # ── building ──────────────────────────────────────────────────────────────
    op.create_table(
        "building",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("footprint", Geometry("POLYGON", srid=4326), nullable=False),
        sa.Column("centroid", Geometry("POINT", srid=4326), nullable=False),
        sa.Column(
            "source",
            sa.Text,
            nullable=False,
        ),
        sa.Column("external_id", sa.String, nullable=False),
        sa.Column(
            "current_severity",
            sa.Text,
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

    # Cast text columns to their proper enum types now that both exist.
    #
    # FIX: current_severity has a text server_default ('none') that PostgreSQL
    # cannot automatically cast when changing the column type to an enum — even
    # though 'none' is a valid damage_severity_enum value. The default must be
    # dropped first, the type changed, then the default re-applied with an
    # explicit enum cast. source has no default so it can be converted inline.
    op.execute(
        "ALTER TABLE building "
        "ALTER COLUMN source TYPE building_source_enum "
        "    USING source::building_source_enum, "
        "ALTER COLUMN current_severity DROP DEFAULT"
    )
    op.execute(
        "ALTER TABLE building "
        "ALTER COLUMN current_severity TYPE damage_severity_enum "
        "    USING current_severity::damage_severity_enum"
    )
    op.execute(
        "ALTER TABLE building "
        "ALTER COLUMN current_severity SET DEFAULT 'none'::damage_severity_enum"
    )

    op.create_index("ix_building_footprint", "building", ["footprint"], postgresql_using="gist")
    op.create_index("ix_building_centroid", "building", ["centroid"], postgresql_using="gist")

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
        sa.Column("crisis_type",           sa.Text, nullable=False),
        sa.Column("infrastructure_type",   sa.Text, nullable=False),
        sa.Column("damage_severity",       sa.Text, nullable=False),
        sa.Column("lat",                   sa.Float, nullable=False),
        sa.Column("lng",                   sa.Float, nullable=False),
        sa.Column("gps_accuracy_m",        sa.Float, nullable=True),
        sa.Column("landmark_description",  sa.Text, nullable=True),
        sa.Column("electricity_status",    sa.Text, nullable=True),
        sa.Column("health_services_status", sa.Text, nullable=True),
        sa.Column("most_pressing_needs",   sa.Text, nullable=True),
        sa.Column("debris_clearing_needed", sa.Boolean, nullable=True),
        sa.Column("photo_url",             sa.String, nullable=True),
        sa.Column("photo_phash",           sa.String(64), nullable=True),
        sa.Column("photo_status",          sa.Text, nullable=False, server_default="pending"),
        sa.Column("status",                sa.Text, nullable=False, server_default="pending"),
        sa.Column("reporter_token_hash",   sa.String, nullable=False),
        sa.Column("reporter_trust_tier",   sa.Integer, nullable=False, server_default="0"),
        sa.Column("offline_queued_at",     sa.DateTime(timezone=True), nullable=True),
        sa.Column("ai_severity_prediction", sa.Text, nullable=True),
        sa.Column("ai_confidence",         sa.Float, nullable=True),
        sa.Column("ai_quality_score",      sa.Float, nullable=True),
        sa.Column("ai_divergence",         sa.Boolean, nullable=True),
        sa.Column("footprint_match_confidence", sa.Float, nullable=True),
        sa.Column("duplicate_of_id",       sa.UUID(as_uuid=True), nullable=True),
        sa.Column("possible_duplicate_of_id", sa.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicate_score",       sa.Float, nullable=True),
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

    # FIX: photo_status and status have text server_defaults ('pending').
    # Same pattern as current_severity above — drop defaults, retype, restore.
    op.execute(
        """
        ALTER TABLE report
            ALTER COLUMN photo_status DROP DEFAULT,
            ALTER COLUMN status       DROP DEFAULT
        """
    )
    op.execute(
        """
        ALTER TABLE report
            ALTER COLUMN crisis_type            TYPE crisis_type_enum
                USING crisis_type::crisis_type_enum,
            ALTER COLUMN infrastructure_type    TYPE infrastructure_type_enum
                USING infrastructure_type::infrastructure_type_enum,
            ALTER COLUMN damage_severity        TYPE report_damage_severity_enum
                USING damage_severity::report_damage_severity_enum,
            ALTER COLUMN electricity_status     TYPE electricity_status_enum
                USING electricity_status::electricity_status_enum,
            ALTER COLUMN health_services_status TYPE health_services_status_enum
                USING health_services_status::health_services_status_enum,
            ALTER COLUMN photo_status           TYPE photo_status_enum
                USING photo_status::photo_status_enum,
            ALTER COLUMN status                 TYPE report_status_enum
                USING status::report_status_enum,
            ALTER COLUMN ai_severity_prediction TYPE report_damage_severity_enum
                USING ai_severity_prediction::report_damage_severity_enum
        """
    )
    op.execute(
        """
        ALTER TABLE report
            ALTER COLUMN photo_status SET DEFAULT 'pending'::photo_status_enum,
            ALTER COLUMN status       SET DEFAULT 'pending'::report_status_enum
        """
    )

    # Self-referential FK constraints added after table creation.
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
    op.create_index("ix_report_created_at", "report", [sa.text("created_at DESC")])
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
        sa.Column("operation",     sa.String, nullable=False),
        sa.Column("actor_id_hash", sa.String, nullable=False),
        sa.Column("record_id",     sa.UUID(as_uuid=True), nullable=False),
        sa.Column("before_state",  JSONB, nullable=True),
        sa.Column("after_state",   JSONB, nullable=False),
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
    # Hard security constraint: app_user must never delete audit records.
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
        sa.Column("body",            sa.Text, nullable=False),
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
        sa.Column("type",            sa.Text, nullable=False),
        sa.Column("recipient_hash",  sa.String, nullable=False),
        sa.Column(
            "report_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status",  sa.Text, nullable=False, server_default="pending"),
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

    # FIX: status has a text server_default ('pending') — same pattern.
    op.execute("ALTER TABLE notification ALTER COLUMN status DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE notification
            ALTER COLUMN type   TYPE notification_type_enum
                USING type::notification_type_enum,
            ALTER COLUMN status TYPE notification_status_enum
                USING status::notification_status_enum
        """
    )
    op.execute(
        "ALTER TABLE notification "
        "ALTER COLUMN status SET DEFAULT 'pending'::notification_status_enum"
    )

    op.create_index("ix_notification_report_id", "notification", ["report_id"])


def downgrade() -> None:
    # Re-grant DELETE so teardown can proceed cleanly in CI.
    op.execute("GRANT DELETE ON audit_log TO app_user")

    op.drop_table("notification")
    op.drop_table("analyst_note")
    op.drop_table("audit_log")

    op.drop_constraint("fk_report_duplicate_of", "report", type_="foreignkey")
    op.drop_constraint("fk_report_possible_duplicate_of", "report", type_="foreignkey")
    op.drop_table("report")
    op.drop_table("building")

    _drop_enums()
