import pytest

from app.database import SessionLocal
from app.models import AuditLog, PatientOffer
from app.qr.service import redeem_atomic
from app.time_utils import utc_now
from tests.test_staff_interface import _login_as_staff
from tests.test_workflow import registration


def _register_and_get_coupon(client, name, mobile, campaign_id="1", offer_id="1"):
    response = client.post("/staff/register", data={
        "full_name": name, "mobile": mobile, "campaign_id": campaign_id,
        "offer_id": offer_id, "beneficiary_category": "CGHS", "consent_given": "true",
    }, follow_redirects=False)
    assert response.status_code == 303
    patient_id = int(response.headers["location"].rsplit("/", 1)[-1])
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).filter(PatientOffer.patient_id == patient_id).one()
        return patient_id, coupon.id, coupon.coupon_uid
    finally:
        db.close()


def test_staff_redemption_shows_success_receipt_not_already_used(client):
    _login_as_staff(client)
    patient_id, _coupon_id, coupon_uid = _register_and_get_coupon(client, "Receipt Patient", "9888800001")

    redeem_response = client.post(f"/staff/patients/{patient_id}/redeem", follow_redirects=True)
    assert redeem_response.status_code == 200
    text = redeem_response.text
    assert "Offer redeemed successfully" in text
    assert "OFFER ALREADY USED" not in text
    assert "Receipt Patient" in text
    assert "Free In-House Zirconia Crown" in text
    assert coupon_uid in text

    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).filter(PatientOffer.coupon_uid == coupon_uid).one()
        assert coupon.status == "REDEEMED"
        assert coupon.redeemed_at is not None
    finally:
        db.close()


def test_later_separate_scan_of_a_redeemed_qr_shows_already_used(client):
    _login_as_staff(client)
    patient_id, _coupon_id, coupon_uid = _register_and_get_coupon(client, "Later Scan Patient", "9888800002")

    client.post(f"/staff/patients/{patient_id}/redeem", follow_redirects=True)

    # A later, separate reload of the same result page must no longer show the
    # one-time success receipt -- it must show the plain "already used" state.
    reload_response = client.get(f"/staff/validate/result/{coupon_uid}")
    assert "OFFER ALREADY USED" in reload_response.text
    assert "Offer redeemed successfully" not in reload_response.text

    # A fresh, explicit validate attempt (as if scanned again later) also shows
    # "already used", never the success receipt.
    rescan_response = client.post("/staff/validate", data={"token": coupon_uid})
    assert "OFFER ALREADY USED" in rescan_response.text
    assert "Offer redeemed successfully" not in rescan_response.text


def test_admin_redemption_receipt_then_later_scan_is_already_used(client):
    client.post("/patients/register", data=registration("Admin Receipt Patient", "9888800003"))
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).order_by(PatientOffer.id.desc()).first()
        cid, coupon_uid = coupon.id, coupon.coupon_uid
    finally:
        db.close()

    redeem_response = client.post(f"/redeem/{cid}", follow_redirects=True)
    assert "Offer redeemed successfully" in redeem_response.text
    assert "OFFER ALREADY USED" not in redeem_response.text

    later_response = client.get(f"/validate/result/{coupon_uid}")
    assert "OFFER ALREADY USED" in later_response.text
    assert "Offer redeemed successfully" not in later_response.text


def test_redeem_atomic_rejects_a_repeated_sequential_redemption(client):
    """Sequential, single-session coverage only. Truly simultaneous redemptions are covered
    on PostgreSQL by tests/test_redemption_concurrency.py."""
    db = SessionLocal()
    try:
        from app.models import Campaign, Offer, Patient
        from app.qr.service import expiry_for, new_uid, token_for, token_hash

        now = utc_now()
        patient = Patient(
            patient_uid=new_uid("PAT"), full_name="Idempotency Patient", mobile="9888800004",
            registration_week=now.date(), consent_given=True, consent_version="test",
            consented_at=now, created_at=now,
        )
        db.add(patient)
        db.flush()
        coupon_uid = new_uid("SRD")
        coupon = PatientOffer(
            coupon_uid=coupon_uid, patient_id=patient.id, offer_id=1, campaign_id=1,
            secure_token_hash=token_hash(token_for(coupon_uid)), created_at=now,
            expires_at=expiry_for(now), status="ACTIVE", beneficiary_category="CGHS",
        )
        db.add(coupon)
        db.commit()
        coupon_id = coupon.id

        first = redeem_atomic(db, coupon_id, "staff-one", utc_now())
        second = redeem_atomic(db, coupon_id, "staff-two", utc_now())

        assert first is True
        assert second is False
        assert db.get(PatientOffer, coupon_id).redeemed_by == "staff-one"
        # The winner's audit event is written by the same call; the repeat adds none.
        [event] = db.query(AuditLog).filter_by(action="QR_REDEEMED").all()
        assert (event.user, event.coupon_id, event.patient_id) == ("staff-one", coupon_id, patient.id)
    finally:
        db.close()


def test_redemption_is_rolled_back_when_its_audit_event_cannot_be_written(client, monkeypatch):
    """The state change and its audit event share one transaction: no audit, no redemption."""
    client.post("/patients/register", data=registration("Atomic Audit Patient", "9888800005"))
    db = SessionLocal()
    try:
        coupon_id = db.query(PatientOffer).one().id

        def failing_audit(*args, **kwargs):
            raise RuntimeError("audit unavailable")

        monkeypatch.setattr("app.qr.service.audit", failing_audit)
        with pytest.raises(RuntimeError):
            redeem_atomic(db, coupon_id, "staff-one", utc_now())
        monkeypatch.undo()

        coupon = db.get(PatientOffer, coupon_id)
        assert (coupon.status, coupon.redeemed_at, coupon.redeemed_by) == ("ACTIVE", None, None)
        assert db.query(AuditLog).filter_by(action="QR_REDEEMED").count() == 0
        # Nothing is left half-done: the same coupon can still be redeemed normally.
        assert redeem_atomic(db, coupon_id, "staff-one", utc_now()) is True
        assert db.query(AuditLog).filter_by(action="QR_REDEEMED").count() == 1
    finally:
        db.close()
