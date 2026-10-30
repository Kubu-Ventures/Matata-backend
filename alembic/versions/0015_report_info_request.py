"""Add request-for-more-information columns to report.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-30

An analyst can ask the reporter for more detail. The question and the
reporter's answer are stored beside the original report, which is never
edited. Every round is also kept in analyst notes and the audit log.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("report", sa.Column("info_request_message", sa.Text(), nullable=True))
    op.add_column(
        "report",
        sa.Column("info_requested_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("report", sa.Column("reporter_followup", sa.Text(), nullable=True))
    op.add_column(
        "report",
        sa.Column("info_responded_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("report", "info_responded_at")
    op.drop_column("report", "reporter_followup")
    op.drop_column("report", "info_requested_at")
    op.drop_column("report", "info_request_message")
