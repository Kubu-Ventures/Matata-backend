"""Create analyst_accounts table.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-13

Analyst, responder, and admin identities are stored as hashed phone numbers
so the OTP flow can issue elevated-role JWTs without any password storage.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "analyst_accounts",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("phone_hash", sa.Text, nullable=False, unique=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("region_geojson", sa.Text, nullable=True),
        sa.Column("created_by_sub", sa.Text, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
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
    op.create_index(
        "ix_analyst_accounts_phone_hash",
        "analyst_accounts",
        ["phone_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_analyst_accounts_phone_hash", table_name="analyst_accounts")
    op.drop_table("analyst_accounts")
