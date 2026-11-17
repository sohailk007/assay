"""Review dashboard.

The one place a person can authorise outreach. Everything else in this
project can only ever write HOLD.

Run it with:
    python run.py serve
then open http://127.0.0.1:8000

This is an internal tool with no login, so bind it to localhost or put it
behind your own authentication before anyone outside the compliance team can
reach it. The approver name is typed per decision and must match a name in
AUTHORISED_APPROVERS.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.approvals import ApprovalError, decide_call, decide_email
from app.config import settings
from app.db import SessionLocal
from app.models import (
    GATE_APPROVED,
    CallGate,
    Company,
    EmailGate,
    Evidence,
)

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

app = FastAPI(title="Assay")


def get_session() -> Session:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    channel: str = "call",
    band: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    stmt = select(Company).order_by(Company.score_total.desc(), Company.id)
    if band:
        stmt = stmt.where(Company.score_band == band.upper())
    companies = list(session.scalars(stmt).all())

    totals = {
        "companies": session.scalar(select(func.count(Company.id))) or 0,
        "email_approved": session.scalar(
            select(func.count(EmailGate.id)).where(
                EmailGate.human_decision == GATE_APPROVED
            )
        )
        or 0,
        "call_approved": session.scalar(
            select(func.count(CallGate.id)).where(
                CallGate.human_decision == GATE_APPROVED
            )
        )
        or 0,
        "with_phone": session.scalar(
            select(func.count(CallGate.id)).where(CallGate.phone_e164.is_not(None))
        )
        or 0,
        "with_email": session.scalar(
            select(func.count(EmailGate.id)).where(
                EmailGate.email_candidate.is_not(None)
            )
        )
        or 0,
    }

    return TEMPLATES.TemplateResponse(
        request,
        "index.html",
        {
            "companies": companies,
            "channel": channel,
            "band": band,
            "totals": totals,
            "valid_days": settings.screening_valid_days,
        },
    )


@app.get("/company/{company_id}", response_class=HTMLResponse)
def detail(
    request: Request,
    company_id: int,
    error: str = "",
    session: Session = Depends(get_session),
) -> HTMLResponse:
    company = session.get(Company, company_id)
    if company is None:
        return HTMLResponse("Not found", status_code=404)

    evidence = list(
        session.scalars(
            select(Evidence)
            .where(Evidence.company_id == company_id)
            .order_by(Evidence.superseded, Evidence.field, Evidence.checked_date.desc())
        ).all()
    )

    return TEMPLATES.TemplateResponse(
        request,
        "detail.html",
        {
            "company": company,
            "evidence": evidence,
            "approvers": settings.approver_list,
            "valid_days": settings.screening_valid_days,
            "error": error,
        },
    )


@app.post("/company/{company_id}/checklist")
def update_checklist(
    company_id: int,
    channel: str = Form(...),
    session: Session = Depends(get_session),
    lawful_basis: str = Form(""),
    privacy_notice: str = Form(""),
    suppression: str = Form(""),
    sender_optout: str = Form(""),
    course_offer: str = Form(""),
    subscriber_category: str = Form(""),
    internal_dnc: str = Form(""),
    gdpr_review: str = Form(""),
    caller_id: str = Form(""),
    phone_ownership: str = Form(""),
) -> RedirectResponse:
    """Record the manual checks. This never approves anything by itself."""
    company = session.get(Company, company_id)
    if company is None:
        return RedirectResponse("/", status_code=303)

    from app.stages.s09_route import evaluate_call_gate, evaluate_email_gate

    if channel == "email" and company.email_gate:
        gate = company.email_gate
        gate.lawful_basis_recorded = bool(lawful_basis)
        gate.privacy_notice_ready = bool(privacy_notice)
        gate.suppression_checked = bool(suppression)
        gate.sender_and_optout_ready = bool(sender_optout)
        gate.course_offer_checked = bool(course_offer)
        if subscriber_category:
            gate.subscriber_category = subscriber_category
        evaluate_email_gate(session, company, gate)

    if channel == "call" and company.call_gate:
        gate = company.call_gate
        gate.internal_dnc_checked = bool(internal_dnc)
        gate.gdpr_review_done = bool(gdpr_review)
        gate.caller_id_configured = bool(caller_id)
        if phone_ownership:
            gate.phone_ownership_status = phone_ownership
        evaluate_call_gate(session, company, gate)

    session.commit()
    return RedirectResponse(f"/company/{company_id}", status_code=303)


@app.post("/company/{company_id}/decide")
def decide(
    company_id: int,
    channel: str = Form(...),
    decision: str = Form(...),
    approver: str = Form(...),
    note: str = Form(""),
    session: Session = Depends(get_session),
) -> RedirectResponse:
    company = session.get(Company, company_id)
    if company is None:
        return RedirectResponse("/", status_code=303)

    try:
        if channel == "email":
            decide_email(session, company, decision, approver, note or None)
        else:
            decide_call(session, company, decision, approver, note or None)
        session.commit()
    except ApprovalError as exc:
        session.rollback()
        from urllib.parse import quote

        return RedirectResponse(
            f"/company/{company_id}?error={quote(str(exc))}", status_code=303
        )

    return RedirectResponse(f"/company/{company_id}", status_code=303)
