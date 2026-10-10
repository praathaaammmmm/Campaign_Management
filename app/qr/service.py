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

from ..config import QR_DIR, QR_SIGNING_KEY
from ..models import PatientOffer


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


def refresh_expiry(coupon: PatientOffer, now: datetime) -> bool:
    if coupon.status == "ACTIVE" and now >= coupon.expires_at:
        coupon.status = "EXPIRED"
        return True
    return False


def redeem_atomic(db: Session, coupon_id: int, redeemed_by: str, now: datetime):
    # Conditional UPDATE makes ACTIVE -> REDEEMED a single atomic state transition.
    stmt = (
        update(PatientOffer)
        .where(
            PatientOffer.id == coupon_id,
            PatientOffer.status == "ACTIVE",
            PatientOffer.expires_at > now,
        )
        .values(status="REDEEMED", redeemed_at=now, redeemed_by=redeemed_by)
    )
    result = db.execute(stmt)
    db.commit()
    return result.rowcount == 1
