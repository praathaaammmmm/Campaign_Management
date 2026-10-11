import secrets

from fastapi import APIRouter, Request, Header, HTTPException

from sqlalchemy.orm import contains_eager, joinedload

from ..audit_service import audit
from ..auth import require_admin
from ..models import DeliveryLog, PatientOffer
from ..config import N8N_WEBHOOK_LEGACY_CALLBACK_COMPAT, N8N_WEBHOOK_SECRET
from ..reporting.filters import ReportFilters, coupon_conditions, filter_choices, filter_summary
from ..time_utils import utc_now

router = APIRouter()

DELIVERY_FILTERS = ("campaign_id", "offer_id", "channel", "delivery_status", "q", "dates")

SYSTEM_ACTOR = "system:n8n-callback"
CALLBACK_STATUSES = {"SENT", "DELIVERED", "FAILED"}
# Option A: DELIVERED is only ever reported after SENT is persisted. No real provider/n8n
# behavior in this project reports DELIVERED before a SENT confirmation, so DELIVERED is not
# accepted directly from SENDING.
LEGAL_TRANSITIONS = {
    ("SENDING", "SENT"),
    ("SENDING", "FAILED"),
    ("SENT", "DELIVERED"),
    ("SENT", "FAILED"),
}


@router.get("/delivery")
def delivery(request: Request):
    guard = require_admin(request)
    if guard:
        return guard
    filters = ReportFilters.from_request(request)
    db = request.app.state.db()
    try:
        # A delivery attempt has no campaign of its own: it is filtered through its coupon.
        conditions = coupon_conditions(filters, "campaign_id", "offer_id", "q", "dates", date_column=DeliveryLog.sent_at)
        if filters.channel:
            conditions.append(DeliveryLog.channel == filters.channel)
        if filters.delivery_status:
            conditions.append(DeliveryLog.status == filters.delivery_status)
        rows = (
            db.query(DeliveryLog, PatientOffer).join(PatientOffer, DeliveryLog.coupon_id == PatientOffer.id).join(PatientOffer.patient)
            .options(contains_eager(PatientOffer.patient), joinedload(PatientOffer.campaign))
            .filter(*conditions).order_by(DeliveryLog.sent_at.desc(), DeliveryLog.id.desc()).all()
        )
        return request.app.state.templates.TemplateResponse(request, "delivery.html", {
            "request": request, "rows": rows, **filter_choices(db), "filters": filters,
            "filter_fields": DELIVERY_FILTERS, "reset_url": "/delivery", "date_label": "Sent",
            "summary": filter_summary(db, filters, *DELIVERY_FILTERS, date_label="Sent"),
        })
    finally:
        db.close()


@router.post("/webhooks/n8n/delivery")
def n8n_delivery_webhook(request: Request, payload: dict, x_n8n_webhook_secret: str = Header(default="")):
    """Receive delivery status updates from n8n. Never expose this endpoint without a secret.

    Callback authentication is mandatory (X-N8N-Webhook-Secret, constant-time compare).
    Require HTTPS in front of this endpoint in production deployments.
    """
    if not N8N_WEBHOOK_SECRET or not secrets.compare_digest(x_n8n_webhook_secret, N8N_WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Unauthorized")

    idempotency_key = payload.get("idempotency_key")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="idempotency_key is required")

    status = str(payload.get("status", "")).upper()
    if status not in CALLBACK_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid delivery status")

    db = request.app.state.db()
    try:
        log = db.query(DeliveryLog).filter(DeliveryLog.idempotency_key == idempotency_key).first()

        if not log and N8N_WEBHOOK_LEGACY_CALLBACK_COMPAT:
            registration_id = payload.get("registration_id")
            if registration_id:
                log = (
                    db.query(DeliveryLog)
                    .join(PatientOffer, DeliveryLog.coupon_id == PatientOffer.id)
                    .filter(PatientOffer.coupon_uid == registration_id, DeliveryLog.idempotency_key.is_(None))
                    .order_by(DeliveryLog.sent_at.desc())
                    .first()
                )

        if not log:
            raise HTTPException(status_code=404, detail="Delivery record not found")

        if log.status == status:
            return {"status": "ok", "noop": True}

        if (log.status, status) not in LEGAL_TRANSITIONS:
            raise HTTPException(status_code=409, detail=f"Cannot transition delivery from {log.status} to {status}")

        log.status = status
        if payload.get("provider_message_id"):
            log.provider_message_id = payload["provider_message_id"]
        if status == "FAILED":
            log.failure_reason = payload.get("failure_reason")
            if "permanent" in payload:
                log.retryable = not bool(payload["permanent"])
        if status == "DELIVERED":
            log.delivered_at = utc_now()

        coupon = db.get(PatientOffer, log.coupon_id)
        audit(db, SYSTEM_ACTOR, f"{log.channel}_DELIVERY_{status}", log.coupon_id, coupon.patient_id if coupon else None, {
            "idempotency_key": log.idempotency_key, "delivery_intent_key": log.delivery_intent_key,
        })
        db.commit()
        return {"status": "ok"}
    finally:
        db.close()
