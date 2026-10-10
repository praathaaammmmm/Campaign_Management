from datetime import timedelta

from sqlalchemy import inspect

from app.database import SessionLocal, engine
from app.models import PatientOffer
from app.reporting.service import registrations_by_category
from app.time_utils import utc_now
from tests.test_staff_interface import _login_as_staff


def _register(client, name, mobile, category="CGHS", path="/patients/register"):
    return client.post(path, data={
        "full_name": name, "mobile": mobile, "campaign_id": "1", "offer_id": "1",
        "beneficiary_category": category, "consent_given": "true",
    }, follow_redirects=False)


def test_beneficiary_category_column_exists_and_is_required(client):
    # Dialect-neutral (SQLite and PostgreSQL) replacement for a raw PRAGMA table_info check.
    columns = {column["name"]: column for column in inspect(engine).get_columns("patient_offers")}
    assert "beneficiary_category" in columns
    assert columns["beneficiary_category"]["nullable"] is False


def test_valid_category_persists_through_staff_registration(client):
    _login_as_staff(client)
    response = client.post("/staff/register", data={
        "full_name": "Category Patient", "mobile": "9070000001", "campaign_id": "1",
        "offer_id": "1", "beneficiary_category": "CAPF", "consent_given": "true",
    }, follow_redirects=False)
    assert response.status_code == 303
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).one()
        assert coupon.beneficiary_category == "CAPF"
    finally:
        db.close()


def test_missing_category_is_rejected_with_friendly_error(client):
    response = client.post("/patients/register", data={
        "full_name": "No Category", "mobile": "9070000002", "campaign_id": "1",
        "offer_id": "1", "consent_given": "true",
    })
    assert response.status_code == 422
    assert "beneficiary category" in response.text.lower()


def test_selected_category_is_preserved_when_another_field_fails_validation(client):
    response = client.post("/patients/register", data={
        "full_name": "Bad Mobile", "mobile": "12", "campaign_id": "1",
        "offer_id": "1", "beneficiary_category": "NHAI", "consent_given": "true",
    })
    assert response.status_code == 422
    assert 'value="NHAI" selected' in response.text


def test_invalid_category_value_is_rejected(client):
    response = client.post("/patients/register", data={
        "full_name": "Bad Category", "mobile": "9070000003", "campaign_id": "1",
        "offer_id": "1", "beneficiary_category": "UNSPECIFIED", "consent_given": "true",
    })
    assert response.status_code == 422
    assert "beneficiary category" in response.text.lower()


def test_unspecified_is_never_offered_as_selectable_option(client):
    page = client.get("/patients/register")
    assert 'value="UNSPECIFIED"' not in page.text
    staff_page_source = client.get("/staff/register")
    assert 'value="UNSPECIFIED"' not in staff_page_source.text or staff_page_source.status_code == 303


def test_admin_registration_persists_category(client):
    response = _register(client, "Admin Path Patient", "9070000004", category="NHAI")
    assert response.status_code == 303
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).filter_by(beneficiary_category="NHAI").one()
        assert coupon.patient.full_name == "Admin Path Patient"
    finally:
        db.close()


def test_historical_unspecified_rows_are_preserved_and_counted(client):
    _register(client, "Modern Patient", "9070000005", category="DU")
    db = SessionLocal()
    try:
        legacy = db.query(PatientOffer).one()
        legacy.beneficiary_category = "UNSPECIFIED"
        db.commit()

        breakdown = {row["value"]: row["count"] for row in registrations_by_category(db)}
        assert breakdown["UNSPECIFIED"] == 1
        assert breakdown["DU"] == 0
        assert breakdown["NOT_APPLICABLE"] == 0
        assert set(breakdown) == {"CGHS", "ECHS", "CAPF", "CISF", "DU", "NHAI", "NOT_APPLICABLE", "UNSPECIFIED"}
    finally:
        db.close()


def test_category_analytics_respects_existing_filters(client):
    _register(client, "Filtered One", "9070000006", category="CGHS")
    _register(client, "Filtered Two", "9070000007", category="ECHS")
    now = utc_now()
    db = SessionLocal()
    try:
        rows = db.query(PatientOffer).order_by(PatientOffer.id).all()
        rows[0].created_at = now - timedelta(days=5)
        db.commit()

        # Wide margin around "now" so the Asia/Kolkata-vs-UTC day boundary can never
        # accidentally exclude "now" itself; the excluded row is 5 days in the past,
        # well outside this window either way.
        breakdown = registrations_by_category(db, start=(now - timedelta(days=2)).date(), end=(now + timedelta(days=1)).date())
        counts = {row["value"]: row["count"] for row in breakdown}
        assert counts["CGHS"] == 0
        assert counts["ECHS"] == 1
    finally:
        db.close()


def test_category_breakdown_appears_on_admin_dashboard(client):
    _register(client, "Dashboard Category", "9070000008", category="CISF")
    dashboard = client.get("/admin/dashboard")
    assert dashboard.status_code == 200
    assert "Registrations by beneficiary category" in dashboard.text
    assert "Central Industrial Security Force (CISF)" in dashboard.text


def test_patient_csv_export_includes_beneficiary_category(client):
    _register(client, "CSV Category Patient", "9070000009", category="CGHS")
    csv_response = client.get("/admin/reports/patients.csv")
    assert csv_response.status_code == 200
    assert "Beneficiary Category" in csv_response.text
    assert "CGHS" in csv_response.text
