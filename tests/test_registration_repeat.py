"""Repeat registrations, the recent-duplicate confirmation, and submission-token replay."""
import json
import re
from datetime import timedelta

from app.database import SessionLocal
from app.models import AuditLog, DeliveryLog, Patient, PatientOffer

MOBILE = "9777700001"
TOKEN_PATTERN = re.compile(r'name="submission_token" value="([^"]+)"')


def _form(mobile=MOBILE, offer="1", name="Repeat Patient", **extra):
    return {
        "full_name": name, "mobile": mobile, "email": "repeat@example.com", "age": "41",
        "gender": "Female", "city": "Delhi", "doctor_name": "Dr Rao", "campaign_name": "Camp ref",
        "campaign_id": "1", "offer_id": offer, "beneficiary_category": "CGHS",
        "consent_given": "true", **extra,
    }


def _post(client, data, path="/patients/register"):
    return client.post(path, data=data, follow_redirects=False)


def _counts():
    db = SessionLocal()
    try:
        return {
            "patients": db.query(Patient).count(),
            "coupons": db.query(PatientOffer).count(),
            "audits": db.query(AuditLog).count(),
            "deliveries": db.query(DeliveryLog).count(),
        }
    finally:
        db.close()


def _coupons():
    db = SessionLocal()
    try:
        return db.query(PatientOffer).order_by(PatientOffer.id).all()
    finally:
        db.close()


def _age_all_registrations(minutes):
    db = SessionLocal()
    try:
        for coupon in db.query(PatientOffer).all():
            coupon.created_at -= timedelta(minutes=minutes)
        db.commit()
    finally:
        db.close()


# --- Repeats that need no confirmation ----------------------------------------

def test_same_mobile_different_service_registers_without_confirmation(client):
    assert _post(client, _form(offer="1")).status_code == 303
    assert _post(client, _form(offer="2")).status_code == 303
    assert _counts()["coupons"] == 2


def test_same_mobile_same_service_in_another_campaign_registers_without_confirmation(client):
    from datetime import date
    from app.models import Campaign, Offer
    db = SessionLocal()
    campaign = Campaign(
        name="Second Campaign", start_date=date.today() - timedelta(days=1),
        end_date=date.today() + timedelta(days=1), status="ACTIVE", created_by="test",
    )
    campaign.offers = db.query(Offer).all()
    db.add(campaign); db.commit(); second_id = campaign.id; db.close()

    assert _post(client, _form()).status_code == 303
    assert _post(client, _form(campaign_id=str(second_id))).status_code == 303
    assert _counts()["coupons"] == 2


def test_same_registration_after_five_minutes_needs_no_confirmation(client):
    assert _post(client, _form()).status_code == 303
    _age_all_registrations(minutes=6)
    assert _post(client, _form()).status_code == 303
    assert _counts()["coupons"] == 2


def test_cancelled_recent_registration_does_not_trigger_confirmation(client):
    first = _post(client, _form())
    patient_id = first.headers["location"].rsplit("/", 1)[1]
    assert client.post(f"/patients/{patient_id}/cancel", data={"reason": "Entered by mistake"}, follow_redirects=False).status_code == 303

    assert _post(client, _form()).status_code == 303
    statuses = [coupon.status for coupon in _coupons()]
    assert statuses == ["CANCELLED", "ACTIVE"]


# --- Recent-duplicate confirmation --------------------------------------------

def test_recent_duplicate_shows_confirmation_and_creates_nothing(client):
    first = _post(client, _form())
    before = _counts()
    existing = _coupons()[0]

    response = _post(client, _form(name="Repeat Patient Again"))

    assert response.status_code == 409
    assert _counts() == before
    assert existing.coupon_uid in response.text
    assert "<dt>Issued by</dt><dd>admin</dd>" in response.text
    assert "IST" in response.text  # creation time, clinic-local
    assert 'name="confirm_repeat"' in response.text and "Issue another QR" in response.text
    assert f'href="{first.headers["location"]}"' in response.text  # view existing
    assert "Cancel" in response.text


def test_recent_duplicate_preserves_entered_input(client):
    _post(client, _form())
    response = _post(client, _form(name="Kept Name"))
    text = response.text
    assert 'name="full_name" value="Kept Name"' in text
    assert f'name="mobile" value="{MOBILE}"' in text
    assert 'value="repeat@example.com"' in text and 'value="41"' in text
    assert 'value="Delhi"' in text and 'value="Dr Rao"' in text and 'value="Camp ref"' in text
    assert re.search(r'<option value="1" selected>', text)  # campaign
    assert re.search(r'<option selected>Female</option>', text)
    assert re.search(r'name="offer_id" value="1" required checked', text)
    assert re.search(r'name="consent_given" value="true" required checked', text)
    assert re.search(r'<option value="CGHS" selected>', text)


def test_validation_error_preserves_entered_input(client):
    response = _post(client, _form(mobile="12", name="Typo Patient"))
    assert response.status_code == 422
    assert 'name="full_name" value="Typo Patient"' in response.text
    assert 'name="mobile" value="12"' in response.text


def test_confirmed_repeat_creates_an_independent_registration(client):
    _post(client, _form())
    response = _post(client, _form(confirm_repeat="true"))
    assert response.status_code == 303

    first, second = _coupons()
    assert first.patient_id != second.patient_id
    assert first.coupon_uid != second.coupon_uid
    assert first.secure_token_hash != second.secure_token_hash
    assert second.status == "ACTIVE" and second.expires_at - second.created_at == timedelta(days=10)

    db = SessionLocal()
    try:
        for coupon in (first, second):
            assert db.query(DeliveryLog).filter_by(coupon_id=coupon.id).count() == 2  # email + WhatsApp
            assert db.query(AuditLog).filter_by(coupon_id=coupon.id, action="PATIENT_REGISTERED").count() == 1
        assert response.headers["location"] == f"/patients/{second.patient_id}"
    finally:
        db.close()


def test_confirmed_repeat_is_recorded_in_audit_with_previous_qr_id(client):
    _post(client, _form())
    _post(client, _form(confirm_repeat="true"))
    first, second = _coupons()
    db = SessionLocal()
    try:
        first_details = json.loads(db.query(AuditLog).filter_by(coupon_id=first.id, action="PATIENT_REGISTERED").one().details)
        second_details = json.loads(db.query(AuditLog).filter_by(coupon_id=second.id, action="PATIENT_REGISTERED").one().details)
    finally:
        db.close()
    assert "repeat_confirmed" not in first_details
    assert second_details["repeat_confirmed"] is True
    assert second_details["previous_coupon_uid"] == first.coupon_uid


def test_repeat_registrations_keep_independent_redemption_and_cancellation_state(client):
    _post(client, _form())
    _post(client, _form(confirm_repeat="true"))
    first, second = _coupons()

    client.post(f"/redeem/{first.id}")
    client.post(f"/patients/{second.patient_id}/cancel", data={"reason": "Not needed"})

    first, second = _coupons()
    assert first.status == "REDEEMED" and first.cancelled_at is None
    assert second.status == "CANCELLED" and second.redeemed_at is None


def test_staff_route_has_the_same_confirmation_flow(client):
    from tests.test_staff_interface import _login_as_staff
    _login_as_staff(client)

    first = _post(client, _form(), path="/staff/register")
    assert first.status_code == 303 and first.headers["location"].startswith("/staff/patients/")

    warned = _post(client, _form(), path="/staff/register")
    assert warned.status_code == 409
    assert "<dt>Issued by</dt><dd>staff-user</dd>" in warned.text
    assert f'href="{first.headers["location"]}"' in warned.text
    assert 'action="/staff/register"' in warned.text
    assert _counts()["coupons"] == 1

    confirmed = _post(client, _form(confirm_repeat="true"), path="/staff/register")
    assert confirmed.status_code == 303
    assert _counts()["coupons"] == 2


# --- Submission token / idempotency -------------------------------------------

def test_form_renders_a_fresh_submission_token_and_double_click_guard(client):
    first = client.get("/patients/register").text
    second = client.get("/patients/register").text
    tokens = [TOKEN_PATTERN.search(page).group(1) for page in (first, second)]
    assert tokens[0] != tokens[1] and all(len(token) >= 16 for token in tokens)
    assert "form.dataset.submitting" in first


def test_replayed_submission_token_redirects_to_original_and_creates_nothing(client):
    token = TOKEN_PATTERN.search(client.get("/patients/register").text).group(1)
    first = _post(client, _form(submission_token=token))
    assert first.status_code == 303
    before = _counts()

    replay = _post(client, _form(submission_token=token))

    assert replay.status_code == 303
    assert replay.headers["location"] == first.headers["location"]
    assert _counts() == before
    assert _coupons()[0].submission_key == token


def test_replayed_confirmation_creates_only_one_extra_registration(client):
    _post(client, _form(submission_token="first-submission-token-0001"))
    warned = _post(client, _form(submission_token="second-submission-token-0002"))
    assert warned.status_code == 409
    # The warning page keeps the same token, so a double-clicked "Issue another QR" is one submission.
    assert TOKEN_PATTERN.search(warned.text).group(1) == "second-submission-token-0002"

    confirm = _form(submission_token="second-submission-token-0002", confirm_repeat="true")
    first_click = _post(client, confirm)
    second_click = _post(client, confirm)

    assert first_click.status_code == second_click.status_code == 303
    assert first_click.headers["location"] == second_click.headers["location"]
    assert _counts()["coupons"] == 2


def test_replay_wins_over_a_changed_form(client):
    """A retried request is answered from the original even if the campaign has since closed."""
    from app.models import Campaign
    first = _post(client, _form(submission_token="closing-campaign-token-0003"))
    db = SessionLocal(); db.get(Campaign, 1).status = "CLOSED"; db.commit(); db.close()

    replay = _post(client, _form(submission_token="closing-campaign-token-0003"))
    assert replay.status_code == 303 and replay.headers["location"] == first.headers["location"]


def test_malformed_submission_token_is_rejected_and_replaced(client):
    response = _post(client, _form(submission_token="bad token!"))
    assert response.status_code == 422
    assert "form has expired" in response.text
    assert _counts()["coupons"] == 0
    assert TOKEN_PATTERN.search(response.text).group(1) != "bad token!"


def test_submission_without_token_still_registers(client):
    assert _post(client, _form()).status_code == 303
    assert _coupons()[0].submission_key is None


# --- Error handling -----------------------------------------------------------

def test_qr_id_collision_is_retried_with_a_new_id(client, monkeypatch):
    from app.registrations import service as registration_service
    _post(client, _form(mobile="9777700010"))
    taken = _coupons()[0].coupon_uid
    real_new_uid = registration_service.new_uid
    issued = iter([taken])

    def colliding_new_uid(prefix):
        if prefix == "SRD":
            return next(issued, None) or real_new_uid(prefix)
        return real_new_uid(prefix)

    monkeypatch.setattr(registration_service, "new_uid", colliding_new_uid)
    response = _post(client, _form(mobile="9777700011"))

    assert response.status_code == 303
    coupons = _coupons()
    assert len(coupons) == 2 and coupons[1].coupon_uid != taken
    assert _counts()["patients"] == 2  # the collided attempt left no orphan patient row


def test_persistent_id_collision_fails_safely(client, monkeypatch):
    from app.registrations import service as registration_service
    _post(client, _form(mobile="9777700012"))
    taken = _coupons()[0].coupon_uid
    real_new_uid = registration_service.new_uid
    monkeypatch.setattr(registration_service, "new_uid", lambda prefix: taken if prefix == "SRD" else real_new_uid(prefix))

    response = _post(client, _form(mobile="9777700013"))

    assert response.status_code == 422
    assert "Registration could not be completed" in response.text
    assert "IntegrityError" not in response.text and "UNIQUE" not in response.text.upper().replace("UNIQUE SEARCH", "")
    assert _counts()["coupons"] == 1 and _counts()["patients"] == 1


def test_staff_route_logs_and_hides_unexpected_failures(client, monkeypatch, caplog):
    from app.registrations import service as registration_service
    from tests.test_staff_interface import _login_as_staff
    _login_as_staff(client)

    def explode(*_args, **_kwargs):
        raise RuntimeError("SECRET INTERNAL FAILURE")

    monkeypatch.setattr(registration_service, "queue_registration_deliveries", explode)
    with caplog.at_level("ERROR"):
        response = _post(client, _form(), path="/staff/register")

    assert response.status_code == 500
    assert "Registration could not be completed" in response.text
    assert "SECRET INTERNAL FAILURE" not in response.text
    assert "Staff patient registration failed" in caplog.text
    assert _counts()["coupons"] == 0
