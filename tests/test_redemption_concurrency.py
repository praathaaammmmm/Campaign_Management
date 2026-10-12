"""True parallel-redemption test (PostgreSQL only).

SQLite serialises writers on one file lock, so it cannot exercise this race; the test runs
only when the suite is pointed at a scratch Postgres database via TEST_DATABASE_URL.

Each worker is a thread with its own session on its own dedicated connection, as each web
request has. Every worker opens its connection first and then parks on a barrier, so all
of them issue the redemption at the same moment against one fresh ACTIVE coupon.
"""
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.database import SessionLocal, engine
from app.models import AuditLog, PatientOffer
from app.qr.service import redeem_atomic
from app.registrations.service import register_patient_offer
from app.time_utils import utc_now

WORKERS = 16
ROUNDS = 50
BARRIER_TIMEOUT_SECONDS = 30

pytestmark = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs real PostgreSQL concurrency; set TEST_DATABASE_URL to a scratch Postgres database",
)


@pytest.fixture()
def worker_engine(client):
    """One brand-new connection per worker session, independent of the app's pool size."""
    workers = create_engine(engine.url, poolclass=NullPool, future=True)
    try:
        yield workers
    finally:
        workers.dispose()


def _fresh_active_coupon(round_number: int) -> int:
    db = SessionLocal()
    try:
        return register_patient_offer(
            db, full_name=f"Parallel Patient {round_number}", mobile=f"98222{round_number:05d}",
            email="", age="", gender="", city="", doctor_name="", campaign_name="",
            campaign_id=1, offer_id=1, beneficiary_category="CGHS", consent_given=True,
            actor="staff-a",
        ).id
    finally:
        db.close()


def _redeem_simultaneously(worker_engine, coupon_id: int):
    """Race WORKERS redemptions of one coupon; return (actor, outcome) per worker, where the
    outcome is redeem_atomic's True/False or the exception the worker died with."""
    barrier = threading.Barrier(WORKERS)

    def worker(index: int):
        actor = f"staff-{index}"
        db = None
        try:
            try:
                db = Session(bind=worker_engine, autoflush=False, future=True)
                db.connection()  # connect before the barrier so the race is on the UPDATE itself
            except BaseException:
                # This worker will never reach the barrier: release the others immediately
                # instead of leaving them parked until the barrier times out.
                barrier.abort()
                raise
            barrier.wait(timeout=BARRIER_TIMEOUT_SECONDS)
            return actor, redeem_atomic(db, coupon_id, actor, utc_now())
        except Exception as exc:  # collected and asserted on by the test
            return actor, exc
        finally:
            if db is not None:
                db.close()

    # Leaving the block joins every worker thread, whatever happened inside any of them.
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return list(pool.map(worker, range(WORKERS)))


def test_parallel_redemptions_of_one_coupon_redeem_and_audit_exactly_once(worker_engine):
    for round_number in range(ROUNDS):
        coupon_id = _fresh_active_coupon(round_number)
        outcomes = _redeem_simultaneously(worker_engine, coupon_id)

        errors = [outcome for _, outcome in outcomes if isinstance(outcome, Exception)]
        winners = [actor for actor, outcome in outcomes if outcome is True]
        losers = [actor for actor, outcome in outcomes if outcome is False]
        assert errors == [], f"round {round_number}"
        assert len(winners) == 1, f"round {round_number}: {winners}"
        assert len(losers) == WORKERS - 1, f"round {round_number}"  # every other attempt loses safely

        db = SessionLocal()
        try:
            coupon = db.get(PatientOffer, coupon_id)
            assert coupon.status == "REDEEMED" and coupon.redeemed_at is not None
            assert coupon.redeemed_by == winners[0]
            [event] = db.query(AuditLog).filter_by(coupon_id=coupon_id, action="QR_REDEEMED").all()
            assert (event.user, event.patient_id) == (winners[0], coupon.patient_id)
        finally:
            db.close()

    db = SessionLocal()
    try:
        assert db.query(PatientOffer).filter_by(status="REDEEMED").count() == ROUNDS
        assert db.query(AuditLog).filter_by(action="QR_REDEEMED").count() == ROUNDS
    finally:
        db.close()


def test_a_worker_that_fails_before_the_barrier_releases_the_others_and_leaks_nothing(worker_engine, monkeypatch):
    """Harness check: one worker dying before the barrier must not hang or leak the rest."""
    coupon_id = _fresh_active_coupon(0)
    real_connection = Session.connection
    first_caller = threading.Lock()

    def connection_failing_once(self, *args, **kwargs):
        if first_caller.acquire(blocking=False):  # never released: only one caller fails
            raise RuntimeError("worker could not connect")
        return real_connection(self, *args, **kwargs)

    monkeypatch.setattr(Session, "connection", connection_failing_once)
    outcomes = _redeem_simultaneously(worker_engine, coupon_id)
    monkeypatch.undo()

    assert all(isinstance(outcome, Exception) for _, outcome in outcomes)  # nobody raced
    # BrokenBarrierError subclasses RuntimeError, so match the failed worker by exact type.
    assert sum(type(outcome) is RuntimeError for _, outcome in outcomes) == 1
    assert sum(isinstance(outcome, threading.BrokenBarrierError) for _, outcome in outcomes) == WORKERS - 1
    db = SessionLocal()
    try:
        assert db.get(PatientOffer, coupon_id).status == "ACTIVE"
        assert db.query(AuditLog).filter_by(action="QR_REDEEMED").count() == 0
        # Every worker connection was closed: only this session is still attached.
        from sqlalchemy import text
        assert db.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
            " AND state <> 'idle'"
        )).scalar_one() == 0
    finally:
        db.close()
