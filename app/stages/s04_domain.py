"""Stages 03 and 04: trading identity and domain ownership, plus stage 05.

These three are one network operation, so they run as one pass. The site is
fetched once and three questions get answered from it:

  03  Is this the company's real trading presence?
  04  Does this domain belong to this legal entity?
  05  What contact details does the company itself publish?

The protocol's rule is honoured strictly. A matching name or a nice logo is
not ownership. Only the company number published on the site produces LINKED.
Anything weaker is CANDIDATE and a human decides.

Contact details are read off the page, never constructed. If the site shows
no mailbox, this stage records no mailbox.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.models import (
    MATCH_CANDIDATE,
    MATCH_LINKED,
    MATCH_UNKNOWN,
    SOURCE_COMPANY_WEBSITE,
    STAGE_DOMAIN,
    STAGE_ENRICHED,
    Company,
)
from app.sources.website import WebsiteProbe, find_website
from app.stages.common import claim, log, record_evidence


def run(
    session: Session,
    limit: int | None = None,
    actor: str = "stage-04",
) -> dict[str, int]:
    companies: list[Company] = claim(session, STAGE_DOMAIN, limit)
    if not companies:
        return {"checked": 0, "linked": 0, "candidate": 0, "none": 0, "contacts": 0}

    today = dt.date.today()
    counts = {"linked": 0, "candidate": 0, "none": 0, "contacts": 0}

    with WebsiteProbe() as probe:
        for company in companies:
            if company.adverse_status_flag:
                company.stage = STAGE_ENRICHED
                continue

            finding = find_website(
                legal_name=company.legal_name,
                company_number=company.company_number,
                postcode=company.registered_postcode,
                probe=probe,
            )

            if finding is None or not finding.reachable:
                company.website_match_status = MATCH_UNKNOWN
                company.website_match_rationale = (
                    "No candidate domain responded. Needs manual search."
                )
                company.trading_match_status = MATCH_UNKNOWN
                company.trading_match_rationale = "No trading presence found online."
                counts["none"] += 1
                company.stage = STAGE_ENRICHED
                continue

            status = finding.match_status
            company.website_url = finding.url
            company.website_match_status = status
            company.website_match_rationale = finding.rationale

            # Trading identity rides on the same evidence. The site proving it
            # belongs to the company is also the site proving the company
            # trades under that name.
            company.trading_match_status = status
            company.trading_match_rationale = finding.rationale
            if status == MATCH_LINKED:
                counts["linked"] += 1
            elif status == MATCH_CANDIDATE:
                counts["candidate"] += 1
            else:
                counts["none"] += 1

            record_evidence(
                session,
                company,
                field="website",
                value=finding.url,
                source_class=SOURCE_COMPANY_WEBSITE,
                source_url=finding.pages_checked[0] if finding.pages_checked else finding.url,
                match_status=status,
                checked_date=today,
                follow_up=(
                    None
                    if status == MATCH_LINKED
                    else "Confirm the site belongs to this legal entity before use"
                ),
            )

            if finding.phones:
                record_evidence(
                    session,
                    company,
                    field="phone",
                    value=finding.phones[0],
                    source_class=SOURCE_COMPANY_WEBSITE,
                    source_url=finding.pages_checked[-1],
                    match_status=status,
                    checked_date=today,
                    follow_up=(
                        "Confirm current ownership of the number, then run dated "
                        "TPS and CTPS screening and check internal suppression"
                    ),
                )
                counts["contacts"] += 1

            if finding.emails:
                record_evidence(
                    session,
                    company,
                    field="email",
                    value=finding.emails[0],
                    source_class=SOURCE_COMPANY_WEBSITE,
                    source_url=finding.pages_checked[-1],
                    match_status=status,
                    checked_date=today,
                    follow_up=(
                        "Confirm mailbox, recipient class, lawful basis, privacy "
                        "notice and suppression before any send"
                    ),
                )

            company.stage = STAGE_ENRICHED

    log(
        session,
        actor=actor,
        action="STAGE_04_DOMAIN",
        detail=(
            f"checked={len(companies)} linked={counts['linked']} "
            f"candidate={counts['candidate']} none={counts['none']} "
            f"with_phone={counts['contacts']}"
        ),
    )
    return {"checked": len(companies), **counts}
