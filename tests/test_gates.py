"""Tests for the parts that must not break: scoring and the two gates.

Run with:  python -m pytest tests -q
No network, no API key, no Postgres. Uses an in-memory SQLite database.
"""

from __future__ import annotations

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["AUTHORISED_APPROVERS"] = "Compliance Owner,Marcus"

import pytest  # noqa: E402
from sqlalchemy import create_engine, event  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.approvals import ApprovalError, decide_call, decide_email  # noqa: E402
from app.models import (  # noqa: E402
    Base,
    CallGate,
    Company,
    EmailGate,
    MATCH_CANDIDATE,
    MATCH_LINKED,
    SOURCE_COMPANIES_HOUSE,
    SOURCE_COMPANY_WEBSITE,
    SOURCE_THIRD_PARTY_DIRECTORY,
    SuppressionEntry,
)
from app.scoring import score_company  # noqa: E402
from app.stages.common import record_evidence  # noqa: E402
from app.stages.s09_route import ensure_gates, evaluate_call_gate, evaluate_email_gate  # noqa: E402


@pytest.fixture()
def session() -> Session:
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _fk(dbapi_connection, _record):
        cur = dbapi_connection.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s


def make_company(session: Session, **kwargs) -> Company:
    defaults = dict(
        company_number="12411761",
        pilot_id="N-001",
        legal_name="TREAD SAFE UK SCAFFOLDING LTD",
        company_status="active",
        registered_postcode="M1 1AA",
    )
    defaults.update(kwargs)
    company = Company(**defaults)
    session.add(company)
    session.flush()
    return company


def fully_clear_call_gate(session: Session, company: Company) -> CallGate:
    _, gate = ensure_gates(session, company)
    gate.phone_e164 = "07539526885"
    gate.phone_ownership_status = MATCH_LINKED
    gate.tps_result = "CLEAR"
    gate.ctps_result = "CLEAR"
    gate.screened_at = dt.datetime.now(dt.timezone.utc)
    gate.screening_provider = "test"
    gate.screening_receipt = "test receipt"
    gate.internal_dnc_checked = True
    gate.gdpr_review_done = True
    gate.caller_id_configured = True
    evaluate_call_gate(session, company, gate)
    return gate


def fully_clear_email_gate(session: Session, company: Company) -> EmailGate:
    gate, _ = ensure_gates(session, company)
    gate.email_candidate = "info@treadsafeukscaffolding.co.uk"
    gate.mailbox_entity_match = MATCH_LINKED
    gate.subscriber_category = "CORPORATE"
    gate.lawful_basis_recorded = True
    gate.privacy_notice_ready = True
    gate.suppression_checked = True
    gate.sender_and_optout_ready = True
    gate.course_offer_checked = True
    evaluate_email_gate(session, company, gate)
    return gate


# ---------------------------------------------------------------- scoring


def test_score_matches_workbook_for_a_bare_record(session: Session) -> None:
    """N-002 in the workbook: active company, nothing else. Should be 30."""
    company = make_company(session, company_number="17373882", legal_name="DSA ROOFING LTD")
    record_evidence(
        session, company, "legal_name", "DSA ROOFING LTD",
        SOURCE_COMPANIES_HOUSE, match_status=MATCH_LINKED,
    )
    session.flush()
    result = score_company(company, company.evidence)
    assert result.identity == 25
    assert result.freshness == 5
    assert result.total == 30
    assert result.band == "ENRICH"


def test_score_matches_workbook_for_a_corroborated_record(session: Session) -> None:
    """N-006 in the workbook: 25 + 10 + 7 + 15 + 5 = 62, band REVIEW."""
    company = make_company(
        session,
        trading_match_status=MATCH_CANDIDATE,
        website_match_status=MATCH_CANDIDATE,
    )
    record_evidence(
        session, company, "phone", "07539526885",
        SOURCE_COMPANY_WEBSITE, match_status=MATCH_CANDIDATE,
    )
    session.flush()
    result = score_company(company, company.evidence)
    assert (result.identity, result.trading, result.website) == (25, 10, 7)
    assert result.contact == 15
    assert result.total == 62
    assert result.band == "REVIEW"


def test_strike_off_zeroes_identity(session: Session) -> None:
    company = make_company(
        session,
        company_number="05666596",
        company_status="active - proposal to strike off",
        adverse_status_flag=True,
        adverse_status_reason="Active proposal to strike off",
    )
    result = score_company(company, [])
    assert result.identity == 0


def test_stale_evidence_loses_the_freshness_point(session: Session) -> None:
    company = make_company(session)
    record_evidence(
        session, company, "legal_name", "X", SOURCE_COMPANIES_HOUSE,
        checked_date=dt.date.today() - dt.timedelta(days=200),
    )
    session.flush()
    assert score_company(company, company.evidence).freshness == 0


def test_best_contact_source_wins_and_does_not_stack(session: Session) -> None:
    company = make_company(session)
    record_evidence(
        session, company, "phone", "01612345678", SOURCE_THIRD_PARTY_DIRECTORY
    )
    session.flush()
    assert score_company(company, company.evidence).contact == 5


# ------------------------------------------------------------- call gate


def test_call_gate_holds_without_screening(session: Session) -> None:
    company = make_company(session)
    _, gate = ensure_gates(session, company)
    gate.phone_e164 = "07539526885"
    gate.phone_ownership_status = MATCH_LINKED
    evaluate_call_gate(session, company, gate)
    assert gate.auto_gate_result == "HOLD"
    assert "TPS" in gate.auto_gate_reason


def test_call_gate_holds_when_registered_on_tps(session: Session) -> None:
    company = make_company(session)
    gate = fully_clear_call_gate(session, company)
    gate.tps_result = "REGISTERED"
    evaluate_call_gate(session, company, gate)
    assert gate.auto_gate_result == "HOLD"


def test_expired_screening_closes_the_gate_again(session: Session) -> None:
    company = make_company(session)
    gate = fully_clear_call_gate(session, company)
    assert gate.auto_gate_result == "APPROVED"
    gate.screened_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=40)
    evaluate_call_gate(session, company, gate)
    assert gate.auto_gate_result == "HOLD"
    assert "older than" in gate.auto_gate_reason


def test_automated_calls_need_consent_even_when_tps_is_clear(session: Session) -> None:
    company = make_company(session)
    gate = fully_clear_call_gate(session, company)
    gate.call_method = "AUTOMATED"
    evaluate_call_gate(session, company, gate)
    assert gate.auto_gate_result == "HOLD"
    assert "consent" in gate.auto_gate_reason


def test_suppression_beats_a_clear_screening(session: Session) -> None:
    company = make_company(session)
    session.add(
        SuppressionEntry(
            value="07539526885", kind="PHONE",
            reason="asked not to be contacted", added_by="Compliance Owner",
        )
    )
    session.flush()
    gate = fully_clear_call_gate(session, company)
    assert gate.auto_gate_result == "HOLD"
    assert "suppression" in gate.auto_gate_reason


def test_adverse_company_status_holds_both_routes(session: Session) -> None:
    company = make_company(
        session, adverse_status_flag=True, adverse_status_reason="Dissolved"
    )
    call = fully_clear_call_gate(session, company)
    email = fully_clear_email_gate(session, company)
    assert call.auto_gate_result == "HOLD"
    assert email.auto_gate_result == "HOLD"


# ------------------------------------------------------------ email gate


def test_email_gate_ignores_tps_entirely(session: Session) -> None:
    """A number on TPS must not block the email route. Different law."""
    company = make_company(session)
    call = fully_clear_call_gate(session, company)
    call.tps_result = "REGISTERED"
    evaluate_call_gate(session, company, call)
    email = fully_clear_email_gate(session, company)
    assert call.auto_gate_result == "HOLD"
    assert email.auto_gate_result == "APPROVED"


def test_email_gate_holds_while_subscriber_category_unresolved(session: Session) -> None:
    company = make_company(session)
    gate = fully_clear_email_gate(session, company)
    gate.subscriber_category = "REVIEW_LIKELY_CORPORATE"
    evaluate_email_gate(session, company, gate)
    assert gate.auto_gate_result == "HOLD"
    assert "subscriber category" in gate.auto_gate_reason


# ------------------------------------------------------------- approvals


def test_cannot_approve_while_auto_gate_holds(session: Session) -> None:
    company = make_company(session)
    ensure_gates(session, company)
    evaluate_call_gate(session, company, company.call_gate)
    with pytest.raises(ApprovalError):
        decide_call(session, company, "APPROVED", "Compliance Owner")


def test_cannot_approve_as_an_unlisted_person(session: Session) -> None:
    company = make_company(session)
    fully_clear_call_gate(session, company)
    with pytest.raises(ApprovalError):
        decide_call(session, company, "APPROVED", "Someone Random")


def test_approval_records_the_name_and_time(session: Session) -> None:
    company = make_company(session)
    fully_clear_call_gate(session, company)
    gate = decide_call(session, company, "APPROVED", "Compliance Owner", "checked")
    session.commit()
    assert gate.human_decision == "APPROVED"
    assert gate.approved_by == "Compliance Owner"
    assert gate.approved_at is not None
    assert gate.is_callable(28)


def test_approving_calls_does_not_approve_email(session: Session) -> None:
    company = make_company(session)
    fully_clear_call_gate(session, company)
    fully_clear_email_gate(session, company)
    decide_call(session, company, "APPROVED", "Compliance Owner")
    session.commit()
    assert company.call_gate.human_decision == "APPROVED"
    assert company.email_gate.human_decision == "HOLD"
    assert company.email_gate.approved_by is None


def test_database_refuses_an_approval_with_no_named_approver(session: Session) -> None:
    """The constraint, not the application, is the last line of defence."""
    company = make_company(session)
    fully_clear_call_gate(session, company)
    session.commit()
    # Simulate a stray UPDATE that skips app.approvals entirely.
    company.call_gate.human_decision = "APPROVED"
    company.call_gate.approved_by = None
    company.call_gate.approved_at = None
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_database_refuses_an_approval_without_screening(session: Session) -> None:
    company = make_company(session)
    _, gate = ensure_gates(session, company)
    gate.phone_e164 = "07539526885"
    session.commit()
    gate.human_decision = "APPROVED"
    gate.approved_by = "Compliance Owner"
    gate.approved_at = dt.datetime.now(dt.timezone.utc)
    with pytest.raises(IntegrityError):
        session.commit()
    session.rollback()


def test_a_changed_number_wipes_the_old_screening(session: Session) -> None:
    company = make_company(session)
    gate = fully_clear_call_gate(session, company)
    session.commit()
    record_evidence(
        session, company, "phone", "01619998888",
        SOURCE_COMPANY_WEBSITE, match_status=MATCH_LINKED,
    )
    session.flush()
    ensure_gates(session, company)
    assert gate.phone_e164 == "01619998888"
    assert gate.tps_result == "NOT_RUN"
    assert gate.screened_at is None


def test_rescreening_onto_tps_revokes_and_clears_the_approver(session: Session) -> None:
    """The bug this guards: a revoked record still naming its old approver."""
    import datetime as _dt

    from app.screening.base import ScreenResult, ScreeningProvider
    from app.stages import s11_screen

    company = make_company(session)
    gate = fully_clear_call_gate(session, company)
    decide_call(session, company, "APPROVED", "Compliance Owner")
    session.commit()
    assert gate.human_decision == "APPROVED"

    gate.screened_at = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=40)
    session.commit()

    class NowOnTps(ScreeningProvider):
        name = "test-registered"

        def screen(self, phones: list[str]) -> dict[str, ScreenResult]:
            return {
                p: ScreenResult(
                    phone=p, tps="REGISTERED", ctps="CLEAR",
                    provider=self.name, receipt="test re-screen",
                )
                for p in phones
            }

    s11_screen.run(session, provider=NowOnTps())
    session.commit()

    assert gate.tps_result == "REGISTERED"
    assert gate.human_decision == "HOLD"
    assert gate.approved_by is None, "a revoked gate must not still name an approver"
    assert gate.approved_at is None
    assert gate.auto_gate_result == "HOLD", "the auto gate must be re-evaluated"
    assert gate.is_callable(28) is False


# -------------------------------------------------------------- evidence


def test_a_changed_value_supersedes_rather_than_overwrites(session: Session) -> None:
    company = make_company(session)
    record_evidence(session, company, "phone", "01611111111", SOURCE_COMPANY_WEBSITE)
    session.flush()
    record_evidence(session, company, "phone", "01612222222", SOURCE_COMPANY_WEBSITE)
    session.flush()
    rows = sorted(company.evidence, key=lambda e: e.id)
    assert len(rows) == 2
    assert rows[0].superseded is True
    assert rows[1].superseded is False
