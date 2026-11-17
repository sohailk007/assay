"""Shared helpers for stage scripts.

Every stage follows the same shape: claim rows at one status, do one job,
write evidence, advance the status. That makes each stage re-runnable and
means a failure in stage 4 never forces you to redo stages 1 to 3.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, Company, Evidence, MATCH_CANDIDATE


def record_evidence(
    session: Session,
    company: Company,
    field: str,
    value: str,
    source_class: str,
    source_url: str | None = None,
    match_status: str = MATCH_CANDIDATE,
    follow_up: str | None = None,
    checked_date: dt.date | None = None,
) -> Evidence:
    """Add a fact. An identical live fact is refreshed, not duplicated.

    A different value supersedes the old row rather than overwriting it, so
    the ledger still shows what was believed before and when it changed.
    """
    checked_date = checked_date or dt.date.today()

    existing = session.scalars(
        select(Evidence).where(
            Evidence.company_id == company.id,
            Evidence.field == field,
            Evidence.superseded.is_(False),
        )
    ).all()

    for item in existing:
        if item.observed_value == value and item.source_url == source_url:
            item.checked_date = checked_date
            item.entity_match_status = match_status
            item.follow_up = follow_up
            return item
        item.superseded = True

    evidence = Evidence(
        company_id=company.id,
        field=field,
        observed_value=value,
        source_class=source_class,
        source_url=source_url,
        entity_match_status=match_status,
        checked_date=checked_date,
        follow_up=follow_up,
    )
    session.add(evidence)
    return evidence


def live_evidence(session: Session, company: Company) -> list[Evidence]:
    return list(
        session.scalars(
            select(Evidence).where(
                Evidence.company_id == company.id,
                Evidence.superseded.is_(False),
            )
        ).all()
    )


def log(
    session: Session,
    actor: str,
    action: str,
    company: Company | None = None,
    channel: str | None = None,
    detail: str | None = None,
) -> None:
    session.add(
        AuditLog(
            company_id=company.id if company else None,
            actor=actor,
            action=action,
            channel=channel,
            detail=detail,
        )
    )


def claim(session: Session, stage: str, limit: int | None = None) -> list[Company]:
    stmt = select(Company).where(Company.stage == stage).order_by(Company.id)
    if limit:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())


def next_pilot_id(session: Session) -> str:
    """N-001, N-002 and so on, matching the identifiers in the workbook."""
    highest = 0
    for (pid,) in session.execute(select(Company.pilot_id)).all():
        if pid and pid.startswith("N-"):
            try:
                highest = max(highest, int(pid[2:]))
            except ValueError:
                continue
    return f"N-{highest + 1:03d}"
