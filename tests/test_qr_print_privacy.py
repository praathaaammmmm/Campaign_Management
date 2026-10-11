"""The QR image, its download and the printable coupon carry no patient data.

The image is read back with a real QR decoder (zxing-cpp, from requirements-dev.txt) so
this checks what a phone camera would actually get out of the PNG, not what the code
intended to put in.
"""
import io
import re
from datetime import timedelta

import pytest
from PIL import Image

from app.database import SessionLocal
from app.models import Patient, PatientOffer
from app.qr.service import qr_png_bytes, token_for
from app.registrations.service import register_patient_offer
from app.routes.auth import _attempts
from tests.test_staff_interface import _create_staff

zxingcpp = pytest.importorskip("zxingcpp", reason="install requirements-dev.txt to decode QR images")

PII = {
    "name": "Kavita Printcheck", "mobile": "+919855000001", "email": "kavita.printcheck@example.com",
    "city": "Udaipurtown", "doctor": "Dr Fernandes-Iyer", "age": "63", "gender": "Female",
}
TOKEN_PATTERN = re.compile(r"SRD-(SRD-[0-9A-F]{8})\.([0-9a-f]{64})")


@pytest.fixture()
def record(client):
    db = SessionLocal()
    try:
        coupon = register_patient_offer(
            db, full_name=PII["name"], mobile=PII["mobile"], email=PII["email"], age=PII["age"], gender=PII["gender"],
            city=PII["city"], doctor_name=PII["doctor"], campaign_name="Walk-in referral", campaign_id=1, offer_id=1,
            beneficiary_category="ECHS", consent_given=True, actor="admin",
        )
        patient = db.get(Patient, coupon.patient_id)
        return {
            "client": client, "uid": coupon.coupon_uid, "coupon_id": coupon.id, "patient_id": patient.id,
            "patient_uid": patient.patient_uid, "token_hash": coupon.secure_token_hash,
        }
    finally:
        db.close()


def _decode(png: bytes) -> str:
    image = Image.open(io.BytesIO(png))
    image.load()
    results = zxingcpp.read_barcodes(image)
    assert len(results) == 1, f"expected exactly one QR code in the image, found {len(results)}"
    assert results[0].format == zxingcpp.BarcodeFormat.QRCode
    return results[0].text


def _assert_no_patient_data(text, record):
    lowered = text.lower()
    for key, value in PII.items():
        if key in ("age", "gender"):
            continue  # too short/common to search for on their own; checked by exact-format assertions instead
        assert value.lower() not in lowered, f"{key} found"
    for fragment in ("kavita", "printcheck", "9855000001", "@example.com", "udaipur", "fernandes", record["patient_uid"].lower(), "pat-", "echs", "walk-in"):
        assert fragment not in lowered, f"{fragment!r} found"


def test_decoded_qr_png_payload_is_only_the_opaque_token(record):
    client = record["client"]
    expected = token_for(record["uid"])

    for url in (f"/qr/{record['uid']}.png", f"/qr/{record['uid']}/download"):
        response = client.get(url)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        payload = _decode(response.content)

        assert payload == expected
        match = TOKEN_PATTERN.fullmatch(payload)
        assert match, "payload is not exactly <prefix>-<QR ID>.<signature>"
        assert match.group(1) == record["uid"]                 # the QR ID printed under the code
        assert len(match.group(2)) == 64                        # an HMAC signature, not data
        assert set(payload) <= set("SRD-.0123456789ABCDEFabcdef")  # no room for a name, number or address
        _assert_no_patient_data(payload, record)
        assert str(record["patient_id"]) != match.group(2) and record["token_hash"] not in payload
        assert not payload.lower().startswith(("http", "www")) and "/" not in payload and "?" not in payload

    # The same bytes the service builds directly, and they validate as that coupon.
    assert _decode(qr_png_bytes(record["uid"], record["token_hash"])) == expected
    assert "OFFER VALID" in client.post("/validate", data={"token": payload}).text


def test_qr_png_has_no_patient_data_in_its_bytes_or_metadata(record):
    client = record["client"]
    response = client.get(f"/qr/{record['uid']}.png")
    image = Image.open(io.BytesIO(response.content))
    assert image.format == "PNG"
    assert {key: value for key, value in image.info.items() if isinstance(value, (str, bytes))} == {}  # no text chunks
    raw = response.content.decode("latin-1")
    _assert_no_patient_data(raw, record)
    for header in ("content-disposition", "etag", "x-patient", "link"):
        _assert_no_patient_data(response.headers.get(header, ""), record)
    assert response.headers["content-disposition"] == f'inline; filename="{record["uid"]}.png"'
    assert client.get(f"/qr/{record['uid']}/download").headers["content-disposition"] == f'attachment; filename="{record["uid"]}.png"'


def test_two_patients_qr_payloads_differ_only_by_id_and_signature(record):
    client = record["client"]
    db = SessionLocal()
    try:
        other = register_patient_offer(
            db, full_name="Entirely Different Person", mobile="9855000002", email="", age="", gender="", city="",
            doctor_name="", campaign_name="", campaign_id=1, offer_id=2, beneficiary_category="CGHS",
            consent_given=True, actor="admin",
        ).coupon_uid
    finally:
        db.close()
    first, second = (_decode(client.get(f"/qr/{uid}.png").content) for uid in (record["uid"], other))
    assert first != second and TOKEN_PATTERN.fullmatch(first) and TOKEN_PATTERN.fullmatch(second)
    assert len(first) == len(second)  # nothing patient-specific changes the size of the payload


def _print_view(client, uid):
    response = client.get(f"/qr/{uid}/print")
    assert response.status_code == 200
    return response.text


def test_print_view_shows_operational_details_and_no_patient_data(record):
    client = record["client"]
    html = _print_view(client, record["uid"])

    _assert_no_patient_data(html, record)
    body = re.sub(r"<[^>]+>", " ", html.split("<body>", 1)[1])
    words = set(re.findall(r"[A-Za-z0-9+@.-]+", body))
    assert not words & {"Kavita", "Printcheck", "63", "Female", "+919855000001", "9855000001", "Udaipurtown"}
    assert "patient" not in body.lower() and "mobile" not in body.lower() and "email" not in body.lower()

    # What it does carry: clinic branding, service, campaign, QR ID, validity and status.
    assert "SMRITI RAJ DENTISTRY" in html and "COMPLIMENTARY OFFER" in html
    assert "FREE IN-HOUSE ZIRCONIA CROWN" in html and "Test Active Campaign" in html
    assert f"<b>QR ID:</b> {record['uid']}" in html and "<b>Status:</b> ACTIVE" in html
    assert re.search(r"<b>Valid until:</b> \d{2} \w{3} \d{4}", html) and "One-time use only" in html
    assert f'<img src="/qr/{record["uid"]}.png" class="qr">' in html
    assert html.count("<img") == 1 and "<script" not in html and "http" not in html.split("<body>", 1)[1]
    assert "<title>Printable Offer</title>" in html


def test_print_view_stays_pii_free_in_every_status_and_for_every_viewer(record):
    client = record["client"]
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, record["coupon_id"])
        for status in ("REDEEMED", "CANCELLED", "EXPIRED"):
            coupon.status = status
            coupon.redeemed_by = "desk-staff" if status == "REDEEMED" else None
            db.commit()
            html = _print_view(client, record["uid"])
            _assert_no_patient_data(html, record)
            assert f"<b>Status:</b> {status}" in html and "desk-staff" not in html
        coupon.status, coupon.campaign_id = "ACTIVE", None  # a legacy registration with no campaign
        db.commit()
    finally:
        db.close()
    html = _print_view(client, record["uid"])
    _assert_no_patient_data(html, record)
    assert "print-campaign" not in html and record["uid"] in html

    for assignments in ([1], []):
        _create_staff(username=f"print-staff-{len(assignments)}", campaign_ids=assignments)
        _attempts.clear()
        client.post("/logout")
        client.post("/login", data={"username": f"print-staff-{len(assignments)}", "password": "staff-password"})
        staff_html = _print_view(client, record["uid"])
        _assert_no_patient_data(staff_html, record)
        assert staff_html == html  # staff and admin get the identical PII-free coupon
        assert _decode(client.get(f"/qr/{record['uid']}.png").content) == token_for(record["uid"])


def test_qr_routes_are_still_authenticated(record):
    client = record["client"]
    client.post("/logout")
    for suffix in (".png", "/download", "/print"):
        response = client.get(f"/qr/{record['uid']}{suffix}", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/login")
        final = client.get(f"/qr/{record['uid']}{suffix}")
        assert not final.content.startswith(b"\x89PNG") and record["uid"] not in final.text
        _assert_no_patient_data(final.text, record)
