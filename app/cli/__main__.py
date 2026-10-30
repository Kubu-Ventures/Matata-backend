"""CrisisMap management CLI.

Usage
-----
Bootstrap the very first admin account (run once on the server):

    python -m app.cli create-admin --email admin@example.org [--label ops-lead]

`--label` is an optional, operator-chosen display name (never the email —
that's never stored) so `list-accounts` shows something recognisable besides
a bare UUID.

Provision an analyst, responder, or admin (idempotent — an existing account
is reported and left unchanged):

    python -m app.cli create-account --email a@example.org --role analyst [--label ...]

List all provisioned analyst/responder/admin accounts:

    python -m app.cli list-accounts

Deactivate an account by its UUID:

    python -m app.cli deactivate-account --id <uuid>

Re-drive reports whose GIS / AI / duplicate-scoring job was lost after the
report committed (audit M-8 / M-9) — the same sweep Celery beat runs every
5 minutes, on demand:

    python -m app.cli reconcile [--grace-minutes 15] [--limit 500]

Delete report photos older than PHOTO_RETENTION_DAYS (or --days) — the same
sweep Celery beat runs daily. --dry-run only counts what would be deleted:

    python -m app.cli purge-photos [--days 90] [--limit 500] [--dry-run]

Register a paid field reporter under a code (the pilot organiser keeps
the list matching codes to people; only the email hash and code are stored):

    python -m app.cli add-field-reporter --email r@example.org --code KIB-01

List field reporters, or deactivate one by code:

    python -m app.cli list-field-reporters
    python -m app.cli deactivate-field-reporter --code KIB-01

Print the per-code report counts used for paying field reporters (CSV):

    python -m app.cli field-reporter-summary [--from 2026-11-01] [--to 2026-11-08]

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
_ELEVATED_ROLES = ("analyst", "responder", "admin")


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------


async def _create_admin(email: str, label: str | None = None) -> None:
    await _create_account(email, "admin", label)


async def _create_account(email: str, role: str, label: str | None = None) -> None:
    import sqlalchemy as sa
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings
    from app.models.analyst_account import AnalystAccount
    from app.services.auth_service import hash_identifier

    email = email.strip()
    if not _EMAIL_RE.match(email):
        print(f"ERROR: '{email}' is not a valid email address.")
        sys.exit(1)

    if role not in _ELEVATED_ROLES:
        print(f"ERROR: '{role}' is not one of: {', '.join(_ELEVATED_ROLES)}.")
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
            if existing.role != role:
                print(
                    f"      To change it to {role}, update the role directly"
                    " in the database."
                )
            await engine.dispose()
            sys.exit(0)

        account = AnalystAccount(
            id=uuid.uuid4(),
            email_hash=email_hash,
            label=label,
            role=role,
            region_geojson=None,
            created_by_sub="cli-bootstrap",
            is_active=True,
        )
        session.add(account)
        await session.commit()

    await engine.dispose()

    print(f"SUCCESS: {role.capitalize()} account created.")
    print(f"  Account ID : {account.id}")
    print(f"  Label      : {label or '—'}")
    print(f"  Role       : {role}")
    print()
    print(f"The {role} can now log in through the frontend's Privy email OTP flow,")
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

    fmt = "{:<36}  {:<20}  {:<12}  {:<8}  {}"
    print(fmt.format("ID", "LABEL", "ROLE", "ACTIVE", "CREATED AT"))
    print("-" * 96)
    for row in rows:
        print(
            fmt.format(
                str(row.id),
                row.label or "—",
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


def _reconcile(grace_minutes: int, limit: int) -> None:
    """Run the pipeline reconciliation sweep synchronously (no Celery needed)."""
    from app.workers.reconciliation_tasks import _reconcile_impl

    summary = _reconcile_impl(grace_minutes=grace_minutes, limit=limit)
    print("Reconciliation sweep complete:")
    print(f"  stuck reports scanned : {summary['scanned']}")
    print(f"  GIS jobs re-dispatched : {summary['gis']}")
    print(f"  AI jobs re-dispatched  : {summary['ai']}")
    print(f"  score_report re-dispatched (dedup only) : {summary['dedup_direct']}")
    if summary.get("error"):
        print("  NOTE: the sweep hit an error — see logs.")
        sys.exit(1)


def _purge_photos(days: int | None, limit: int, dry_run: bool) -> None:
    """Run the photo retention sweep synchronously (no Celery needed)."""
    from app.workers.retention_tasks import _purge_impl

    summary = _purge_impl(retention_days=days, limit=limit, dry_run=dry_run)
    if summary["retention_days"] <= 0:
        print("Photo retention is off (PHOTO_RETENTION_DAYS=0). Pass --days N to run.")
        return
    print(f"Photo retention sweep ({summary['retention_days']} days) complete:")
    print(f"  photos past retention : {summary['expired']}")
    if dry_run:
        print("  dry run: nothing was deleted")
    else:
        print(f"  photos deleted        : {summary['deleted']}")
    if summary["failed"]:
        print(f"  delete failures (retried next run) : {summary['failed']}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Field reporters
# ---------------------------------------------------------------------------


def _session_factory():
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.config import settings

    engine = create_async_engine(settings.DATABASE_URL, echo=False)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def _add_field_reporter(email: str, code: str) -> None:
    import sqlalchemy as sa

    from app.models.field_reporter import FieldReporter
    from app.services.auth_service import hash_identifier
    from app.services.field_reporter_service import normalise_code

    email = email.strip()
    if not _EMAIL_RE.match(email):
        print(f"ERROR: '{email}' is not a valid email address.")
        sys.exit(1)
    try:
        code = normalise_code(code)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    email_hash = hash_identifier(email.lower())
    engine, factory = _session_factory()
    async with factory() as session:
        clash = (
            await session.execute(
                sa.select(FieldReporter).where(
                    sa.or_(
                        FieldReporter.email_hash == email_hash,
                        FieldReporter.code == code,
                    )
                )
            )
        ).scalar_one_or_none()
        if clash is not None:
            await engine.dispose()
            if clash.email_hash == email_hash:
                print(f"ERROR: This email is already registered as {clash.code}.")
            else:
                print(f"ERROR: Code {code} is already in use.")
            sys.exit(1)

        session.add(
            FieldReporter(
                id=uuid.uuid4(),
                code=code,
                email_hash=email_hash,
                created_by_sub="cli",
                is_active=True,
            )
        )
        await session.commit()
    await engine.dispose()
    print(f"SUCCESS: Field reporter {code} registered.")
    print("Reports they send after signing in with this email are tagged with")
    print("the code. If they were already signed in, they must sign out and in.")


async def _list_field_reporters() -> None:
    import sqlalchemy as sa

    from app.models.field_reporter import FieldReporter

    engine, factory = _session_factory()
    async with factory() as session:
        rows = (
            (
                await session.execute(
                    sa.select(FieldReporter).order_by(FieldReporter.code)
                )
            )
            .scalars()
            .all()
        )
    await engine.dispose()

    if not rows:
        print("No field reporters registered.")
        return
    fmt = "{:<10}  {:<8}  {}"
    print(fmt.format("CODE", "ACTIVE", "REGISTERED AT"))
    print("-" * 44)
    for row in rows:
        print(
            fmt.format(
                row.code,
                "yes" if row.is_active else "no",
                str(row.created_at)[:19] if row.created_at else "—",
            )
        )


async def _deactivate_field_reporter(code: str) -> None:
    import sqlalchemy as sa

    from app.models.field_reporter import FieldReporter

    code = code.strip().upper()
    engine, factory = _session_factory()
    async with factory() as session:
        row = (
            await session.execute(
                sa.select(FieldReporter).where(FieldReporter.code == code)
            )
        ).scalar_one_or_none()
        if row is None:
            await engine.dispose()
            print(f"ERROR: No field reporter with code {code}.")
            sys.exit(1)
        row.is_active = False
        await session.commit()
    await engine.dispose()
    print(f"SUCCESS: Field reporter {code} deactivated. New reports are no")
    print("longer tagged with this code; past reports keep it.")


async def _field_reporter_summary(date_from: str | None, date_to: str | None) -> None:
    from datetime import datetime, timezone

    from app.services.field_reporter_service import payment_summary, summary_to_csv

    def _parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"ERROR: '{value}' is not a date like 2026-11-01.")
            sys.exit(1)

    engine, factory = _session_factory()
    async with factory() as session:
        rows = await payment_summary(
            session, date_from=_parse(date_from), date_to=_parse(date_to)
        )
    await engine.dispose()
    print(summary_to_csv(rows), end="")


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
    p_create.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help=(
            "Optional display name shown in list-accounts (e.g. "
            "'ops-lead-nairobi'). Never store the email here — it is never "
            "persisted, by design."
        ),
    )

    # create-account
    p_account = sub.add_parser(
        "create-account",
        help="Provision an analyst, responder, or admin account by email.",
    )
    p_account.add_argument(
        "--email",
        required=True,
        metavar="EMAIL",
        help="Email address the person will log in with.",
    )
    p_account.add_argument(
        "--role",
        default="analyst",
        choices=_ELEVATED_ROLES,
        help="Role to assign. Defaults to 'analyst'.",
    )
    p_account.add_argument(
        "--label",
        default=None,
        metavar="LABEL",
        help="Optional display name shown in list-accounts. Never the email.",
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

    # reconcile
    p_recon = sub.add_parser(
        "reconcile",
        help="Re-drive reports whose async pipeline job was lost (M-8 / M-9).",
    )
    p_recon.add_argument(
        "--grace-minutes",
        type=int,
        default=15,
        help="Only touch reports older than this (default 15).",
    )
    p_recon.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum reports to re-drive in one sweep (default 500).",
    )

    # purge-photos
    p_purge = sub.add_parser(
        "purge-photos",
        help="Delete report photos older than PHOTO_RETENTION_DAYS.",
    )
    p_purge.add_argument(
        "--days",
        type=int,
        default=None,
        help="Retention in days; overrides PHOTO_RETENTION_DAYS for this run.",
    )
    p_purge.add_argument(
        "--limit",
        type=int,
        default=500,
        help="Maximum photos to delete in one sweep (default 500).",
    )
    p_purge.add_argument(
        "--dry-run",
        action="store_true",
        help="Count expired photos without deleting anything.",
    )

    # field reporters
    p_fr = sub.add_parser(
        "add-field-reporter",
        help="Register a paid field reporter's email under a code like KIB-01.",
    )
    p_fr.add_argument("--email", required=True, metavar="EMAIL")
    p_fr.add_argument("--code", required=True, metavar="CODE")

    sub.add_parser("list-field-reporters", help="List registered field reporters.")

    p_fr_deact = sub.add_parser(
        "deactivate-field-reporter", help="Stop tagging reports with a code."
    )
    p_fr_deact.add_argument("--code", required=True, metavar="CODE")

    p_fr_sum = sub.add_parser(
        "field-reporter-summary",
        help="CSV of report counts per field reporter code, for payment.",
    )
    p_fr_sum.add_argument("--from", dest="date_from", default=None, metavar="DATE")
    p_fr_sum.add_argument("--to", dest="date_to", default=None, metavar="DATE")

    args = parser.parse_args()

    if args.command == "create-admin":
        asyncio.run(_create_admin(args.email, args.label))
    elif args.command == "create-account":
        asyncio.run(_create_account(args.email, args.role, args.label))
    elif args.command == "list-accounts":
        asyncio.run(_list_accounts())
    elif args.command == "deactivate-account":
        asyncio.run(_deactivate_account(args.account_id))
    elif args.command == "reconcile":
        _reconcile(args.grace_minutes, args.limit)
    elif args.command == "purge-photos":
        _purge_photos(args.days, args.limit, args.dry_run)
    elif args.command == "add-field-reporter":
        asyncio.run(_add_field_reporter(args.email, args.code))
    elif args.command == "list-field-reporters":
        asyncio.run(_list_field_reporters())
    elif args.command == "deactivate-field-reporter":
        asyncio.run(_deactivate_field_reporter(args.code))
    elif args.command == "field-reporter-summary":
        asyncio.run(_field_reporter_summary(args.date_from, args.date_to))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
