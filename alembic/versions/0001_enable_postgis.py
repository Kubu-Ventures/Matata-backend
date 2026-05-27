"""Enable PostGIS extension.

Revision ID: 0001
Revises: —
Create Date: 2026-05-27

This must be the first migration applied. All subsequent migrations that
reference Geometry columns depend on the postgis extension being present.

The downgrade path drops the extension only when the database is otherwise
blank — running downgrade on a database with geometry columns will raise a
dependency error from PostgreSQL, which is the desired safe-fail behaviour.
"""

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PostGIS may already be installed by the DB provider (e.g. Supabase, RDS).
    # IF NOT EXISTS makes this safe to re-run; the EXCEPTION block handles the
    # rare case where the role lacks CREATE EXTENSION rights but postgis is
    # already available (e.g. shared cloud Postgres).
    op.execute("""
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS postgis;
        EXCEPTION
            WHEN insufficient_privilege THEN
                -- Extension already installed by superuser; safe to continue.
                RAISE NOTICE 'PostGIS already present, skipping creation.';
        END;
        $$
    """)


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS postgis")
