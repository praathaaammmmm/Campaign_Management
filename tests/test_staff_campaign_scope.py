"""Staff campaign assignments: admin management and server-side enforcement.

The `client` fixture provides campaign 1 ("Test Active Campaign", ACTIVE) with offers 1
and 2 and an admin session. Registrations are created through the service layer so a test
can stay logged in as one staff member while data exists in several campaigns.
"""
import json
import re
import threading
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from app.database import SessionLocal, engine
from app.models import AuditLog, Campaign, DeliveryLog, Offer, Patient, PatientOffer, StaffUser, staff_campaigns
from app.registrations.service import register_patient_offer
from app.routes.auth import _attempts
from app.staff_scope import staff_assigned_to
from tests.test_staff_interface import _create_staff

NO_ASSIGNMENTS = "No campaigns are assigned to you yet"
NOT_FOUND_URL = "/staff/patients?message=Patient%20record%20not%20found"


def _campaign(name, status="ACTIVE"):
    db = SessionLocal()
    try:
        campaign = Campaign(
            name=name, start_date=date.today() - timedelta(days=1), end_date=date.today() + timedelta(days=1),
            status=status, created_by="test",
        )
        campaign.offers = db.query(Offer).all()
        db.add(campaign)
        db.commit()
        return campaign.id
    finally:
        db.close()


def _register(campaign_id, mobile, name):
    """Returns (patient_id, coupon_id, coupon_uid) of a new registration in the campaign."""
    db = SessionLocal()
    try:
        coupon = register_patient_offer(
            db, full_name=name, mobile=mobile, email="", age="", gender="", city="", doctor_name="",
            campaign_name="", campaign_id=campaign_id, offer_id=1, beneficiary_category="CGHS",
            consent_given=True, actor="admin",
        )
        return coupon.patient_id, coupon.id, coupon.coupon_uid
    finally:
        db.close()


def _staff_session(client, campaign_ids, username="staff-user"):
    staff_id = _create_staff(username=username, campaign_ids=campaign_ids)
    _attempts.clear()
    client.post("/logout")
    response = client.post("/login", data={"username": username, "password": "staff-password"}, follow_redirects=False)
    assert response.headers["location"] == "/staff/home"
    return staff_id


def _assigned(staff_id):
    db = SessionLocal()
    try:
        return set(db.execute(select(staff_campaigns.c.campaign_id).where(staff_campaigns.c.staff_user_id == staff_id)).scalars())
    finally:
        db.close()


def _set_assignments(staff_id, campaign_ids):
    """Change assignments behind a live staff session, as an admin would from another browser."""
    db = SessionLocal()
    try:
        db.execute(staff_campaigns.delete().where(staff_campaigns.c.staff_user_id == staff_id))
        for campaign_id in campaign_ids:
            db.execute(staff_campaigns.insert().values(staff_user_id=staff_id, campaign_id=campaign_id))
        db.commit()
    finally:
        db.close()


def _audits(action):
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter_by(action=action).order_by(AuditLog.id).all()
        return [(row.user, json.loads(row.details) if row.details else None, row.timestamp) for row in rows]
    finally:
        db.close()


def _coupon(coupon_id):
    db = SessionLocal()
    try:
        coupon = db.get(PatientOffer, coupon_id)
        return coupon.status, coupon.redeemed_at, coupon.redeemed_by, coupon.expires_at
    finally:
        db.close()


def _count(model):
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _campaign_options(html):
    select_html = html.split('<select name="campaign_id"', 1)[1].split("</select>", 1)[0]
    return {int(value) for value in re.findall(r'<option value="(\d+)"', select_html)}


def _stat(html, label):
    return int(re.search(rf"<small>{label}</small><strong>(\d+)</strong>", html).group(1))


def _registration(campaign_id, mobile):
    return {
        "full_name": "Scoped Staff Patient", "mobile": mobile, "campaign_id": str(campaign_id),
        "offer_id": "1", "beneficiary_category": "CGHS", "consent_given": "true",
    }


# --- Admin: managing assignments -------------------------------------------------------

def test_new_staff_account_has_no_assignments(client):
    client.post("/admin/staff", data={"username": "fresh-staff", "password": "a-long-staff-password"})
    db = SessionLocal()
    try:
        staff_id = db.query(StaffUser).filter_by(username="fresh-staff").one().id
    finally:
        db.close()
    assert _assigned(staff_id) == set()
    assert "None — no campaign access" in client.get("/admin/staff").text


def test_admin_assigns_one_campaign_with_audit(client):
    staff_id = _create_staff(campaign_ids=[])
    response = client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": ["1"]}, follow_redirects=False)

    assert response.status_code == 303 and response.headers["location"].startswith("/admin/staff?message=")
    assert _assigned(staff_id) == {1}
    [(user, details, timestamp)] = _audits("STAFF_CAMPAIGN_ASSIGNED")
    assert user == "admin" and timestamp is not None
    assert details == {"username": "staff-user", "staff_user_id": staff_id, "campaign": "Test Active Campaign", "campaign_id": 1}
    page = client.get("/admin/staff").text
    assert f'action="/admin/staff/{staff_id}/campaigns/1/remove"' in page and "Test Active Campaign" in page


def test_admin_assigns_many_campaigns_at_once_without_a_limit(client):
    staff_id = _create_staff(campaign_ids=[])
    campaign_ids = [1] + [_campaign(f"Bulk Campaign {index:02d}", status=("DRAFT", "ACTIVE", "PAUSED")[index % 3]) for index in range(24)]

    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": [str(i) for i in campaign_ids]})

    assert _assigned(staff_id) == set(campaign_ids) and len(campaign_ids) == 25
    assert len(_audits("STAFF_CAMPAIGN_ASSIGNED")) == 25
    page = client.get("/admin/staff").text
    assert all(f"Bulk Campaign {index:02d}" in page for index in range(24))
    assert "Assign campaigns" not in page  # nothing left to offer


def test_admin_removes_an_assignment_with_audit_and_keeps_the_rest(client):
    second = _campaign("Second Campaign")
    staff_id = _create_staff(campaign_ids=[1, second])
    patient_id, coupon_id, _ = _register(second, "9822000001", "Kept Patient")

    response = client.post(f"/admin/staff/{staff_id}/campaigns/{second}/remove", follow_redirects=False)

    assert response.status_code == 303
    assert _assigned(staff_id) == {1}
    [(user, details, _)] = _audits("STAFF_CAMPAIGN_REMOVED")
    assert user == "admin"
    assert details == {"username": "staff-user", "staff_user_id": staff_id, "campaign": "Second Campaign", "campaign_id": second}
    # Only the link went: the staff account, campaign and its registration are untouched.
    db = SessionLocal()
    try:
        assert db.get(StaffUser, staff_id).active is True and db.get(Campaign, second).status == "ACTIVE"
        assert db.get(PatientOffer, coupon_id).patient_id == patient_id
    finally:
        db.close()
    # Repeating either action changes nothing and adds no audit rows.
    client.post(f"/admin/staff/{staff_id}/campaigns/{second}/remove")
    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": ["1"]})
    assert _assigned(staff_id) == {1}
    assert len(_audits("STAFF_CAMPAIGN_REMOVED")) == 1 and _audits("STAFF_CAMPAIGN_ASSIGNED") == []


def test_closed_campaigns_are_not_assignable_but_history_stays_visible(client):
    closed = _campaign("Closed Campaign", status="CLOSED")
    was_open = _campaign("Later Closed Campaign")
    staff_id = _create_staff(campaign_ids=[was_open])
    client.post(f"/admin/campaigns/{was_open}/close")

    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": [str(closed)]})
    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": ["1", str(closed)]})  # mixed: all refused
    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": ["99999"]})
    client.post(f"/admin/staff/{staff_id}/campaigns", data={})

    assert _assigned(staff_id) == {was_open}
    assert _audits("STAFF_CAMPAIGN_ASSIGNED") == []
    page = client.get("/admin/staff").text
    assert "Later Closed Campaign <em>(closed)</em>" in page  # historical assignment, still removable
    assert f'value="{closed}"' not in page and f'name="campaign_ids" value="{was_open}"' not in page
    assert 'name="campaign_ids" value="1"' in page
    assert client.post(f"/admin/staff/{staff_id}/campaigns/{was_open}/remove", follow_redirects=False).status_code == 303
    assert _assigned(staff_id) == set()


def test_removed_staff_account_cannot_be_assigned(client):
    staff_id = _create_staff(campaign_ids=[])
    client.post(f"/admin/staff/{staff_id}/remove")
    client.post(f"/admin/staff/{staff_id}/campaigns", data={"campaign_ids": ["1"]})
    client.post("/admin/staff/99999/campaigns", data={"campaign_ids": ["1"]})
    assert _assigned(staff_id) == set() and _audits("STAFF_CAMPAIGN_ASSIGNED") == []


def test_assignment_routes_need_admin_and_csrf(client):
    target = _create_staff(username="other-staff", campaign_ids=[1])
    assert client.post(f"/admin/staff/{target}/campaigns/1/remove", include_csrf=False, follow_redirects=False).status_code == 403
    assert client.post(f"/admin/staff/{target}/campaigns", include_csrf=False, data={"campaign_ids": ["1"]}, follow_redirects=False).status_code == 403

    own = _staff_session(client, [])
    responses = [
        client.post(f"/admin/staff/{own}/campaigns", data={"campaign_ids": ["1"]}, follow_redirects=False),
        client.post(f"/admin/staff/{target}/campaigns/1/remove", follow_redirects=False),
        client.get("/admin/staff", follow_redirects=False),
    ]
    assert [response.status_code for response in responses] == [403, 403, 403]
    for url in ("/staff/home", "/staff/register", "/staff/patients"):
        page = client.get(url).text
        assert "/admin/staff" not in page and "Assign campaigns" not in page

    client.post("/logout")
    response = client.post(f"/admin/staff/{own}/campaigns", data={"campaign_ids": ["1"]}, follow_redirects=False)
    assert response.status_code in (303, 403) and "/admin/staff" not in response.headers.get("location", "")
    assert _assigned(own) == set() and _assigned(target) == {1}
    assert _audits("STAFF_CAMPAIGN_ASSIGNED") == [] and _audits("STAFF_CAMPAIGN_REMOVED") == []


def _select_count(client, url):
    statements = []

    def record(_conn, _cursor, statement, *_):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        assert client.get(url).status_code == 200
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_staff_list_and_staff_patient_list_do_not_query_per_row(client):
    second = _campaign("Second Campaign")
    _create_staff(username="staff-00", campaign_ids=[1, second])
    few = _select_count(client, "/admin/staff")
    for index in range(1, 8):
        _create_staff(username=f"staff-{index:02d}", campaign_ids=[1, second])
    assert _select_count(client, "/admin/staff") == few

    _register(1, "9822000100", "Row Patient 0")
    _staff_session(client, [1, second])
    few = _select_count(client, "/staff/patients")
    for index in range(1, 8):
        _register(1 if index % 2 else second, f"98220001{index:02d}", f"Row Patient {index}")
    assert _select_count(client, "/staff/patients") == few
    few = _select_count(client, "/staff/home")
    _set_assignments(_staff_id("staff-user"), [1, second, _campaign("Third Campaign"), _campaign("Fourth Campaign")])
    assert _select_count(client, "/staff/home") == few


def _staff_id(username):
    db = SessionLocal()
    try:
        return db.query(StaffUser).filter_by(username=username).one().id
    finally:
        db.close()


# --- Staff: registration ---------------------------------------------------------------

def test_staff_registration_form_lists_only_assigned_usable_campaigns(client):
    unassigned = _campaign("Unassigned Campaign")
    paused = _campaign("Assigned Paused Campaign", status="PAUSED")
    closed = _campaign("Assigned Closed Campaign", status="CLOSED")
    _staff_session(client, [1, paused, closed])

    page = client.get("/staff/register").text

    assert _campaign_options(page) == {1}
    for name in ("Unassigned Campaign", "Assigned Paused Campaign", "Assigned Closed Campaign"):
        assert name not in page
    assert unassigned not in _campaign_options(page)


def test_crafted_staff_registration_to_another_campaign_is_rejected(client):
    other = _campaign("Other Campaign")
    _staff_session(client, [1])

    response = client.post("/staff/register", data=_registration(other, "9822000002"))

    assert response.status_code == 403 and "You are not assigned to this campaign." in response.text
    assert _campaign_options(response.text) == {1}  # the re-rendered form is still scoped
    assert _count(PatientOffer) == 0 and _count(Patient) == 0 and _count(DeliveryLog) == 0
    no_campaign = {key: value for key, value in _registration(other, "9822000003").items() if key != "campaign_id"}
    assert client.post("/staff/register", data=no_campaign).status_code == 403
    assert _count(PatientOffer) == 0

    response = client.post("/staff/register", data=_registration(1, "9822000004"), follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/staff/patients/")
    assert _count(PatientOffer) == 1


def test_staff_with_several_campaigns_works_in_each_and_loses_one_immediately(client):
    second, third = _campaign("Second Campaign"), _campaign("Third Campaign")
    staff_id = _staff_session(client, [1, second])
    second_patient, _, second_uid = _register(second, "9822000005", "Second Campaign Patient")
    _register(third, "9822000006", "Third Campaign Patient")

    assert _campaign_options(client.get("/staff/register").text) == {1, second}
    for campaign_id, mobile in ((1, "9822000007"), (second, "9822000008")):
        assert client.post("/staff/register", data=_registration(campaign_id, mobile), follow_redirects=False).status_code == 303
    assert client.post("/staff/register", data=_registration(third, "9822000009")).status_code == 403
    listing = client.get("/staff/patients").text
    assert "Second Campaign Patient" in listing and "Third Campaign Patient" not in listing
    assert "3 matching records" in listing
    home = client.get("/staff/home").text
    assert _stat(home, "Your active campaigns") == 2 and _stat(home, "Active registrations") == 3

    _set_assignments(staff_id, [1])  # an admin removes "Second Campaign"; the staff session stays logged in

    assert _campaign_options(client.get("/staff/register").text) == {1}
    assert client.post("/staff/register", data=_registration(second, "9822000010")).status_code == 403
    listing = client.get("/staff/patients").text
    assert "Second Campaign Patient" not in listing and "1 matching record<" in listing
    assert client.get(f"/staff/patients/{second_patient}", follow_redirects=False).headers["location"] == NOT_FOUND_URL
    # QR validation is the one thing that does not follow assignments.
    assert "OFFER VALID" in client.post("/staff/validate", data={"token": second_uid}).text
    home = client.get("/staff/home").text
    assert _stat(home, "Your active campaigns") == 1 and _stat(home, "Active registrations") == 1


# --- Staff: patients -------------------------------------------------------------------

def test_staff_patient_list_and_detail_are_isolated_by_campaign(client):
    other = _campaign("Other Campaign")
    own_patient, _, _ = _register(1, "9822000011", "Own Campaign Patient")
    other_patient, _, _ = _register(other, "9822000012", "Other Campaign Patient")
    _staff_session(client, [1])

    listing = client.get("/staff/patients").text
    assert "Own Campaign Patient" in listing and "Other Campaign Patient" not in listing
    assert "Other Campaign" not in listing  # not even in the campaign filter
    for query in ("q=Other+Campaign+Patient", "q=9822000012", f"campaign_id={other}", "status=ACTIVE"):
        assert "<b>Other Campaign Patient</b>" not in client.get(f"/staff/patients?{query}").text  # no result row
    assert "0 matching records" in client.get(f"/staff/patients?campaign_id={other}").text

    assert client.get(f"/staff/patients/{own_patient}").status_code == 200
    hidden = client.get(f"/staff/patients/{other_patient}", follow_redirects=False)
    missing = client.get("/staff/patients/99999", follow_redirects=False)
    # Another campaign's patient is indistinguishable from one that does not exist.
    assert (hidden.status_code, hidden.headers["location"]) == (missing.status_code, missing.headers["location"]) == (303, NOT_FOUND_URL)
    assert "Other Campaign Patient" not in client.get(f"/staff/patients/{other_patient}").text

    deliveries = _count(DeliveryLog)
    response = client.post(f"/staff/patients/{other_patient}/delivery/whatsapp", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, NOT_FOUND_URL)
    assert _count(DeliveryLog) == deliveries


# --- Staff: QR validation and redemption (open to all staff, not campaign-scoped) -------

FAKE_UID, FAKE_PATIENT = "SRD-0000ABCD", 99999
INVALID_MESSAGE = "This QR/token is not registered in the Smriti Raj Dentistry system."


def _visible(response):
    """Everything a browser can observe about a response, minus the per-request CSRF token."""
    body = re.sub(r'name="_csrf_token" value="[^"]+"', 'name="_csrf_token" value="-"', response.text)
    headers = {key: value for key, value in response.headers.items() if key.lower() not in ("set-cookie", "date")}
    return response.status_code, headers, body


def _qr_views(client, uid):
    """What a logged-in staff member is shown for a QR at each read-only entry point."""
    from app.qr.service import token_for

    return {
        "validate by id": _visible(client.post("/staff/validate", data={"token": uid})),
        "validate by token": _visible(client.post("/staff/validate", data={"token": token_for(uid)})),
        "result page": _visible(client.get(f"/staff/validate/result/{uid}", follow_redirects=False)),
        "image": _visible(client.get(f"/qr/{uid}.png", follow_redirects=False)),
        "download": _visible(client.get(f"/qr/{uid}/download", follow_redirects=False)),
        "print": _visible(client.get(f"/qr/{uid}/print", follow_redirects=False)),
    }


def test_unassigned_staff_get_the_same_valid_qr_result_as_assigned_staff(client):
    other = _campaign("Other Campaign")
    patient_id, coupon_id, coupon_uid = _register(other, "9822000040", "Other Campaign Patient")

    _staff_session(client, [other], username="assigned-staff")
    assigned_views = _qr_views(client, coupon_uid)
    _staff_session(client, [1], username="unassigned-staff")
    unassigned_views = _qr_views(client, coupon_uid)
    _staff_session(client, [], username="no-campaign-staff")
    no_campaign_views = _qr_views(client, coupon_uid)

    assert unassigned_views == assigned_views == no_campaign_views
    for key in ("validate by id", "validate by token", "result page"):
        status, _, body = unassigned_views[key]
        assert status == 200 and "OFFER VALID" in body
        # The authorized QR details and the Redeem button are shown.
        assert "Other Campaign Patient" in body and "Free In-House Zirconia Crown" in body
        assert f'action="/staff/patients/{patient_id}/redeem"' in body and "Redeem offer" in body
    assert unassigned_views["image"][0] == 200 and unassigned_views["image"][1]["content-type"] == "image/png"
    assert "attachment" in unassigned_views["download"][1]["content-disposition"]
    assert unassigned_views["print"][0] == 200 and coupon_uid in unassigned_views["print"][2]
    assert "Other Campaign Patient" not in unassigned_views["print"][2]  # the printable coupon carries no patient details
    assert _coupon(coupon_id)[0] == "ACTIVE"  # looking is not redeeming
    assert {details["result"] for _, details, _ in _audits("QR_VALIDATED")} == {"VALID"}


@pytest.mark.parametrize("assignments", ["another campaign", "no campaigns"])
def test_staff_can_redeem_a_valid_qr_from_a_campaign_they_are_not_assigned_to(client, assignments):
    other = _campaign("Other Campaign")
    patient_id, coupon_id, coupon_uid = _register(other, "9822000041", "Other Campaign Patient")
    _staff_session(client, [1] if assignments == "another campaign" else [])

    assert "OFFER VALID" in client.post("/staff/validate", data={"token": coupon_uid}).text
    response = client.post(f"/staff/patients/{patient_id}/redeem", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, f"/staff/validate/result/{coupon_uid}")
    receipt = client.get(response.headers["location"]).text
    assert "Offer redeemed successfully" in receipt and "Other Campaign Patient" in receipt

    status, redeemed_at, redeemed_by, _ = _coupon(coupon_id)
    assert status == "REDEEMED" and redeemed_at is not None and redeemed_by == "staff-user"
    [(user, _, _)] = _audits("QR_REDEEMED")
    assert user == "staff-user"
    # One-time use still holds, for this and every other staff member.
    client.post(f"/staff/patients/{patient_id}/redeem")
    assert len(_audits("QR_REDEEMED")) == 1
    assert "OFFER ALREADY USED" in client.post("/staff/validate", data={"token": coupon_uid}).text

    # Redeeming does not widen campaign scope: the patient record and lists stay hidden.
    assert "Other Campaign Patient" not in client.get("/staff/patients").text
    assert client.get(f"/staff/patients/{patient_id}", follow_redirects=False).headers["location"] == NOT_FOUND_URL
    assert client.post("/staff/register", data=_registration(other, "9822000042")).status_code == 403
    assert _stat(client.get("/staff/home").text, "Active registrations") == 0


def test_expired_and_cancelled_qrs_cannot_be_redeemed_by_any_staff(client):
    other = _campaign("Other Campaign")
    expired_patient, expired_coupon, expired_uid = _register(other, "9822000043", "Expired Patient")
    cancelled_patient, cancelled_coupon, cancelled_uid = _register(other, "9822000044", "Cancelled Patient")
    db = SessionLocal()
    try:
        db.get(PatientOffer, expired_coupon).expires_at = db.get(PatientOffer, expired_coupon).created_at - timedelta(days=1)
        db.get(PatientOffer, cancelled_coupon).status = "CANCELLED"
        db.commit()
    finally:
        db.close()
    _staff_session(client, [1])

    assert "OFFER EXPIRED" in client.post("/staff/validate", data={"token": expired_uid}).text
    assert "OFFER CANCELLED" in client.post("/staff/validate", data={"token": cancelled_uid}).text
    client.post(f"/staff/patients/{expired_patient}/redeem")
    client.post(f"/staff/patients/{cancelled_patient}/redeem")
    assert _coupon(expired_coupon)[0] == "EXPIRED" and _coupon(cancelled_coupon)[0] == "CANCELLED"
    assert _audits("QR_REDEEMED") == []


def test_nonexistent_qr_stays_generic_and_private_for_staff(client):
    other = _campaign("Other Campaign")
    _register(other, "9822000045", "Other Campaign Patient")
    _staff_session(client, [1])

    views = _qr_views(client, FAKE_UID)

    for key in ("validate by id", "validate by token"):
        status, _, body = views[key]
        assert status == 200 and "INVALID QR" in body and INVALID_MESSAGE in body
        for leak in ("Other Campaign", "9822000045", "Redeem offer", "/redeem", "OFFER VALID"):
            assert leak not in body
    assert (views["result page"][0], views["result page"][1]["location"]) == (303, "/staff/validate")
    for key in ("image", "download", "print"):  # a clean staff-safe 404, never a redirect to an admin page
        status, headers, body = views[key]
        assert status == 404 and body == "QR not found" and "location" not in headers
    response = client.post(f"/staff/patients/{FAKE_PATIENT}/redeem", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/staff/validate")
    assert _audits("QR_REDEEMED") == []
    assert {details["result"] for _, details, _ in _audits("QR_VALIDATED")} == {"INVALID"}


def test_qr_routes_stay_blocked_without_a_login(client):
    patient_id, coupon_id, coupon_uid = _register(1, "9822000046", "Logged Out Patient")
    client.post("/logout")

    for url in ("/staff/validate", f"/staff/validate/result/{coupon_uid}", f"/qr/{coupon_uid}.png", f"/qr/{coupon_uid}/download", f"/qr/{coupon_uid}/print"):
        response = client.get(url, follow_redirects=False)
        assert response.status_code == 303 and response.headers["location"] in ("/login", "/staff/login")
        assert "Logged Out Patient" not in client.get(url).text
    for url, data in (("/staff/validate", {"token": coupon_uid}), (f"/staff/patients/{patient_id}/redeem", {})):
        assert "Logged Out Patient" not in client.post(url, data=data).text
        assert client.post(url, data=data, include_csrf=False, follow_redirects=False).status_code in (303, 403)
    assert _coupon(coupon_id)[0] == "ACTIVE" and _audits("QR_VALIDATED") == [] and _audits("QR_REDEEMED") == []


def test_staff_redeem_still_requires_csrf(client):
    patient_id, coupon_id, _ = _register(1, "9822000047", "Csrf Patient")
    _staff_session(client, [])
    assert client.post(f"/staff/patients/{patient_id}/redeem", include_csrf=False, follow_redirects=False).status_code == 403
    assert _coupon(coupon_id)[0] == "ACTIVE"


def test_admin_qr_access_is_unchanged(client):
    other = _campaign("Other Campaign")
    _create_staff(campaign_ids=[1])
    _, coupon_id, coupon_uid = _register(other, "9822000041", "Other Campaign Patient")

    assert client.get(f"/qr/{coupon_uid}.png").headers["content-type"] == "image/png"
    assert "attachment" in client.get(f"/qr/{coupon_uid}/download").headers["content-disposition"]
    printed = client.get(f"/qr/{coupon_uid}/print").text
    assert coupon_uid in printed and "Other Campaign Patient" not in printed
    for suffix in (".png", "/download", "/print"):  # a missing QR still sends an admin to the coupon ledger
        response = client.get(f"/qr/SRD-0000ABCD{suffix}", follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/offers")
    page = client.post("/validate", data={"token": coupon_uid}).text
    assert "OFFER VALID" in page and "Other Campaign Patient" in page and f"/redeem/{coupon_id}" in page


# --- Staff: home and the zero-assignment state -----------------------------------------

def test_staff_home_counts_are_scoped_to_assigned_campaigns(client):
    other = _campaign("Other Campaign")
    _register(1, "9822000015", "Own One")
    _register(1, "9822000016", "Own Two")
    for index in range(3):
        _register(other, f"982200002{index}", "Other Patient")
    _staff_session(client, [1])

    home = client.get("/staff/home").text

    assert _stat(home, "Your active campaigns") == 1 and _stat(home, "Active registrations") == 2
    assert "Test Active Campaign" in home and "Other Campaign" not in home
    assert NO_ASSIGNMENTS not in home


def test_staff_with_no_assignments_sees_an_empty_state_and_is_denied_campaign_work(client):
    patient_id, coupon_id, coupon_uid = _register(1, "9822000030", "Hidden Patient")
    before = _coupon(coupon_id)
    _staff_session(client, [])

    home = client.get("/staff/home").text
    assert NO_ASSIGNMENTS in home
    assert _stat(home, "Your active campaigns") == 0 and _stat(home, "Active registrations") == 0
    assert "Test Active Campaign" not in home

    register_page = client.get("/staff/register").text
    assert NO_ASSIGNMENTS in register_page and _campaign_options(register_page) == set()
    response = client.post("/staff/register", data=_registration(1, "9822000031"))
    assert response.status_code == 403 and "not assigned to this campaign" in response.text

    listing = client.get("/staff/patients").text
    assert NO_ASSIGNMENTS in listing and "Hidden Patient" not in listing and "0 matching records" in listing
    assert "Hidden Patient" not in client.get("/staff/patients?q=Hidden").text
    assert client.get(f"/staff/patients/{patient_id}", follow_redirects=False).headers["location"] == NOT_FOUND_URL
    assert client.post(f"/staff/patients/{patient_id}/delivery/whatsapp", follow_redirects=False).headers["location"] == NOT_FOUND_URL

    # Scanning is not campaign work: a QR can still be validated (redemption is covered above).
    assert "OFFER VALID" in client.post("/staff/validate", data={"token": coupon_uid}).text
    assert _coupon(coupon_id) == before and _count(PatientOffer) == 1


def test_one_staff_members_assignments_do_not_leak_to_another(client):
    other = _campaign("Other Campaign")
    _register(1, "9822000032", "Campaign One Patient")
    _register(other, "9822000033", "Other Campaign Patient")
    _create_staff(username="first-staff", campaign_ids=[1])
    _staff_session(client, [other], username="second-staff")

    listing = client.get("/staff/patients").text
    assert "Other Campaign Patient" in listing and "Campaign One Patient" not in listing
    assert _campaign_options(client.get("/staff/register").text) == {other}


# --- Admin stays unrestricted ----------------------------------------------------------

def test_admin_is_not_restricted_by_assignments(client):
    other = _campaign("Other Campaign")
    _create_staff(campaign_ids=[1])  # assignments exist, but none for the admin
    patient_id, coupon_id, coupon_uid = _register(other, "9822000034", "Other Campaign Patient")
    _register(1, "9822000035", "Campaign One Patient")

    listing = client.get("/patients").text
    assert "Other Campaign Patient" in listing and "Campaign One Patient" in listing
    assert "Other Campaign Patient" in client.get(f"/patients/{patient_id}").text
    assert _campaign_options(client.get("/patients/register").text) == {1, other}
    response = client.post("/patients/register", data=_registration(other, "9822000036"), follow_redirects=False)
    assert response.status_code == 303
    assert client.get(f"/qr/{coupon_uid}.png").headers["content-type"] == "image/png"
    assert "OFFER VALID" in client.post("/validate", data={"token": coupon_uid}).text
    assert "Offer redeemed successfully" in client.post(f"/redeem/{coupon_id}", follow_redirects=True).text
    assert _coupon(coupon_id)[0] == "REDEEMED"
    for url in ("/admin/dashboard", "/admin/campaigns", f"/admin/campaigns/{other}", "/offers", "/redemptions", "/delivery"):
        assert client.get(url).status_code == 200


# --- Concurrency (PostgreSQL only) -----------------------------------------------------

@pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs PostgreSQL row locks; set TEST_DATABASE_URL to a scratch Postgres database",
)
def test_registration_check_waits_for_a_concurrent_unassignment(client):
    """The assignment check a staff registration makes inside its own transaction cannot
    slip past an unassignment that is committing at the same moment."""
    staff_id = _create_staff(campaign_ids=[1])
    request = SimpleNamespace(session={"role": "staff", "staff_user_id": staff_id})
    outcome = {}

    def check():
        db = SessionLocal()
        try:
            outcome["assigned"] = staff_assigned_to(db, request, 1, lock=True)
        finally:
            db.rollback(); db.close()

    admin = SessionLocal()
    try:
        admin.execute(staff_campaigns.delete().where(staff_campaigns.c.staff_user_id == staff_id))
        thread = threading.Thread(target=check)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive() and not outcome  # waiting on the uncommitted unassignment
        admin.commit()
        thread.join(timeout=30)
    finally:
        admin.rollback(); admin.close()
    assert outcome == {"assigned": False}
