"""Rename analyst_accounts.phone_hash to email_hash.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-07

Login moves from phone/SMS OTP to email OTP via Privy (see
Kubu-Ventures/Matata-backend#15 and #18). Provisioning and lookup for
analyst/responder/admin accounts are now keyed by a salted SHA-256 hash of the
normalised email address instead of the phone number. The column stored the
opaque hash all along, so this is a straight rename: the column, its unique
constraint, and its index are all renamed in place, and the unique + indexed
properties are preserved exactly as migration 0003 created them.

The analyst_accounts table is empty in every environment at the time of this
migration (login was never launched on the phone flow), so no data backfill is
required.
"""

from __future__ import annotations

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_index("ix_analyst_accounts_phone_hash", table_name="analyst_accounts")
    op.alter_column(
        "analyst_accounts", "phone_hash", new_column_name="email_hash"
    )
    op.execute(
        "ALTER TABLE analyst_accounts RENAME CONSTRAINT "
        "analyst_accounts_phone_hash_key TO analyst_accounts_email_hash_key"
    )
    op.create_index(
        "ix_analyst_accounts_email_hash", "analyst_accounts", ["email_hash"]
    )


def downgrade() -> None:
    op.drop_index("ix_analyst_accounts_email_hash", table_name="analyst_accounts")
    op.execute(
        "ALTER TABLE analyst_accounts RENAME CONSTRAINT "
        "analyst_accounts_email_hash_key TO analyst_accounts_phone_hash_key"
    )
    op.alter_column(
        "analyst_accounts", "email_hash", new_column_name="phone_hash"
    )
    op.create_index(
        "ix_analyst_accounts_phone_hash", "analyst_accounts", ["phone_hash"]
    )
