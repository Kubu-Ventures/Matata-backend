"""CrisisMap management CLI.

Usage
-----
Bootstrap the very first admin account (run once on the server):

    python -m app.cli create-admin --email admin@example.org

List all provisioned analyst/responder/admin accounts:

    python -m app.cli list-accounts

Deactivate an account by its UUID:

    python -m app.cli deactivate-account --id <uuid>

All commands connect to the database defined by DATABASE_URL in the
environment (or .env file).  No running server is required.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import uuid

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _create_admin(email: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.models.analyst_account import AnalystAccount
    from app.services.auth_service import Role, hash_identifier

    if not _EMAIL_RE.match(email):
        print(f"ERROR: '{email}' is not a valid email address.")
        sys.exit(1)

    email_hash = hash_identifier(email.strip().lower())

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        existing = (
            await session.execute(
                sa.select(AnalystAccount).where(
                    AnalystAccount.email_hash == email_hash,
                )
            )
        ).scalar_one_or_none()

        if existing is not None:
            status = "active" if existing.is_active else "inactive (deactivated)"
            print(
                f"INFO: This email address is already registered as "
                f"role={existing.role} ({status})."
            )
            if existing.role != Role.admin.value:
                print(
                    "      To promote to admin, update the role directly"
                    " in the database."
                )
            sys.exit(0)

        account = AnalystAccount(
            id=uuid.uuid4(),
            email_hash=email_hash,
            role=Role.admin.value,
            region_geojson=None,
            created_by_sub="cli-bootstrap",
            is_active=True,
        )
        session.add(account)
        await session.commit()

    await engine.dispose()

    print("SUCCESS: Admin account created.")
    print(f"  Account ID : {account.id}")
    print("  Role       : admin")
    print()
    print("The admin can now log in through the frontend's Privy email OTP flow,")
    print("which posts the resulting tokens to POST /api/v1/auth/privy/verify.")


async def _list_accounts() -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.models.analyst_account import AnalystAccount

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(AnalystAccount).order_by(AnalystAccount.created_at)
                )
            )
            .scalars()
            .all()
        )

    await engine.dispose()

    if not rows:
        print("No analyst accounts found.")
        return

    fmt = "{:<36}  {:<12}  {:<8}  {}"
    print(fmt.format("ID", "ROLE", "ACTIVE", "CREATED AT"))
    print("-" * 75)
    for row in rows:
        print(
            fmt.format(
                str(row.id),
                row.role,
                "yes" if row.is_active else "no",
                str(row.created_at)[:19] if row.created_at else "—",
            )
        )


async def _deactivate_account(account_id: str) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.models.analyst_account import AnalystAccount

    try:
        uid = uuid.UUID(account_id)
    except ValueError:
        print(f"ERROR: '{account_id}' is not a valid UUID.")
        sys.exit(1)

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        row = (
            await session.execute(
                sa.select(AnalystAccount).where(AnalystAccount.id == uid)
            )
        ).scalar_one_or_none()

        if row is None:
            print(f"ERROR: No account found with ID {account_id}.")
            await engine.dispose()
            sys.exit(1)

        if not row.is_active:
            print(f"INFO: Account {account_id} is already inactive.")
            await engine.dispose()
            sys.exit(0)

        row.is_active = False
        await session.commit()

    await engine.dispose()
    print(f"SUCCESS: Account {account_id} (role={row.role}) deactivated.")
    print("  Existing tokens will expire naturally.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="CrisisMap management commands.",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # create-admin
    p_create = sub.add_parser(
        "create-admin",
        help="Bootstrap the first admin account from an email address.",
    )
    p_create.add_argument(
        "--email",
        required=True,
        metavar="EMAIL",
        help="Email address, e.g. admin@example.org",
    )

    # list-accounts
    sub.add_parser(
        "list-accounts",
        help="List all provisioned analyst/responder/admin accounts.",
    )

    # deactivate-account
    p_deact = sub.add_parser(
        "deactivate-account",
        help="Deactivate a provisioned account by its UUID.",
    )
    p_deact.add_argument(
        "--id",
        required=True,
        dest="account_id",
        metavar="UUID",
        help="Account UUID shown by list-accounts.",
    )

    args = parser.parse_args()

    if args.command == "create-admin":
        asyncio.run(_create_admin(args.email))
    elif args.command == "list-accounts":
        asyncio.run(_list_accounts())
    elif args.command == "deactivate-account":
        asyncio.run(_deactivate_account(args.account_id))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
