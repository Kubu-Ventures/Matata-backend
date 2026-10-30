"""Add report.medical_help_needed and the health_facility table.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-30

The form now asks whether anyone at the location needs medical help (urgent,
not urgent, no) in place of whether health services are accessible. The old
health_services_status column is kept so existing reports still read.

health_facility holds hospitals, clinics and health centres imported from
OpenStreetMap, used to show the nearest places to refer someone.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE TYPE medical_help_needed_enum AS ENUM "
        "('urgent', 'not_urgent', 'none')"
    )
    op.add_column(
        "report",
        sa.Column(
            "medical_help_needed",
            postgresql.ENUM(
                "urgent",
                "not_urgent",
                "none",
                name="medical_help_needed_enum",
                create_type=False,
            ),
            nullable=True,
        ),
    )

    op.create_table(
        "health_facility",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("external_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("facility_type", sa.String(32), nullable=False),
        sa.Column("lat", sa.Float(), nullable=False),
        sa.Column("lng", sa.Float(), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("opening_hours", sa.Text(), nullable=True),
        sa.Column("source", sa.String(32), nullable=False, server_default="osm"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_health_facility_lat_lng", "health_facility", ["lat", "lng"])


def downgrade() -> None:
    op.drop_index("ix_health_facility_lat_lng", table_name="health_facility")
    op.drop_table("health_facility")
    op.drop_column("report", "medical_help_needed")
    op.execute("DROP TYPE medical_help_needed_enum")
