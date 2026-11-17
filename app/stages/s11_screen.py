"""Stage 11: TPS and CTPS screening.

Screens every number on a call gate whose result is missing or expired, and
writes the provider, the timestamp and the receipt against the record. That
receipt is the thing you produce if anyone ever asks who screened the number
and when.

Re-screening is not optional housekeeping. A number that was clear last month
can be registered today, so a result older than SCREENING_VALID_DAYS is
treated as no result and the gate closes again on its own.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GATE_APPROVED,
    GATE_HOLD,
    SCREEN_CLEAR,
    CallGate,
    Company,
)
from app.screening import ScreeningProvider, get_provider
from app.stages.common import log
from app.stages.s09_route import evaluate_call_gate


def due_for_screening(session: Session, limit: int | None = None) -> list[CallGate]:
    gates = session.scalars(
        select(CallGate).where(CallGate.phone_e164.is_not(None)).order_by(CallGate.id)
    ).all()
    due = [
        g
        for g in gates
        if g.screening_expired(settings.screening_valid_days)
        or g.tps_result != SCREEN_CLEAR
        or g.ctps_result != SCREEN_CLEAR
    ]
    return due[:limit] if limit else list(due)


def run(
    session: Session,
    provider: ScreeningProvider | None = None,
    limit: int | None = None,
    actor: str = "stage-11",
) -> dict[str, int]:
    gates = due_for_screening(session, limit)
    if not gates:
        return {"screened": 0, "clear": 0, "registered": 0, "errors": 0}

    owned = provider is None
    provider = provider or get_provider()

    try:
        phones = [g.phone_e164 for g in gates if g.phone_e164]
        results = provider.screen(phones)

        clear = registered = errors = 0
        for gate in gates:
            result = results.get(gate.phone_e164 or "")
            if result is None:
                continue

            gate.tps_result = result.tps
            gate.ctps_result = result.ctps
            gate.screened_at = result.checked_at
            gate.screening_provider = result.provider
            gate.screening_receipt = result.receipt

            if result.clear:
                clear += 1
            elif "ERROR" in (result.tps, result.ctps):
                errors += 1
            else:
                registered += 1

            company = session.get(Company, gate.company_id)

            # A number that is now registered revokes any standing approval.
            # Nobody should have to remember to do this by hand. The approver
            # fields are cleared too: approved_by means "approved right now",
            # and the history lives in the audit log.
            if not result.clear and gate.human_decision == GATE_APPROVED:
                gate.human_decision = GATE_HOLD
                previous = gate.approved_by
                gate.approved_by = None
                gate.approved_at = None
                gate.decision_note = (
                    f"Approval by {previous} revoked automatically on "
                    f"re-screening: {result.summary}"
                )
                log(
                    session,
                    actor=actor,
                    action="CALL_APPROVAL_REVOKED",
                    company=company,
                    channel="CALL",
                    detail=f"previously approved by {previous}; {result.summary}",
                )

            # Re-run the auto gate so its verdict reflects the new result
            # rather than the one it was given before screening.
            if company is not None:
                evaluate_call_gate(session, company, gate)
    finally:
        if owned:
            provider.close()

    log(
        session,
        actor=actor,
        action="STAGE_11_SCREEN",
        channel="CALL",
        detail=(
            f"screened={len(gates)} clear={clear} registered={registered} "
            f"errors={errors} provider={provider.name}"
        ),
    )
    return {
        "screened": len(gates),
        "clear": clear,
        "registered": registered,
        "errors": errors,
    }
