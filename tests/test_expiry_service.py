from contextlib import contextmanager
from datetime import timedelta

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.expiry_service import REMINDER_ACTION, run_expiry_sweep
from app.models import AuditLog, PatientOffer
from app.qr.service import redeem_atomic, refresh_expiry, result_for, token_for
from app.time_utils import utc_now


def _register(client, name: str, mobile: str):
    response = client.post("/patients/register", data={
        "full_name": name, "mobile": mobile, "campaign_id": "1",
        "offer_id": "1", "beneficiary_category": "CGHS", "consent_given": "true",
    })
    assert response.status_code == 200


@contextmanager
def _cancelled_just_before_the_next_coupon_write(coupon_id: int, now):
    """Reproduce the cancellation race deterministically, on SQLite and PostgreSQL alike:
    the instant before the next ``UPDATE patient_offers`` reaches the database, a second,
    separate session (as the admin's cancel request would use) cancels the coupon and
    commits. Yields a list that is non-empty once that has happened."""
    raced = []

    def cancel(conn, cursor, statement, parameters, context, executemany):
        if raced or not statement.lstrip().upper().startswith("UPDATE PATIENT_OFFERS"):
            return
        raced.append(True)
        admin = SessionLocal()
        try:
            coupon = admin.get(PatientOffer, coupon_id)
            coupon.status, coupon.cancelled_at, coupon.cancelled_by = "CANCELLED", now, "admin"
            admin.commit()
        finally:
            admin.close()

    event.listen(engine, "before_cursor_execute", cancel)
    try:
        yield raced
    finally:
        event.remove(engine, "before_cursor_execute", cancel)


def _expired_active_coupon(client, name: str, mobile: str):
    """Register a coupon and push its expiry into the past, leaving it ACTIVE."""
    _register(client, name, mobile)
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).order_by(PatientOffer.id.desc()).first()
        coupon.expires_at = utc_now() - timedelta(seconds=1)
        db.commit()
        return coupon.id, coupon.patient_id, coupon.coupon_uid
    finally:
        db.close()


def test_expiry_sweep_expires_stale_records_and_triggers_one_reminder(client, monkeypatch):
    _register(client, "Expired Sweep", "9000000001")
    _register(client, "Reminder Sweep", "9000000002")
    now = utc_now()
    db = SessionLocal()
    try:
        expired, reminder = db.query(PatientOffer).order_by(PatientOffer.id).all()
        expired.expires_at = now - timedelta(seconds=1)
        reminder.expires_at = now + timedelta(hours=30)
        db.commit()

        calls = []
        monkeypatch.setattr(
            "app.expiry_service.trigger_delivery",
            lambda payload: calls.append(payload) or {"status": "PENDING", "reason": "test"},
        )
        assert run_expiry_sweep(db, now) == {"expired": 1, "reminders_triggered": 1}
        assert db.get(PatientOffer, expired.id).status == "EXPIRED"
        assert db.query(AuditLog).filter_by(coupon_id=expired.id, action="QR_AUTO_EXPIRED").count() == 1
        assert db.query(AuditLog).filter_by(coupon_id=reminder.id, action=REMINDER_ACTION).count() == 1
        assert calls[0]["event"] == "REGISTRATION_EXPIRY_REMINDER"

        assert run_expiry_sweep(db, now) == {"expired": 0, "reminders_triggered": 0}
        assert len(calls) == 1
    finally:
        db.close()


def test_expiry_sweep_never_overwrites_a_coupon_cancelled_mid_sweep(client, monkeypatch):
    """Race: an admin's cancellation commits after the sweep has started but before the
    sweep's own write reaches the row. The cancellation must stand -- the sweep may only
    expire a row that is still ACTIVE at the moment it is updated -- and no
    QR_AUTO_EXPIRED event may be written for a row the sweep did not change."""
    _register(client, "Cancelled Mid Sweep", "9000000005")
    _register(client, "Plainly Expired", "9000000006")
    now = utc_now()
    monkeypatch.setattr("app.expiry_service.trigger_delivery", lambda payload: {"status": "PENDING"})
    db = SessionLocal()
    try:
        cancelled, expired = db.query(PatientOffer).order_by(PatientOffer.id).all()
        cancelled_id, expired_id = cancelled.id, expired.id
        cancelled.expires_at = expired.expires_at = now - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        with _cancelled_just_before_the_next_coupon_write(cancelled_id, now) as raced:
            result = run_expiry_sweep(db, now)
    finally:
        db.close()

    assert raced  # the cancellation really was interleaved with the sweep
    assert result["expired"] == 1
    db = SessionLocal()
    try:
        assert db.get(PatientOffer, cancelled_id).status == "CANCELLED"
        assert db.get(PatientOffer, cancelled_id).cancelled_by == "admin"
        assert db.query(AuditLog).filter_by(coupon_id=cancelled_id, action="QR_AUTO_EXPIRED").count() == 0
        assert db.get(PatientOffer, expired_id).status == "EXPIRED"
        assert db.query(AuditLog).filter_by(coupon_id=expired_id, action="QR_AUTO_EXPIRED").count() == 1
    finally:
        db.close()


def test_expiry_reminder_callback_url_uses_n8n_callback_base_url(client, monkeypatch):
    """Expiry reminders share the same callback-reachability concern as delivery
    dispatch: n8n must be able to reach the CRM, which is not always the same address
    the CRM considers its own public URL (e.g. n8n running in Docker)."""
    monkeypatch.setattr("app.expiry_service.N8N_CALLBACK_BASE_URL", "http://host.docker.internal:8000")
    _register(client, "Reminder Callback URL", "9000000004")
    now = utc_now()
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).one()
        coupon.expires_at = now + timedelta(hours=30)
        db.commit()

        calls = []
        monkeypatch.setattr(
            "app.expiry_service.trigger_delivery",
            lambda payload: calls.append(payload) or {"status": "PENDING", "reason": "test"},
        )
        run_expiry_sweep(db, now)
        assert calls[0]["callback_url"] == "http://host.docker.internal:8000/webhooks/n8n/delivery"
    finally:
        db.close()


def test_validation_rejects_expired_registration_before_a_sweep_runs(client):
    _register(client, "Lazy Expiry", "9000000003")
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).one()
        coupon.expires_at = utc_now() - timedelta(seconds=1)
        token = token_for(coupon.coupon_uid)
        coupon_id = coupon.id
        db.commit()
    finally:
        db.close()

    response = client.post("/validate", data={"token": token})
    assert "OFFER EXPIRED" in response.text
    db = SessionLocal()
    try:
        assert db.get(PatientOffer, coupon_id).status == "EXPIRED"
        assert db.query(AuditLog).filter_by(coupon_id=coupon_id, action="QR_EXPIRED").count() == 1
    finally:
        db.close()


def test_lazy_expiry_never_overwrites_a_coupon_cancelled_by_another_session(client):
    """Race: one request has loaded a stale ACTIVE coupon and is about to mark it EXPIRED
    when another session's cancellation commits. The cancellation must stand, and the
    request must go on to report the state the database actually holds."""
    coupon_id, _patient_id, _uid = _expired_active_coupon(client, "Cancelled Mid Validation", "9000000007")
    now = utc_now()
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        assert coupon.status == "ACTIVE"  # what this request saw when it loaded the coupon
        with _cancelled_just_before_the_next_coupon_write(coupon_id, now) as raced:
            changed = refresh_expiry(db, coupon, now)
        assert raced and changed is False
        assert (coupon.status, coupon.cancelled_by) == ("CANCELLED", "admin")
        assert result_for(coupon)["kind"] == "CANCELLED"
        db.commit()
    finally:
        db.close()

    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        assert (coupon.status, coupon.cancelled_by, coupon.redeemed_at) == ("CANCELLED", "admin", None)
        assert redeem_atomic(db, coupon_id, "staff-one", now) is False
        assert db.get(PatientOffer, coupon_id).status == "CANCELLED"
    finally:
        db.close()


@pytest.mark.parametrize("role", ["admin", "staff"])
@pytest.mark.parametrize("page", ["submit", "result"])
def test_validation_shows_cancelled_when_a_cancel_lands_during_lazy_expiry(client, role, page):
    coupon_id, patient_id, uid = _expired_active_coupon(client, "Cancelled Mid Scan", "9000000008")
    prefix = ""
    if role == "staff":
        from tests.test_staff_interface import _login_as_staff
        _login_as_staff(client)
        prefix = "/staff"

    with _cancelled_just_before_the_next_coupon_write(coupon_id, utc_now()) as raced:
        if page == "submit":
            response = client.post(f"{prefix}/validate", data={"token": uid})
        else:
            response = client.get(f"{prefix}/validate/result/{uid}")
    assert raced
    assert "OFFER CANCELLED" in response.text and "OFFER EXPIRED" not in response.text
    assert "Redeem offer" not in response.text

    client.post(f"/redeem/{coupon_id}" if role == "admin" else f"/staff/patients/{patient_id}/redeem")
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        assert (coupon.status, coupon.redeemed_at) == ("CANCELLED", None)
        # No expiry was recorded for a coupon this request did not expire.
        assert db.query(AuditLog).filter_by(coupon_id=coupon_id, action="QR_EXPIRED").count() == 0
        assert db.query(AuditLog).filter_by(coupon_id=coupon_id, action="QR_REDEEMED").count() == 0
    finally:
        db.close()


def test_lazy_expiry_leaves_an_unexpired_active_coupon_valid_and_redeemable(client):
    _register(client, "Still Valid", "9000000009")
    db = SessionLocal()
    try:
        coupon = db.query(PatientOffer).one()
        assert refresh_expiry(db, coupon, utc_now()) is False
        assert coupon.status == "ACTIVE" and result_for(coupon)["kind"] == "VALID"
        assert redeem_atomic(db, coupon.id, "staff-one", utc_now()) is True
    finally:
        db.close()
