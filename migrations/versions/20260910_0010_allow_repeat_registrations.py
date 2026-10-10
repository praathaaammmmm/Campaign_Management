"""allow repeat registrations per mobile; add per-submission idempotency key

Drops the one-registration-per-mobile-per-week unique rule and adds a nullable, uniquely
indexed ``patient_offers.submission_key``. No existing row is rewritten or deleted:
``patients.registration_week`` and its plain index stay, and existing coupons keep a NULL
submission key.

Revision ID: 20260910_0010
Revises: 20260906_0009
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260910_0010"
down_revision: Union[str, None] = "20260906_0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("patients") as batch_op:
        batch_op.drop_constraint("uq_patient_mobile_registration_week", type_="unique")
    # Plain add_column/create_index rather than a batch: patient_offers must never be
    # recreated (delivery_logs/audit_logs hold foreign keys to it).
    op.add_column("patient_offers", sa.Column("submission_key", sa.String(64), nullable=True))
    op.create_index("ux_patient_offers_submission_key", "patient_offers", ["submission_key"], unique=True)


def downgrade() -> None:
    # Restoring the weekly unique rule is only possible while no mobile number has two
    # registrations in the same week. Refuse rather than delete or merge patient records.
    bind = op.get_bind()
    conflicts = bind.execute(sa.text(
        "SELECT COUNT(*) FROM (SELECT mobile FROM patients "
        "GROUP BY mobile, registration_week HAVING COUNT(*) > 1) AS repeats"
    )).scalar()
    if conflicts:
        raise RuntimeError(
            f"Cannot downgrade past 20260910_0010: {conflicts} mobile/week combination(s) have "
            "repeat registrations, which the restored uq_patient_mobile_registration_week rule "
            "would reject. No data was changed. Roll back the application version instead and "
            "leave the schema at this revision."
        )
    op.drop_index("ux_patient_offers_submission_key", table_name="patient_offers")
    with op.batch_alter_table("patient_offers") as batch_op:
        batch_op.drop_column("submission_key")
    with op.batch_alter_table("patients") as batch_op:
        batch_op.create_unique_constraint("uq_patient_mobile_registration_week", ["mobile", "registration_week"])
