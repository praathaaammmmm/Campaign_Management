from datetime import date, datetime
from sqlalchemy import Boolean, CheckConstraint, Date, String, Integer, DateTime, ForeignKey, Text, Index, UniqueConstraint, Table, Column
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .beneficiary_categories import ALL_VALUES
from .database import Base
from .time_utils import utc_now

class Patient(Base):
    __tablename__ = "patients"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    patient_uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(150))
    mobile: Mapped[str] = mapped_column(String(30), index=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(30), nullable=True)
    city: Mapped[str | None] = mapped_column(String(100), nullable=True)
    doctor_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    campaign_name: Mapped[str | None] = mapped_column(String(150), nullable=True)
    registration_week: Mapped[date] = mapped_column(Date, index=True)
    consent_given: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_version: Mapped[str] = mapped_column(String(20))
    consented_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)

    offers = relationship("PatientOffer", back_populates="patient", cascade="all, delete-orphan")

class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="staff", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    campaigns = relationship("Campaign", secondary="staff_campaigns", order_by="Campaign.name")

class Offer(Base):
    __tablename__ = "offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    description: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)

    patient_offers = relationship("PatientOffer", back_populates="offer")
    campaigns = relationship("Campaign", secondary="campaign_offers", back_populates="offers")

campaign_offers = Table(
    "campaign_offers", Base.metadata,
    Column("campaign_id", ForeignKey("campaigns.id"), primary_key=True),
    Column("offer_id", ForeignKey("offers.id"), primary_key=True),
)

# Which campaigns a staff member may work with. No row means no access: a staff account
# with no assignments can register, see and redeem nothing. Admins are never restricted.
staff_campaigns = Table(
    "staff_campaigns", Base.metadata,
    Column("staff_user_id", ForeignKey("staff_users.id"), primary_key=True),
    Column("campaign_id", ForeignKey("campaigns.id"), primary_key=True),
    Index("ix_staff_campaigns_campaign_id", "campaign_id"),
)

class Campaign(Base):
    __tablename__ = "campaigns"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(150), unique=True)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="DRAFT", index=True)
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    offers = relationship("Offer", secondary=campaign_offers, back_populates="campaigns")
    registrations = relationship("PatientOffer", back_populates="campaign")

class PatientOffer(Base):
    __tablename__ = "patient_offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coupon_uid: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("offers.id"), index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True, index=True)
    secure_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(20), default="ACTIVE", index=True)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    redeemed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    beneficiary_category: Mapped[str] = mapped_column(String(20), index=True)
    submission_key: Mapped[str | None] = mapped_column(String(64), nullable=True)

    patient = relationship("Patient", back_populates="offers")
    offer = relationship("Offer", back_populates="patient_offers")
    campaign = relationship("Campaign", back_populates="registrations")

    __table_args__ = (
        Index("ix_patient_offers_status_expiry", "status", "expires_at"),
        Index("ux_patient_offers_submission_key", "submission_key", unique=True),
        CheckConstraint(
            "beneficiary_category IN (" + ", ".join(f"'{value}'" for value in sorted(ALL_VALUES)) + ")",
            name="ck_patient_offers_beneficiary_category",
        ),
    )

class DeliveryLog(Base):
    __tablename__ = "delivery_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    coupon_id: Mapped[int] = mapped_column(ForeignKey("patient_offers.id"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    recipient: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    n8n_workflow_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    provider_message_id: Mapped[str | None] = mapped_column(String(150), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now)
    delivery_intent_key: Mapped[str] = mapped_column(String(40), index=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    attempt_number: Mapped[int] = mapped_column(Integer, default=1)
    retryable: Mapped[bool] = mapped_column(Boolean, default=True)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("delivery_intent_key", "attempt_number", name="uq_delivery_logs_intent_attempt"),
        Index("ux_delivery_logs_idempotency_key", "idempotency_key", unique=True),
        CheckConstraint(
            "status IN ('PREPARED','SENDING','SENT','DELIVERED','FAILED')",
            name="ck_delivery_logs_status",
        ),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    user: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    coupon_id: Mapped[int | None] = mapped_column(ForeignKey("patient_offers.id"), nullable=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey("patients.id"), nullable=True)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
