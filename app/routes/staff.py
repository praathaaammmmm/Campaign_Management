import logging
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import contains_eager, selectinload

from ..audit_service import audit
from ..auth import require_staff
from ..beneficiary_categories import SELECTABLE_CATEGORIES
from ..query_params import optional_int
from ..qr.service import find_coupon, record_detail_view, redeem_atomic, refresh_expiry, result_for
from ..models import AuditLog, Campaign, Patient, PatientOffer
from ..config import VALIDATION_RATE_LIMIT_ATTEMPTS, VALIDATION_RATE_LIMIT_WINDOW_SECONDS
from ..security import client_key, is_rate_limited, record_rate_limit_event, require_csrf
from ..registrations.service import (
    RecentDuplicateError, RegistrationError, latest_coupon_for, register_patient_offer,
    registration_form_options, registration_form_retry, resend_patient_delivery,
)
from ..staff_scope import assigned_campaign_ids, coupon_in_scope, staff_assigned_to
from ..time_utils import clinic_date_range_to_utc, utc_now

router = APIRouter()
logger = logging.getLogger(__name__)


def _context(request: Request, **values):
    return {"request": request, "workspace": "staff", **values}


NO_ASSIGNMENTS_MESSAGE = "No campaigns are assigned to you yet. Please ask an administrator to assign you to a campaign."
NO_USABLE_CAMPAIGN_MESSAGE = "None of your assigned campaigns is open for registration right now."
NOT_ASSIGNED_MESSAGE = "You are not assigned to this campaign."
INVALID_QR_MESSAGE = "This QR/token is not registered in the Smriti Raj Dentistry system."
PATIENT_NOT_FOUND_URL = "/staff/patients?message=Patient record not found"


class CampaignNotAssignedError(Exception):
    pass


def _active_campaigns(db, assigned: set[int]):
    """The staff member's assigned campaigns that can take a registration today."""
    if not assigned:
        return []
    today = utc_now().date()
    return db.query(Campaign).options(selectinload(Campaign.offers)).filter(
        Campaign.id.in_(assigned),
        Campaign.status == "ACTIVE", Campaign.start_date <= today, Campaign.end_date >= today,
    ).order_by(Campaign.start_date.desc()).all()


def _scope_notice(assigned: set[int], campaigns=None) -> str | None:
    if not assigned:
        return NO_ASSIGNMENTS_MESSAGE
    if campaigns is not None and not campaigns:
        return NO_USABLE_CAMPAIGN_MESSAGE
    return None


def _scoped_patient_coupon(db, request, patient_id):
    """(patient, coupon) when the patient's registration belongs to one of this staff
    member's campaigns, else (None, None) — a patient from another campaign is
    indistinguishable from one that does not exist.
    """
    patient = db.get(Patient, patient_id)
    coupon = coupon_in_scope(db, request, latest_coupon_for(patient)) if patient else None
    return (patient, coupon) if coupon else (None, None)


@router.get("/staff")
def staff_root():
    return RedirectResponse("/staff/home", status_code=303)


@router.get("/staff/home")
def staff_home(request: Request):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        assigned = assigned_campaign_ids(db, request)
        return request.app.state.templates.TemplateResponse(request, "staff_home.html", _context(
            request, campaigns=_active_campaigns(db, assigned), notice=_scope_notice(assigned),
            active_qrs=db.query(PatientOffer).filter(
                PatientOffer.status == "ACTIVE", PatientOffer.campaign_id.in_(assigned),
            ).count() if assigned else 0,
        ))
    finally:
        db.close()


@router.get("/staff/register")
def register_page(request: Request):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        assigned = assigned_campaign_ids(db, request)
        campaigns = _active_campaigns(db, assigned)
        return request.app.state.templates.TemplateResponse(request, "register.html", _context(
            request, **registration_form_options(db, campaigns=campaigns), error=None,
            notice=_scope_notice(assigned, campaigns),
            form_action="/staff/register", back_url="/staff/home",
            beneficiary_categories=SELECTABLE_CATEGORIES, selected_category="",
        ))
    finally:
        db.close()


@router.post("/staff/register")
def register_patient(
    request: Request, full_name: str = Form(...), mobile: str = Form(...), email: str = Form(""),
    age: str = Form(""), gender: str = Form(""), city: str = Form(""), doctor_name: str = Form(""),
    campaign_name: str = Form(""), campaign_id: int | None = Form(None), offer_id: int = Form(...),
    beneficiary_category: str = Form(""),
    consent_given: bool = Form(False), submission_token: str = Form(""),
    confirm_repeat: bool = Form(False), _csrf: None = Depends(require_csrf),
):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        duplicate = None
        try:
            # Checked (and share-locked on PostgreSQL) in the registration's own
            # transaction: the form only lists assigned campaigns, but a crafted or stale
            # POST must not register against any other.
            if not staff_assigned_to(db, request, campaign_id, lock=True):
                raise CampaignNotAssignedError
            coupon = register_patient_offer(
                db, full_name=full_name, mobile=mobile, email=email, age=age, gender=gender, city=city,
                doctor_name=doctor_name, campaign_name=campaign_name, campaign_id=campaign_id, offer_id=offer_id,
                beneficiary_category=beneficiary_category, consent_given=consent_given, actor=request.session["user"],
                submission_key=submission_token, confirm_repeat=confirm_repeat,
            )
            return RedirectResponse(f"/staff/patients/{coupon.patient_id}", status_code=303)
        except CampaignNotAssignedError:
            db.rollback()
            error, status_code = NOT_ASSIGNED_MESSAGE, 403
        except RecentDuplicateError as exc:
            duplicate, error, status_code = exc.recent, None, 409
        except RegistrationError as exc:
            error, status_code = str(exc), 422
        except Exception:
            logger.exception("Staff patient registration failed")
            db.rollback()
            error, status_code = "Registration could not be completed. Please try again.", 500
        submitted = {
            "full_name": full_name, "mobile": mobile, "email": email, "age": age, "gender": gender,
            "city": city, "doctor_name": doctor_name, "campaign_name": campaign_name,
            "campaign_id": campaign_id, "offer_id": offer_id, "consent_given": consent_given,
        }
        assigned = assigned_campaign_ids(db, request)
        options = registration_form_options(db, campaigns=_active_campaigns(db, assigned))
        options.update(registration_form_retry(submitted, submission_token, duplicate))
        return request.app.state.templates.TemplateResponse(request, "register.html", _context(
            request, **options, error=error,
            form_action="/staff/register", back_url="/staff/home", detail_prefix="/staff/patients",
            beneficiary_categories=SELECTABLE_CATEGORIES, selected_category=beneficiary_category,
        ), status_code=status_code)
    finally:
        db.close()


@router.get("/staff/patients")
def patients(
    request: Request, q: str = Query("", max_length=100), status: str = "", campaign_id: str = Query(""),
    start: str = "", end: str = "",
):
    guard = require_staff(request)
    if guard:
        return guard
    campaign_id = optional_int(campaign_id)
    db = request.app.state.db()
    try:
        assigned = assigned_campaign_ids(db, request)
        query = (
            db.query(PatientOffer).join(PatientOffer.patient).join(PatientOffer.offer)
            .options(contains_eager(PatientOffer.patient), contains_eager(PatientOffer.offer))
            .filter(PatientOffer.campaign_id.in_(assigned))
        )
        if q:
            query = query.filter(or_(Patient.full_name.ilike(f"%{q}%"), Patient.mobile.ilike(f"%{q}%")))
        if status:
            query = query.filter(PatientOffer.status == status)
        if campaign_id:
            query = query.filter(PatientOffer.campaign_id == campaign_id)
        try:
            start_date = date.fromisoformat(start) if start else None
            end_date = date.fromisoformat(end) if end else None
            start_utc, end_utc = clinic_date_range_to_utc(start_date, end_date)
            if start_utc:
                query = query.filter(PatientOffer.created_at >= start_utc)
            if end_utc:
                query = query.filter(PatientOffer.created_at < end_utc)
        except ValueError:
            return request.app.state.templates.TemplateResponse(request, "patients.html", _context(
                request, rows=[], offers=[], campaigns=[], q=q, status=status, campaign_id=campaign_id, start=start, end=end,
                error="Dates must use YYYY-MM-DD.", list_url="/staff/patients", register_url="/staff/register", detail_prefix="/staff/patients",
            ), status_code=422)
        return request.app.state.templates.TemplateResponse(request, "patients.html", _context(
            request, rows=query.order_by(PatientOffer.created_at.desc()).all() if assigned else [],
            offers=[], notice=_scope_notice(assigned),
            campaigns=db.query(Campaign).filter(Campaign.id.in_(assigned)).order_by(Campaign.name).all() if assigned else [],
            q=q, status=status, campaign_id=campaign_id, start=start, end=end,
            list_url="/staff/patients", register_url="/staff/register", detail_prefix="/staff/patients",
        ))
    finally:
        db.close()


@router.get("/staff/patients/{patient_id}")
def patient_detail(request: Request, patient_id: int):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        patient, coupon = _scoped_patient_coupon(db, request, patient_id)
        if not patient:
            return RedirectResponse(PATIENT_NOT_FOUND_URL, status_code=303)
        events = db.query(AuditLog).filter(AuditLog.patient_id == patient.id).order_by(AuditLog.timestamp.desc()).all()
        return request.app.state.templates.TemplateResponse(request, "patient_detail.html", _context(
            request, patient=patient, coupon=coupon, events=events, back_url="/staff/patients",
            validate_url="/staff/validate", delivery_prefix=f"/staff/patients/{patient.id}/delivery", can_cancel=False,
        ))
    finally:
        db.close()


@router.post("/staff/patients/{patient_id}/delivery/{channel}")
def resend_delivery(request: Request, patient_id: int, channel: str, _csrf: None = Depends(require_csrf)):
    guard = require_staff(request)
    if guard:
        return guard
    channel = channel.upper()
    db = request.app.state.db()
    try:
        if not _scoped_patient_coupon(db, request, patient_id)[0]:
            return RedirectResponse(PATIENT_NOT_FOUND_URL, status_code=303)
        if channel not in {"EMAIL", "WHATSAPP"}:
            return RedirectResponse(f"/staff/patients/{patient_id}?message=Delivery is unavailable", status_code=303)
        _ok, message = resend_patient_delivery(db, patient_id, channel, request.session["user"])
        if not _ok and message == "No active registration":
            message = "Delivery is unavailable"
        return RedirectResponse(f"/staff/patients/{patient_id}?message={message}", status_code=303)
    finally:
        db.close()


@router.get("/staff/validate")
def validate_page(request: Request):
    guard = require_staff(request)
    if guard:
        return guard
    return request.app.state.templates.TemplateResponse(request, "validate.html", _context(
        request, result=None, token="", form_action="/staff/validate", back_url="/staff/home",
    ))


@router.post("/staff/validate")
def validate_submit(request: Request, token: str = Form(...), _csrf: None = Depends(require_csrf)):
    guard = require_staff(request)
    if guard:
        return guard
    request_key = client_key(request)
    validation_window = timedelta(seconds=VALIDATION_RATE_LIMIT_WINDOW_SECONDS)
    if is_rate_limited("staff-validate", request_key, VALIDATION_RATE_LIMIT_ATTEMPTS, validation_window):
        return request.app.state.templates.TemplateResponse(request, "validate.html", _context(
            request, result=None, token="", form_action="/staff/validate", back_url="/staff/home",
            error="Too many validation attempts. Please wait a minute and try again.",
        ), status_code=429)
    record_rate_limit_event("staff-validate", request_key)
    db = request.app.state.db()
    try:
        coupon = find_coupon(db, token)
        # QR validation and redemption are deliberately NOT limited by campaign assignment:
        # any authenticated staff member may scan and redeem any valid QR, whichever
        # campaign it was registered under. Assignments scope registration, the patient
        # list/detail pages and the staff home figures only.
        if not coupon:
            result = {"kind": "INVALID", "message": INVALID_QR_MESSAGE}
            audit(db, request.session["user"], "QR_VALIDATED", details={"result": "INVALID"})
        else:
            if refresh_expiry(db, coupon, utc_now()):
                audit(db, request.session["user"], "QR_EXPIRED", coupon.id, coupon.patient_id)
            result = result_for(coupon)
            audit(db, request.session["user"], "QR_VALIDATED", coupon.id, coupon.patient_id, {"result": result["kind"]})
            record_detail_view(db, request, result)
        db.commit()
        return request.app.state.templates.TemplateResponse(request, "validate.html", _context(
            request, result=result, token="", form_action="/staff/validate", back_url="/staff/home",
            redeem_url=f"/staff/patients/{coupon.patient_id}/redeem" if coupon else None,
        ))
    finally:
        db.close()


@router.get("/staff/validate/result/{coupon_uid}")
def validation_result(request: Request, coupon_uid: str):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        coupon = db.execute(select(PatientOffer).where(PatientOffer.coupon_uid == coupon_uid)).scalar_one_or_none()
        if not coupon:
            return RedirectResponse("/staff/validate", status_code=303)
        refresh_expiry(db, coupon, utc_now())
        result = result_for(coupon)
        if result["kind"] == "REDEEMED" and request.session.get("just_redeemed") == coupon.coupon_uid:
            request.session.pop("just_redeemed", None)
            result = {"kind": "JUST_REDEEMED", "coupon": coupon}
        record_detail_view(db, request, result)
        db.commit()
        return request.app.state.templates.TemplateResponse(request, "validate.html", _context(
            request, result=result, token="", form_action="/staff/validate", back_url="/staff/home",
            redeem_url=f"/staff/patients/{coupon.patient_id}/redeem",
        ))
    finally:
        db.close()


@router.post("/staff/patients/{patient_id}/redeem")
def redeem(request: Request, patient_id: int, _csrf: None = Depends(require_csrf)):
    guard = require_staff(request)
    if guard:
        return guard
    db = request.app.state.db()
    try:
        # Not campaign-scoped (see validate_submit): redemption is open to all staff.
        patient = db.get(Patient, patient_id)
        coupon = latest_coupon_for(patient) if patient else None
        if not coupon:
            return RedirectResponse("/staff/validate", status_code=303)
        if redeem_atomic(db, coupon.id, request.session["user"], utc_now()):
            # One-time flash: only the very next load of this coupon's result page shows
            # the "just redeemed" success receipt; later visits show the plain "already
            # used" state once this has been popped.
            request.session["just_redeemed"] = coupon.coupon_uid
        return RedirectResponse(f"/staff/validate/result/{coupon.coupon_uid}", status_code=303)
    finally:
        db.close()
