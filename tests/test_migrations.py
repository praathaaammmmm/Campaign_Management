"""Regression coverage for the Alembic migration chain itself.

The rest of the suite builds its schema with `Base.metadata.create_all`, which never
exercises the migration files, so a broken migration (wrong batch mode, a dialect-specific
literal, etc.) can ship unnoticed. These tests actually invoke `alembic upgrade head` /
`downgrade base` in a subprocess against scratch databases to catch that class of bug.

The SQLite run is unconditional. A Postgres run only happens when TEST_POSTGRES_URL is set
(e.g. `postgresql+psycopg://postgres:test@localhost:55432/migtest`) since most local/dev
environments don't have a Postgres server available — this is where the real bug this file
guards against (a batch `recreate="always"` trying to drop `patient_offers_pkey` while
`delivery_logs`/`audit_logs` foreign keys still reference it) actually reproduces; SQLite's
batch mode never hits it.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _alembic(*args, database_url):
    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result


def _run_alembic(*args, database_url):
    result = _alembic(*args, database_url=database_url)
    assert result.returncode == 0, (
        f"alembic {' '.join(args)} failed against {database_url!r}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    return result


def _insert_registration(connection, suffix, mobile, week="2026-09-06"):
    """Insert one patient + coupon with raw SQL valid on both the 0009 and 0010 schemas."""
    connection.execute(sa.text(
        "INSERT INTO patients (patient_uid, full_name, mobile, registration_week, consent_given, "
        "consent_version, consented_at, created_at) VALUES (:uid, :name, :mobile, :week, :consent, "
        "'2026-09-02', '2026-09-07 10:00:00', '2026-09-07 10:00:00')"
    ), {"uid": f"PAT-{suffix}", "name": f"Patient {suffix}", "mobile": mobile, "week": week, "consent": True})
    patient_id = connection.execute(sa.text("SELECT id FROM patients WHERE patient_uid=:uid"), {"uid": f"PAT-{suffix}"}).scalar()
    connection.execute(sa.text(
        "INSERT INTO patient_offers (coupon_uid, patient_id, offer_id, secure_token_hash, created_at, "
        "expires_at, status, beneficiary_category) VALUES (:uid, :patient_id, :offer_id, :hash, "
        "'2026-09-07 10:00:00', '2026-09-17 10:00:00', 'ACTIVE', 'CGHS')"
    ), {"uid": f"SRD-{suffix}", "patient_id": patient_id, "offer_id": connection.execute(sa.text("SELECT MIN(id) FROM offers")).scalar(), "hash": suffix * 8})


def _snapshot(connection):
    return {
        "patients": [tuple(row) for row in connection.execute(sa.text(
            "SELECT id, patient_uid, full_name, mobile, registration_week, consent_version FROM patients ORDER BY id"))],
        "coupons": [tuple(row) for row in connection.execute(sa.text(
            "SELECT id, coupon_uid, patient_id, offer_id, secure_token_hash, status, expires_at FROM patient_offers ORDER BY id"))],
    }


def _check_repeat_registration_migration(database_url):
    """0010 must keep every existing row, allow same-week repeats, and refuse a downgrade
    that would have to throw repeat registrations away."""
    _run_alembic("upgrade", "20260906_0009", database_url=database_url)
    engine = sa.create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("INSERT INTO offers (name, description, active) VALUES ('Migration Offer', 'x', :active)"), {"active": True})
            _insert_registration(connection, "AAAAAAAA", "9000000001")
            _insert_registration(connection, "BBBBBBBB", "9000000002")
            before = _snapshot(connection)
        # The weekly rule is real before the upgrade.
        with pytest.raises(sa.exc.IntegrityError):
            with engine.begin() as connection:
                _insert_registration(connection, "CCCCCCCC", "9000000001")

        _run_alembic("upgrade", "head", database_url=database_url)

        inspector = sa.inspect(engine)
        with engine.begin() as connection:
            assert _snapshot(connection) == before
            assert connection.execute(sa.text("SELECT COUNT(*) FROM patient_offers WHERE submission_key IS NOT NULL")).scalar() == 0
        assert "uq_patient_mobile_registration_week" not in {item["name"] for item in inspector.get_unique_constraints("patients")}
        patient_indexes = {item["name"]: item for item in inspector.get_indexes("patients")}
        assert not patient_indexes["ix_patients_registration_week"]["unique"]
        coupon_indexes = {item["name"]: item for item in inspector.get_indexes("patient_offers")}
        assert coupon_indexes["ux_patient_offers_submission_key"]["unique"]
        column = {item["name"]: item for item in inspector.get_columns("patient_offers")}["submission_key"]
        assert column["nullable"] and column["type"].length == 64

        # No repeats yet: the downgrade is clean and the upgrade can be re-applied.
        _run_alembic("downgrade", "20260906_0009", database_url=database_url)
        _run_alembic("upgrade", "head", database_url=database_url)

        with engine.begin() as connection:
            assert _snapshot(connection) == before
            _insert_registration(connection, "CCCCCCCC", "9000000001")  # same mobile, same week
            with_repeat = _snapshot(connection)

        refused = _alembic("downgrade", "20260906_0009", database_url=database_url)
        assert refused.returncode != 0
        assert "Cannot downgrade past 20260910_0010" in refused.stderr
        with engine.begin() as connection:
            assert _snapshot(connection) == with_repeat
            assert connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar() == "20260910_0010"
            connection.execute(sa.text("DELETE FROM patient_offers"))
            connection.execute(sa.text("DELETE FROM patients"))
            connection.execute(sa.text("DELETE FROM offers WHERE name = 'Migration Offer'"))
    finally:
        engine.dispose()
    _run_alembic("downgrade", "base", database_url=database_url)


def test_repeat_registration_migration_preserves_data_on_sqlite(tmp_path):
    _check_repeat_registration_migration(f"sqlite:///{tmp_path / 'repeat_migration_test.db'}")


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="set TEST_POSTGRES_URL to a scratch Postgres database to run this check",
)
def test_repeat_registration_migration_preserves_data_on_postgres():
    _check_repeat_registration_migration(os.environ["TEST_POSTGRES_URL"])


def test_full_migration_chain_upgrades_and_downgrades_on_sqlite(tmp_path):
    db_path = tmp_path / "migration_chain_test.db"
    database_url = f"sqlite:///{db_path}"
    _run_alembic("upgrade", "head", database_url=database_url)
    _run_alembic("downgrade", "base", database_url=database_url)


@pytest.mark.skipif(
    not os.getenv("TEST_POSTGRES_URL"),
    reason="set TEST_POSTGRES_URL to a scratch Postgres database to run this check",
)
def test_full_migration_chain_upgrades_and_downgrades_on_postgres():
    database_url = os.environ["TEST_POSTGRES_URL"]
    _run_alembic("upgrade", "head", database_url=database_url)
    _run_alembic("downgrade", "base", database_url=database_url)
