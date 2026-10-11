from datetime import date, timedelta

from app.database import SessionLocal
from app.models import Campaign, Offer, PatientOffer
from app.time_utils import clinic_local_day_start_utc
from tests.test_staff_interface import _login_as_staff


def _staff_register(client, name, mobile, campaign_id="1", offer_id="1"):
    response = client.post("/staff/register", data={
        "full_name": name, "mobile": mobile, "campaign_id": campaign_id,
        "offer_id": offer_id, "beneficiary_category": "CGHS", "consent_given": "true",
    }, follow_redirects=False)
    assert response.status_code == 303
    return response


def _admin_register(client, name, mobile, offer_id="1"):
    response = client.post("/patients/register", data={
        "full_name": name, "mobile": mobile, "campaign_id": "1",
        "offer_id": offer_id, "beneficiary_category": "CGHS", "consent_given": "true",
    }, follow_redirects=False)
    assert response.status_code == 303
    return response


def _second_campaign():
    db = SessionLocal()
    try:
        offers = db.query(Offer).all()
        campaign = Campaign(
            name="Second Campaign",
            start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1),
            status="ACTIVE",
            created_by="test",
        )
        campaign.offers = offers
        db.add(campaign)
        db.commit()
        return campaign.id
    finally:
        db.close()


# --- Staff filters -----------------------------------------------------

def test_staff_search_by_name_matches(client):
    _login_as_staff(client)
    _staff_register(client, "Findable Staff Patient", "9100000001")
    page = client.get("/staff/patients?q=Findable+Staff")
    assert page.status_code == 200
    assert "Findable Staff Patient" in page.text


def test_staff_search_by_mobile_matches(client):
    _login_as_staff(client)
    _staff_register(client, "Mobile Match Patient", "9100000002")
    page = client.get("/staff/patients?q=9100000002")
    assert "Mobile Match Patient" in page.text


def test_staff_status_filter_matches(client):
    _login_as_staff(client)
    _staff_register(client, "Active Status Patient", "9100000003")
    active_page = client.get("/staff/patients?status=ACTIVE")
    assert "Active Status Patient" in active_page.text
    redeemed_page = client.get("/staff/patients?status=REDEEMED")
    assert "Active Status Patient" not in redeemed_page.text


def test_staff_campaign_filter_matches(client):
    other_campaign_id = _second_campaign()  # before login: staff are assigned the campaigns that exist
    _login_as_staff(client)
    _staff_register(client, "First Campaign Patient", "9100000004", campaign_id="1")
    _staff_register(client, "Second Campaign Patient", "9100000005", campaign_id=str(other_campaign_id))

    first_page = client.get("/staff/patients?campaign_id=1")
    assert "First Campaign Patient" in first_page.text
    assert "Second Campaign Patient" not in first_page.text

    second_page = client.get(f"/staff/patients?campaign_id={other_campaign_id}")
    assert "Second Campaign Patient" in second_page.text
    assert "First Campaign Patient" not in second_page.text


def test_staff_date_range_is_inclusive_of_end_date(client):
    _login_as_staff(client)
    _staff_register(client, "Boundary Date Patient", "9100000006")
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).one()
        boundary_day = date.today() - timedelta(days=3)
        # created_at is stored as naive UTC. "Just before local (Asia/Kolkata) midnight
        # at the end of boundary_day" is NOT hour=23/minute=45 in UTC (that lands in the
        # small hours of the *next* IST calendar day) — it must be computed from the
        # clinic timezone directly.
        coupon.created_at = clinic_local_day_start_utc(boundary_day + timedelta(days=1)) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    page = client.get(f"/staff/patients?start={boundary_day.isoformat()}&end={boundary_day.isoformat()}")
    assert "Boundary Date Patient" in page.text


def test_staff_combined_filters_apply_together(client):
    other_campaign_id = _second_campaign()  # before login: staff are assigned the campaigns that exist
    _login_as_staff(client)
    _staff_register(client, "Combo Match Patient", "9100000007", campaign_id="1")
    _staff_register(client, "Combo Wrong Campaign Patient", "9100000008", campaign_id=str(other_campaign_id))

    page = client.get(
        f"/staff/patients?q=Combo&status=ACTIVE&campaign_id=1"
        f"&start={date.today().isoformat()}&end={date.today().isoformat()}"
    )
    assert "Combo Match Patient" in page.text
    assert "Combo Wrong Campaign Patient" not in page.text


def test_staff_invalid_date_returns_friendly_error_not_500(client):
    _login_as_staff(client)
    page = client.get("/staff/patients?start=not-a-date")
    assert page.status_code == 422
    assert "YYYY-MM-DD" in page.text


# --- Admin filters (regression guard) -----------------------------------

def test_admin_patient_filter_tolerates_blank_offer_and_status_query_params(client):
    _admin_register(client, "Vansh Blank Filter Patient", "9100000010", offer_id="1")
    page = client.get("/patients?q=vansh&status=&offer_id=")
    assert page.status_code == 200
    assert "Vansh Blank Filter Patient" in page.text


def test_staff_patient_filter_tolerates_blank_campaign_query_param(client):
    _login_as_staff(client)
    _staff_register(client, "Blank Campaign Filter Patient", "9100000011")
    page = client.get("/staff/patients?q=Blank+Campaign&status=&campaign_id=")
    assert page.status_code == 200
    assert "Blank Campaign Filter Patient" in page.text


def test_admin_dashboard_and_reports_tolerate_blank_campaign_and_offer_query_params(client):
    _admin_register(client, "Dashboard Blank Filter Patient", "9100000012")
    dashboard = client.get("/admin/dashboard?campaign_id=&offer_id=&start=&end=")
    assert dashboard.status_code == 200
    campaign_csv = client.get("/admin/reports/campaigns.csv?campaign_id=&offer_id=")
    assert campaign_csv.status_code == 200
    patient_csv = client.get("/admin/reports/patients.csv?campaign_id=&offer_id=")
    assert patient_csv.status_code == 200
    assert "Dashboard Blank Filter Patient" in patient_csv.text


def test_admin_patient_search_status_and_offer_filters_still_work(client):
    _admin_register(client, "Admin Filter Patient", "9100000009", offer_id="1")
    assert "Admin Filter Patient" in client.get("/patients?q=Admin+Filter").text
    assert "Admin Filter Patient" in client.get("/patients?status=ACTIVE").text
    assert "Admin Filter Patient" not in client.get("/patients?status=REDEEMED").text
    assert "Admin Filter Patient" in client.get("/patients?offer_id=1").text
    assert "Admin Filter Patient" not in client.get("/patients?offer_id=2").text


# --- Form rendering ------------------------------------------------------

def test_admin_filter_form_action_and_reset_link(client):
    page = client.get("/patients")
    assert 'action="/patients"' in page.text
    assert 'method="get"' in page.text
    assert 'href="/patients"' in page.text  # reset link


def test_staff_filter_form_action_and_reset_link(client):
    _login_as_staff(client)
    page = client.get("/staff/patients")
    assert 'action="/staff/patients"' in page.text
    assert 'method="get"' in page.text
    assert 'href="/staff/patients"' in page.text  # reset link
