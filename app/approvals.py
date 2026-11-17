"""The only code path in the system that can authorise outreach.

Everything else writes HOLD. This module is the single door, and it is narrow
on purpose:

  * The approver must be a name from AUTHORISED_APPROVERS in .env.
  * The auto gate for that channel must already pass.
  * Approving one channel never touches the other.
  * Every decision, in either direction, lands in the audit log.

If you ever need to widen this, widen it here, where it is visible in a diff.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GATE_APPROVED,
    GATE_HOLD,
    GATE_REJECTED,
    CallGate,
    Company,
    EmailGate,
)
from app.stages.common import log


class ApprovalError(RuntimeError):
    pass


def _check_approver(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ApprovalError("An approval must carry the name of the person approving.")
    allowed = settings.approver_list
    if not allowed:
        raise ApprovalError(
            "AUTHORISED_APPROVERS is empty in .env, so nobody can approve "
            "anything yet. Add the compliance owner's name there first."
        )
    if name not in allowed:
        raise ApprovalError(
            f"{name!r} is not in AUTHORISED_APPROVERS. "
            f"Authorised: {', '.join(allowed)}"
        )
    return name


def decide_email(
    session: Session,
    company: Company,
    decision: str,
    approver: str,
    note: str | None = None,
) -> EmailGate:
    gate = company.email_gate
    if gate is None:
        raise ApprovalError("This company has no email gate. Run stage 09 first.")

    approver = _check_approver(approver)
    decision = decision.upper()

    if decision == GATE_APPROVED:
        if gate.auto_gate_result != GATE_APPROVED:
            raise ApprovalError(
                "The automatic email checks have not passed, so this cannot be "
                f"approved yet. Outstanding: {gate.auto_gate_reason}"
            )
        gate.human_decision = GATE_APPROVED
        gate.approved_by = approver
        gate.approved_at = dt.datetime.now(dt.timezone.utc)
    elif decision in (GATE_HOLD, GATE_REJECTED):
        gate.human_decision = decision
        gate.approved_by = None
        gate.approved_at = None
    else:
        raise ApprovalError(f"Unknown decision {decision!r}.")

    gate.decision_note = note
    log(
        session,
        actor=approver,
        action=f"EMAIL_{decision}",
        company=company,
        channel="EMAIL",
        detail=note or gate.auto_gate_reason,
    )
    return gate


def decide_call(
    session: Session,
    company: Company,
    decision: str,
    approver: str,
    note: str | None = None,
) -> CallGate:
    gate = company.call_gate
    if gate is None:
        raise ApprovalError("This company has no call gate. Run stage 09 first.")

    approver = _check_approver(approver)
    decision = decision.upper()

    if decision == GATE_APPROVED:
        if gate.auto_gate_result != GATE_APPROVED:
            raise ApprovalError(
                "The automatic call checks have not passed, so this cannot be "
                f"approved yet. Outstanding: {gate.auto_gate_reason}"
            )
        if gate.screening_expired(settings.screening_valid_days):
            raise ApprovalError(
                "The TPS and CTPS screening has expired. Re-screen before approving."
            )
        gate.human_decision = GATE_APPROVED
        gate.approved_by = approver
        gate.approved_at = dt.datetime.now(dt.timezone.utc)
    elif decision in (GATE_HOLD, GATE_REJECTED):
        gate.human_decision = decision
        gate.approved_by = None
        gate.approved_at = None
    else:
        raise ApprovalError(f"Unknown decision {decision!r}.")

    gate.decision_note = note
    log(
        session,
        actor=approver,
        action=f"CALL_{decision}",
        company=company,
        channel="CALL",
        detail=note or gate.screening_receipt or gate.auto_gate_reason,
    )
    return gate
