"""Add offline_area table.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-30

Areas whose building outlines phones save in advance: field reporters'
home areas (matched by code prefix) and areas at risk.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "offline_area",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False, server_default="risk"),
        sa.Column("code_prefix", sa.String(8), nullable=True),
        sa.Column("min_lat", sa.Float(), nullable=False),
        sa.Column("min_lng", sa.Float(), nullable=False),
        sa.Column("max_lat", sa.Float(), nullable=False),
        sa.Column("max_lng", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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


def downgrade() -> None:
    op.drop_table("offline_area")
