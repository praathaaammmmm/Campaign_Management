"""Authorized QR patient details and access auditing.

Scanning a valid QR shows any logged-in staff member or admin the patient's details; the
QR itself stays an opaque token, and nobody else sees anything.
"""
import base64
import json
import re
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import AuditLog, Campaign, Offer, Patient, PatientOffer
from app.qr.service import qr_payload, qr_png_bytes, token_for
from app.registrations.service import register_patient_offer
from app.routes.auth import _attempts
from tests.test_staff_interface import _create_staff

PII = {
    "name": "Meera Qrdetail", "mobile": "9844000001", "email": "meera.qrdetail@example.com",
    "city": "Jaipurville", "doctor": "Dr Sharma-Rao",
}
INVALID_MESSAGE = "This QR/token is not registered in the Smriti Raj Dentistry system."
STYLE_CSS = (Path(__file__).resolve().parent.parent / "app" / "static" / "style.css").read_text()


def _register(campaign_id=1, **overrides):
    values = dict(
        full_name=PII["name"], mobile=PII["mobile"], email=PII["email"], age="47", gender="Female",
        city=PII["city"], doctor_name=PII["doctor"], campaign_name="", campaign_id=campaign_id, offer_id=1,
        beneficiary_category="ECHS", consent_given=True, actor="admin",
    )
    values.update(overrides)
    db = SessionLocal()
    try:
        coupon = register_patient_offer(db, **values)
        return {"patient_id": coupon.patient_id, "coupon_id": coupon.id, "uid": coupon.coupon_uid}
    finally:
        db.close()


def _campaign(name):
    db = SessionLocal()
    try:
        campaign = Campaign(name=name, start_date=date.today() - timedelta(days=1), end_date=date.today() + timedelta(days=1), status="ACTIVE", created_by="test")
        campaign.offers = db.query(Offer).all()
        db.add(campaign)
        db.commit()
        return campaign.id
    finally:
        db.close()


def _staff(client, campaign_ids, username="desk-staff"):
    _create_staff(username=username, campaign_ids=campaign_ids)
    _attempts.clear()
    client.post("/logout")
    assert client.post("/login", data={"username": username, "password": "staff-password"}, follow_redirects=False).headers["location"] == "/staff/home"


def _set(coupon_id, **values):
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        for key, value in values.items():
            setattr(coupon, key, value)
        db.commit()
    finally:
        db.close()


def _coupon(coupon_id):
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        return coupon.status, coupon.redeemed_by, coupon.redeemed_at
    finally:
        db.close()


def _views():
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter_by(action="QR_DETAILS_VIEWED").order_by(AuditLog.id).all()
        return [(row.user, row.coupon_id, row.patient_id, json.loads(row.details), row.timestamp) for row in rows]
    finally:
        db.close()


def _audit_count(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog).filter_by(action=action).count()
    finally:
        db.close()


def _result(html):
    """The result panel only (the page chrome never contains patient data)."""
    assert 'id="qr-result"' in html, "no result panel rendered"
    return html.split('id="qr-result"', 1)[1].split("</section>", 1)[0]


def _cell(panel, label):
    match = re.search(rf"<small>{re.escape(label)}</small><b[^>]*>(.*?)</b>", panel)
    return match.group(1) if match else None


def _assert_no_pii(text, *, allow=()):
    for key, value in PII.items():
        if key not in allow:
            assert value not in text, f"{key} leaked"


FULL_DETAILS = {
    "Patient": PII["name"], "Mobile": PII["mobile"], "Email": PII["email"], "Age": "47", "Gender": "Female",
    "City": PII["city"], "Doctor": PII["doctor"], "Beneficiary category": "Ex-Servicemen Contributory Health Scheme (ECHS)",
    "Campaign": "Test Active Campaign", "Service": "Free In-House Zirconia Crown", "Status": "ACTIVE",
}


def _assert_full_details(panel, record, status="ACTIVE"):
    for label, value in {**FULL_DETAILS, "QR ID": record["uid"], "Status": status}.items():
        assert _cell(panel, label) == value, label
    today = date.today()
    assert re.fullmatch(r"\d{2} \w{3} \d{4}", _cell(panel, "Issued")) and re.fullmatch(r"\d{2} \w{3} \d{4}", _cell(panel, "Expires"))
    assert str(today.year) in _cell(panel, "Issued") or str(today.year - 1) in _cell(panel, "Issued")


# --- Who sees what ----------------------------------------------------------------------

def test_admin_sees_the_full_detail_set_for_a_valid_qr(client):
    record = _register()
    for token in (record["uid"], token_for(record["uid"]), record["uid"].lower()):
        panel = _result(client.post("/validate", data={"token": token}).text)
        assert "OFFER VALID" in panel
        _assert_full_details(panel, record)
        assert _cell(panel, "Redeemed") is None and _cell(panel, "Redeemed by") is None
        assert f'action="/redeem/{record["coupon_id"]}"' in panel and "Redeem offer" in panel
    assert _cell(_result(client.get(f"/validate/result/{record['uid']}").text), "Mobile") == PII["mobile"]


@pytest.mark.parametrize("assignments", ["assigned", "another campaign", "none"])
def test_any_staff_member_sees_full_details_and_can_redeem(client, assignments):
    other = _campaign("Other Campaign")
    record = _register()
    _staff(client, {"assigned": [1], "another campaign": [other], "none": []}[assignments])

    panel = _result(client.post("/staff/validate", data={"token": record["uid"]}).text)
    assert "OFFER VALID" in panel
    _assert_full_details(panel, record)
    assert f'action="/staff/patients/{record["patient_id"]}/redeem"' in panel

    response = client.post(f"/staff/patients/{record['patient_id']}/redeem", follow_redirects=False)
    assert response.headers["location"] == f"/staff/validate/result/{record['uid']}"
    receipt = _result(client.get(response.headers["location"]).text)
    assert "Offer redeemed successfully" in receipt
    _assert_full_details(receipt, record, status="REDEEMED")
    assert _cell(receipt, "Redeemed by") == "desk-staff" and _cell(receipt, "Redeemed").endswith("IST")
    assert "Redeem offer" not in receipt
    assert _coupon(record["coupon_id"])[:2] == ("REDEEMED", "desk-staff")


def test_optional_fields_show_a_dash_and_a_missing_email_is_omitted(client):
    record = _register(email="", age="", gender="", city="", doctor_name="", mobile="9844000002")
    panel = _result(client.post("/validate", data={"token": record["uid"]}).text)
    assert _cell(panel, "Email") is None
    assert [_cell(panel, label) for label in ("Age", "Gender", "City", "Doctor")] == ["—"] * 4
    assert _cell(panel, "Mobile") == "9844000002"


def test_details_are_escaped(client):
    record = _register(full_name="<script>alert(1)</script>", city="<b>x</b>", mobile="9844000003")
    panel = _result(client.post("/validate", data={"token": record["uid"]}).text)
    assert "<script>alert(1)</script>" not in panel and "&lt;script&gt;alert(1)&lt;/script&gt;" in panel
    assert _cell(panel, "City") == "&lt;b&gt;x&lt;/b&gt;"


# --- Nobody else sees anything ----------------------------------------------------------

def test_logged_out_visitors_are_sent_to_login_and_see_no_details(client):
    record = _register()
    client.post("/logout")

    for url in ("/validate", "/staff/validate", f"/validate/result/{record['uid']}", f"/staff/validate/result/{record['uid']}"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] in ("/login", "/staff/login"), url
        _assert_no_pii(client.get(url).text)
    for url, data in (
        ("/validate", {"token": record["uid"]}), ("/staff/validate", {"token": token_for(record["uid"])}),
        (f"/redeem/{record['coupon_id']}", {}), (f"/staff/patients/{record['patient_id']}/redeem", {}),
    ):
        response = client.post(url, data=data)
        _assert_no_pii(response.text)
        assert 'id="qr-result"' not in response.text
    assert _coupon(record["coupon_id"])[0] == "ACTIVE"
    assert _views() == [] and _audit_count("QR_VALIDATED") == 0 and _audit_count("QR_REDEEMED") == 0


@pytest.mark.parametrize("token", [
    "SRD-0000ABCD", "srd-ffffffff", "SRD-NOTHEX!!", "garbage", "1", "' OR 1=1 --", "<script>alert(1)</script>",
    "SRD-", "%00", "x" * 5000, "SRD-12345678.deadbeef", "  ",
])
def test_nonexistent_or_malformed_qr_is_generic_and_writes_no_detail_audit(client, token):
    record = _register()
    forged = f"SRD-{record['uid']}.{'0' * 64}"  # a real QR ID with a wrong signature
    for role in ("admin", "staff"):
        if role == "staff":
            _staff(client, [1])
        url = "/validate" if role == "admin" else "/staff/validate"
        for value in (token, forged):
            response = client.post(url, data={"token": value})
            assert response.status_code == 200
            panel = _result(response.text)
            assert "INVALID QR" in panel and INVALID_MESSAGE in panel
            _assert_no_pii(response.text)
            for leak in (record["uid"], "Test Active Campaign", "Free In-House", "Redeem offer", "qr-detail-group", "<script>alert(1)</script>"):
                assert leak not in panel
    assert _views() == []
    assert _coupon(record["coupon_id"])[0] == "ACTIVE"


def test_unknown_result_page_redirects_without_details(client):
    _register()
    for url, back in (("/validate/result/SRD-0000ABCD", "/validate"), ("/validate/result/not-a-qr", "/validate")):
        response = client.get(url, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, back)
    _staff(client, [])
    response = client.get("/staff/validate/result/SRD-0000ABCD", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/staff/validate")
    assert _views() == []


# --- Status behaviour -------------------------------------------------------------------

@pytest.mark.parametrize("state, heading", [("EXPIRED", "OFFER EXPIRED"), ("CANCELLED", "OFFER CANCELLED")])
def test_expired_and_cancelled_show_status_but_no_patient_details_and_cannot_be_redeemed(client, state, heading):
    record = _register()
    if state == "EXPIRED":  # still marked ACTIVE in the database, but past its expiry
        db = SessionLocal()
        try:
            coupon = db.get(PatientOffer, record["coupon_id"])
            coupon.expires_at = coupon.created_at - timedelta(minutes=1)
            db.commit()
        finally:
            db.close()
    else:
        _set(record["coupon_id"], status="CANCELLED")

    for role in ("admin", "staff"):
        if role == "staff":
            _staff(client, [])
        prefix = "" if role == "admin" else "/staff"
        for response in (client.post(f"{prefix}/validate", data={"token": record["uid"]}), client.get(f"{prefix}/validate/result/{record['uid']}")):
            panel = _result(response.text)
            assert heading in panel and _cell(panel, "Status") == state
            _assert_no_pii(response.text)
            assert 'data-group="patient"' not in panel and _cell(panel, "Mobile") is None
            # The offer it was for is still identified, so the desk can explain what lapsed.
            assert _cell(panel, "QR ID") == record["uid"] and _cell(panel, "Service") == "Free In-House Zirconia Crown"
            assert _cell(panel, "Campaign") == "Test Active Campaign"
            assert "Redeem offer" not in panel and "/redeem" not in panel
        redeem = f"/redeem/{record['coupon_id']}" if role == "admin" else f"/staff/patients/{record['patient_id']}/redeem"
        client.post(redeem)
        assert _coupon(record["coupon_id"])[0] == state and _coupon(record["coupon_id"])[1] is None
    assert _views() == [] and _audit_count("QR_REDEEMED") == 0


def test_redeemed_qr_shows_details_and_who_redeemed_but_cannot_be_redeemed_again(client):
    record = _register()
    assert "Offer redeemed successfully" in client.post(f"/redeem/{record['coupon_id']}", follow_redirects=True).text
    first = _coupon(record["coupon_id"])

    _staff(client, [])
    for response in (client.post("/staff/validate", data={"token": record["uid"]}), client.get(f"/staff/validate/result/{record['uid']}")):
        panel = _result(response.text)
        assert "OFFER ALREADY USED" in panel
        _assert_full_details(panel, record, status="REDEEMED")
        assert _cell(panel, "Redeemed by") == "admin" and _cell(panel, "Redeemed").endswith("IST")
        assert "Redeem offer" not in panel and "/redeem" not in panel
    client.post(f"/staff/patients/{record['patient_id']}/redeem")
    assert _coupon(record["coupon_id"]) == first  # still the first redemption, by the first redeemer
    assert _audit_count("QR_REDEEMED") == 1


# --- The QR stays opaque ----------------------------------------------------------------

def test_qr_payload_token_and_urls_contain_no_patient_data(client):
    record = _register()
    token = token_for(record["uid"])
    assert qr_payload(token) == token and re.fullmatch(r"SRD-SRD-[0-9A-F]{8}\.[0-9a-f]{64}", token)
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, record["coupon_id"])
        patient = db.get(Patient, record["patient_id"])
        secrets_ = [patient.full_name, patient.mobile, patient.email, patient.city, patient.doctor_name, patient.patient_uid, str(patient.id)]
        for value in secrets_[:6]:
            assert value not in token and value.lower() not in token.lower()
        assert coupon.secure_token_hash not in token  # nor is the stored hash itself exposed
        image = qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)
    finally:
        db.close()
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    for value in PII.values():
        assert value.encode() not in image

    page = client.post("/validate", data={"token": token})
    response = client.post(f"/redeem/{record['coupon_id']}", follow_redirects=False)
    urls = [str(page.url), response.headers["location"], f"/qr/{record['uid']}.png", f"/qr/{record['uid']}/download", f"/qr/{record['uid']}/print"]
    assert response.headers["location"] == f"/validate/result/{record['uid']}"
    for url in urls:
        _assert_no_pii(url)
        assert PII["name"].split()[0] not in url and "@" not in url


def test_result_page_keeps_patient_data_out_of_client_side_storage(client):
    record = _register()
    page = client.post("/validate", data={"token": record["uid"]})
    client.post(f"/redeem/{record['coupon_id']}")
    receipt = client.get(f"/validate/result/{record['uid']}")

    for response in (page, receipt):
        html = response.text
        assert response.headers["cache-control"] == "no-store"
        for api in ("localStorage", "sessionStorage", "indexedDB", "document.cookie"):
            assert api not in html
        scripts = "".join(re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S))
        _assert_no_pii(scripts)
        assert 'id="token" name="token" value=""' in html  # the scanned token is not echoed back
        _assert_no_pii(html.split('id="qr-result"', 1)[0])   # details live only in the result panel
    for name, value in client.cookies.items():
        _assert_no_pii(value)
        payload = value.split(".")[0]
        decoded = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", "ignore") if name == "session" else ""
        _assert_no_pii(decoded)


# --- Audit ------------------------------------------------------------------------------

def test_detail_view_is_audited_for_admin_and_staff_with_identity_role_and_outcome(client):
    record = _register()
    client.post("/validate", data={"token": record["uid"]})
    client.get(f"/validate/result/{record['uid']}")
    _staff(client, [], username="unassigned-desk")
    client.post("/staff/validate", data={"token": token_for(record["uid"])})
    client.post(f"/staff/patients/{record['patient_id']}/redeem", follow_redirects=True)   # shows the receipt
    client.post("/staff/validate", data={"token": record["uid"]})                          # already used

    views = _views()
    assert [(user, details["role"], details["status"]) for user, _, _, details, _ in views] == [
        ("admin", "admin", "VALID"), ("admin", "admin", "VALID"),
        ("unassigned-desk", "staff", "VALID"), ("unassigned-desk", "staff", "JUST_REDEEMED"), ("unassigned-desk", "staff", "REDEEMED"),
    ]
    for user, coupon_id, patient_id, details, timestamp in views:
        assert (coupon_id, patient_id) == (record["coupon_id"], record["patient_id"]) and timestamp is not None
        assert details == {"role": details["role"], "coupon_uid": record["uid"], "campaign_id": 1, "status": details["status"]}
        _assert_no_pii(json.dumps(details))  # the audit row identifies the record, it does not copy the data

    # Redemption auditing is unchanged: one plain QR_REDEEMED row.
    db = SessionLocal()
    try:
        [redeemed] = db.query(AuditLog).filter_by(action="QR_REDEEMED").all()
        assert (redeemed.user, redeemed.coupon_id, redeemed.patient_id, redeemed.details) == ("unassigned-desk", record["coupon_id"], record["patient_id"], None)
        assert db.query(AuditLog).filter_by(action="QR_VALIDATED").count() == 3
    finally:
        db.close()
    assert "Qr Details Viewed" in _admin_audit_page(client)


def _admin_audit_page(client):
    _attempts.clear()
    client.post("/logout")
    client.post("/login", data={"username": "smritiraj-clinic", "password": "test-password"})
    return client.get("/admin/audit").text


def test_no_detail_audit_without_a_successful_authorized_view(client):
    record = _register()
    lapsed = _register(mobile="9844000009", full_name="Lapsed Patient")
    _set(lapsed["coupon_id"], status="CANCELLED")

    client.post("/validate", data={"token": "SRD-0000ABCD"})
    client.post("/validate", data={"token": "random words"})
    client.post("/validate", data={"token": lapsed["uid"]})
    client.get(f"/validate/result/{lapsed['uid']}")
    client.get("/validate/result/SRD-0000ABCD")
    client.get("/validate")
    for suffix in (".png", "/download"):
        client.get(f"/qr/{record['uid']}{suffix}")
    client.post("/validate", include_csrf=False, data={"token": record["uid"]})  # rejected: no CSRF token
    client.get(f"/patients/{record['patient_id']}")
    assert _views() == []

    client.post("/logout")
    client.post("/validate", data={"token": record["uid"]})
    client.get(f"/validate/result/{record['uid']}")
    assert _views() == []


# --- Image, download and print ----------------------------------------------------------

def test_qr_image_download_and_print_stay_authenticated_and_pii_safe(client):
    record = _register()
    image, download, printed = (client.get(f"/qr/{record['uid']}{suffix}") for suffix in (".png", "/download", "/print"))
    for response in (image, download):
        assert response.headers["content-type"] == "image/png" and response.content.startswith(b"\x89PNG")
        for value in PII.values():
            assert value.encode() not in response.content
        _assert_no_pii(response.headers["content-disposition"])
        assert response.headers["content-disposition"].endswith(f'filename="{record["uid"]}.png"')
    assert image.content == download.content
    # The printable coupon identifies the offer, never the patient (see test_qr_print_privacy.py).
    _assert_no_pii(printed.text)
    for extra in ("47", "Female", "ECHS", "Ex-Servicemen", "qr-detail-group"):
        assert extra not in printed.text

    _staff(client, [])
    assert client.get(f"/qr/{record['uid']}.png").headers["content-type"] == "image/png"
    assert client.get("/qr/SRD-0000ABCD.png", follow_redirects=False).status_code == 404
    _assert_no_pii(client.get(f"/qr/{record['uid']}/print").text)

    client.post("/logout")
    for suffix in (".png", "/download", "/print"):
        response = client.get(f"/qr/{record['uid']}{suffix}", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
        final = client.get(f"/qr/{record['uid']}{suffix}")
        assert not final.content.startswith(b"\x89PNG")
        _assert_no_pii(final.text)
    assert _views() == []


# --- Layout -----------------------------------------------------------------------------

def test_result_layout_puts_status_first_then_patient_then_offer_then_redeem(client):
    record = _register()
    html = client.post("/validate", data={"token": record["uid"]}).text
    panel = _result(html)
    order = [panel.index(marker) for marker in ("OFFER VALID", 'data-group="patient"', 'data-group="offer"', 'data-group="validity"', 'class="qr-redeem"')]
    assert order == sorted(order)
    assert [title for title in re.findall(r"<h3>(.*?)</h3>", panel)] == ["Patient", "Campaign &amp; service", "Validity"]
    assert '<meta name="viewport" content="width=device-width,initial-scale=1">' in html
    assert "onsubmit=\"return confirm('Redeem this offer? This cannot be undone.')\"" in panel  # one deliberate click


def test_result_layout_css_supports_narrow_screens():
    assert ".result-copy{min-width:0}" in STYLE_CSS                       # the text column may shrink
    assert ".qr-detail-group .result-details b{overflow-wrap:anywhere}" in STYLE_CSS  # long emails wrap
    assert "@media(max-width:520px){.result-panel{flex-direction:column" in STYLE_CSS
    phone = STYLE_CSS.split("@media(max-width:520px){", 1)[1]
    assert ".result-details{grid-template-columns:1fr}" in phone          # one detail per row on a phone
    assert "style=" not in (Path(__file__).resolve().parent.parent / "app" / "templates" / "validate.html").read_text()
