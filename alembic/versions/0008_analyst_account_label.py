"""Add analyst_accounts.label.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-14

analyst_accounts stores only a salted hash of the login email — by design,
the plaintext is never persisted (see 0007). That leaves admins with no way
to recognise which account belongs to whom in `list-accounts` beyond a bare
UUID. `label` is an optional, operator-supplied display name (e.g.
"ops-lead-nairobi") set at provisioning time — never the email itself — so
the no-plaintext-identifier guarantee holds.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "analyst_accounts",
        sa.Column("label", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("analyst_accounts", "label")
