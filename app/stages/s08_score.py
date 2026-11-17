"""Stage 08: evidence confidence score.

Pure computation over the evidence ledger. No network, no side effects beyond
the score columns. Safe to re-run as often as you like.

Worth repeating because it is the most misread number in the system: this
score prioritises research. It does not authorise contact. Stage 09 does not
read it.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import STAGE_ENRICHED, STAGE_SCORED, Company
from app.scoring import apply_score, score_company
from app.stages.common import claim, live_evidence, log


def run(
    session: Session,
    limit: int | None = None,
    rescore_all: bool = False,
    actor: str = "stage-08",
) -> dict[str, int]:
    if rescore_all:
        from sqlalchemy import select

        companies = list(session.scalars(select(Company).order_by(Company.id)).all())
    else:
        companies = claim(session, STAGE_ENRICHED, limit)

    if not companies:
        return {"scored": 0, "strong": 0, "review": 0, "enrich": 0}

    bands = {"STRONG": 0, "REVIEW": 0, "ENRICH": 0}

    for company in companies:
        result = score_company(company, live_evidence(session, company))
        apply_score(company, result)
        bands[result.band] += 1
        if company.stage == STAGE_ENRICHED:
            company.stage = STAGE_SCORED

    log(
        session,
        actor=actor,
        action="STAGE_08_SCORE",
        detail=(
            f"scored={len(companies)} strong={bands['STRONG']} "
            f"review={bands['REVIEW']} enrich={bands['ENRICH']}"
        ),
    )
    return {
        "scored": len(companies),
        "strong": bands["STRONG"],
        "review": bands["REVIEW"],
        "enrich": bands["ENRICH"],
    }
