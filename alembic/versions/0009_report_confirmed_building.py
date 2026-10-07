"""Add report.reporter_confirmed_building_id.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-07

Stores the building a reporter picked from the footprint-match candidates on
the report form. Kept separate from ``building_id`` (the GIS worker's resolved
match) so analysts can see whether the reporter confirmed the building. No
foreign key: the value is an unverified client claim, validated by the GIS
worker before it is adopted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "report",
        sa.Column("reporter_confirmed_building_id", sa.UUID(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("report", "reporter_confirmed_building_id")
