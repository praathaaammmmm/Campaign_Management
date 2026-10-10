import pytest

from app.database import SessionLocal
from app.models import AuditLog, PatientOffer
from app.registrations.service import RecentDuplicateError, register_patient_offer


def _register(db, mobile="9999999977"):
    return register_patient_offer(
        db,
        full_name="Service Test Patient",
        mobile=mobile,
        email="",
        age="",
        gender="",
        city="",
        doctor_name="",
        campaign_name="",
        campaign_id=1,
        offer_id=1,
        beneficiary_category="CGHS",
        consent_given=True,
        actor="admin",
    )


def test_service_creates_legacy_patient_offer_and_audit(client):
    db = SessionLocal()
    try:
        coupon = _register(db)
        assert coupon.id is not None
        assert db.query(PatientOffer).count() == 1
        assert db.query(AuditLog).filter(AuditLog.action == "PATIENT_REGISTERED").count() == 1
    finally:
        db.close()


def test_service_asks_before_repeating_a_recent_registration(client):
    db = SessionLocal()
    try:
        first = _register(db, mobile="9999999976")
        with pytest.raises(RecentDuplicateError) as raised:
            _register(db, mobile="9999999976")
        assert raised.value.recent.coupon_uid == first.coupon_uid
        assert raised.value.recent.issued_by == "admin"
        assert db.query(PatientOffer).count() == 1
    finally:
        db.close()