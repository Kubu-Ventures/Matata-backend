"""Add analyst severity override, pending_merge_review status, and ai_feedback table.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-13

Three schema additions that complete the human-in-the-loop pipeline:

1. report.analyst_severity_override  — nullable enum column; analyst's explicit
   correction of the AI damage severity prediction.

2. report_status_enum += 'pending_merge_review'  — reports scored ≥ 0.9 by the
   duplicate detector are held here for analyst confirmation instead of being
   silently merged.

3. ai_feedback table  — append-only record of every analyst decision that can
   be compared against the AI prediction; drives the /analyst/ai-accuracy
   accuracy-calibration endpoint.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── 1. Extend the report status enum ────────────────────────────────────
    # ALTER TYPE ... ADD VALUE is transactional in PostgreSQL 12+.
    # IF NOT EXISTS makes re-runs safe.
    op.execute(
        sa.text(
            "ALTER TYPE report_status_enum "
            "ADD VALUE IF NOT EXISTS 'pending_merge_review'"
        )
    )

    # ── 2. Analyst severity override on the report table ────────────────────
    op.add_column(
        "report",
        sa.Column(
            "analyst_severity_override",
            sa.Enum(
                "minimal", "partial", "destroyed",
                name="report_damage_severity_enum",
                create_type=False,  # enum already exists
            ),
            nullable=True,
        ),
    )

    # ── 3. AI feedback table ─────────────────────────────────────────────────
    op.create_table(
        "ai_feedback",
        sa.Column(
            "id",
            sa.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "report_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feedback_type", sa.String(50), nullable=False),
        sa.Column("ai_prediction", sa.String(50), nullable=True),
        sa.Column("analyst_decision", sa.String(50), nullable=True),
        sa.Column("is_agreement", sa.Boolean, nullable=True),
        sa.Column("ai_confidence", sa.Float, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_ai_feedback_report_id", "ai_feedback", ["report_id"])
    op.create_index(
        "ix_ai_feedback_created_at",
        "ai_feedback",
        [sa.text("created_at DESC")],
    )
    op.create_index("ix_ai_feedback_type", "ai_feedback", ["feedback_type"])


def downgrade() -> None:
    # Drop ai_feedback table
    op.drop_index("ix_ai_feedback_type", table_name="ai_feedback")
    op.drop_index("ix_ai_feedback_created_at", table_name="ai_feedback")
    op.drop_index("ix_ai_feedback_report_id", table_name="ai_feedback")
    op.drop_table("ai_feedback")

    # Drop analyst_severity_override column
    op.drop_column("report", "analyst_severity_override")

    # NOTE: PostgreSQL does not support removing enum values.
    # 'pending_merge_review' remains in report_status_enum after downgrade.
    # Manually recreate the enum type if full rollback is required.
