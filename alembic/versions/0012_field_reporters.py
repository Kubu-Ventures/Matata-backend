"""Add field_reporters table and report.field_reporter_code.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-30

Paid pilot reporters are registered under a short code (e.g. KIB-01) keyed by
their salted email hash. Reports they send are tagged with the code so report
counts per code can be exported for payment. No names or contact details are
stored.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "field_reporters",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False),
        sa.Column("email_hash", sa.Text(), nullable=False),
        sa.Column("created_by_sub", sa.Text(), nullable=True),
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
    op.create_index("ix_field_reporters_code", "field_reporters", ["code"], unique=True)
    op.create_index(
        "ix_field_reporters_email_hash",
        "field_reporters",
        ["email_hash"],
        unique=True,
    )

    op.add_column(
        "report", sa.Column("field_reporter_code", sa.String(32), nullable=True)
    )
    op.create_index("ix_report_field_reporter_code", "report", ["field_reporter_code"])


def downgrade() -> None:
    op.drop_index("ix_report_field_reporter_code", table_name="report")
    op.drop_column("report", "field_reporter_code")
    op.drop_index("ix_field_reporters_email_hash", table_name="field_reporters")
    op.drop_index("ix_field_reporters_code", table_name="field_reporters")
    op.drop_table("field_reporters")
