"""Database model for Assay, the TrainifyHub lead validation pipeline.

Four ideas carry the whole design.

1. A company is one row, keyed on its Companies House number. Never on a name.
2. Every fact about that company is a separate Evidence row carrying its own
   source URL and the date it was checked. A fact with no source cannot exist.
3. The email route and the calling route are two different tables. They are
   governed by different law, so clearance for one must never leak into the
   other. There is no shared "approved" column anywhere.
4. A gate can only reach APPROVED with a named human and a timestamp attached.
   That is a database CHECK constraint, not application logic, so no future
   bug and no stray UPDATE can authorise outreach by accident.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# Pipeline position. A stage script claims rows at one status and advances them.
STAGE_INTAKE = "01_INTAKE"
STAGE_IDENTITY = "02_IDENTITY"
STAGE_TRADING = "03_TRADING"
STAGE_DOMAIN = "04_DOMAIN"
STAGE_ENRICHED = "05_ENRICHED"
STAGE_SCORED = "08_SCORED"
STAGE_ROUTED = "09_ROUTED"

# Where a single fact came from. Drives the contact provenance score.
SOURCE_COMPANIES_HOUSE = "COMPANIES_HOUSE"
SOURCE_COMPANY_WEBSITE = "COMPANY_WEBSITE"
SOURCE_COMPANY_SOCIAL = "COMPANY_SOCIAL"
SOURCE_TRADE_BODY = "TRADE_BODY"
SOURCE_THIRD_PARTY_DIRECTORY = "THIRD_PARTY_DIRECTORY"
SOURCE_CONVERSATION = "CONVERSATION"
SOURCE_MANUAL = "MANUAL"

# How confident we are that this fact belongs to this legal entity.
MATCH_LINKED = "LINKED"        # company number found on the source itself
MATCH_CANDIDATE = "CANDIDATE"  # plausible, not proven
MATCH_REJECTED = "REJECTED"
MATCH_UNKNOWN = "UNKNOWN"

# Gate outcomes. HOLD is the only value a machine may write.
GATE_HOLD = "HOLD"
GATE_APPROVED = "APPROVED"
GATE_REJECTED = "REJECTED"

SCREEN_NOT_RUN = "NOT_RUN"
SCREEN_CLEAR = "CLEAR"
SCREEN_REGISTERED = "REGISTERED"
SCREEN_ERROR = "ERROR"


class Company(Base):
    """One legal entity. The company number is the only identifier that matters."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_number: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    pilot_id: Mapped[str | None] = mapped_column(String(16), unique=True)

    legal_name: Mapped[str] = mapped_column(String(300))
    company_status: Mapped[str | None] = mapped_column(String(64))
    company_type: Mapped[str | None] = mapped_column(String(64))
    date_of_creation: Mapped[dt.date | None] = mapped_column(Date)
    sic_codes: Mapped[str | None] = mapped_column(String(128))

    # Registered office. This is NOT a trading address and must never be
    # presented as one.
    registered_locality: Mapped[str | None] = mapped_column(String(160))
    registered_postcode: Mapped[str | None] = mapped_column(String(16))

    # Set when the register shows anything that should stop research dead:
    # dissolved, liquidation, or an active proposal to strike off.
    adverse_status_flag: Mapped[bool] = mapped_column(default=False)
    adverse_status_reason: Mapped[str | None] = mapped_column(String(300))

    trading_name: Mapped[str | None] = mapped_column(String(300))
    trading_match_status: Mapped[str] = mapped_column(String(16), default=MATCH_UNKNOWN)
    trading_match_rationale: Mapped[str | None] = mapped_column(Text)

    website_url: Mapped[str | None] = mapped_column(String(500))
    website_match_status: Mapped[str] = mapped_column(String(16), default=MATCH_UNKNOWN)
    website_match_rationale: Mapped[str | None] = mapped_column(Text)

    # Buyer and demand are filled in by a human, never by a stage script.
    training_buyer_name: Mapped[str | None] = mapped_column(String(200))
    training_buyer_role: Mapped[str | None] = mapped_column(String(200))
    training_buyer_confirmed: Mapped[bool] = mapped_column(default=False)
    demand_evidence: Mapped[str | None] = mapped_column(Text)
    demand_confirmed: Mapped[bool] = mapped_column(default=False)

    course_match: Mapped[str | None] = mapped_column(Text)

    score_total: Mapped[int] = mapped_column(Integer, default=0)
    score_identity: Mapped[int] = mapped_column(Integer, default=0)
    score_trading: Mapped[int] = mapped_column(Integer, default=0)
    score_website: Mapped[int] = mapped_column(Integer, default=0)
    score_contact: Mapped[int] = mapped_column(Integer, default=0)
    score_buyer: Mapped[int] = mapped_column(Integer, default=0)
    score_demand: Mapped[int] = mapped_column(Integer, default=0)
    score_freshness: Mapped[int] = mapped_column(Integer, default=0)
    score_band: Mapped[str | None] = mapped_column(String(16))
    score_reasons: Mapped[str | None] = mapped_column(Text)
    scored_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    stage: Mapped[str] = mapped_column(String(24), default=STAGE_INTAKE, index=True)
    cohort: Mapped[str | None] = mapped_column(String(64), index=True)
    notes: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    evidence: Mapped[list["Evidence"]] = relationship(
        back_populates="company", cascade="all, delete-orphan"
    )
    email_gate: Mapped["EmailGate | None"] = relationship(
        back_populates="company", cascade="all, delete-orphan", uselist=False
    )
    call_gate: Mapped["CallGate | None"] = relationship(
        back_populates="company", cascade="all, delete-orphan", uselist=False
    )

    @property
    def companies_house_url(self) -> str:
        from app.config import settings

        return f"{settings.companies_house_web_url}/company/{self.company_number}"

    def __repr__(self) -> str:
        return f"<Company {self.company_number} {self.legal_name!r}>"


class Evidence(Base):
    """One observed fact, one source, one date.

    Nothing is ever overwritten in place. A changed value is a new row, so the
    ledger shows what was believed and when.
    """

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), index=True
    )

    field: Mapped[str] = mapped_column(String(64), index=True)
    observed_value: Mapped[str] = mapped_column(Text)
    source_class: Mapped[str] = mapped_column(String(32))
    source_url: Mapped[str | None] = mapped_column(String(800))
    entity_match_status: Mapped[str] = mapped_column(
        String(16), default=MATCH_CANDIDATE
    )
    checked_date: Mapped[dt.date] = mapped_column(Date, index=True)
    follow_up: Mapped[str | None] = mapped_column(Text)
    superseded: Mapped[bool] = mapped_column(default=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    company: Mapped[Company] = relationship(back_populates="evidence")

    __table_args__ = (
        Index("ix_evidence_company_field", "company_id", "field", "superseded"),
    )

    def __repr__(self) -> str:
        return f"<Evidence {self.field}={self.observed_value!r} {self.source_class}>"


class EmailGate(Base):
    """Marcus's route. Governed by UK GDPR and PECR rules for electronic mail.

    TPS and CTPS are irrelevant here. Do not add them to this table.
    """

    __tablename__ = "email_gate"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), unique=True
    )

    email_candidate: Mapped[str | None] = mapped_column(String(320))
    email_evidence_url: Mapped[str | None] = mapped_column(String(800))

    # Corporate subscriber vs individual subscriber changes the PECR rules.
    # A limited company mailbox is usually corporate, a sole trader is not.
    subscriber_category: Mapped[str] = mapped_column(
        String(32), default="REVIEW_LIKELY_CORPORATE"
    )
    mailbox_entity_match: Mapped[str] = mapped_column(String(16), default=MATCH_UNKNOWN)

    lawful_basis_recorded: Mapped[bool] = mapped_column(default=False)
    privacy_notice_ready: Mapped[bool] = mapped_column(default=False)
    suppression_checked: Mapped[bool] = mapped_column(default=False)
    sender_and_optout_ready: Mapped[bool] = mapped_column(default=False)
    course_offer_checked: Mapped[bool] = mapped_column(default=False)

    auto_gate_result: Mapped[str] = mapped_column(String(16), default=GATE_HOLD)
    auto_gate_reason: Mapped[str | None] = mapped_column(Text)

    human_decision: Mapped[str] = mapped_column(String(16), default=GATE_HOLD)
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship(back_populates="email_gate")

    __table_args__ = (
        CheckConstraint(
            "human_decision <> 'APPROVED' "
            "OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_email_gate_named_approver",
        ),
        CheckConstraint(
            "auto_gate_result IN ('HOLD', 'APPROVED', 'REJECTED')",
            name="ck_email_gate_auto_values",
        ),
    )

    @property
    def is_sendable(self) -> bool:
        return (
            self.auto_gate_result == GATE_APPROVED
            and self.human_decision == GATE_APPROVED
            and bool(self.approved_by)
            and bool(self.email_candidate)
        )


class CallGate(Base):
    """Abby's route. Governed by PECR regulation 21 for live marketing calls.

    A screening result expires. SCREENING_VALID_DAYS in .env sets how fast.
    An expired result is treated exactly like no result at all.
    """

    __tablename__ = "call_gate"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("companies.id", ondelete="CASCADE"), unique=True
    )

    phone_e164: Mapped[str | None] = mapped_column(String(32))
    phone_as_published: Mapped[str | None] = mapped_column(String(64))
    phone_evidence_url: Mapped[str | None] = mapped_column(String(800))
    phone_ownership_status: Mapped[str] = mapped_column(
        String(16), default=MATCH_UNKNOWN
    )

    tps_result: Mapped[str] = mapped_column(String(16), default=SCREEN_NOT_RUN)
    ctps_result: Mapped[str] = mapped_column(String(16), default=SCREEN_NOT_RUN)
    screened_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    screening_provider: Mapped[str | None] = mapped_column(String(64))
    screening_receipt: Mapped[str | None] = mapped_column(Text)

    internal_dnc_checked: Mapped[bool] = mapped_column(default=False)
    gdpr_review_done: Mapped[bool] = mapped_column(default=False)
    caller_id_configured: Mapped[bool] = mapped_column(default=False)
    call_method: Mapped[str] = mapped_column(String(24), default="LIVE_HUMAN")
    automated_call_consent: Mapped[bool] = mapped_column(default=False)

    auto_gate_result: Mapped[str] = mapped_column(String(16), default=GATE_HOLD)
    auto_gate_reason: Mapped[str | None] = mapped_column(Text)

    human_decision: Mapped[str] = mapped_column(String(16), default=GATE_HOLD)
    approved_by: Mapped[str | None] = mapped_column(String(120))
    approved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(Text)

    company: Mapped[Company] = relationship(back_populates="call_gate")

    __table_args__ = (
        CheckConstraint(
            "human_decision <> 'APPROVED' "
            "OR (approved_by IS NOT NULL AND approved_at IS NOT NULL)",
            name="ck_call_gate_named_approver",
        ),
        # An approved call record must carry a dated screening result for both
        # registers. Belt and braces alongside the auto gate logic.
        CheckConstraint(
            "human_decision <> 'APPROVED' OR ("
            "tps_result = 'CLEAR' AND ctps_result = 'CLEAR' "
            "AND screened_at IS NOT NULL)",
            name="ck_call_gate_screened_before_approval",
        ),
        # An approved automated-dialling record must carry specific consent.
        # PECR regulation 19 governs recorded and generated speech, and TPS
        # screening does nothing for it. Scoped to approval so a record can
        # still be marked AUTOMATED while it sits on hold waiting for consent.
        CheckConstraint(
            "human_decision <> 'APPROVED' OR call_method <> 'AUTOMATED' "
            "OR automated_call_consent = 1",
            name="ck_call_gate_automated_needs_consent",
        ),
    )

    def screening_expired(self, valid_days: int) -> bool:
        if self.screened_at is None:
            return True
        now = dt.datetime.now(dt.timezone.utc)
        seen = self.screened_at
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=dt.timezone.utc)
        return (now - seen).days >= valid_days

    def is_callable(self, valid_days: int) -> bool:
        return (
            self.auto_gate_result == GATE_APPROVED
            and self.human_decision == GATE_APPROVED
            and bool(self.approved_by)
            and not self.screening_expired(valid_days)
        )


class SuppressionEntry(Base):
    """Internal do-not-contact list. Checked by both gates, owned by neither.

    An entry here beats every other signal in the system, permanently.
    """

    __tablename__ = "suppression"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String(320), index=True)
    kind: Mapped[str] = mapped_column(String(16))  # PHONE | EMAIL | DOMAIN | COMPANY_NO
    reason: Mapped[str | None] = mapped_column(Text)
    added_by: Mapped[str] = mapped_column(String(120))
    added_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("kind", "value", name="uq_suppression_kind_val"),)


class AuditLog(Base):
    """Append only. Every gate decision and every screening run lands here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id", ondelete="SET NULL"), index=True
    )
    actor: Mapped[str] = mapped_column(String(120))
    action: Mapped[str] = mapped_column(String(64))
    channel: Mapped[str | None] = mapped_column(String(16))  # EMAIL | CALL | None
    detail: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
