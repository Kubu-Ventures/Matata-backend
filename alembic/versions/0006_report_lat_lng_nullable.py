"""Make report.lat and report.lng nullable.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-07

ReportCreateSchema.require_location already allows a report to carry a
landmark_description instead of GPS coordinates when GPS is unavailable to
the reporter, and every downstream consumer (analyst_service, export_service,
duplicate_service, the GIS/AI/notification workers) already treats lat/lng as
Optional. The report table's NOT NULL constraint on these two columns was
never updated to match, so any landmark-only submission fails with a
NotNullViolationError at INSERT time.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("report", "lat", existing_type=sa.Float(), nullable=True)
    op.alter_column("report", "lng", existing_type=sa.Float(), nullable=True)


def downgrade() -> None:
    op.alter_column("report", "lat", existing_type=sa.Float(), nullable=False)
    op.alter_column("report", "lng", existing_type=sa.Float(), nullable=False)
