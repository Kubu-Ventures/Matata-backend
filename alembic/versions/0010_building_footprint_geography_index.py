"""Add a GiST index on building.footprint cast to geography.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-07

Every footprint-matching query filters with
``ST_DWithin(footprint::geography, <point>::geography, <metres>)``. The
cast means the plain geometry index ``ix_building_footprint`` is never
used, so each match was a sequential scan of the whole building table:
about 300 ms on 6,145 Nairobi OSM buildings, growing linearly with a
country-scale import. An expression index on the same cast lets the
planner use an index scan (about 40 ms on the same data, cold).

Building index creation blocks writes to ``building`` while it runs. That
table is written only by the footprint importer and the GIS worker's
severity update, so run this before or between bulk imports.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_building_footprint_geog",
        "building",
        [sa.text("(footprint::geography)")],
        postgresql_using="gist",
    )


def downgrade() -> None:
    op.drop_index("ix_building_footprint_geog", table_name="building")
