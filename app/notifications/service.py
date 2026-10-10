"""Durable, outbox-backed QR delivery pipeline.

FastAPI owns registration state, QR validity, DeliveryLog records, delivery state
transitions, retry logic, and audit trail. n8n owns actual email/WhatsApp provider
orchestration. No function in this module ever calls n8n from a request path — only
the dispatcher (``app/delivery_job.py``) makes outbound HTTP calls, so a registration
or manual resend is never blocked or lost when n8n is slow or unavailable.
"""
import base64
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..audit_service import audit
from ..config import (
    DELIVERY_STALE_SENDING_SECONDS, HOSPITAL_NAME, N8N_CALLBACK_BASE_URL,
    N8N_DELIVERY_MAX_RETRIES, N8N_WEBHOOK_URL,
)
from ..models import DeliveryLog, Patient, PatientOffer
from .n8n import trigger_delivery
from ..qr.service import qr_png_bytes
from ..time_utils import utc_now

CALLBACK_PATH = "/webhooks/n8n/delivery"

# Gap before attempt 2, then before attempt 3. No attempt 4 (N8N_DELIVERY_MAX_RETRIES caps it).
RETRY_BACKOFF = [timedelta(minutes=2), timedelta(minutes=10)]


def new_key(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def _create_attempt(db: Session, *, coupon: PatientOffer, channel: str, recipient: str,
                     delivery_intent_key: str, attempt_number: int) -> DeliveryLog:
    row = DeliveryLog(
        coupon_id=coupon.id, channel=channel, recipient=recipient, status="PREPARED",
        delivery_intent_key=delivery_intent_key, idempotency_key=new_key("dlv"),
        attempt_number=attempt_number, retryable=True,
    )
    db.add(row)
    return row


def queue_registration_deliveries(db: Session, patient: Patient, coupon: PatientOffer, actor: str) -> list[DeliveryLog]:
    """Create durable PREPARED delivery intents for a new registration.

    One intent per available contact channel: email and/or WhatsApp (mobile). A missing
    channel is simply skipped, never an error. This only writes local rows — no network
    call happens here, so it cannot block or roll back registration.
    """
    channels = []
    if patient.email:
        channels.append(("EMAIL", patient.email))
    if patient.mobile:
        channels.append(("WHATSAPP", patient.mobile))

    created = [
        _create_attempt(
            db, coupon=coupon, channel=channel, recipient=recipient,
            delivery_intent_key=new_key("int"), attempt_number=1,
        )
        for channel, recipient in channels
    ]
    if created:
        db.flush()
        for row in created:
            audit(db, actor, f"{row.channel}_DELIVERY_QUEUED", coupon.id, patient.id, {
                "delivery_intent_key": row.delivery_intent_key,
            })
    return created


def queue_manual_resend(db: Session, patient: Patient, coupon: PatientOffer, channel: str, actor: str) -> DeliveryLog:
    """Queue a manual resend as a brand-new delivery intent — never a retry of an earlier intent."""
    recipient = patient.email if channel == "EMAIL" else patient.mobile
    if not recipient:
        raise ValueError("No email address" if channel == "EMAIL" else "No phone number")
    row = _create_attempt(
        db, coupon=coupon, channel=channel, recipient=recipient,
        delivery_intent_key=new_key("int"), attempt_number=1,
    )
    db.flush()
    audit(db, actor, f"{channel}_DELIVERY_QUEUED", coupon.id, patient.id, {
        "delivery_intent_key": row.delivery_intent_key, "manual": True,
    })
    db.commit()
    return row


def send_qr_delivery(db: Session, patient: Patient, coupon: PatientOffer, channel: str, actor: str) -> dict:
    """Manual resend entry point used by the staff/admin UI. Queues, never sends inline."""
    queue_manual_resend(db, patient, coupon, channel, actor)
    return {"status": "QUEUED"}


def _payload_for(db: Session, row: DeliveryLog) -> dict:
    coupon = db.get(PatientOffer, row.coupon_id)
    patient = coupon.patient
    # Generated on demand, never read from a per-registration file: the dispatcher may run
    # in a different container from the web app, with no shared disk.
    try:
        qr_b64 = base64.b64encode(qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)).decode("ascii")
    except FileNotFoundError:
        qr_b64 = None
    event = "REGISTRATION_QR_DELIVERY" if row.attempt_number == 1 else "REGISTRATION_QR_DELIVERY_RETRY"
    return {
        "event": event,
        "idempotency_key": row.idempotency_key,
        "delivery_intent_key": row.delivery_intent_key,
        "channel": row.channel,
        "hospital": HOSPITAL_NAME,
        "registration_id": coupon.coupon_uid,
        "recipient": row.recipient,
        "patient_name": patient.full_name,
        "service": coupon.offer.name,
        "campaign": coupon.campaign.name if coupon.campaign else patient.campaign_name,
        "expires_at": coupon.expires_at.isoformat(),
        "qr_base64_png": qr_b64,
        "callback_url": f"{N8N_CALLBACK_BASE_URL}{CALLBACK_PATH}",
    }


def _recover_stale_sending(db: Session, now: datetime) -> int:
    cutoff = now - timedelta(seconds=DELIVERY_STALE_SENDING_SECONDS)
    stale_ids = db.execute(
        select(DeliveryLog.id).where(DeliveryLog.status == "SENDING", DeliveryLog.dispatched_at < cutoff)
    ).scalars().all()
    for row_id in stale_ids:
        db.execute(
            update(DeliveryLog)
            .where(DeliveryLog.id == row_id, DeliveryLog.status == "SENDING")
            .values(status="FAILED", failure_reason="stale dispatch: worker did not report a result within the timeout")
        )
    if stale_ids:
        db.commit()
    return len(stale_ids)


def _claim_prepared_rows(db: Session, now: datetime) -> list[int]:
    """Atomically claim PREPARED rows via a conditional UPDATE. Only one dispatcher can win each row."""
    candidate_ids = db.execute(select(DeliveryLog.id).where(DeliveryLog.status == "PREPARED")).scalars().all()
    claimed = []
    for row_id in candidate_ids:
        result = db.execute(
            update(DeliveryLog)
            .where(DeliveryLog.id == row_id, DeliveryLog.status == "PREPARED")
            .values(status="SENDING", dispatched_at=now)
        )
        db.commit()
        if result.rowcount == 1:
            claimed.append(row_id)
    return claimed


def _finish_sending_attempt(db: Session, row_id: int, status: str, extra: dict) -> bool:
    """Persist a dispatch outcome only if the row is still SENDING.

    A callback from n8n can race ahead of the dispatcher's own HTTP response handling
    and already mark the row SENT/DELIVERED/FAILED. In that case this update matches
    zero rows and is a safe no-op — the callback's result is preserved, never overwritten.
    """
    result = db.execute(
        update(DeliveryLog)
        .where(DeliveryLog.id == row_id, DeliveryLog.status == "SENDING")
        .values(status=status, **extra)
    )
    db.commit()
    return result.rowcount == 1


def dispatch_pending_deliveries(db: Session, now: datetime | None = None) -> dict:
    """One dispatcher tick: recover stale SENDING rows, claim PREPARED rows, send them.

    Safe to run every minute from an external scheduler (``app/delivery_job.py``).
    Every visible PREPARED row is always claimed and attempted, regardless of whether
    n8n is configured — ``trigger_delivery`` reports a clear "N8N_WEBHOOK_URL is not
    configured" failure per row rather than the dispatcher silently skipping rows, so a
    missing/misconfigured n8n URL is never mistaken for "nothing to deliver". Since the
    resulting FAILED rows stay retryable, fixing the configuration self-heals them on the
    next retry tick without manual intervention.
    """
    now = now or utc_now()
    stale_recovered = _recover_stale_sending(db, now)

    claimed_ids = _claim_prepared_rows(db, now)
    sent = failed = 0
    for row_id in claimed_ids:
        row = db.get(DeliveryLog, row_id)
        if row is None or row.status != "SENDING":
            continue  # already resolved by a racing callback; nothing left to do
        payload = _payload_for(db, row)
        result = trigger_delivery(payload)
        if result["status"] == "SENT":
            updated = _finish_sending_attempt(db, row_id, "SENT", {"n8n_workflow_id": result.get("workflow_id")})
            if updated:
                sent += 1
        else:
            reason = (result.get("reason") or "delivery could not be dispatched")[:255]
            updated = _finish_sending_attempt(db, row_id, "FAILED", {"failure_reason": reason})
            if updated:
                failed += 1
    return {"claimed": len(claimed_ids), "sent": sent, "failed": failed, "stale_recovered": stale_recovered}


def maybe_retry_failed_intents(db: Session, now: datetime | None = None) -> int:
    """Create a new PREPARED attempt for any intent whose latest attempt is a retryable FAILED,
    under the retry cap and past its backoff window. Retry is only ever a new row, never a
    mutation of the old one.
    """
    now = now or utc_now()
    max_attempt = (
        select(DeliveryLog.delivery_intent_key, func.max(DeliveryLog.attempt_number).label("max_attempt"))
        .group_by(DeliveryLog.delivery_intent_key)
        .subquery()
    )
    latest_failed = db.execute(
        select(DeliveryLog)
        .join(
            max_attempt,
            (DeliveryLog.delivery_intent_key == max_attempt.c.delivery_intent_key)
            & (DeliveryLog.attempt_number == max_attempt.c.max_attempt),
        )
        .where(DeliveryLog.status == "FAILED", DeliveryLog.retryable == True)
    ).scalars().all()

    created = 0
    for row in latest_failed:
        if row.attempt_number >= N8N_DELIVERY_MAX_RETRIES:
            continue
        backoff_index = row.attempt_number - 1
        backoff = RETRY_BACKOFF[backoff_index] if backoff_index < len(RETRY_BACKOFF) else RETRY_BACKOFF[-1]
        if now < row.sent_at + backoff:
            continue
        try:
            db.add(DeliveryLog(
                coupon_id=row.coupon_id, channel=row.channel, recipient=row.recipient, status="PREPARED",
                delivery_intent_key=row.delivery_intent_key, idempotency_key=new_key("dlv"),
                attempt_number=row.attempt_number + 1, retryable=True,
            ))
            db.commit()
            created += 1
        except IntegrityError:
            # UNIQUE(delivery_intent_key, attempt_number) was already claimed by another worker
            # for this same retry slot. Roll back; the next tick re-evaluates from current state.
            db.rollback()
    return created
