"""Stage 02: legal identity.

Re-fetch the live company profile and record the status with today's date.
Search results go stale; this confirms the register as of now.

The one thing this stage decides is whether research continues at all. An
adverse status, including an active proposal to strike off, stops the record
here with a reason attached.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.models import (
    MATCH_LINKED,
    SOURCE_COMPANIES_HOUSE,
    STAGE_DOMAIN,
    STAGE_IDENTITY,
    Company,
)
from app.sources.companies_house import CompaniesHouseClient
from app.stages.common import claim, log, record_evidence


def run(
    session: Session,
    limit: int | None = None,
    actor: str = "stage-02",
) -> dict[str, int]:
    companies: list[Company] = claim(session, STAGE_IDENTITY, limit)
    if not companies:
        return {"checked": 0, "active": 0, "held": 0}

    today = dt.date.today()
    active = 0
    held = 0

    with CompaniesHouseClient() as client:
        for company in companies:
            record = client.get_company(company.company_number)

            if record is None:
                company.adverse_status_flag = True
                company.adverse_status_reason = "Not found on the register"
                held += 1
                continue

            company.legal_name = record.legal_name
            company.company_status = record.company_status
            company.company_type = record.company_type
            company.date_of_creation = record.date_of_creation
            company.sic_codes = ",".join(record.sic_codes)
            company.registered_locality = record.registered_locality
            company.registered_postcode = record.registered_postcode

            adverse, reason = record.adverse
            company.adverse_status_flag = adverse
            company.adverse_status_reason = reason

            record_evidence(
                session,
                company,
                field="company_status",
                value=record.company_status or "unknown",
                source_class=SOURCE_COMPANIES_HOUSE,
                source_url=company.companies_house_url,
                match_status=MATCH_LINKED,
                checked_date=today,
                follow_up="Status is a snapshot. Re-check before import or outreach.",
            )

            if adverse:
                held += 1
                company.notes = (
                    f"HOLD at stage 02: {reason}. "
                    "No further research or contact without a manual override."
                )
                log(
                    session,
                    actor=actor,
                    action="IDENTITY_HOLD",
                    company=company,
                    detail=reason,
                )
            else:
                active += 1
                # Trading identity (stage 03) is proven by the same website
                # check as domain ownership, so the two run together.
                company.stage = STAGE_DOMAIN

    log(
        session,
        actor=actor,
        action="STAGE_02_IDENTITY",
        detail=f"checked={len(companies)} active={active} held={held}",
    )
    return {"checked": len(companies), "active": active, "held": held}
