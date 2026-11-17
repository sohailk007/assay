"""Evidence confidence scoring, exactly as sheet 02 of the protocol defines it.

Read the docstring once and then trust the code, because the single most
important thing about this module is what it does NOT do.

The score measures how well evidenced a record is. It says nothing about
whether the company needs training and nothing about whether you may lawfully
contact them. An 85 does not open a gate. A 30 does not close one. The gates
are in app/stages/s09_route.py and they ignore this number entirely.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from app.config import settings
from app.models import (
    Company,
    Evidence,
    MATCH_LINKED,
    SOURCE_COMPANY_SOCIAL,
    SOURCE_COMPANY_WEBSITE,
    SOURCE_CONVERSATION,
    SOURCE_THIRD_PARTY_DIRECTORY,
    SOURCE_TRADE_BODY,
)

MAX_IDENTITY = 25
MAX_TRADING = 20
MAX_WEBSITE = 15
MAX_CONTACT = 15
MAX_BUYER = 10
MAX_DEMAND = 10
MAX_FRESHNESS = 5

BAND_STRONG = "STRONG"
BAND_REVIEW = "REVIEW"
BAND_ENRICH = "ENRICH"

# Contact provenance points by where the contact detail was published.
CONTACT_POINTS = {
    SOURCE_COMPANY_WEBSITE: 15,
    SOURCE_CONVERSATION: 15,
    SOURCE_COMPANY_SOCIAL: 10,
    SOURCE_TRADE_BODY: 8,
    SOURCE_THIRD_PARTY_DIRECTORY: 5,
}

CONTACT_FIELDS = {"phone", "email"}


@dataclass
class ScoreResult:
    identity: int
    trading: int
    website: int
    contact: int
    buyer: int
    demand: int
    freshness: int
    reasons: list[str]

    @property
    def total(self) -> int:
        return (
            self.identity
            + self.trading
            + self.website
            + self.contact
            + self.buyer
            + self.demand
            + self.freshness
        )

    @property
    def band(self) -> str:
        total = self.total
        if total >= 80:
            return BAND_STRONG
        if total >= 55:
            return BAND_REVIEW
        return BAND_ENRICH


def score_company(
    company: Company,
    evidence: list[Evidence],
    today: dt.date | None = None,
) -> ScoreResult:
    today = today or dt.date.today()
    reasons: list[str] = []
    live = [e for e in evidence if not e.superseded]

    # Legal identity. Binary by design: either the register shows an active
    # company right now, or it does not. There is no partial credit for
    # "probably still trading".
    if company.adverse_status_flag:
        identity = 0
        reasons.append(
            f"identity 0/25: {company.adverse_status_reason or 'adverse register status'}"
        )
    elif (company.company_status or "").lower() == "active":
        identity = MAX_IDENTITY
        reasons.append("identity 25/25: active Companies House record")
    else:
        identity = 0
        reasons.append(
            f"identity 0/25: status is {company.company_status or 'unknown'}"
        )

    # Legal to trading link.
    if company.trading_match_status == MATCH_LINKED:
        trading = MAX_TRADING
        reasons.append("trading 20/20: unambiguous first-party match")
    elif company.trading_match_status == "CANDIDATE":
        trading = 10
        reasons.append("trading 10/20: candidate name with consistent location")
    else:
        trading = 0
        reasons.append("trading 0/20: no corroborated relationship")

    # Website and domain ownership.
    if company.website_match_status == MATCH_LINKED:
        website = MAX_WEBSITE
        reasons.append("website 15/15: site explicitly linked to the legal entity")
    elif company.website_match_status == "CANDIDATE":
        website = 7
        reasons.append("website 7/15: candidate operating site, not proven")
    else:
        website = 0
        reasons.append("website 0/15: unknown or conflicting domain")

    # Contact provenance. Best single source wins; stacking two weak sources
    # does not make a strong one.
    contact_points = 0
    best_source = None
    for item in live:
        if item.field not in CONTACT_FIELDS:
            continue
        points = CONTACT_POINTS.get(item.source_class, 0)
        if points > contact_points:
            contact_points = points
            best_source = item.source_class
    contact = min(contact_points, MAX_CONTACT)
    if contact:
        reasons.append(f"contact {contact}/15: best source is {best_source}")
    else:
        reasons.append("contact 0/15: no contact evidence")

    # Training buyer. An inferred role scores zero. That is deliberate: a
    # guessed job title is not a person who buys training.
    if company.training_buyer_confirmed and company.training_buyer_name:
        buyer = MAX_BUYER
        reasons.append("buyer 10/10: named decision-maker confirmed")
    else:
        buyer = 0
        reasons.append("buyer 0/10: no confirmed training decision-maker")

    # Demand. Confirmed by the business is full marks, a documented trigger is
    # half, an assumption is nothing. Never infer a compliance failure.
    if company.demand_confirmed:
        demand = MAX_DEMAND
        reasons.append("demand 10/10: requirement confirmed by the business")
    elif company.demand_evidence:
        demand = 5
        reasons.append("demand 5/10: documented trigger only")
    else:
        demand = 0
        reasons.append("demand 0/10: no demand evidence")

    # Freshness. All-or-nothing on the newest piece of evidence.
    freshness = 0
    if live:
        newest = max(e.checked_date for e in live)
        age = (today - newest).days
        if age <= settings.evidence_fresh_days:
            freshness = MAX_FRESHNESS
            reasons.append(f"freshness 5/5: newest evidence is {age} days old")
        else:
            reasons.append(
                f"freshness 0/5: newest evidence is {age} days old, refresh needed"
            )
    else:
        reasons.append("freshness 0/5: no dated evidence")

    return ScoreResult(
        identity=identity,
        trading=trading,
        website=website,
        contact=contact,
        buyer=buyer,
        demand=demand,
        freshness=freshness,
        reasons=reasons,
    )


def apply_score(company: Company, result: ScoreResult) -> None:
    company.score_identity = result.identity
    company.score_trading = result.trading
    company.score_website = result.website
    company.score_contact = result.contact
    company.score_buyer = result.buyer
    company.score_demand = result.demand
    company.score_freshness = result.freshness
    company.score_total = result.total
    company.score_band = result.band
    company.score_reasons = "\n".join(result.reasons)
    company.scored_at = dt.datetime.now(dt.timezone.utc)
