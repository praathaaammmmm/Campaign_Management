"""QR images are derived on demand from the coupon row and signing key, never stored."""
import base64
import hashlib
from io import BytesIO

import pytest
import qrcode
from PIL import Image

from app.database import SessionLocal
from app.models import DeliveryLog, PatientOffer
from app.notifications import service as notification_service
from app.qr import service as qr_service
from app.qr.service import qr_png_bytes, token_for

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture()
def qr_dir(tmp_path, monkeypatch):
    """An empty stand-in for generated_qr/, so tests never touch the real directory."""
    monkeypatch.setattr(qr_service, "QR_DIR", tmp_path)
    return tmp_path


def _register(client, mobile="9666600001", email="qr@example.com"):
    response = client.post("/patients/register", data={
        "full_name": "QR Patient", "mobile": mobile, "email": email, "campaign_id": "1",
        "offer_id": "1", "beneficiary_category": "CGHS", "consent_given": "true",
    }, follow_redirects=False)
    assert response.status_code == 303
    db = SessionLocal()
    try:
        return db.query(PatientOffer).order_by(PatientOffer.id.desc()).first()
    finally:
        db.close()


def _expected_png(coupon_uid: str) -> bytes:
    """The QR for a coupon's secure token, built independently of the code under test."""
    buffer = BytesIO()
    qrcode.make(token_for(coupon_uid)).save(buffer, format="PNG")
    return buffer.getvalue()


def _make_legacy(coupon_id):
    """Turn a coupon into a pre-hashed-token legacy coupon: its hash matches no derivable token."""
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        coupon.secure_token_hash = hashlib.sha256(b"legacy-random-token").hexdigest()
        db.commit()
    finally:
        db.close()


def test_registration_writes_no_qr_file(client, qr_dir):
    _register(client)
    assert list(qr_dir.iterdir()) == []


def test_qr_image_is_served_from_memory_with_no_file_on_disk(client, qr_dir):
    coupon = _register(client)
    response = client.get(f"/qr/{coupon.coupon_uid}.png")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content.startswith(PNG_MAGIC)
    assert response.content == qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)
    assert Image.open(BytesIO(response.content)).size[0] > 100
    assert list(qr_dir.iterdir()) == []


def test_qr_download_keeps_attachment_filename(client, qr_dir):
    coupon = _register(client)
    response = client.get(f"/qr/{coupon.coupon_uid}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"] == f'attachment; filename="{coupon.coupon_uid}.png"'
    assert response.content.startswith(PNG_MAGIC)
    assert list(qr_dir.iterdir()) == []


def test_qr_image_encodes_the_coupons_secure_token(client, qr_dir):
    coupon = _register(client)
    assert client.get(f"/qr/{coupon.coupon_uid}.png").content == _expected_png(coupon.coupon_uid)


def test_generated_qr_validates(client, qr_dir):
    coupon = _register(client)
    response = client.post("/validate", data={"token": token_for(coupon.coupon_uid)})
    assert "OFFER VALID" in response.text


def test_print_page_still_references_the_image_url(client, qr_dir):
    coupon = _register(client)
    response = client.get(f"/qr/{coupon.coupon_uid}/print")
    assert response.status_code == 200
    assert f'src="/qr/{coupon.coupon_uid}.png"' in response.text


def test_repeat_registrations_for_one_mobile_get_different_qr_images(client, qr_dir):
    first = _register(client)
    db = SessionLocal(); db.get(PatientOffer, first.id).status = "CANCELLED"; db.commit(); db.close()
    second = _register(client)
    assert first.coupon_uid != second.coupon_uid
    assert client.get(f"/qr/{first.coupon_uid}.png").content != client.get(f"/qr/{second.coupon_uid}.png").content


def test_unknown_coupon_and_unauthenticated_requests_get_no_image(client, qr_dir):
    coupon = _register(client)
    assert client.get("/qr/SRD-00000000.png", follow_redirects=False).status_code == 303
    client.post("/logout")
    response = client.get(f"/qr/{coupon.coupon_uid}.png", follow_redirects=False)
    assert response.status_code in (303, 401, 403) and not response.content.startswith(PNG_MAGIC)


def test_delivery_payload_generates_qr_without_local_file(client, qr_dir):
    coupon = _register(client)
    db = SessionLocal()
    try:
        rows = db.query(DeliveryLog).filter_by(coupon_id=coupon.id).all()
        assert {row.channel for row in rows} == {"EMAIL", "WHATSAPP"}
        for row in rows:
            payload = notification_service._payload_for(db, row)
            png = base64.b64decode(payload["qr_base64_png"])
            assert png == qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)
            assert payload["registration_id"] == coupon.coupon_uid
    finally:
        db.close()
    assert list(qr_dir.iterdir()) == []


def test_legacy_coupon_is_served_from_existing_file_read_only(client, qr_dir):
    coupon = _register(client)
    _make_legacy(coupon.id)
    legacy_png = qr_dir / f"{coupon.coupon_uid}.png"
    Image.new("L", (40, 40), 255).save(legacy_png)
    stored = legacy_png.read_bytes()

    assert client.get(f"/qr/{coupon.coupon_uid}.png").content == stored
    assert client.get(f"/qr/{coupon.coupon_uid}/download").content == stored
    db = SessionLocal()
    try:
        row = db.query(DeliveryLog).filter_by(coupon_id=coupon.id).first()
        assert base64.b64decode(notification_service._payload_for(db, row)["qr_base64_png"]) == stored
    finally:
        db.close()
    assert legacy_png.read_bytes() == stored and len(list(qr_dir.iterdir())) == 1


def test_legacy_coupon_without_file_keeps_qr_unavailable_behavior(client, qr_dir):
    """Also the changed-signing-key case: a QR that would not validate is never served."""
    coupon = _register(client)
    _make_legacy(coupon.id)

    for path in (f"/qr/{coupon.coupon_uid}.png", f"/qr/{coupon.coupon_uid}/download"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert "QR%20file%20unavailable" in response.headers["location"]
    db = SessionLocal()
    try:
        row = db.query(DeliveryLog).filter_by(coupon_id=coupon.id).first()
        assert notification_service._payload_for(db, row)["qr_base64_png"] is None
    finally:
        db.close()
    assert list(qr_dir.iterdir()) == []
