"""CSV exports must not let a stored value run as a spreadsheet formula."""
import csv
import io
from datetime import date, datetime, timedelta

import pytest

from app.database import SessionLocal
from app.models import Campaign, Offer, Patient, PatientOffer
from app.registrations.service import register_patient_offer
from app.reporting.routes import csv_safe
from app.time_utils import utc_now


@pytest.mark.parametrize("value", [
    "=1+1", "=HYPERLINK(\"http://evil.example\",\"click\")", "+1+1", "-1+1", "@SUM(A1:A9)",
    "=", "+", "-", "@", "-", "+cmd|' /C calc'!A0", "@admin", "-dash@example.com",
])
def test_each_dangerous_leading_character_is_prefixed_with_one_apostrophe(value):
    assert csv_safe(value) == "'" + value
    assert csv_safe(value)[1:] == value  # the visible value is preserved exactly


@pytest.mark.parametrize("value", [" =1+1", "   +1", "\t=1+1", "\n-1", "\r@x", " \t\r\n =SUM(1)", " =1"])
def test_leading_whitespace_does_not_hide_a_formula(value):
    assert csv_safe(value) == "'" + value


@pytest.mark.parametrize("value", [
    "Asha Verma", "Anne-Marie", "a=b", "1+1", "name@example.com", "50.0%", "SRD-AB12CD34", "PAT-1EBE48B6",
    "9876543210", "ACTIVE", "", "   ", "'=already quoted", "Free In-House Zirconia Crown", "2026-10-11", "0", "12",
])
def test_normal_text_is_left_alone(value):
    assert csv_safe(value) == value


@pytest.mark.parametrize("value", [0, 7, -5, 3.5, -0.5, None, True, date(2026, 10, 11), datetime(2026, 10, 11, 9, 30)])
def test_numbers_dates_and_blanks_are_not_touched(value):
    assert csv_safe(value) is value


def test_legitimate_plus_mobile_number_keeps_its_digits_but_cannot_be_a_formula():
    assert csv_safe("+919876543210") == "'+919876543210"
    assert csv_safe("+91 98765 43210") == "'+91 98765 43210"
    assert csv_safe("919876543210") == "919876543210"


def _rows(response):
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(response.text)))


@pytest.fixture()
def hostile(client):
    """One registration whose every exported text field starts with a formula character."""
    db = SessionLocal()
    try:
        coupon = register_patient_offer(
            db, full_name="=cmd|' /C calc'!A0", mobile="+919876543210", email="", age="30", gender="", city="",
            doctor_name="", campaign_name="", campaign_id=1, offer_id=1, beneficiary_category="CGHS",
            consent_given=True, actor="admin",
        )
        coupon_id, patient_id = coupon.id, coupon.patient_id
        plain = register_patient_offer(
            db, full_name="Plain Patient", mobile="9876500000", email="plain@example.com", age="41", gender="Female",
            city="", doctor_name="", campaign_name="", campaign_id=1, offer_id=2, beneficiary_category="ECHS",
            consent_given=True, actor="admin",
        )
        plain_uid = plain.coupon_uid
        patient = db.get(Patient, patient_id)
        patient.email, patient.gender, patient.patient_uid = "-dash@example.com", "  -1+1", "@PAT-EVIL"
        coupon = db.get(PatientOffer, coupon_id)
        coupon.coupon_uid, coupon.status = "=SRD-EVIL", "REDEEMED"
        coupon.redeemed_at, coupon.redeemed_by = utc_now(), "@SUM(1)"
        db.get(Campaign, 1).name = '=HYPERLINK("http://evil.example","Campaign")'
        db.get(Offer, 1).name = "+Service"
        db.commit()
    finally:
        db.close()
    return client, plain_uid


def test_patient_csv_neutralizes_every_text_cell(hostile):
    client, plain_uid = hostile
    rows = {row["Patient"]: row for row in _rows(client.get("/admin/reports/patients.csv"))}
    assert set(rows) == {"'=cmd|' /C calc'!A0", "Plain Patient"}

    evil = rows["'=cmd|' /C calc'!A0"]
    assert evil["Patient ID"] == "'@PAT-EVIL"
    assert evil["Mobile"] == "'+919876543210"
    assert evil["Email"] == "'-dash@example.com"
    assert evil["Gender"] == "'  -1+1"  # leading whitespace kept, formula defused
    assert evil["Campaign"] == '\'=HYPERLINK("http://evil.example","Campaign")'
    assert evil["Service"] == "'+Service"
    assert evil["Registration ID"] == "'=SRD-EVIL"
    assert evil["Redeemed by"] == "'@SUM(1)"
    # Numbers, dates, status and category are written exactly as before.
    assert evil["Age"] == "30" and evil["Status"] == "REDEEMED" and evil["Beneficiary Category"] == "CGHS"
    assert evil["Registered"][:4].isdigit() and evil["Redeemed"][:4].isdigit()
    # No exported cell in the whole file can start a formula.
    for row in csv.reader(io.StringIO(client.get("/admin/reports/patients.csv").text)):
        assert not any(cell.lstrip().startswith(("=", "+", "-", "@")) for cell in row), row

    plain = rows["Plain Patient"]
    assert plain == {
        "Patient ID": plain["Patient ID"], "Patient": "Plain Patient", "Mobile": "9876500000", "Email": "plain@example.com",
        "Age": "41", "Gender": "Female", "Campaign": evil["Campaign"], "Service": "Free In-House Aligner Scan",
        "Beneficiary Category": "ECHS", "Registration ID": plain_uid, "Registered": plain["Registered"],
        "Expires": plain["Expires"], "Status": "ACTIVE", "Redeemed": "", "Redeemed by": "",
    }
    assert plain["Patient ID"].startswith("PAT-") and not plain["Patient ID"].startswith("'")


def test_campaign_csv_neutralizes_names_and_keeps_counts_numeric(hostile):
    client, _ = hostile
    [row] = _rows(client.get("/admin/reports/campaigns.csv"))
    assert row["Campaign"] == '\'=HYPERLINK("http://evil.example","Campaign")'
    assert (row["Issued"], row["Redeemed"], row["Expired"], row["Conversion rate"]) == ("2", "1", "0", "50.0%")
    assert row["Status"] == "ACTIVE" and row["Start date"] == (date.today() - timedelta(days=1)).isoformat()
    header = client.get("/admin/reports/campaigns.csv").text.splitlines()[0]
    assert header == "Campaign,Start date,End date,Status,Issued,Redeemed,Expired,Conversion rate"


def test_filtered_exports_are_neutralized_too(hostile):
    client, _ = hostile
    for query in ("campaign_id=1", "campaign_id=1&offer_id=1", "status=REDEEMED&date_field=redeemed", "q=cmd"):
        rows = _rows(client.get(f"/admin/reports/patients.csv?{query}"))
        assert "'=cmd|' /C calc'!A0" in {row["Patient"] for row in rows}, query
    response = client.get("/admin/reports/patients.csv?campaign_id=1")
    assert response.headers["content-disposition"] == 'attachment; filename="patient-registrations-campaign-1-hyperlink-http-evil-example-campaign.csv"'


def test_html_pages_show_the_stored_value_unchanged(hostile):
    client, _ = hostile
    for url in ("/patients", "/offers", "/redemptions", "/delivery", "/admin/campaigns"):
        page = client.get(url).text
        assert "&#39;=" not in page and "'=" not in page and "&#39;+" not in page, url  # no apostrophe prefix in the UI
    patients = client.get("/patients").text
    assert "<b>=cmd|&#39; /C calc&#39;!A0</b>" in patients and "+919876543210<small>" in patients
    assert "<td>+Service</td>" in patients
    assert "=HYPERLINK(&#34;http://evil.example&#34;,&#34;Campaign&#34;)" in client.get("/admin/campaigns").text
    db = SessionLocal()
    try:  # exporting never rewrites what is stored
        assert db.query(Patient).filter_by(mobile="+919876543210").one().full_name == "=cmd|' /C calc'!A0"
    finally:
        db.close()
