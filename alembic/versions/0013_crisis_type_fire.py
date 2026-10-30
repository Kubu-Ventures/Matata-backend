"""Add 'fire' to crisis_type_enum.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-30

Building and settlement fires are common in informal settlements and are a
different event from a vegetation 'wildfire'.

PostgreSQL cannot remove a value from an enum type, so downgrade moves any
'fire' reports to 'other' and leaves the value in place.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ALTER TYPE ... ADD VALUE is transactional in PostgreSQL 12+; the new
    # value is not used in this migration, so no autocommit block is needed.
    op.execute(sa.text("ALTER TYPE crisis_type_enum ADD VALUE IF NOT EXISTS 'fire'"))


def downgrade() -> None:
    op.execute(
        sa.text("UPDATE report SET crisis_type = 'other' WHERE crisis_type = 'fire'")
    )
