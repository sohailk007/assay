"""Stage 09: split routing, and the two gates.

This is the file that matters most, so read the whole docstring.

The email route and the calling route are governed by different law. Email
marketing to a corporate subscriber turns on lawful basis, a privacy notice,
source legitimacy and a working opt-out. Live marketing calls turn on TPS and
CTPS screening, the internal do-not-call list and caller identification.
Neither set of checks says anything about the other.

So the two gates are separate tables, built from the same company but sharing
no clearance field. There is no "approved" column on Company and there never
will be. Clearing one route does not clear the other.

The auto gate is a checklist, not a decision. The best it can do is say
"nothing mechanical is blocking this". A human with a name still has to say
yes afterwards, in the dashboard, and the database refuses to store an
approval without that name attached.

Three things override everything else, permanently:
  1. An adverse Companies House status.
  2. An entry in the internal suppression table.
  3. Screening older than SCREENING_VALID_DAYS, which counts as no screening.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GATE_APPROVED,
    GATE_HOLD,
    MATCH_UNKNOWN,
    SCREEN_CLEAR,
    STAGE_ROUTED,
    STAGE_SCORED,
    CallGate,
    Company,
    EmailGate,
    Evidence,
    SuppressionEntry,
)
from app.stages.common import claim, log


def _suppressed(session: Session, *values: str | None) -> str | None:
    """Return the matching suppression reason, or None."""
    wanted = [v for v in values if v]
    if not wanted:
        return None
    rows = session.scalars(
        select(SuppressionEntry).where(SuppressionEntry.value.in_(wanted))
    ).all()
    if rows:
        entry = rows[0]
        return f"internal suppression: {entry.kind} {entry.value} ({entry.reason or 'no reason given'})"
    return None


def _best_evidence(session: Session, company: Company, field: str) -> Evidence | None:
    return session.scalars(
        select(Evidence)
        .where(
            Evidence.company_id == company.id,
            Evidence.field == field,
            Evidence.superseded.is_(False),
        )
        .order_by(Evidence.checked_date.desc())
    ).first()


def evaluate_email_gate(session: Session, company: Company, gate: EmailGate) -> None:
    """Machine checks only. Never writes APPROVED to human_decision."""
    blockers: list[str] = []

    if company.adverse_status_flag:
        blockers.append(
            f"Companies House status: {company.adverse_status_reason or 'adverse'}"
        )

    reason = _suppressed(
        session,
        gate.email_candidate,
        company.company_number,
        (gate.email_candidate or "").split("@")[-1] or None,
    )
    if reason:
        blockers.append(reason)

    if not gate.email_candidate:
        blockers.append("no corporate email candidate on file")
    if gate.mailbox_entity_match == MATCH_UNKNOWN:
        blockers.append("mailbox not matched to the legal entity")
    if gate.subscriber_category.startswith("REVIEW"):
        blockers.append(
            "subscriber category unresolved: corporate and individual "
            "subscribers follow different PECR rules"
        )
    if not gate.lawful_basis_recorded:
        blockers.append("UK GDPR lawful basis not recorded")
    if not gate.privacy_notice_ready:
        blockers.append("privacy notice not ready")
    if not gate.suppression_checked:
        blockers.append("internal suppression not checked")
    if not gate.sender_and_optout_ready:
        blockers.append("sender identity and opt-out not in place")
    if not gate.course_offer_checked:
        blockers.append("course offer not confirmed with TrainifyHub ops")

    if blockers:
        gate.auto_gate_result = GATE_HOLD
        gate.auto_gate_reason = "; ".join(blockers)
    else:
        gate.auto_gate_result = GATE_APPROVED
        gate.auto_gate_reason = (
            "All mechanical email checks pass. This is not authorisation. "
            "A named person must approve in the dashboard."
        )


def evaluate_call_gate(session: Session, company: Company, gate: CallGate) -> None:
    """Machine checks only. Never writes APPROVED to human_decision."""
    blockers: list[str] = []

    if company.adverse_status_flag:
        blockers.append(
            f"Companies House status: {company.adverse_status_reason or 'adverse'}"
        )

    reason = _suppressed(session, gate.phone_e164, company.company_number)
    if reason:
        blockers.append(reason)

    if not gate.phone_e164:
        blockers.append("no business telephone number on file")
    if gate.phone_ownership_status == MATCH_UNKNOWN:
        blockers.append("current ownership of the number not established")

    if gate.tps_result != SCREEN_CLEAR:
        blockers.append(f"TPS screening is {gate.tps_result}")
    if gate.ctps_result != SCREEN_CLEAR:
        blockers.append(f"CTPS screening is {gate.ctps_result}")
    if gate.screening_expired(settings.screening_valid_days):
        blockers.append(
            f"screening missing or older than {settings.screening_valid_days} days"
        )

    if not gate.internal_dnc_checked:
        blockers.append("internal do-not-call list not checked")
    if not gate.gdpr_review_done:
        blockers.append("UK GDPR review not done")
    if not gate.caller_id_configured:
        blockers.append("caller identification not configured")
    if gate.call_method == "AUTOMATED" and not gate.automated_call_consent:
        blockers.append(
            "automated calls need the subscriber's prior specific consent "
            "(PECR regulation 19), which TPS screening does not provide"
        )

    if blockers:
        gate.auto_gate_result = GATE_HOLD
        gate.auto_gate_reason = "; ".join(blockers)
    else:
        gate.auto_gate_result = GATE_APPROVED
        gate.auto_gate_reason = (
            "All mechanical call checks pass. This is not authorisation. "
            "A named person must approve in the dashboard."
        )


def ensure_gates(session: Session, company: Company) -> tuple[EmailGate, CallGate]:
    """Create both gates if missing and refresh their contact candidates."""
    email_gate = company.email_gate
    if email_gate is None:
        email_gate = EmailGate(company_id=company.id)
        session.add(email_gate)
        session.flush()
        company.email_gate = email_gate

    call_gate = company.call_gate
    if call_gate is None:
        call_gate = CallGate(company_id=company.id)
        session.add(call_gate)
        session.flush()
        company.call_gate = call_gate

    email_evidence = _best_evidence(session, company, "email")
    if email_evidence and email_gate.human_decision != GATE_APPROVED:
        if email_gate.email_candidate != email_evidence.observed_value:
            # A changed address invalidates whatever review was done before.
            email_gate.email_candidate = email_evidence.observed_value
            email_gate.email_evidence_url = email_evidence.source_url
            email_gate.mailbox_entity_match = email_evidence.entity_match_status
            email_gate.lawful_basis_recorded = False
            email_gate.privacy_notice_ready = False
            email_gate.sender_and_optout_ready = False

    phone_evidence = _best_evidence(session, company, "phone")
    if phone_evidence and call_gate.human_decision != GATE_APPROVED:
        if call_gate.phone_e164 != phone_evidence.observed_value:
            # A changed number invalidates any screening held against the old
            # one. Reset it rather than carrying a stale clearance forward.
            call_gate.phone_e164 = phone_evidence.observed_value
            call_gate.phone_as_published = phone_evidence.observed_value
            call_gate.phone_evidence_url = phone_evidence.source_url
            call_gate.phone_ownership_status = phone_evidence.entity_match_status
            call_gate.tps_result = "NOT_RUN"
            call_gate.ctps_result = "NOT_RUN"
            call_gate.screened_at = None
            call_gate.screening_receipt = None

    return email_gate, call_gate


def run(
    session: Session,
    limit: int | None = None,
    reroute_all: bool = False,
    actor: str = "stage-09",
) -> dict[str, int]:
    if reroute_all:
        companies = list(session.scalars(select(Company).order_by(Company.id)).all())
    else:
        companies = claim(session, STAGE_SCORED, limit)

    if not companies:
        return {"routed": 0, "email_ready": 0, "call_ready": 0}

    email_ready = 0
    call_ready = 0

    for company in companies:
        email_gate, call_gate = ensure_gates(session, company)
        evaluate_email_gate(session, company, email_gate)
        evaluate_call_gate(session, company, call_gate)

        email_ready += int(email_gate.auto_gate_result == GATE_APPROVED)
        call_ready += int(call_gate.auto_gate_result == GATE_APPROVED)

        if company.stage == STAGE_SCORED:
            company.stage = STAGE_ROUTED

    log(
        session,
        actor=actor,
        action="STAGE_09_ROUTE",
        detail=(
            f"routed={len(companies)} email_auto_pass={email_ready} "
            f"call_auto_pass={call_ready} (auto pass is not authorisation)"
        ),
    )
    return {"routed": len(companies), "email_ready": email_ready, "call_ready": call_ready}
