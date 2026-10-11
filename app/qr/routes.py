from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from ..auth import require_admin, require_staff_or_admin
from ..models import PatientOffer
from ..staff_scope import is_staff
from .service import qr_png_bytes

router = APIRouter()


def _qr_not_found(request: Request):
    """A QR that does not exist. Staff get a plain 404 rather than being bounced to an
    admin page; admins keep the redirect to the coupon ledger. QR images are available to
    every authenticated staff member, whatever their campaign assignments.
    """
    if is_staff(request):
        return PlainTextResponse("QR not found", status_code=404)
    return RedirectResponse("/offers", status_code=303)


@router.get("/qr/{coupon_uid}.png")
def qr_image(request: Request, coupon_uid: str):
    guard = require_staff_or_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        coupon = db.query(PatientOffer).filter(PatientOffer.coupon_uid == coupon_uid).first()
    finally:
        db.close()
    if not coupon:
        return _qr_not_found(request)
    try:
        image = qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)
    except FileNotFoundError:
        return RedirectResponse(f"/patients/{coupon.patient_id}?message=QR file unavailable", status_code=303)
    return Response(image, media_type="image/png", headers={"Content-Disposition": f'inline; filename="{coupon.coupon_uid}.png"'})


@router.get("/qr/{coupon_uid}/download")
def qr_download(request: Request, coupon_uid: str):
    guard = require_staff_or_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        coupon = db.query(PatientOffer).filter(PatientOffer.coupon_uid == coupon_uid).first()
    finally:
        db.close()
    if not coupon:
        return _qr_not_found(request)
    try:
        image = qr_png_bytes(coupon.coupon_uid, coupon.secure_token_hash)
    except FileNotFoundError:
        return RedirectResponse(f"/patients/{coupon.patient_id}?message=QR file unavailable", status_code=303)
    return Response(image, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="{coupon.coupon_uid}.png"'})


@router.get("/qr/{coupon_uid}/print")
def qr_print(request: Request, coupon_uid: str):
    guard = require_staff_or_admin(request)
    if guard: return guard
    db = request.app.state.db()
    try:
        coupon = db.query(PatientOffer).filter(PatientOffer.coupon_uid == coupon_uid).first()
        if not coupon:
            return _qr_not_found(request)
        return request.app.state.templates.TemplateResponse(request, "print_coupon.html", {"request": request, "coupon": coupon})
    finally:
        db.close()
