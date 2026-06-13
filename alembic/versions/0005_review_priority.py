"""Add review_priority column for confidence-based analyst routing.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-13

The AI worker sets review_priority after image analysis based on configurable
confidence/quality thresholds.  This drives sort order in the analyst feed so
critical items (low confidence, unusable images, AI failures) surface first.

Priority enum values (ordered critical→high→normal→low):
  critical — ai_confidence < 0.60 OR quality_score < 0.30 OR AI failed.
  high     — 0.60 ≤ ai_confidence < 0.80 OR ai_divergence is True.
  normal   — default before AI processes the report.
  low      — ai_confidence ≥ 0.80, no divergence, quality ≥ 0.60.

All existing rows default to 'normal' (pre-AI state).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create the enum type first.
    op.execute(
        "CREATE TYPE review_priority_enum AS ENUM "
        "('critical', 'high', 'normal', 'low')"
    )

    # Add the column — NOT NULL with a server default so existing rows get
    # 'normal' immediately without a two-step nullable→backfill→not-null dance.
    op.add_column(
        "report",
        sa.Column(
            "review_priority",
            sa.Enum(
                "critical",
                "high",
                "normal",
                "low",
                name="review_priority_enum",
                create_type=False,
            ),
            nullable=False,
            server_default="normal",
        ),
    )

    # Index for fast priority-based ORDER BY and WHERE filters.
    op.create_index(
        "ix_report_review_priority",
        "report",
        ["review_priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_review_priority", table_name="report")
    op.drop_column("report", "review_priority")
    op.execute("DROP TYPE review_priority_enum")
