import csv
from io import StringIO
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from ..auth import require_admin
from .filters import DATE_FIELDS, ReportFilters, coupon_conditions, export_filename, filter_choices, filter_summary
from .service import (
    campaign_performance, daily_time_series, dashboard_summary, default_chart_mode,
    delivery_summary, patient_export_rows, registrations_by_category, staff_performance,
)

router = APIRouter(prefix="/admin")

DASHBOARD_FILTERS = ("campaign_id", "offer_id", "dates")
PATIENT_EXPORT_FILTERS = ("campaign_id", "offer_id", "status", "q", "dates")


FORMULA_PREFIXES = ("=", "+", "-", "@")


def csv_safe(value):
    """Neutralize spreadsheet formula injection. A text cell whose first non-whitespace
    character is ``=``, ``+``, ``-`` or ``@`` would be evaluated as a formula by Excel,
    LibreOffice or Google Sheets, so it is prefixed with a single apostrophe, which those
    programs treat as "this is text" and do not display. The value itself is otherwise
    unchanged. Only strings are touched: numbers, dates and blanks are written as they are.
    """
    if isinstance(value, str) and value.lstrip().startswith(FORMULA_PREFIXES):
        return "'" + value
    return value


def _write_row(writer, values) -> None:
    """The only way a row reaches an export: every cell goes through ``csv_safe``."""
    writer.writerow([csv_safe(value) for value in values])


def _csv_response(output: StringIO, filename: str) -> StreamingResponse:
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/reports/campaigns.csv")
def campaign_report(request: Request):
    """Campaign performance, honouring the dashboard's campaign, service and date filters."""
    guard = require_admin(request)
    if guard: return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        output = StringIO()
        writer = csv.writer(output)
        _write_row(writer, ["Campaign", "Start date", "End date", "Status", "Issued", "Redeemed", "Expired", "Conversion rate"])
        for campaign in campaign_performance(db, filters.campaign_id, filters.offer_id, filters.start_date, filters.end_date):
            issued, redeemed = campaign["issued"], campaign["redeemed"]
            _write_row(writer, [campaign["name"], campaign["start_date"], campaign["end_date"], campaign["status"], issued, redeemed, campaign["expired"], f"{(redeemed * 100 / issued) if issued else 0:.1f}%"])
        selected = filter_summary(db, filters, "campaign_id")["campaign"]
        return _csv_response(output, export_filename("campaign-performance", selected))
    finally:
        db.close()


@router.get("/dashboard")
def dashboard(request: Request):
    guard = require_admin(request)
    if guard: return guard
    filters = ReportFilters.from_request(request)
    campaign_id, offer_id, start_date, end_date = filters.campaign_id, filters.offer_id, filters.start_date, filters.end_date
    db = request.app.state.db()
    try:
        summary = dashboard_summary(db, campaign_id, offer_id, start_date, end_date)
        issued, redeemed, expired = summary["issued"], summary["redeemed"], summary["expired"]
        conversion = round(redeemed * 100 / issued, 1) if issued else 0
        deliveries = delivery_summary(db, campaign_id, offer_id, start_date, end_date)
        time_series = daily_time_series(db, campaign_id, offer_id, start_date, end_date)
        report_query = filters.query("campaign_id", "offer_id", "start", "end")
        suffix = f"?{report_query}" if report_query else ""
        return request.app.state.templates.TemplateResponse(request, "admin_dashboard.html", {
            "request": request, "issued": issued, "redeemed": redeemed, "expired": expired,
            "conversion": conversion, **filter_choices(db),
            "campaign_id": campaign_id, "offer_id": offer_id, "start": filters.start, "end": filters.end,
            "filters": filters, "filter_fields": DASHBOARD_FILTERS, "reset_url": "/admin/dashboard",
            "summary": filter_summary(db, filters, *DASHBOARD_FILTERS, date_label="Dates"), "date_label": "Dates",
            "email_rate": round(deliveries["email_sent"] * 100 / deliveries["email_total"], 1) if deliveries["email_total"] else 0,
            "whatsapp_rate": round(deliveries["whatsapp_sent"] * 100 / deliveries["whatsapp_total"], 1) if deliveries["whatsapp_total"] else 0,
            "time_series": time_series,
            "default_chart_mode": default_chart_mode(time_series),
            "staff_performance": staff_performance(db, start_date, end_date, campaign_id, offer_id),
            "category_breakdown": registrations_by_category(db, campaign_id, offer_id, start_date, end_date),
            "report_query": report_query,
            "drilldowns": [
                {"label": "View patients", "url": f"/patients{suffix}"},
                {"label": "View issued coupons", "url": f"/offers{suffix}"},
                {"label": "View redemptions", "url": f"/redemptions{'?' + filters.query('campaign_id', 'offer_id') if filters.any('campaign_id', 'offer_id') else ''}"},
                {"label": "View delivery activity", "url": f"/delivery{'?' + filters.query('campaign_id', 'offer_id') if filters.any('campaign_id', 'offer_id') else ''}"},
            ],
        })
    finally: db.close()


@router.get("/reports/patients.csv")
def patient_report(request: Request):
    """Patient registrations, honouring campaign, service, status, search and date
    filters — the same row set as the patients and issued-coupons pages. With
    ``date_field=redeemed`` the dates apply to the redemption time instead, which is how
    the redemptions page exports exactly what it shows.
    """
    guard = require_admin(request)
    if guard:
        return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        conditions = coupon_conditions(filters, *PATIENT_EXPORT_FILTERS, date_column=DATE_FIELDS[filters.date_field])
        output = StringIO()
        writer = csv.writer(output)
        _write_row(writer, ["Patient ID", "Patient", "Mobile", "Email", "Age", "Gender", "Campaign", "Service", "Beneficiary Category", "Registration ID", "Registered", "Expires", "Status", "Redeemed", "Redeemed by"])
        for row in patient_export_rows(db, conditions=conditions):
            _write_row(writer, [row["patient_uid"], row["full_name"], row["mobile"], row["email"], row["age"], row["gender"], row["campaign"], row["service"], row["beneficiary_category"], row["coupon_uid"], row["created_at"], row["expires_at"], row["status"], row["redeemed_at"], row["redeemed_by"]])
        selected = filter_summary(db, filters, "campaign_id")["campaign"]
        return _csv_response(output, export_filename("patient-registrations", selected))
    finally:
        db.close()
