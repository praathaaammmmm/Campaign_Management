"""Campaign scope for staff sessions.

Staff may only register patients for, and see the patient records and home figures of,
the campaigns an administrator has assigned to them; admins are never restricted. Those
routes ask this module, with the staff identity taken from the authenticated session,
rather than trusting anything the browser submits.

QR validation, redemption and QR images are the deliberate exception: they are open to
every authenticated staff member regardless of assignment, and do not use this module.
"""
from fastapi import Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import SESSION_STAFF_USER_ID
from .models import PatientOffer, staff_campaigns


def is_staff(request: Request) -> bool:
    return request.session.get("role") == "staff"


def assigned_campaign_ids(db: Session, request: Request) -> set[int]:
    """Campaign ids assigned to the logged-in staff member (empty if none)."""
    return set(db.execute(
        select(staff_campaigns.c.campaign_id)
        .where(staff_campaigns.c.staff_user_id == request.session.get(SESSION_STAFF_USER_ID))
    ).scalars())


def staff_assigned_to(db: Session, request: Request, campaign_id: int | None, *, lock: bool = False) -> bool:
    """Whether the logged-in staff member is assigned to the campaign right now.

    With ``lock`` the assignment row is share-locked (PostgreSQL) until the caller's
    transaction ends, so an administrator removing the assignment at the same moment
    either commits first (this then reports False) or waits for the caller to finish.
    """
    if not campaign_id:
        return False
    query = select(staff_campaigns.c.campaign_id).where(
        staff_campaigns.c.staff_user_id == request.session.get(SESSION_STAFF_USER_ID),
        staff_campaigns.c.campaign_id == campaign_id,
    )
    if lock:
        query = query.with_for_update(read=True)
    return db.execute(query).first() is not None


def coupon_in_scope(db: Session, request: Request, coupon: PatientOffer | None) -> PatientOffer | None:
    """The coupon if this session may see it, else None — so a caller treats a coupon
    from an unassigned campaign exactly like one that does not exist.
    """
    if coupon is None or not is_staff(request):
        return coupon
    return coupon if staff_assigned_to(db, request, coupon.campaign_id) else None
