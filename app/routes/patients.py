import logging
from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import contains_eager, joinedload
from ..auth import require_admin
from ..audit_service import audit
from ..beneficiary_categories import SELECTABLE_CATEGORIES
from ..models import Patient, PatientOffer, Offer, AuditLog
from ..reporting.filters import ReportFilters, coupon_conditions, filter_choices, filter_summary
from ..security import require_csrf
from ..time_utils import utc_now
from ..registrations.service import (
    RecentDuplicateError, RegistrationError, latest_coupon_for, register_patient_offer,
    registration_form_options, registration_form_retry, resend_patient_delivery,
)

router = APIRouter()
logger = logging.getLogger(__name__)


PATIENT_FILTERS = ("campaign_id", "offer_id", "status", "q", "dates")


@router.get("/patients")
def patients(request: Request):
    guard = require_admin(request)
    if guard: return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        rows = (
            db.query(PatientOffer).join(PatientOffer.patient).join(PatientOffer.offer)
            .options(contains_eager(PatientOffer.patient), contains_eager(PatientOffer.offer), joinedload(PatientOffer.campaign))
            .filter(*coupon_conditions(filters, *PATIENT_FILTERS))
            .order_by(PatientOffer.created_at.desc(), PatientOffer.id.desc()).all()
        )
        export_query = filters.query("campaign_id", "offer_id", "status", "q", "start", "end")
        return request.app.state.templates.TemplateResponse(request, "patients.html", {
            "request": request, "rows": rows, **filter_choices(db), "q": filters.q, "status": filters.status,
            "offer_id": filters.offer_id, "campaign_id": filters.campaign_id, "start": filters.start, "end": filters.end,
            "filters": filters, "summary": filter_summary(db, filters, *PATIENT_FILTERS),
            "exports": [{"label": "Download CSV", "url": f"/admin/reports/patients.csv{'?' + export_query if export_query else ''}"}],
        })
    finally:
        db.close()

@router.get("/patients/register")
def register_page(request: Request):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        return request.app.state.templates.TemplateResponse(request, "register.html", {
            "request": request, "error": None, **registration_form_options(db),
            "beneficiary_categories": SELECTABLE_CATEGORIES, "selected_category": "",
        })
    finally:
        db.close()

@router.post("/patients/register")
def register_patient(
    request: Request,
    full_name: str = Form(...),
    mobile: str = Form(...),
    email: str = Form(""),
    age: str = Form(""),
    gender: str = Form(""),
    city: str = Form(""),
    doctor_name: str = Form(""),
    campaign_name: str = Form(""),
    campaign_id: int | None = Form(None),
    offer_id: int = Form(...),
    beneficiary_category: str = Form(""),
    consent_given: bool = Form(False),
    submission_token: str = Form(""),
    confirm_repeat: bool = Form(False),
    _csrf: None = Depends(require_csrf),
):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        duplicate = None
        try:
            coupon = register_patient_offer(
                db, full_name=full_name, mobile=mobile, email=email, age=age, gender=gender,
                city=city, doctor_name=doctor_name, campaign_name=campaign_name,
                campaign_id=campaign_id, offer_id=offer_id, beneficiary_category=beneficiary_category,
                consent_given=consent_given, actor=request.session.get("user", "admin"),
                submission_key=submission_token, confirm_repeat=confirm_repeat,
            )
            return RedirectResponse(f"/patients/{coupon.patient_id}", status_code=303)
        except RecentDuplicateError as exc:
            duplicate, error, status_code = exc.recent, None, 409
        except RegistrationError as exc:
            error, status_code = str(exc), 422
        except Exception:
            logger.exception("Patient registration failed")
            db.rollback()
            error, status_code = "Registration could not be completed. Please try again.", 500
        submitted = {
            "full_name": full_name, "mobile": mobile, "email": email, "age": age, "gender": gender,
            "city": city, "doctor_name": doctor_name, "campaign_name": campaign_name,
            "campaign_id": campaign_id, "offer_id": offer_id, "consent_given": consent_given,
        }
        return request.app.state.templates.TemplateResponse(request, "register.html", {
            "request": request, "error": error, **registration_form_options(db),
            **registration_form_retry(submitted, submission_token, duplicate),
            "beneficiary_categories": SELECTABLE_CATEGORIES, "selected_category": beneficiary_category,
        }, status_code=status_code)
    finally:
        db.close()


@router.get("/patients/{patient_id}")
def patient_detail(request: Request, patient_id: int):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        patient = db.get(Patient, patient_id)
        if not patient:
            return RedirectResponse("/patients", status_code=303)
        coupon = latest_coupon_for(patient)
        events = db.query(AuditLog).filter(AuditLog.patient_id == patient.id).order_by(AuditLog.timestamp.desc()).all()
        return request.app.state.templates.TemplateResponse(request, "patient_detail.html", {"request": request, "patient": patient, "coupon": coupon, "events": events})
    finally:
        db.close()

@router.post("/patients/{patient_id}/cancel")
def cancel_coupon(
    request: Request,
    patient_id: int,
    reason: str = Form(..., min_length=3, max_length=255),
    _csrf: None = Depends(require_csrf),
):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        patient = db.get(Patient, patient_id)
        coupon = latest_coupon_for(patient) if patient else None
        if not coupon:
            return RedirectResponse("/patients", status_code=303)
        if coupon.status != "ACTIVE":
            return RedirectResponse(f"/patients/{patient_id}?message=Only active offers can be cancelled", status_code=303)
        now = utc_now()
        coupon.status = "CANCELLED"
        coupon.cancelled_at = now
        coupon.cancelled_by = request.session.get("user", "admin")
        coupon.cancellation_reason = " ".join(reason.split())
        audit(db, coupon.cancelled_by, "QR_CANCELLED", coupon.id, patient.id, {"reason": coupon.cancellation_reason})
        db.commit()
        return RedirectResponse(f"/patients/{patient_id}?message=Offer cancelled", status_code=303)
    finally:
        db.close()

@router.post("/patients/{patient_id}/delivery/email")
def send_email(request: Request, patient_id: int, _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        _ok, message = resend_patient_delivery(db, patient_id, "EMAIL", request.session.get("user", "admin"))
        return RedirectResponse(f"/patients/{patient_id}?message={message}", status_code=303)
    finally:
        db.close()

@router.post("/patients/{patient_id}/delivery/whatsapp")
def whatsapp(request: Request, patient_id: int, _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        _ok, message = resend_patient_delivery(db, patient_id, "WHATSAPP", request.session.get("user", "admin"))
        return RedirectResponse(f"/patients/{patient_id}?message={message}", status_code=303)
    finally:
        db.close()
