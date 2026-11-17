"""Stage 01: intake.

Pull a trade and geography from the Companies House register into the local
database. Deduplication is on company number only, never on spelling, because
two companies can share a name and one company can change its name.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    SOURCE_COMPANIES_HOUSE,
    MATCH_LINKED,
    STAGE_IDENTITY,
    STAGE_INTAKE,
    Company,
)
from app.sources.companies_house import CompaniesHouseClient, CompanyRecord
from app.stages.common import log, next_pilot_id, record_evidence

# SIC codes that map to TrainifyHub's construction catalogue.
TRADE_SIC = {
    "roofing": ["43910"],
    "scaffolding": ["43991"],
    "electrical": ["43210"],
    "building": ["41201", "41202"],
    "groundworks": ["43120", "43990"],
    "plumbing": ["43220"],
}

COURSE_HINTS = {
    "43910": "Roofing NVQ L2; working at height awareness",
    "43991": "Scaffolding NVQ pathway; supervisor competence",
    "43210": "Site safety awareness; working at height; manual handling",
    "41201": "SSSTS/SMSTS if eligible; construction NVQ; site safety awareness",
    "41202": "SSSTS/SMSTS if eligible; construction NVQ; site safety awareness",
    "43120": "Plant and groundworks NVQ; site safety awareness",
    "43220": "Site safety awareness; working at height; manual handling",
    "43990": "Construction NVQ; site safety awareness",
}


def course_hint(sic_codes: list[str]) -> str:
    hints = [COURSE_HINTS[c] for c in sic_codes if c in COURSE_HINTS]
    return hints[0] if hints else "Site safety awareness"


def upsert_company(
    session: Session, record: CompanyRecord, cohort: str | None
) -> tuple[Company, bool]:
    existing = session.scalars(
        select(Company).where(Company.company_number == record.company_number)
    ).first()

    adverse, reason = record.adverse

    if existing:
        existing.legal_name = record.legal_name
        existing.company_status = record.company_status
        existing.company_type = record.company_type
        existing.sic_codes = ",".join(record.sic_codes)
        existing.registered_locality = record.registered_locality
        existing.registered_postcode = record.registered_postcode
        existing.adverse_status_flag = adverse
        existing.adverse_status_reason = reason
        return existing, False

    company = Company(
        company_number=record.company_number,
        pilot_id=next_pilot_id(session),
        legal_name=record.legal_name,
        company_status=record.company_status,
        company_type=record.company_type,
        date_of_creation=record.date_of_creation,
        sic_codes=",".join(record.sic_codes),
        registered_locality=record.registered_locality,
        registered_postcode=record.registered_postcode,
        adverse_status_flag=adverse,
        adverse_status_reason=reason,
        course_match=course_hint(record.sic_codes),
        cohort=cohort,
        stage=STAGE_INTAKE,
    )
    session.add(company)
    session.flush()
    return company, True


def run(
    session: Session,
    trade: str,
    location: str | None,
    limit: int = 50,
    cohort: str | None = None,
    actor: str = "stage-01",
) -> dict[str, int]:
    sic_codes = TRADE_SIC.get(trade.lower())
    if not sic_codes:
        raise ValueError(
            f"Unknown trade {trade!r}. Known trades: {', '.join(sorted(TRADE_SIC))}"
        )

    today = dt.date.today()
    created = 0
    refreshed = 0

    with CompaniesHouseClient() as client:
        records = client.advanced_search(
            sic_codes=sic_codes,
            location=location,
            company_status=["active"],
            limit=limit,
        )

    for record in records:
        company, is_new = upsert_company(session, record, cohort)
        created += int(is_new)
        refreshed += int(not is_new)

        record_evidence(
            session,
            company,
            field="legal_name",
            value=record.legal_name,
            source_class=SOURCE_COMPANIES_HOUSE,
            source_url=company.companies_house_url,
            match_status=MATCH_LINKED,
            checked_date=today,
            follow_up="Recheck legal status immediately before any outreach",
        )
        if company.stage == STAGE_INTAKE:
            company.stage = STAGE_IDENTITY

    log(
        session,
        actor=actor,
        action="STAGE_01_INTAKE",
        detail=(
            f"trade={trade} location={location or 'any'} limit={limit} "
            f"new={created} refreshed={refreshed}"
        ),
    )
    return {"found": len(records), "new": created, "refreshed": refreshed}
