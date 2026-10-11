from fastapi import APIRouter, Request
from sqlalchemy.orm import contains_eager, joinedload
from ..auth import require_admin
from ..models import PatientOffer
from ..reporting.filters import ReportFilters, coupon_conditions, filter_choices, filter_summary

router = APIRouter()

COUPON_FILTERS = ("campaign_id", "offer_id", "status", "q", "dates")
REDEMPTION_FILTERS = ("campaign_id", "offer_id", "q", "dates")


def _coupon_rows(db, conditions, order_by):
    return (
        db.query(PatientOffer).join(PatientOffer.patient).join(PatientOffer.offer)
        .options(contains_eager(PatientOffer.patient), contains_eager(PatientOffer.offer), joinedload(PatientOffer.campaign))
        .filter(*conditions).order_by(*order_by).all()
    )


@router.get("/offers")
def offers(request: Request):
    guard = require_admin(request)
    if guard: return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        rows = _coupon_rows(db, coupon_conditions(filters, *COUPON_FILTERS), (PatientOffer.created_at.desc(), PatientOffer.id.desc()))
        export_query = filters.query("campaign_id", "offer_id", "status", "q", "start", "end")
        return request.app.state.templates.TemplateResponse(request, "offers.html", {
            "request": request, "rows": rows, **filter_choices(db), "filters": filters,
            "filter_fields": COUPON_FILTERS, "reset_url": "/offers", "date_label": "Issued",
            "summary": filter_summary(db, filters, *COUPON_FILTERS, date_label="Issued"),
            "exports": [{"label": "Download CSV", "url": f"/admin/reports/patients.csv{'?' + export_query if export_query else ''}"}],
        })
    finally:
        db.close()


@router.get("/redemptions")
def redemptions(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        # Dates on this page mean "redeemed on", not "registered on".
        conditions = [PatientOffer.status == "REDEEMED", *coupon_conditions(filters, *REDEMPTION_FILTERS, date_column=PatientOffer.redeemed_at)]
        rows = _coupon_rows(db, conditions, (PatientOffer.redeemed_at.desc(), PatientOffer.id.desc()))
        export_query = filters.query("campaign_id", "offer_id", "q", "start", "end", status="REDEEMED", date_field="redeemed")
        return request.app.state.templates.TemplateResponse(request, "redemptions.html", {
            "request": request, "rows": rows, **filter_choices(db), "filters": filters,
            "filter_fields": REDEMPTION_FILTERS, "reset_url": "/redemptions", "date_label": "Redeemed",
            "summary": filter_summary(db, filters, *REDEMPTION_FILTERS, date_label="Redeemed"),
            "exports": [{"label": "Download CSV", "url": f"/admin/reports/patients.csv?{export_query}"}],
        })
    finally:
        db.close()
