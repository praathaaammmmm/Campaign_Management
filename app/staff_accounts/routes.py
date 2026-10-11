from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from ..audit_service import audit
from ..auth import password_hasher, require_admin
from ..models import Campaign, StaffUser, staff_campaigns
from ..query_params import optional_int
from ..security import require_csrf
from ..time_utils import utc_now

router = APIRouter(prefix="/admin")


@router.get("/staff")
def staff_list(request: Request):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        rows = db.query(StaffUser).options(selectinload(StaffUser.campaigns)).filter(StaffUser.removed_at.is_(None)).order_by(StaffUser.username).all()
        # Only campaigns that can still take work are offered for new assignments; a closed
        # campaign stays visible on staff who already have it.
        assignable = db.query(Campaign).filter(Campaign.status != "CLOSED").order_by(Campaign.name).all()
        restore_id = optional_int(request.query_params.get("restore_id"))
        restore_candidate = None
        if restore_id:
            candidate = db.get(StaffUser, restore_id)
            if candidate and candidate.removed_at is not None and candidate.role == "staff":
                restore_candidate = candidate
        return request.app.state.templates.TemplateResponse(request, "staff.html", {
            "request": request, "rows": rows, "restore_candidate": restore_candidate, "assignable_campaigns": assignable,
        })
    finally: db.close()


@router.post("/staff")
def create_staff(request: Request, username: str = Form(...), password: str = Form(...), _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        clean_username = username.strip()
        existing = db.query(StaffUser).filter(StaffUser.username == clean_username).first()
        if existing and existing.removed_at is not None:
            return RedirectResponse(
                f"/admin/staff?message=That username belongs to a removed staff account. Restore it below instead of creating a new one.&restore_id={existing.id}",
                status_code=303,
            )
        if existing:
            return RedirectResponse("/admin/staff?message=That staff username is already in use", status_code=303)
        user = StaffUser(username=clean_username, password_hash=password_hasher.hash(password), role="staff", active=True)
        db.add(user)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return RedirectResponse("/admin/staff?message=That staff username is already in use", status_code=303)
        return RedirectResponse("/admin/staff?message=Staff account created", status_code=303)
    finally: db.close()


@router.post("/staff/{staff_id}/toggle")
def toggle_staff(request: Request, staff_id: int, _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        user = db.get(StaffUser, staff_id)
        if not user or user.removed_at:
            return RedirectResponse("/admin/staff?message=Staff account not found", status_code=303)
        user.active = not user.active
        audit(db, request.session.get("user", "admin"), "STAFF_ACCOUNT_UPDATED", details={"username": user.username, "active": user.active})
        db.commit()
        return RedirectResponse("/admin/staff?message=Staff account updated", status_code=303)
    finally: db.close()


@router.post("/staff/{staff_id}/remove")
def remove_staff(request: Request, staff_id: int, _csrf: None = Depends(require_csrf)):
    """Safe removal: revokes access immediately (active=False) and marks the account
    removed so it drops out of the normal staff list, but the row itself — and every
    AuditLog entry recorded under this username — is preserved rather than deleted.

    The clinic/admin account is never a StaffUser row (it's env-var based, role never
    "admin" here), so this route can only ever affect a real "staff" row; the role check
    is still enforced defensively in case that invariant is ever broken elsewhere.
    """
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        user = db.get(StaffUser, staff_id)
        if not user:
            return RedirectResponse("/admin/staff?message=Staff account not found", status_code=303)
        if user.role != "staff":
            return RedirectResponse("/admin/staff?message=This account cannot be removed", status_code=303)
        if user.removed_at:
            return RedirectResponse("/admin/staff?message=That staff account was already removed.", status_code=303)
        user.active = False
        user.removed_at = utc_now()
        audit(db, request.session.get("user", "admin"), "STAFF_ACCOUNT_REMOVED", details={"username": user.username})
        db.commit()
        return RedirectResponse("/admin/staff?message=Staff account removed and access revoked.", status_code=303)
    finally: db.close()


@router.post("/staff/{staff_id}/restore")
def restore_staff(request: Request, staff_id: int, password: str = Form(...), _csrf: None = Depends(require_csrf)):
    """Reactivate a previously-removed account under its original username, rather than
    letting an admin create a second row -- the username is still taken at the database
    level by the removed row (it's archived, not deleted), so a plain create would just
    fail. Restoring preserves the row's id and every AuditLog entry already recorded
    under its username; only active/removed_at/password_hash change, plus a new
    STAFF_ACCOUNT_RESTORED entry. Same defensive role check as remove_staff: the
    clinic/admin account is never a StaffUser row, so this can only ever affect staff.
    """
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        user = db.get(StaffUser, staff_id)
        if not user:
            return RedirectResponse("/admin/staff?message=Staff account not found", status_code=303)
        if user.role != "staff":
            return RedirectResponse("/admin/staff?message=This account cannot be restored", status_code=303)
        if user.removed_at is None:
            return RedirectResponse("/admin/staff?message=That staff account is not removed", status_code=303)
        user.active = True
        user.removed_at = None
        user.password_hash = password_hasher.hash(password)
        audit(db, request.session.get("user", "admin"), "STAFF_ACCOUNT_RESTORED", details={"username": user.username})
        db.commit()
        return RedirectResponse("/admin/staff?message=Staff account restored and access re-enabled.", status_code=303)
    finally: db.close()


def _assignment_details(user, campaign):
    return {"username": user.username, "staff_user_id": user.id, "campaign": campaign.name, "campaign_id": campaign.id}


@router.post("/staff/{staff_id}/campaigns")
def assign_campaigns(request: Request, staff_id: int, campaign_ids: list[int] = Form([]), _csrf: None = Depends(require_csrf)):
    """Assign one or more campaigns to a staff member. Each assignment is its own row and
    its own audit event; campaigns the staff member already has are skipped silently.
    """
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        user = db.get(StaffUser, staff_id)
        if not user or user.removed_at or user.role != "staff":
            return RedirectResponse("/admin/staff?message=Staff account not found", status_code=303)
        if not campaign_ids:
            return RedirectResponse("/admin/staff?message=Select at least one campaign to assign", status_code=303)
        # Share-locked (PostgreSQL) so a campaign cannot be closed halfway through.
        campaigns = db.execute(select(Campaign).where(Campaign.id.in_(set(campaign_ids))).order_by(Campaign.name).with_for_update(read=True)).scalars().all()
        if len(campaigns) != len(set(campaign_ids)) or any(campaign.status == "CLOSED" for campaign in campaigns):
            db.rollback()
            return RedirectResponse("/admin/staff?message=Only campaigns that are not closed can be assigned", status_code=303)
        already = set(db.execute(select(staff_campaigns.c.campaign_id).where(staff_campaigns.c.staff_user_id == user.id)).scalars())
        added = [campaign for campaign in campaigns if campaign.id not in already]
        try:
            for campaign in added:
                db.execute(staff_campaigns.insert().values(staff_user_id=user.id, campaign_id=campaign.id))
                audit(db, request.session.get("user", "admin"), "STAFF_CAMPAIGN_ASSIGNED", details=_assignment_details(user, campaign))
            db.commit()
        except IntegrityError:
            # Another admin assigned one of them a moment ago; nothing from this request is kept.
            db.rollback()
            return RedirectResponse("/admin/staff?message=Assignments changed while saving. Please review and try again", status_code=303)
        if not added:
            return RedirectResponse("/admin/staff?message=Those campaigns were already assigned", status_code=303)
        return RedirectResponse(f"/admin/staff?message={len(added)} campaign{'s' if len(added) != 1 else ''} assigned to {user.username}", status_code=303)
    finally: db.close()


@router.post("/staff/{staff_id}/campaigns/{campaign_id}/remove")
def unassign_campaign(request: Request, staff_id: int, campaign_id: int, _csrf: None = Depends(require_csrf)):
    """Remove one campaign from a staff member. Only the assignment row is deleted: the
    staff account, the campaign and every registration made under it are untouched, and
    the staff member immediately loses access to that campaign's records.
    """
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        user = db.get(StaffUser, staff_id)
        campaign = db.get(Campaign, campaign_id)
        if not user or user.role != "staff" or not campaign:
            return RedirectResponse("/admin/staff?message=Staff account not found", status_code=303)
        removed = db.execute(staff_campaigns.delete().where(staff_campaigns.c.staff_user_id == staff_id, staff_campaigns.c.campaign_id == campaign_id)).rowcount
        if not removed:
            db.rollback()
            return RedirectResponse("/admin/staff?message=That campaign is not assigned to this staff member", status_code=303)
        audit(db, request.session.get("user", "admin"), "STAFF_CAMPAIGN_REMOVED", details=_assignment_details(user, campaign))
        db.commit()
        return RedirectResponse(f"/admin/staff?message={campaign.name} removed from {user.username}", status_code=303)
    finally: db.close()
