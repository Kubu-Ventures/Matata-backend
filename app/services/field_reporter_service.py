"""Field reporter codes and the per-code payment summary.

Paid pilot reporters sign in by email like everyone else. If the email hash
matches an active ``field_reporters`` row, their token subject becomes that
email hash (see ``auth_service.verify_privy_and_issue_tokens``), and
``create_report`` tags each report with the reporter's code.

The summary counts reports per code over a date range, split by review
outcome, so the pilot organiser can pay for verified work. It never
contains names, emails or phone numbers.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import List, Optional

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ReportStatus
from app.models.field_reporter import FieldReporter
from app.models.report import Report

# Two to five letters, a dash, then one to four digits: KIB-01, MAT-3, MUK-10.
_CODE_RE = re.compile(r"^[A-Z]{2,5}-\d{1,4}$")


def normalise_code(code: str) -> str:
    """Return *code* upper-cased and trimmed, or raise ``ValueError``."""
    cleaned = code.strip().upper()
    if not _CODE_RE.match(cleaned):
        raise ValueError(
            f"'{code}' is not a valid reporter code. Use letters, a dash and "
            "a number, e.g. KIB-01."
        )
    return cleaned


async def lookup_active_by_email_hash(
    email_hash: str, db: AsyncSession
) -> Optional[FieldReporter]:
    """Return the active field reporter for *email_hash*, or None."""
    result = await db.execute(
        sa.select(FieldReporter).where(
            FieldReporter.email_hash == email_hash,
            FieldReporter.is_active.is_(True),
        )
    )
    return result.scalar_one_or_none()


async def code_for_subject(subject: str, db: AsyncSession) -> Optional[str]:
    """Return the field reporter code for a token subject, if any.

    Field reporters' token subject is their email hash, so this is a direct
    lookup. Anonymous and ordinary reporters never match.
    """
    if not subject:
        return None
    reporter = await lookup_active_by_email_hash(subject, db)
    if reporter is None:
        return None
    code = reporter.code
    return code if isinstance(code, str) else None


@dataclass
class FieldReporterSummaryRow:
    """Report counts for one reporter code over the requested period."""

    code: str
    total: int = 0
    verified: int = 0
    pending: int = 0
    rejected: int = 0
    duplicate: int = 0


_STATUS_BUCKET = {
    ReportStatus.verified.value: "verified",
    ReportStatus.pending.value: "pending",
    ReportStatus.rejected.value: "rejected",
    ReportStatus.duplicate.value: "duplicate",
    # Waiting for an analyst to confirm a suspected duplicate: not yet
    # payable, so it counts as pending.
    ReportStatus.pending_merge_review.value: "pending",
}


async def payment_summary(
    db: AsyncSession,
    *,
    date_from: Optional[datetime] = None,
    date_to: Optional[datetime] = None,
) -> List[FieldReporterSummaryRow]:
    """Count reports per field reporter code, grouped by review outcome.

    Every registered code appears, including ones with no reports in the
    period, so a reporter who sent nothing is visible rather than missing.

    Args:
        db:        Async database session.
        date_from: Include reports created at or after this time.
        date_to:   Include reports created before this time.

    Returns:
        One row per code, sorted by code.
    """
    query = sa.select(
        Report.field_reporter_code, Report.status, sa.func.count(Report.id)
    ).where(Report.field_reporter_code.is_not(None))
    if date_from is not None:
        query = query.where(Report.created_at >= date_from)
    if date_to is not None:
        query = query.where(Report.created_at < date_to)
    query = query.group_by(Report.field_reporter_code, Report.status)

    rows: dict[str, FieldReporterSummaryRow] = {}

    codes = (await db.execute(sa.select(FieldReporter.code))).scalars().all()
    for code in codes:
        rows[code] = FieldReporterSummaryRow(code=code)

    for code, report_status, count in (await db.execute(query)).all():
        row = rows.setdefault(code, FieldReporterSummaryRow(code=code))
        status_value = getattr(report_status, "value", report_status)
        bucket = _STATUS_BUCKET.get(str(status_value), "pending")
        setattr(row, bucket, getattr(row, bucket) + int(count))
        row.total += int(count)

    return [rows[c] for c in sorted(rows)]


def summary_to_csv(rows: List[FieldReporterSummaryRow]) -> str:
    """Render summary rows as CSV with a header line."""
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf,
        fieldnames=["code", "total", "verified", "pending", "rejected", "duplicate"],
    )
    writer.writeheader()
    for row in rows:
        writer.writerow(asdict(row))
    return buf.getvalue()
