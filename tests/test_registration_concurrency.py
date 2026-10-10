"""Concurrency regression tests for registration.

Each worker uses its own database session, as each web request does. These run on the
default SQLite test database; the same-patient race additionally needs PostgreSQL's
advisory lock, so it only runs when the suite is pointed at Postgres via
TEST_DATABASE_URL.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.database import SessionLocal, engine
from app.models import AuditLog, DeliveryLog, Patient, PatientOffer
from app.registrations.service import RecentDuplicateError, register_patient_offer

WORKERS = 8


def _run_concurrently(calls):
    """Run every call at the same moment; return (results, errors) in call order."""
    barrier = threading.Barrier(len(calls))

    def worker(call):
        db = SessionLocal()
        try:
            barrier.wait(timeout=30)
            coupon = call(db)
            return coupon.id, None
        except Exception as exc:  # collected and asserted on by the test
            return None, exc
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=len(calls)) as pool:
        outcomes = list(pool.map(worker, calls))
    return [value for value, _ in outcomes], [error for _, error in outcomes if error]


def _call(mobile, *, submission_key=None, name="Concurrent Patient", actor="staff-a"):
    def call(db):
        return register_patient_offer(
            db, full_name=name, mobile=mobile, email="", age="", gender="", city="",
            doctor_name="", campaign_name="", campaign_id=1, offer_id=1,
            beneficiary_category="CGHS", consent_given=True, actor=actor,
            submission_key=submission_key,
        )
    return call


def _count(model, **filters):
    db = SessionLocal()
    try:
        return db.query(model).filter_by(**filters).count()
    finally:
        db.close()


def test_concurrent_registrations_of_different_patients_all_succeed(client):
    calls = [
        _call(f"98111000{index:02d}", submission_key=f"different-patient-token-{index:04d}", actor=f"staff-{index}")
        for index in range(WORKERS)
    ]
    coupon_ids, errors = _run_concurrently(calls)

    assert errors == []
    assert len(set(coupon_ids)) == WORKERS
    assert _count(Patient) == WORKERS and _count(PatientOffer) == WORKERS
    assert _count(AuditLog, action="PATIENT_REGISTERED") == WORKERS
    assert _count(DeliveryLog) == WORKERS  # one WhatsApp intent each, none lost or doubled


def test_concurrent_replays_of_one_submission_create_one_registration(client):
    calls = [_call("9811100099", submission_key="one-submission-token-0001") for _ in range(WORKERS)]
    coupon_ids, errors = _run_concurrently(calls)

    assert errors == []
    assert len(set(coupon_ids)) == 1  # every request is answered with the same registration
    assert _count(PatientOffer) == 1 and _count(Patient) == 1
    assert _count(AuditLog, action="PATIENT_REGISTERED") == 1
    assert _count(DeliveryLog) == 1


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs PostgreSQL advisory locks; set TEST_DATABASE_URL to a scratch Postgres database",
)
def test_concurrent_same_patient_submissions_issue_one_qr_and_ask_the_rest(client):
    calls = [
        _call("9811100098", submission_key=f"same-patient-token-{index:04d}", actor=f"staff-{index}")
        for index in range(WORKERS)
    ]
    coupon_ids, errors = _run_concurrently(calls)

    created = [coupon_id for coupon_id in coupon_ids if coupon_id is not None]
    assert len(created) == 1
    assert len(errors) == WORKERS - 1
    assert all(isinstance(error, RecentDuplicateError) for error in errors)
    assert _count(PatientOffer) == 1 and _count(Patient) == 1


@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs PostgreSQL advisory locks; set TEST_DATABASE_URL to a scratch Postgres database",
)
def test_same_patient_lock_does_not_block_other_patients(client):
    """While one transaction holds the lock for a patient, a different patient registers."""
    from sqlalchemy import text

    holder = SessionLocal()
    try:
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:slot, 0))"),
            {"slot": "registration:9811100097:1:1"},
        )
        coupon_ids, errors = _run_concurrently([_call("9811100096")])
        assert errors == [] and coupon_ids[0] is not None
    finally:
        holder.rollback()
        holder.close()
