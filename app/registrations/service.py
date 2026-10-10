"""Business workflow for legacy PatientOffer registration creation."""
import logging
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit_service import audit
from ..models import AuditLog, Campaign, Offer, Patient, PatientOffer
from ..qr.service import expiry_for, new_uid, token_for, token_hash
from ..notifications.service import queue_registration_deliveries, send_qr_delivery
from ..schemas import PatientCreate
from ..time_utils import utc_now

logger = logging.getLogger(__name__)

CONSENT_VERSION = "2026-09-02"
RECENT_DUPLICATE_WINDOW = timedelta(minutes=5)
# A fresh random ID colliding with an existing one is rare but real (coupon IDs are 8 hex
# characters); each attempt draws new IDs, so a few tries is ample.
MAX_CREATE_ATTEMPTS = 3
SUBMISSION_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{16,64}")


class RegistrationError(Exception):
    """A safe, user-actionable registration failure."""


class CampaignUnavailableError(RegistrationError):
    pass


class OfferUnavailableError(RegistrationError):
    pass


@dataclass(frozen=True)
class RecentRegistration:
    patient_id: int
    coupon_uid: str
    created_at: datetime
    issued_by: str | None


class RecentDuplicateError(RegistrationError):
    """The same mobile, campaign and service were registered moments ago. Nothing was
    created; the caller must ask for a deliberate confirmation before issuing another QR.
    """

    def __init__(self, recent: RecentRegistration):
        super().__init__(
            "A QR for this mobile number, campaign and service was issued a few minutes ago."
        )
        self.recent = recent


def sunday_for(moment: datetime):
    return (moment - timedelta(days=(moment.weekday() + 1) % 7)).date()


def new_submission_token() -> str:
    """One per rendered registration form; becomes the registration's submission key."""
    return secrets.token_urlsafe(24)


def _validated_form(**values) -> PatientCreate:
    try:
        return PatientCreate(**values)
    except ValidationError as exc:
        first_error = exc.errors(include_url=False)[0]
        field = str(first_error.get("loc", ["field"])[-1]).replace("_", " ").title()
        raise RegistrationError(f"{field}: {first_error['msg']}") from None


def _registration_for_key(db: Session, submission_key: str | None) -> PatientOffer | None:
    if not submission_key:
        return None
    return db.execute(
        select(PatientOffer).where(PatientOffer.submission_key == submission_key)
    ).scalar_one_or_none()


def _lock_registration_slot(db: Session, mobile: str, campaign_id: int, offer_id: int) -> None:
    """Serialize submissions for one mobile + campaign + service until this transaction
    ends, so two concurrent submissions cannot both pass the recent-duplicate check.
    Registrations for any other patient, campaign or service take a different lock and
    never wait. SQLite (development/tests) has no advisory locks.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:slot, 0))"),
        {"slot": f"registration:{mobile}:{campaign_id}:{offer_id}"},
    )


def _recent_registration(db: Session, mobile: str, campaign_id: int, offer_id: int, now: datetime) -> RecentRegistration | None:
    coupon = db.execute(
        select(PatientOffer).join(Patient)
        .where(
            Patient.mobile == mobile,
            PatientOffer.campaign_id == campaign_id,
            PatientOffer.offer_id == offer_id,
            PatientOffer.status != "CANCELLED",
            PatientOffer.created_at >= now - RECENT_DUPLICATE_WINDOW,
        )
        .order_by(PatientOffer.created_at.desc(), PatientOffer.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not coupon:
        return None
    issued_by = db.execute(
        select(AuditLog.user)
        .where(AuditLog.coupon_id == coupon.id, AuditLog.action == "PATIENT_REGISTERED")
        .order_by(AuditLog.id).limit(1)
    ).scalar_one_or_none()
    return RecentRegistration(
        patient_id=coupon.patient_id, coupon_uid=coupon.coupon_uid,
        created_at=coupon.created_at, issued_by=issued_by,
    )


def register_patient_offer(
    db: Session,
    *,
    full_name: str,
    mobile: str,
    email: str,
    age: str,
    gender: str,
    city: str,
    doctor_name: str,
    campaign_name: str,
    campaign_id: int | None,
    offer_id: int,
    beneficiary_category: str,
    consent_given: bool,
    actor: str,
    submission_key: str | None = None,
    confirm_repeat: bool = False,
) -> PatientOffer:
    """Create a patient and its legacy PatientOffer in one transaction.

    Every call that creates anything creates a brand-new, independent Patient +
    PatientOffer (own QR identity, expiry, delivery intents and audit trail); a mobile
    number may be registered any number of times. Two guards keep that from happening by
    accident:

    * ``submission_key`` makes one form submission idempotent. A replay (double-click,
      refresh, network retry) returns the registration that submission already created
      and writes nothing.
    * The same mobile + campaign + service within ``RECENT_DUPLICATE_WINDOW`` raises
      ``RecentDuplicateError`` unless ``confirm_repeat`` is set.

    Notification delivery is queued durably (PREPARED DeliveryLog rows) in the same
    transaction; actual sending happens later via the delivery dispatcher.
    """
    form = _validated_form(
        full_name=full_name,
        mobile=mobile,
        email=email or None,
        age=age or None,
        gender=gender or None,
        city=city or None,
        doctor_name=doctor_name or None,
        campaign_name=campaign_name or None,
        offer_id=offer_id,
        beneficiary_category=beneficiary_category,
        consent_given=consent_given,
    )
    submission_key = (submission_key or "").strip() or None
    if submission_key and not SUBMISSION_TOKEN_PATTERN.fullmatch(submission_key):
        raise RegistrationError("This form has expired. Please review the details and submit again.")

    for _attempt in range(MAX_CREATE_ATTEMPTS):
        try:
            return _create_registration(
                db, form, campaign_id=campaign_id, actor=actor,
                submission_key=submission_key, confirm_repeat=confirm_repeat,
            )
        except IntegrityError:
            db.rollback()
            # Either a concurrent request with the same submission key won the race (its
            # registration is the answer), or a random ID collided (draw new ones).
            existing = _registration_for_key(db, submission_key)
            if existing:
                return existing
        except Exception:
            db.rollback()
            raise
    logger.error("Registration abandoned after %s integrity conflicts", MAX_CREATE_ATTEMPTS)
    raise RegistrationError("Registration could not be completed. Please try again.")


def _create_registration(
    db: Session, form: PatientCreate, *, campaign_id: int | None, actor: str,
    submission_key: str | None, confirm_repeat: bool,
) -> PatientOffer:
    existing = _registration_for_key(db, submission_key)
    if existing:
        return existing

    offer = db.get(Offer, form.offer_id)
    if not offer:
        raise OfferUnavailableError("Please select a valid offer.")
    if not form.consent_given:
        raise RegistrationError("Patient consent is required before registration.")

    now = utc_now()
    campaign = db.get(Campaign, campaign_id) if campaign_id else None
    if not campaign or campaign.status != "ACTIVE":
        raise CampaignUnavailableError("Please select an active campaign.")
    if not (campaign.start_date <= now.date() <= campaign.end_date):
        raise CampaignUnavailableError("The selected campaign is not active for today's date.")
    if offer.id not in {item.id for item in campaign.offers}:
        raise OfferUnavailableError("The selected service is not part of this campaign.")

    _lock_registration_slot(db, form.mobile, campaign.id, offer.id)
    # Re-check under the lock: a same-key request may have committed while this one waited.
    existing = _registration_for_key(db, submission_key)
    if existing:
        return existing
    now = utc_now()
    recent = _recent_registration(db, form.mobile, campaign.id, offer.id, now)
    if recent and not confirm_repeat:
        # Without an advisory lock (SQLite), the "recent" registration may be this very
        # submission, committed by a concurrent replay a moment ago.
        existing = _registration_for_key(db, submission_key)
        if existing:
            return existing
        raise RecentDuplicateError(recent)

    registration_week = sunday_for(now)
    patient = Patient(
        patient_uid=new_uid("PAT"), full_name=form.full_name, mobile=form.mobile,
        email=str(form.email) if form.email else None, age=form.age, gender=form.gender,
        city=form.city, doctor_name=form.doctor_name, campaign_name=form.campaign_name,
        registration_week=registration_week, consent_given=True,
        consent_version=CONSENT_VERSION, consented_at=now, created_at=now,
    )
    db.add(patient)
    db.flush()

    coupon_uid = new_uid("SRD")
    coupon = PatientOffer(
        coupon_uid=coupon_uid, patient_id=patient.id, offer_id=offer.id,
        campaign_id=campaign.id, secure_token_hash=token_hash(token_for(coupon_uid)),
        created_at=now, expires_at=expiry_for(now), status="ACTIVE",
        beneficiary_category=form.beneficiary_category, submission_key=submission_key,
    )
    db.add(coupon)
    db.flush()
    details = {
        "offer": offer.name, "registration_week": str(registration_week),
        "consent_version": CONSENT_VERSION, "beneficiary_category": form.beneficiary_category,
    }
    if recent:
        details.update({"repeat_confirmed": True, "previous_coupon_uid": recent.coupon_uid})
    audit(db, actor, "PATIENT_REGISTERED", coupon.id, patient.id, details)
    # No image is written: the QR is derived on demand from the coupon ID and signing key.
    audit(db, "admin", "QR_GENERATED", coupon.id, patient.id)
    # Durable outbox write only — no network call happens here, so a slow or unavailable
    # n8n endpoint can never block or roll back this registration. The dispatcher
    # (app/delivery_job.py) sends these PREPARED intents separately.
    queue_registration_deliveries(db, patient, coupon, actor)
    db.commit()
    return coupon


def latest_coupon_for(patient: Patient) -> PatientOffer | None:
    """Each registration creates its own Patient row, so a patient row has one
    registration in practice, but the relationship is one-to-many — this is the single
    shared rule for "which one is *the* registration to show/act on", used identically
    by both the admin and staff patient-detail/cancel/delivery/redeem routes so it can
    never drift between the two.
    """
    return max(patient.offers, key=lambda item: item.created_at) if patient.offers else None


def registration_form_options(db: Session, campaigns: list[Campaign] | None = None) -> dict:
    """Shared offer/campaign choices for the registration form, used identically by the
    admin and staff register pages (both the initial GET and a failed-submission
    re-render).

    ``campaigns`` lets a caller substitute its own campaign list — the staff register
    page deliberately shows only campaigns ACTIVE *and* within their date range (see
    ``routes/staff.py``'s ``_active_campaigns``), while the admin register page shows
    every ACTIVE campaign regardless of date. That is a real, pre-existing difference in
    what each role is allowed to register against, not incidental duplication, so it is
    preserved rather than unified.
    """
    return {
        "form": {}, "duplicate": None, "submission_token": new_submission_token(),
        "offers": db.query(Offer).filter(Offer.active == True).order_by(Offer.id).all(),
        "campaigns": campaigns if campaigns is not None else (
            db.query(Campaign).filter(Campaign.status == "ACTIVE").order_by(Campaign.start_date.desc()).all()
        ),
    }


def registration_form_retry(submitted: dict, submission_token: str, duplicate: RecentRegistration | None = None) -> dict:
    """Template values for re-rendering the form after a rejected submission: the entered
    values are kept, and so is the submission token (nothing was created with it), so the
    corrected or confirmed resubmission is still one idempotent submission.
    """
    state = {"form": submitted, "duplicate": duplicate}
    if SUBMISSION_TOKEN_PATTERN.fullmatch(submission_token or ""):
        state["submission_token"] = submission_token
    return state


def resend_patient_delivery(db: Session, patient_id: int, channel: str, actor: str) -> tuple[bool, str]:
    """Shared "resend this patient's QR over one channel" flow used by both the admin
    (two single-channel routes) and staff (one channel-parameterized route) delivery
    endpoints. Returns (ok, message) — message is safe to show the user either way.
    """
    patient = db.get(Patient, patient_id)
    coupon = latest_coupon_for(patient) if patient else None
    if not coupon:
        return False, "No active registration"
    try:
        result = send_qr_delivery(db, patient, coupon, channel, actor)
    except ValueError as exc:
        return False, str(exc)
    channel_label = {"EMAIL": "Email", "WHATSAPP": "WhatsApp"}.get(channel, channel.title())
    return True, f"{channel_label} delivery {result['status'].lower()}"
