from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from ..auth import require_admin, require_staff_or_admin
from ..models import PatientOffer
from .service import qr_png_bytes

router = APIRouter()


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
        return RedirectResponse("/offers", status_code=303)
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
        return RedirectResponse("/offers", status_code=303)
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
            return RedirectResponse("/offers", status_code=303)
        return request.app.state.templates.TemplateResponse(request, "print_coupon.html", {"request": request, "coupon": coupon})
    finally:
        db.close()
