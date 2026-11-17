"""Import the existing 30-record pilot workbook into the database.

Lets you start from the research already done rather than from an empty table.
Reads the "03 MASTER" and "04 CLAIM EVIDENCE" sheets of the founder workbook.

Two things this import deliberately does NOT carry over:

  * Any gate state. Every imported record starts at HOLD on both routes,
    whatever the spreadsheet said, because an approval that cannot be traced
    to a named person and a date is not an approval.
  * Any screening result. The workbook records them as NOT RUN, and even if it
    did not, a screening result from another system without a receipt is not
    evidence you can show anyone.

Every imported record is marked as needing live revalidation, which is what
the workbook itself says on the sheet.
"""

from __future__ import annotations

import datetime as dt

from openpyxl import load_workbook
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    MATCH_CANDIDATE,
    MATCH_LINKED,
    MATCH_UNKNOWN,
    SOURCE_COMPANIES_HOUSE,
    SOURCE_COMPANY_SOCIAL,
    SOURCE_COMPANY_WEBSITE,
    SOURCE_THIRD_PARTY_DIRECTORY,
    SOURCE_TRADE_BODY,
    STAGE_IDENTITY,
    Company,
)
from app.stages.common import log, record_evidence

SOURCE_MAP = {
    "companies house": SOURCE_COMPANIES_HOUSE,
    "company website": SOURCE_COMPANY_WEBSITE,
    "first-party site": SOURCE_COMPANY_WEBSITE,
    "company social post": SOURCE_COMPANY_SOCIAL,
    "social/directory listing": SOURCE_COMPANY_SOCIAL,
    "trade body directory": SOURCE_TRADE_BODY,
    "trade/website corroboration": SOURCE_TRADE_BODY,
    "third-party directory": SOURCE_THIRD_PARTY_DIRECTORY,
}

FIELD_MAP = {
    "legal company": "legal_name",
    "published business phone": "phone",
    "candidate corporate email": "email",
    "trading sector / site": "trading_sector",
}


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _find_header_row(ws, first_header: str) -> int:
    for row in range(1, 12):
        if _text(ws.cell(row=row, column=1).value).lower() == first_header.lower():
            return row
    raise ValueError(f"Could not find a header row starting with {first_header!r}")


def run(
    session: Session,
    workbook_path: str,
    cohort: str = "founder-pilot-30",
    actor: str = "workbook-import",
) -> dict[str, int]:
    wb = load_workbook(workbook_path, data_only=True)

    master = None
    claims = None
    for name in wb.sheetnames:
        upper = name.upper()
        if "MASTER" in upper:
            master = wb[name]
        elif "CLAIM" in upper or "EVIDENCE" in upper:
            claims = wb[name]
    if master is None:
        raise ValueError("No master register sheet found in that workbook.")

    header_row = _find_header_row(master, "Pilot ID")
    headers = {
        _text(master.cell(row=header_row, column=c).value).lower(): c
        for c in range(1, master.max_column + 1)
    }

    def col(name: str) -> int | None:
        for key, index in headers.items():
            if key.startswith(name.lower()):
                return index
        return None

    imported = 0
    skipped = 0

    for row in range(header_row + 1, master.max_row + 1):
        number = _text(master.cell(row=row, column=col("ch no") or 3).value)
        name = _text(master.cell(row=row, column=col("legal name") or 2).value)
        if not number or not name:
            continue
        number = number.upper().zfill(8) if number.isdigit() else number.upper()

        existing = session.scalars(
            select(Company).where(Company.company_number == number)
        ).first()
        if existing:
            skipped += 1
            continue

        status = _text(master.cell(row=row, column=col("ch status") or 5).value)
        adverse = "strike" in status.lower() or status.lower() not in ("active",)

        # The workbook's own trade and site verdicts are carried over, because
        # that research was already done by hand. They stay exactly as
        # recorded: CANDIDATE stays CANDIDATE, nothing is promoted on import.
        def verdict(label: str, default_col: int) -> str:
            raw = _text(master.cell(row=row, column=col(label) or default_col).value).upper()
            if "LINKED" in raw:
                return MATCH_LINKED
            if "CANDIDATE" in raw:
                return MATCH_CANDIDATE
            return MATCH_UNKNOWN

        trade_match = verdict("trade link", 8)
        site_match = verdict("site link", 9)

        company = Company(
            company_number=number,
            pilot_id=_text(master.cell(row=row, column=col("pilot id") or 1).value) or None,
            legal_name=name,
            company_status=status or None,
            sic_codes=_text(master.cell(row=row, column=col("trade / sic") or 4).value),
            registered_locality=_text(master.cell(row=row, column=col("area") or 6).value),
            adverse_status_flag=adverse,
            adverse_status_reason=status if adverse else None,
            trading_match_status=trade_match,
            trading_match_rationale="Carried over from the founder workbook, unverified here",
            website_match_status=site_match,
            website_match_rationale="Carried over from the founder workbook, unverified here",
            course_match=_text(
                master.cell(row=row, column=col("potential course match") or 24).value
            ),
            notes=(
                _text(master.cell(row=row, column=col("notes") or 25).value)
                + " | Imported from the founder workbook. Needs live revalidation."
            ).strip(" |"),
            cohort=cohort,
            # Back to stage 02 so the register status is re-fetched live
            # rather than trusted from a spreadsheet snapshot.
            stage=STAGE_IDENTITY,
        )
        session.add(company)
        session.flush()
        imported += 1

    session.flush()

    # Claim-level evidence.
    evidence_added = 0
    if claims is not None:
        claim_header = _find_header_row(claims, "Claim ID")
        cheaders = {
            _text(claims.cell(row=claim_header, column=c).value).lower(): c
            for c in range(1, claims.max_column + 1)
        }

        def ccol(name: str, default: int) -> int:
            for key, index in cheaders.items():
                if key.startswith(name.lower()):
                    return index
            return default

        by_number = {
            c.company_number: c
            for c in session.scalars(select(Company)).all()
        }

        for row in range(claim_header + 1, claims.max_row + 1):
            number = _text(claims.cell(row=row, column=ccol("ch no", 3)).value)
            if not number:
                continue
            number = number.upper().zfill(8) if number.isdigit() else number.upper()
            company = by_number.get(number)
            if company is None:
                continue

            raw_field = _text(claims.cell(row=row, column=ccol("field", 4)).value).lower()
            field = FIELD_MAP.get(raw_field)
            if field is None:
                continue

            value = _text(claims.cell(row=row, column=ccol("observed value", 5)).value)
            if not value:
                continue
            if field == "phone":
                value = value.replace(" ", "")

            source_raw = _text(
                claims.cell(row=row, column=ccol("source class", 6)).value
            ).lower()
            url = _text(claims.cell(row=row, column=ccol("public evidence url", 7)).value)
            source = SOURCE_MAP.get(source_raw, SOURCE_THIRD_PARTY_DIRECTORY)

            # "Social/directory listing" covers two very different things in
            # the workbook. A post on the company's own LinkedIn page is the
            # company publishing its own contact details and scores 10. A
            # Crunchbase or similar profile is a third party republishing them
            # and scores 5. Decide by the URL rather than the label.
            if source_raw.startswith("social/directory"):
                social_hosts = ("linkedin.com", "facebook.com", "x.com", "twitter.com")
                source = (
                    SOURCE_COMPANY_SOCIAL
                    if any(host in url.lower() for host in social_hosts)
                    else SOURCE_THIRD_PARTY_DIRECTORY
                )
            match_raw = _text(
                claims.cell(row=row, column=ccol("entity-match", 9)).value
            ).upper()
            match = (
                MATCH_LINKED
                if "LINKED" in match_raw
                else MATCH_CANDIDATE
                if "CANDIDATE" in match_raw
                else MATCH_UNKNOWN
            )

            snapshot = _text(
                claims.cell(row=row, column=ccol("research snapshot", 8)).value
            )
            try:
                checked = dt.date.fromisoformat(snapshot[:10])
            except ValueError:
                checked = dt.date.today()

            record_evidence(
                session,
                company,
                field=field,
                value=value,
                source_class=source,
                source_url=url or None,
                match_status=match,
                checked_date=checked,
                follow_up=_text(
                    claims.cell(row=row, column=ccol("follow-up", 11)).value
                )
                or None,
            )
            evidence_added += 1

    log(
        session,
        actor=actor,
        action="WORKBOOK_IMPORT",
        detail=(
            f"imported={imported} skipped={skipped} evidence={evidence_added} "
            f"source={workbook_path}. No gate state or screening result carried over."
        ),
    )
    return {
        "imported": imported,
        "skipped": skipped,
        "evidence": evidence_added,
    }
