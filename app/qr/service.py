"""QR credential lifecycle: issue, look up, validate, expire, and redeem.

Merged from the former ``app/qr_service.py`` (token/image issuance) and
``app/coupon_service.py`` (lookup/expiry/redemption) — both halves of one domain (the QR
credential itself), previously split across two flat top-level modules for no structural
reason. A QR token remains an opaque bearer credential: no patient PII is ever encoded
in it (see ``qr_payload``).
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta
from io import BytesIO

import qrcode
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..audit_service import audit
from ..config import QR_DIR, QR_SIGNING_KEY
from ..models import PatientOffer

# Result kinds whose screen shows the patient's details. An expired or cancelled QR shows
# its status and the offer it was for, but no patient details; an invalid one shows nothing.
PATIENT_DETAIL_KINDS = frozenset({"VALID", "JUST_REDEEMED", "REDEEMED"})


def token_for(coupon_uid: str) -> str:
    signature = hmac.new(QR_SIGNING_KEY.encode(), coupon_uid.encode(), hashlib.sha256).hexdigest()
    return f"SRD-{coupon_uid}.{signature}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.strip().encode()).hexdigest()


def new_uid(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(4).upper()}"


def expiry_for(created_at: datetime) -> datetime:
    return created_at + timedelta(days=10)


def qr_payload(token: str) -> str:
    # Only the opaque secure token is encoded. No patient information is embedded.
    return token


def qr_png_bytes(coupon_uid: str, expected_hash: str) -> bytes:
    """Build a coupon's QR PNG in memory. The database row is the record; the image is
    derived from the coupon ID and ``QR_SIGNING_KEY`` on demand and never stored.

    The regenerated token must hash to the stored ``secure_token_hash`` -- otherwise this
    is a legacy coupon (issued with a random token before hashed tokens) or the signing
    key has changed, and a regenerated image would be a QR that fails validation. For
    those, a PNG already present in ``QR_DIR`` is served read-only; with none, the
    caller gets ``FileNotFoundError`` and shows its "QR unavailable" state.
    """
    token = token_for(coupon_uid)
    if hmac.compare_digest(token_hash(token), expected_hash):
        buffer = BytesIO()
        qrcode.make(qr_payload(token)).save(buffer, format="PNG")
        return buffer.getvalue()
    legacy_path = QR_DIR / f"{coupon_uid}.png"
    if legacy_path.is_file():
        return legacy_path.read_bytes()
    raise FileNotFoundError("Legacy QR cannot be regenerated; issue a replacement coupon")


def find_coupon(db: Session, token: str) -> PatientOffer | None:
    """Look up a coupon by either its human-visible ID (``SRD-XXXXXXXX``) or its raw
    secure token. Shared by every validate/redeem entry point (admin and staff) so the
    lookup rule can never drift between them.
    """
    value = token.strip()
    if re.fullmatch(r"SRD-[A-F0-9]{8}", value, re.IGNORECASE):
        coupon = db.execute(
            select(PatientOffer).where(PatientOffer.coupon_uid == value.upper())
        ).scalar_one_or_none()
        if coupon and coupon.secure_token_hash == token_hash(token_for(coupon.coupon_uid)):
            return coupon
        return None
    return db.execute(
        select(PatientOffer).where(PatientOffer.secure_token_hash == token_hash(value))
    ).scalar_one_or_none()


def result_for(coupon: PatientOffer) -> dict:
    """Classify a coupon's current state for the validate/redeem result screen."""
    if coupon.status == "REDEEMED": return {"kind": "REDEEMED", "coupon": coupon}
    if coupon.status == "CANCELLED": return {"kind": "CANCELLED", "coupon": coupon}
    if coupon.status == "EXPIRED": return {"kind": "EXPIRED", "coupon": coupon}
    return {"kind": "VALID", "coupon": coupon}


def record_detail_view(db: Session, request, result: dict) -> bool:
    """Audit that an authenticated user was shown a patient's details through a QR.

    Written once per rendered result that actually displays patient details (a valid or
    already-redeemed QR) — never for an unknown/malformed entry, and never for an expired
    or cancelled QR, whose screen shows no patient details. The caller commits. The viewer
    is the logged-in session, never anything the browser submitted.
    """
    coupon = result.get("coupon")
    if coupon is None or result["kind"] not in PATIENT_DETAIL_KINDS:
        return False
    audit(db, request.session.get("user", "admin"), "QR_DETAILS_VIEWED", coupon.id, coupon.patient_id, {
        "role": request.session.get("role", "admin"), "coupon_uid": coupon.coupon_uid,
        "campaign_id": coupon.campaign_id, "status": result["kind"],
    })
    return True


def refresh_expiry(db: Session, coupon: PatientOffer, now: datetime) -> bool:
    """Request-time (lazy) expiry: mark a stale ACTIVE coupon EXPIRED. The caller commits.

    The change is a conditional UPDATE, not a write of the loaded object, so it only lands
    if the row is still ACTIVE at that moment. A coupon cancelled or redeemed by another
    request in the meantime is left exactly as it is; either way ``coupon`` is reloaded,
    so the caller classifies the state the database actually holds. Returns True only
    when this call changed the row.
    """
    if coupon.status != "ACTIVE" or now < coupon.expires_at:
        return False
    changed = db.execute(
        update(PatientOffer)
        .where(
            PatientOffer.id == coupon.id,
            PatientOffer.status == "ACTIVE",
            PatientOffer.expires_at <= now,
        )
        .values(status="EXPIRED")
        .execution_options(synchronize_session=False)
    ).rowcount == 1
    db.refresh(coupon)
    return changed


def redeem_atomic(db: Session, coupon_id: int, redeemed_by: str, now: datetime) -> bool:
    """Redeem a coupon and audit it as one transaction. Shared by the admin and staff
    redemption routes, so the rule can never drift between them.

    A conditional UPDATE makes ACTIVE -> REDEEMED a single atomic state transition: of any
    number of simultaneous attempts on one unexpired coupon, exactly one changes the row.
    Only that winner writes the ``QR_REDEEMED`` audit event, in the same transaction, and
    commits once -- the redemption and its audit row are saved together or not at all. A
    losing or repeated attempt changes nothing, writes no audit event and returns False.
    """
    try:
        redeemed = db.execute(
            update(PatientOffer)
            .where(
                PatientOffer.id == coupon_id,
                PatientOffer.status == "ACTIVE",
                PatientOffer.expires_at > now,
            )
            .values(status="REDEEMED", redeemed_at=now, redeemed_by=redeemed_by)
            .returning(PatientOffer.patient_id)
        ).first()
        if redeemed is None:
            db.rollback()
            return False
        audit(db, redeemed_by, "QR_REDEEMED", coupon_id, redeemed.patient_id)
        db.commit()
        return True
    except Exception:
        db.rollback()
        raise
