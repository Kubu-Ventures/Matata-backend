"""Add report.reporter_building_missing.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-07

Set when the reporter says on the building picker that their building is not
on the map. The GIS worker then does not attach the report to a neighbouring
footprint, and these reports can be exported as a layer of possible mapping
gaps for OSM mappers.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "report",
        sa.Column(
            "reporter_building_missing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("report", "reporter_building_missing")
