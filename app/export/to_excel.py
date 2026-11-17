"""Export the database back into the workbook format the founder already uses.

Produces the sheets that carry live data: the master register, the field-level
evidence ledger, the two channel routes and the pilot scorecard. The protocol
map, scoring rules and source policy sheets are fixed reference text and stay
in the original workbook.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    GATE_APPROVED,
    CallGate,
    Company,
    EmailGate,
    Evidence,
)

HEADER_FILL = PatternFill("solid", fgColor="1C1C1A")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=10)
TITLE_FONT = Font(bold=True, size=12)
NOTE_FONT = Font(italic=True, size=9, color="5F5E5A")


def _write_sheet(
    wb: Workbook,
    name: str,
    title: str,
    note: str,
    headers: list[str],
    rows: list[list],
) -> None:
    ws = wb.create_sheet(name[:31])
    ws["A1"] = title
    ws["A1"].font = TITLE_FONT
    ws["A2"] = note
    ws["A2"].font = NOTE_FONT

    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=4, column=col, value=header)
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    for r, row in enumerate(rows, start=5):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.alignment = Alignment(vertical="top", wrap_text=False)

    for col in range(1, len(headers) + 1):
        longest = len(str(headers[col - 1]))
        for row in rows[:200]:
            if col - 1 < len(row):
                longest = max(longest, len(str(row[col - 1] or "")))
        ws.column_dimensions[get_column_letter(col)].width = min(max(longest + 2, 10), 52)

    ws.freeze_panes = "A5"


def export(session: Session, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    today = dt.date.today().isoformat()

    wb = Workbook()
    wb.remove(wb.active)

    companies = list(
        session.scalars(
            select(Company).order_by(Company.score_total.desc(), Company.id)
        ).all()
    )

    # 03 Master register
    master_rows = []
    for c in companies:
        gate = c.call_gate
        master_rows.append(
            [
                c.pilot_id or str(c.id),
                c.legal_name,
                c.company_number,
                c.sic_codes or "",
                c.company_status or "",
                c.registered_locality or "",
                c.companies_house_url,
                c.trading_match_status,
                c.website_url or "",
                c.website_match_status,
                (gate.phone_evidence_url if gate else "") or "",
                "CONFIRMED" if c.training_buyer_confirmed else "NOT CONFIRMED",
                "CONFIRMED" if c.demand_confirmed else "NOT CONFIRMED",
                c.scored_at.date().isoformat() if c.scored_at else "",
                c.score_identity,
                c.score_trading,
                c.score_website,
                c.score_contact,
                c.score_buyer,
                c.score_demand,
                c.score_freshness,
                c.score_total,
                c.score_band or "",
                c.course_match or "",
                c.notes or "",
            ]
        )

    _write_sheet(
        wb,
        "03 MASTER",
        "MASTER ENTITY REGISTER",
        f"Generated {today}. Every record needs live revalidation before outreach. "
        "A score is evidence confidence, never permission to contact.",
        [
            "Pilot ID", "Legal name", "CH no", "SIC", "CH status", "Area",
            "CH link", "Trade match", "Site link", "Site match",
            "Contact evidence URL", "Buyer evidence", "Demand evidence",
            "Checked date", "Identity /25", "Trade /20", "Site /15",
            "Contact /15", "Buyer /10", "Demand /10", "Freshness /5",
            "TOTAL /100", "Evidence band", "Potential course match", "Notes",
        ],
        master_rows,
    )

    # 04 Claim evidence
    evidence_rows = []
    records = list(
        session.scalars(
            select(Evidence).order_by(Evidence.company_id, Evidence.id)
        ).all()
    )
    by_id = {c.id: c for c in companies}
    for n, ev in enumerate(records, start=1):
        company = by_id.get(ev.company_id)
        evidence_rows.append(
            [
                f"EV-{n:03d}",
                (company.pilot_id if company else "") or "",
                company.company_number if company else "",
                ev.field,
                ev.observed_value,
                ev.source_class,
                ev.source_url or "",
                ev.checked_date.isoformat(),
                ev.entity_match_status,
                "SUPERSEDED" if ev.superseded else "LIVE",
                ev.follow_up or "",
            ]
        )

    _write_sheet(
        wb,
        "04 CLAIM EVIDENCE",
        "FIELD-LEVEL EVIDENCE",
        "Each claim stands alone with its own source and date. "
        "Source authenticity is not permission to market.",
        [
            "Claim ID", "Lead ID", "CH no", "Field", "Observed value",
            "Source class", "Public evidence URL", "Checked",
            "Entity match", "Field status", "Follow-up needed",
        ],
        evidence_rows,
    )

    # 05 Email only
    email_rows = []
    for c in companies:
        g = c.email_gate
        if g is None:
            continue
        email_rows.append(
            [
                c.pilot_id or str(c.id),
                c.legal_name,
                g.email_candidate or "",
                g.email_evidence_url or "",
                g.subscriber_category,
                "YES" if g.lawful_basis_recorded else "PENDING",
                "YES" if g.privacy_notice_ready else "PENDING",
                "YES" if g.suppression_checked else "PENDING",
                "YES" if g.sender_and_optout_ready else "PENDING",
                g.mailbox_entity_match,
                "YES" if g.course_offer_checked else "PENDING",
                g.auto_gate_result,
                g.approved_by
                if g.human_decision == GATE_APPROVED and g.approved_by
                else "NOT AUTHORISED",
                g.approved_at.isoformat()
                if g.human_decision == GATE_APPROVED and g.approved_at
                else "",
                g.decision_note or g.auto_gate_reason or "",
            ]
        )

    _write_sheet(
        wb,
        "05 EMAIL ONLY",
        "MARCUS  |  CORPORATE EMAIL ROUTE",
        "TPS and CTPS screening is not an email clearance requirement. "
        "This route carries no calling clearance of any kind.",
        [
            "Lead ID", "Company", "Email candidate", "Email evidence link",
            "Subscriber category", "GDPR basis", "Privacy notice",
            "Internal suppression", "Sender + opt-out", "Mailbox entity match",
            "Course offer checked", "AUTO GATE RESULT", "Human authorisation",
            "Authorised at", "Reason / next action",
        ],
        email_rows,
    )

    # 06 Calls only
    call_rows = []
    for c in companies:
        g = c.call_gate
        if g is None:
            continue
        expired = g.screening_expired(settings.screening_valid_days)
        call_rows.append(
            [
                c.pilot_id or str(c.id),
                c.legal_name,
                g.phone_e164 or "",
                g.phone_evidence_url or "",
                g.phone_ownership_status,
                g.tps_result,
                g.ctps_result,
                g.screened_at.isoformat() if g.screened_at else "",
                "EXPIRED" if expired else "CURRENT",
                g.screening_provider or "",
                "YES" if g.internal_dnc_checked else "PENDING",
                "YES" if g.gdpr_review_done else "PENDING",
                "YES" if g.caller_id_configured else "PENDING",
                g.call_method,
                g.auto_gate_result,
                g.approved_by
                if g.human_decision == GATE_APPROVED and g.approved_by
                else "NOT AUTHORISED",
                g.approved_at.isoformat()
                if g.human_decision == GATE_APPROVED and g.approved_at
                else "",
                g.screening_receipt or "",
            ]
        )

    _write_sheet(
        wb,
        "06 CALLS ONLY",
        "ABBY  |  LIVE TELEPHONE ROUTE",
        f"Screening expires after {settings.screening_valid_days} days and an expired "
        "result counts as no result. A public number does not override TPS or CTPS.",
        [
            "Lead ID", "Company", "Business phone", "Phone evidence link",
            "Phone ownership", "TPS", "CTPS", "Screened at", "Screening state",
            "Provider", "Internal DNC", "GDPR review", "Caller ID",
            "Call method", "AUTO GATE RESULT", "Human authorisation",
            "Authorised at", "Screening receipt",
        ],
        call_rows,
    )

    # 07 Pilot control
    total = len(companies)
    with_phone = session.scalar(
        select(func.count(CallGate.id)).where(CallGate.phone_e164.is_not(None))
    ) or 0
    with_email = session.scalar(
        select(func.count(EmailGate.id)).where(EmailGate.email_candidate.is_not(None))
    ) or 0
    call_auto = session.scalar(
        select(func.count(CallGate.id)).where(CallGate.auto_gate_result == GATE_APPROVED)
    ) or 0
    email_auto = session.scalar(
        select(func.count(EmailGate.id)).where(
            EmailGate.auto_gate_result == GATE_APPROVED
        )
    ) or 0
    call_human = session.scalar(
        select(func.count(CallGate.id)).where(CallGate.human_decision == GATE_APPROVED)
    ) or 0
    email_human = session.scalar(
        select(func.count(EmailGate.id)).where(
            EmailGate.human_decision == GATE_APPROVED
        )
    ) or 0
    buyers = sum(1 for c in companies if c.training_buyer_confirmed)
    strong = sum(1 for c in companies if c.score_band == "STRONG")

    _write_sheet(
        wb,
        "07 PILOT CONTROL",
        "PILOT SCORECARD  |  SOURCE QUALITY vs CONTACT PERMISSION",
        f"Generated {today} from the live database. Counts of approvals, not of dials.",
        ["Metric", "Independent research cohort", "Why it matters"],
        [
            ["Company-level records", total, "A company identified is not a lead ready for contact"],
            ["Numbers present", with_phone, "Counts presence, not verified ownership nor legal eligibility"],
            ["Email candidates present", with_email, "A corporate mailbox still needs independent checks"],
            ["Named training contacts", buyers, "Identifying buying authority should reduce gatekeeper calls"],
            ["Phone auto gate passed", call_auto, "Mechanical checks only. Never a final approval"],
            ["Email auto gate passed", email_auto, "Mechanical checks only. Never a final approval"],
            ["Phone human authorisations", call_human, "Named human sign-off, the only thing that permits a call"],
            ["Email human authorisations", email_human, "Named human sign-off, the only thing that permits a send"],
            ["Scored high-confidence records", strong, "Evidence confidence is distinct from sales qualification"],
        ],
    )

    wb.save(path)
    return path
