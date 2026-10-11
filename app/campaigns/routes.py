from datetime import date
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from ..audit_service import audit
from ..auth import require_admin
from ..models import Campaign, Offer, campaign_offers
from ..security import require_csrf
from ..reporting.service import counts, service_counts

router = APIRouter(prefix="/admin")


@router.get("/campaigns")
def campaigns(request: Request):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        rows = db.query(Campaign).order_by(Campaign.created_at.desc()).all()
        performance = []
        for campaign in rows:
            issued, redeemed, expired = counts(db, campaign.id)
            performance.append({"campaign": campaign, "issued": issued, "redeemed": redeemed, "expired": expired, "conversion": round(redeemed * 100 / issued, 1) if issued else 0})
        return request.app.state.templates.TemplateResponse(request, "campaigns.html", {"request": request, "rows": rows, "performance": performance, "offers": db.query(Offer).filter(Offer.active == True).order_by(Offer.name).all()})
    finally: db.close()


@router.post("/campaigns")
def create_campaign(request: Request, name: str = Form(...), start_date: date = Form(...), end_date: date = Form(...), offer_ids: list[int] = Form([]), _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        if end_date < start_date:
            return RedirectResponse("/admin/campaigns?message=End date must be on or after start date", status_code=303)
        campaign = Campaign(name=" ".join(name.split()), start_date=start_date, end_date=end_date, created_by=request.session.get("user", "admin"), status="DRAFT")
        campaign.offers = db.query(Offer).filter(Offer.id.in_(offer_ids)).all() if offer_ids else []
        db.add(campaign); db.flush(); audit(db, request.session.get("user", "admin"), "CAMPAIGN_CREATED", details={"campaign": campaign.name}); db.commit()
        return RedirectResponse(f"/admin/campaigns/{campaign.id}?message=Campaign created", status_code=303)
    finally: db.close()


def update_status(request, campaign_id, status):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        # Locked (PostgreSQL) so a concurrent close and start/pause cannot both pass the check.
        campaign = db.get(Campaign, campaign_id, with_for_update=True)
        if not campaign: return RedirectResponse("/admin/campaigns", status_code=303)
        if campaign.status == "CLOSED":
            # Closing is final: a closed campaign is a historical record and is never reopened.
            db.rollback()
            return _service_redirect(campaign_id, CLOSED_STATUS_MESSAGE)
        campaign.status = status; audit(db, request.session.get("user", "admin"), f"CAMPAIGN_{status}", details={"campaign": campaign.name}); db.commit()
        return RedirectResponse(f"/admin/campaigns/{campaign_id}", status_code=303)
    finally: db.close()


@router.post("/campaigns/{campaign_id}/start")
def start(request: Request, campaign_id: int, _csrf: None = Depends(require_csrf)): return update_status(request, campaign_id, "ACTIVE")
@router.post("/campaigns/{campaign_id}/pause")
def pause(request: Request, campaign_id: int, _csrf: None = Depends(require_csrf)): return update_status(request, campaign_id, "PAUSED")
@router.post("/campaigns/{campaign_id}/close")
def close(request: Request, campaign_id: int, _csrf: None = Depends(require_csrf)): return update_status(request, campaign_id, "CLOSED")


CLOSED_CAMPAIGN_MESSAGE = "This campaign is closed. Its services can no longer be changed"
CLOSED_STATUS_MESSAGE = "This campaign is closed. Its status can no longer be changed"


def _editable_campaign(db, campaign_id):
    """The campaign whose services are about to change, plus the redirect to send instead
    if they may not be. Services can be managed while a campaign is DRAFT, ACTIVE or
    PAUSED; a CLOSED campaign is a historical record. The row is share-locked
    (PostgreSQL) so the campaign cannot be closed halfway through the change.
    """
    campaign = db.get(Campaign, campaign_id, with_for_update={"read": True})
    if not campaign:
        return None, RedirectResponse("/admin/campaigns", status_code=303)
    if campaign.status == "CLOSED":
        db.rollback()
        return None, _service_redirect(campaign_id, CLOSED_CAMPAIGN_MESSAGE)
    return campaign, None


def _service_redirect(campaign_id, message):
    return RedirectResponse(f"/admin/campaigns/{campaign_id}?message={message}", status_code=303)


def _attach_service(db, user, campaign, offer):
    """Assign a service to a campaign and audit it. The link is written as its own row
    (never by rewriting the campaign's whole service list) so two admins editing the same
    campaign at once cannot undo each other's changes; the composite primary key rejects a
    double assignment. Raises IntegrityError if the service is already assigned.
    """
    db.execute(campaign_offers.insert().values(campaign_id=campaign.id, offer_id=offer.id))
    audit(db, user, "CAMPAIGN_SERVICE_ADDED", details={"campaign": campaign.name, "campaign_id": campaign.id, "service": offer.name, "service_id": offer.id})


@router.post("/campaigns/{campaign_id}/services")
def create_campaign_service(request: Request, campaign_id: int, name: str = Form(...), description: str = Form(...), _csrf: None = Depends(require_csrf)):
    """Create a brand-new catalog service and assign it to this campaign in one step."""
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        campaign, refusal = _editable_campaign(db, campaign_id)
        if refusal: return refusal
        name, description = " ".join(name.split()), description.strip()
        if not name or len(name) > 150 or not description:
            return _service_redirect(campaign_id, "Enter a service name (up to 150 characters) and a description")
        user = request.session.get("user", "admin")
        offer = Offer(name=name, description=description, active=True)
        db.add(offer)
        try:
            db.flush()
            audit(db, user, "SERVICE_CREATED", details={"name": offer.name})
            _attach_service(db, user, campaign, offer)
            db.commit()
        except IntegrityError:
            db.rollback()
            return _service_redirect(campaign_id, "A service with that name already exists. Add it from the existing services list instead")
        return _service_redirect(campaign_id, "Service created and added to this campaign")
    finally: db.close()


@router.post("/campaigns/{campaign_id}/services/attach")
def attach_campaign_service(request: Request, campaign_id: int, offer_id: int = Form(...), _csrf: None = Depends(require_csrf)):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        campaign, refusal = _editable_campaign(db, campaign_id)
        if refusal: return refusal
        offer = db.get(Offer, offer_id)
        if not offer: return _service_redirect(campaign_id, "Service not found")
        if not offer.active: return _service_redirect(campaign_id, "Activate this service in the catalog before adding it to a campaign")
        try:
            _attach_service(db, request.session.get("user", "admin"), campaign, offer)
            db.commit()
        except IntegrityError:
            db.rollback()
            return _service_redirect(campaign_id, "Service is already part of this campaign")
        return _service_redirect(campaign_id, "Service added to this campaign")
    finally: db.close()


@router.post("/campaigns/{campaign_id}/services/{offer_id}/remove")
def remove_campaign_service(request: Request, campaign_id: int, offer_id: int, _csrf: None = Depends(require_csrf)):
    """Unassign a service from a campaign. Only the campaign/service link is deleted: the
    catalog service, its other campaigns, and every registration, coupon, redemption and
    audit row that already references it are left exactly as they are.
    """
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        campaign, refusal = _editable_campaign(db, campaign_id)
        if refusal: return refusal
        offer = db.get(Offer, offer_id)
        # On PostgreSQL this waits for any in-flight registration holding the link (see
        # registrations.service._offer_assigned), so none is cut off mid-transaction.
        removed = db.execute(campaign_offers.delete().where(campaign_offers.c.campaign_id == campaign_id, campaign_offers.c.offer_id == offer_id)).rowcount
        if not offer or not removed:
            db.rollback()
            return _service_redirect(campaign_id, "Service is not part of this campaign")
        audit(db, request.session.get("user", "admin"), "CAMPAIGN_SERVICE_REMOVED", details={"campaign": campaign.name, "campaign_id": campaign.id, "service": offer.name, "service_id": offer.id})
        db.commit()
        return _service_redirect(campaign_id, "Service removed from this campaign")
    finally: db.close()


@router.get("/campaigns/{campaign_id}")
def campaign_detail(request: Request, campaign_id: int):
    guard = require_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        campaign = db.get(Campaign, campaign_id)
        if not campaign: return RedirectResponse("/admin/campaigns", status_code=303)
        issued, redeemed, expired = counts(db, campaign_id)
        by_service = service_counts(db, campaign_id)
        assigned = db.execute(select(Offer).join(campaign_offers, campaign_offers.c.offer_id == Offer.id).where(campaign_offers.c.campaign_id == campaign_id).order_by(Offer.name)).scalars().all()
        assigned_ids = {offer.id for offer in assigned}
        # Services removed from the campaign keep their historical rows in the report.
        removed_ids = [offer_id for offer_id in by_service if offer_id not in assigned_ids]
        removed = db.query(Offer).filter(Offer.id.in_(removed_ids)).order_by(Offer.name).all() if removed_ids else []
        service_stats = []
        for offer in [*assigned, *removed]:
            service_issued, service_redeemed, service_expired = by_service.get(offer.id, (0, 0, 0))
            service_stats.append({"id": offer.id, "name": offer.name, "active": offer.active, "assigned": offer.id in assigned_ids, "issued": service_issued, "redeemed": service_redeemed, "expired": service_expired, "conversion": round(service_redeemed * 100 / service_issued, 1) if service_issued else 0})
        available = db.query(Offer).filter(Offer.active == True, Offer.id.notin_(assigned_ids)).order_by(Offer.name).all()
        return request.app.state.templates.TemplateResponse(request, "campaign_detail.html", {"request": request, "campaign": campaign, "issued": issued, "redeemed": redeemed, "expired": expired, "conversion": round(redeemed * 100 / issued, 1) if issued else 0, "service_stats": service_stats, "assigned_count": len(assigned), "available_offers": available, "services_editable": campaign.status != "CLOSED"})
    finally: db.close()
