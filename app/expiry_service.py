"""Scheduled expiry maintenance for QR registrations.

Run by ``python -m app.expiry_job`` from one external scheduler/worker only.
The validation and redemption paths remain the authority at request time.
"""
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .audit_service import audit
from .config import N8N_CALLBACK_BASE_URL
from .models import AuditLog, PatientOffer
from .notifications.n8n import trigger_delivery
from .notifications.service import CALLBACK_PATH, new_key
from .time_utils import utc_now

SYSTEM_ACTOR = "system:expiry-sweep"
REMINDER_ACTION = "EXPIRY_REMINDER_TRIGGERED"


def _reminder_already_triggered(db: Session, coupon_id: int) -> bool:
    return db.execute(
        select(AuditLog.id).where(
            AuditLog.coupon_id == coupon_id,
            AuditLog.action == REMINDER_ACTION,
        ).limit(1)
    ).scalar_one_or_none() is not None


def _reminder_payload(coupon: PatientOffer) -> dict:
    patient = coupon.patient
    return {
        "event": "REGISTRATION_EXPIRY_REMINDER",
        "idempotency_key": new_key("dlv"),
        "delivery_intent_key": new_key("int"),
        "registration_id": coupon.coupon_uid,
        "patient": {
            "name": patient.full_name,
            "email": patient.email,
            "phone": patient.mobile,
        },
        "service": coupon.offer.name,
        "campaign": coupon.campaign.name if coupon.campaign else patient.campaign_name,
        "expires_at": coupon.expires_at.isoformat(),
        "callback_url": f"{N8N_CALLBACK_BASE_URL}{CALLBACK_PATH}",
    }


def run_expiry_sweep(db: Session, now: datetime | None = None) -> dict[str, int]:
    """Expire stale active offers and send one reminder for offers 24–48h out.

    The caller owns the database session. Each expiry and its audit record are
    committed together, before any reminder webhook is called; reminder audit
    records are committed after the existing n8n webhook boundary.
    It is deliberately suitable for one externally scheduled worker, avoiding
    in-process schedulers that would duplicate work across web replicas.
    """
    now = now or utc_now()
    # Conditional UPDATE: only a row that is still ACTIVE when the statement reaches it is
    # changed, so a coupon cancelled or redeemed in the meantime is never overwritten, and
    # only rows this statement actually changed are audited. Committed before the reminder
    # webhooks below so no coupon row stays locked across a network call.
    try:
        expired = db.execute(
            update(PatientOffer)
            .where(
                PatientOffer.status == "ACTIVE",
                PatientOffer.expires_at <= now,
            )
            .values(status="EXPIRED")
            .returning(PatientOffer.id, PatientOffer.patient_id, PatientOffer.expires_at)
        ).all()
        for coupon in expired:
            audit(db, SYSTEM_ACTOR, "QR_AUTO_EXPIRED", coupon.id, coupon.patient_id, {
                "expires_at": coupon.expires_at.isoformat(),
            })
        db.commit()
    except Exception:
        db.rollback()
        raise

    reminder_start = now + timedelta(hours=24)
    reminder_end = now + timedelta(hours=48)
    reminder_candidates = db.execute(
        select(PatientOffer).where(
            PatientOffer.status == "ACTIVE",
            PatientOffer.expires_at >= reminder_start,
            PatientOffer.expires_at <= reminder_end,
        )
    ).scalars().all()
    reminders_triggered = 0
    for coupon in reminder_candidates:
        if _reminder_already_triggered(db, coupon.id):
            continue
        result = trigger_delivery(_reminder_payload(coupon))
        audit(db, SYSTEM_ACTOR, REMINDER_ACTION, coupon.id, coupon.patient_id, {
            "delivery_status": result["status"],
            "workflow_id": result.get("workflow_id"),
            "failure_reason": result.get("reason"),
        })
        reminders_triggered += 1

    db.commit()
    return {"expired": len(expired), "reminders_triggered": reminders_triggered}
