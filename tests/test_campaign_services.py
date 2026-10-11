"""Adding and removing services on a live campaign.

The `client` fixture provides campaign 1 ("Test Active Campaign", ACTIVE) carrying
offers 1 (Zirconia Crown) and 2 (Aligner Scan), and an admin session.
"""
import json
import threading
from datetime import date, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal, engine
from app.models import AuditLog, Campaign, Offer, Patient, PatientOffer, campaign_offers
from app.registrations.service import OfferUnavailableError, _offer_assigned, register_patient_offer
from app.time_utils import utc_now
from tests.test_campaign_offer_scoping import _data_campaigns_for
from tests.test_staff_interface import _login_as_staff

CAMPAIGN_ID = 1
CROWN_ID = 1
SERVICES_URL = f"/admin/campaigns/{CAMPAIGN_ID}/services"


def _registration(mobile, offer_id, campaign_id=CAMPAIGN_ID):
    return {
        "full_name": "Service Change Patient", "mobile": mobile, "campaign_id": str(campaign_id),
        "offer_id": str(offer_id), "beneficiary_category": "CGHS", "consent_given": "true",
    }


def _assigned_ids(campaign_id=CAMPAIGN_ID):
    db = SessionLocal()
    try:
        return set(db.execute(
            select(campaign_offers.c.offer_id).where(campaign_offers.c.campaign_id == campaign_id)
        ).scalars())
    finally:
        db.close()


def _offer_id(name):
    db = SessionLocal()
    try:
        return db.query(Offer).filter_by(name=name).one().id
    finally:
        db.close()


def _audits(action):
    db = SessionLocal()
    try:
        rows = db.query(AuditLog).filter_by(action=action).order_by(AuditLog.id).all()
        return [(row.user, json.loads(row.details), row.timestamp) for row in rows]
    finally:
        db.close()


def _count(model):
    db = SessionLocal()
    try:
        return db.query(model).count()
    finally:
        db.close()


def _add_new(client, name, description="Added mid-campaign"):
    return client.post(SERVICES_URL, data={"name": name, "description": description}, follow_redirects=False)


def _remove(client, offer_id, campaign_id=CAMPAIGN_ID):
    return client.post(f"/admin/campaigns/{campaign_id}/services/{offer_id}/remove", follow_redirects=False)


def test_admin_creates_a_service_on_an_active_campaign(client):
    before = utc_now()
    response = _add_new(client, "  Free   Whitening Consult ")

    assert response.status_code == 303
    assert response.headers["location"].startswith(f"/admin/campaigns/{CAMPAIGN_ID}?message=")
    new_id = _offer_id("Free Whitening Consult")
    assert _assigned_ids() == {1, 2, new_id}
    db = SessionLocal()
    try:
        assert db.get(Offer, new_id).active is True
        assert db.get(Campaign, CAMPAIGN_ID).status == "ACTIVE"
    finally:
        db.close()

    [(user, details, timestamp)] = _audits("CAMPAIGN_SERVICE_ADDED")
    assert user == "admin"
    assert details == {
        "campaign": "Test Active Campaign", "campaign_id": CAMPAIGN_ID,
        "service": "Free Whitening Consult", "service_id": new_id,
    }
    assert before <= timestamp <= utc_now()
    assert [details for _, details, _ in _audits("SERVICE_CREATED")] == [{"name": "Free Whitening Consult"}]

    detail = client.get(f"/admin/campaigns/{CAMPAIGN_ID}")
    assert "Free Whitening Consult" in detail.text
    assert f"/admin/campaigns/{CAMPAIGN_ID}/services/{new_id}/remove" in detail.text


def test_admin_attaches_an_existing_catalog_service(client):
    client.post("/admin/services", data={"name": "Catalog Only Service", "description": "test"})
    offer_id = _offer_id("Catalog Only Service")
    assert offer_id not in _assigned_ids()
    assert f'<option value="{offer_id}">Catalog Only Service</option>' in client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text

    response = client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(offer_id)}, follow_redirects=False)

    assert response.status_code == 303
    assert offer_id in _assigned_ids()
    [(user, details, _)] = _audits("CAMPAIGN_SERVICE_ADDED")
    assert user == "admin" and details["service_id"] == offer_id and details["campaign_id"] == CAMPAIGN_ID


def test_newly_added_service_is_immediately_registrable(client):
    _add_new(client, "Free Whitening Consult")
    new_id = _offer_id("Free Whitening Consult")

    assert str(CAMPAIGN_ID) in _data_campaigns_for(client.get("/patients/register").text, "Free Whitening Consult")
    response = client.post("/patients/register", data=_registration("9811200001", new_id), follow_redirects=False)
    assert response.status_code == 303

    _login_as_staff(client)
    assert str(CAMPAIGN_ID) in _data_campaigns_for(client.get("/staff/register").text, "Free Whitening Consult")
    response = client.post("/staff/register", data=_registration("9811200002", new_id), follow_redirects=False)
    assert response.status_code == 303

    db = SessionLocal()
    try:
        coupons = db.query(PatientOffer).order_by(PatientOffer.id).all()
        assert [(c.offer_id, c.campaign_id, c.status) for c in coupons] == [(new_id, CAMPAIGN_ID, "ACTIVE")] * 2
    finally:
        db.close()


def test_removed_service_is_no_longer_selectable_or_registrable(client):
    response = _remove(client, CROWN_ID)

    assert response.status_code == 303
    assert _assigned_ids() == {2}
    [(user, details, _)] = _audits("CAMPAIGN_SERVICE_REMOVED")
    assert user == "admin"
    assert details == {
        "campaign": "Test Active Campaign", "campaign_id": CAMPAIGN_ID,
        "service": "Free In-House Zirconia Crown", "service_id": CROWN_ID,
    }
    # The catalog record survives; it is simply offered by no campaign.
    assert _data_campaigns_for(client.get("/patients/register").text, "Free In-House Zirconia Crown") == set()
    assert str(CAMPAIGN_ID) in _data_campaigns_for(client.get("/patients/register").text, "Free In-House Aligner Scan")

    # A hand-crafted submission for the removed service is rejected for admin and staff.
    response = client.post("/patients/register", data=_registration("9811200003", CROWN_ID))
    assert response.status_code == 422 and "not part of this campaign" in response.text
    _login_as_staff(client)
    assert _data_campaigns_for(client.get("/staff/register").text, "Free In-House Zirconia Crown") == set()
    response = client.post("/staff/register", data=_registration("9811200004", CROWN_ID))
    assert response.status_code == 422 and "not part of this campaign" in response.text
    assert _count(PatientOffer) == 0 and _count(Patient) == 0

    db = SessionLocal()
    try:
        with pytest.raises(OfferUnavailableError):
            register_patient_offer(
                db, full_name="Direct Call", mobile="9811200005", email="", age="", gender="", city="",
                doctor_name="", campaign_name="", campaign_id=CAMPAIGN_ID, offer_id=CROWN_ID,
                beneficiary_category="CGHS", consent_given=True, actor="admin",
            )
    finally:
        db.close()
    assert _count(PatientOffer) == 0


def test_removed_service_can_be_added_back(client):
    _remove(client, CROWN_ID)
    assert "Add back" not in client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text  # no history, so not listed
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(CROWN_ID)})
    assert _assigned_ids() == {1, 2}
    response = client.post("/patients/register", data=_registration("9811200006", CROWN_ID), follow_redirects=False)
    assert response.status_code == 303


def _snapshot():
    db = SessionLocal()
    try:
        tables = {}
        for model in (Patient, PatientOffer, Offer):
            rows = db.execute(select(model.__table__).order_by(model.__table__.c.id)).mappings().all()
            tables[model.__tablename__] = [dict(row) for row in rows]
        return tables
    finally:
        db.close()


def test_removal_leaves_historical_registrations_untouched(client):
    client.post("/patients/register", data={**_registration("9811200007", CROWN_ID), "full_name": "History Kept"})
    client.post("/patients/register", data={**_registration("9811200008", CROWN_ID), "full_name": "History Redeemed"})
    db = SessionLocal()
    try:
        kept, redeemed = db.query(PatientOffer).order_by(PatientOffer.id).all()
        kept_id, kept_uid, kept_patient = kept.id, kept.coupon_uid, kept.patient_id
        redeemed_id = redeemed.id
    finally:
        db.close()
    client.post(f"/redeem/{redeemed_id}")
    before = _snapshot()
    audit_rows_before = _count(AuditLog)

    assert _remove(client, CROWN_ID).status_code == 303

    assert _snapshot() == before  # patients, coupons/QR records, redemptions and the catalog
    assert _count(AuditLog) == audit_rows_before + 1  # only the removal event was added
    assert "History Kept" in client.get("/patients").text
    patient_page = client.get(f"/patients/{kept_patient}")
    assert patient_page.status_code == 200 and "Free In-House Zirconia Crown" in patient_page.text
    assert kept_uid in client.get("/offers").text
    assert "History Redeemed" in client.get("/redemptions").text
    assert "Free In-House Zirconia Crown" in client.get("/admin/reports/patients.csv").text

    # The campaign report still shows the removed service's numbers, marked as removed.
    detail = client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text
    row = next(chunk for chunk in detail.split("<tr>") if chunk.startswith("<td>Free In-House Zirconia Crown</td>"))
    assert "REMOVED" in row and "<td>2</td><td>1</td>" in row and "Add back" in row

    # A coupon issued before the removal is still valid and can still be redeemed.
    assert "OFFER VALID" in client.post("/validate", data={"token": kept_uid}).text
    assert "Offer redeemed successfully" in client.post(f"/redeem/{kept_id}", follow_redirects=True).text


def test_removal_only_unassigns_and_spares_other_campaigns(client):
    db = SessionLocal()
    try:
        other = Campaign(
            name="Second Campaign", start_date=date.today() - timedelta(days=1),
            end_date=date.today() + timedelta(days=1), status="ACTIVE", created_by="test",
        )
        other.offers = [db.get(Offer, CROWN_ID)]
        db.add(other)
        db.commit()
        other_id = other.id
    finally:
        db.close()

    _remove(client, CROWN_ID)

    assert CROWN_ID not in _assigned_ids() and _assigned_ids(other_id) == {CROWN_ID}
    assert _offer_id("Free In-House Zirconia Crown") == CROWN_ID
    assert _data_campaigns_for(client.get("/patients/register").text, "Free In-House Zirconia Crown") == {str(other_id)}
    response = client.post("/patients/register", data=_registration("9811200009", CROWN_ID, other_id), follow_redirects=False)
    assert response.status_code == 303
    assert "Free In-House Zirconia Crown" in client.get("/admin/services").text


def test_campaign_has_no_service_count_limit(client):
    total = 60
    for index in range(total):
        assert _add_new(client, f"Bulk Service {index:03d}").status_code == 303

    assert len(_assigned_ids()) == total + 2
    assert len(_audits("CAMPAIGN_SERVICE_ADDED")) == total
    register_page = client.get("/patients/register").text
    detail_page = client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text
    for index in range(total):
        assert f"Bulk Service {index:03d}" in detail_page
        assert str(CAMPAIGN_ID) in _data_campaigns_for(register_page, f"Bulk Service {index:03d}")
    assert 'class="offer-search"' in register_page  # long lists get a search box

    last_id = _offer_id(f"Bulk Service {total - 1:03d}")
    response = client.post("/patients/register", data=_registration("9811200010", last_id), follow_redirects=False)
    assert response.status_code == 303


def test_invalid_or_repeated_changes_are_rejected_without_audit_noise(client):
    # Already assigned.
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(CROWN_ID)})
    # Name already in the catalog: nothing created, nothing attached.
    response = _add_new(client, "Free In-House Zirconia Crown")
    assert "already%20exists" in response.headers["location"]
    # Blank name, unknown service, unknown campaign.
    _add_new(client, "   ")
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": "9999"})
    assert _remove(client, 9999).status_code == 303
    assert _add_new(client, "Orphan").status_code == 303  # control: valid add still works
    client.post("/admin/campaigns/9999/services", data={"name": "Nowhere", "description": "x"})
    # Inactive catalog services cannot be attached.
    client.post("/admin/services", data={"name": "Retired Service", "description": "test"})
    retired_id = _offer_id("Retired Service")
    client.post(f"/admin/services/{retired_id}/toggle")
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(retired_id)})
    # Removing twice only removes (and audits) once.
    _remove(client, CROWN_ID)
    _remove(client, CROWN_ID)

    assert _assigned_ids() == {2, _offer_id("Orphan")}
    assert len(_audits("CAMPAIGN_SERVICE_ADDED")) == 1
    assert len(_audits("CAMPAIGN_SERVICE_REMOVED")) == 1
    db = SessionLocal()
    try:
        assert db.query(Offer).filter_by(name="Free In-House Zirconia Crown").count() == 1
        assert db.query(Offer).filter_by(name="Nowhere").count() == 0
    finally:
        db.close()


def test_staff_cannot_manage_campaign_services(client):
    _login_as_staff(client)

    responses = [
        _add_new(client, "Staff Made Service"),
        client.post(f"{SERVICES_URL}/attach", data={"offer_id": "2"}, follow_redirects=False),
        _remove(client, CROWN_ID),
        client.get(f"/admin/campaigns/{CAMPAIGN_ID}", follow_redirects=False),
    ]

    assert [response.status_code for response in responses] == [403] * 4
    assert _assigned_ids() == {1, 2}
    assert _count(Offer) == 2
    assert _audits("CAMPAIGN_SERVICE_ADDED") == [] and _audits("CAMPAIGN_SERVICE_REMOVED") == []


def test_service_changes_require_login_and_csrf(client):
    assert _remove(client, CROWN_ID).status_code == 303  # control, as admin
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(CROWN_ID)})

    response = client.post(f"/admin/campaigns/{CAMPAIGN_ID}/services/{CROWN_ID}/remove", include_csrf=False, follow_redirects=False)
    assert response.status_code == 403
    response = client.post(SERVICES_URL, include_csrf=False, data={"name": "No Token", "description": "x"}, follow_redirects=False)
    assert response.status_code == 403

    client.post("/logout")
    response = _remove(client, CROWN_ID)
    assert response.status_code in (303, 403) and response.headers.get("location") != f"/admin/campaigns/{CAMPAIGN_ID}"
    assert _assigned_ids() == {1, 2}
    assert len(_audits("CAMPAIGN_SERVICE_REMOVED")) == 1


postgres_only = pytest.mark.skipif(
    engine.dialect.name != "postgresql",
    reason="needs PostgreSQL row locks; set TEST_DATABASE_URL to a scratch Postgres database",
)


def _register_in_thread(mobile):
    outcome = {}

    def run():
        db = SessionLocal()
        try:
            outcome["coupon_id"] = register_patient_offer(
                db, full_name="Racing Patient", mobile=mobile, email="", age="", gender="", city="",
                doctor_name="", campaign_name="", campaign_id=CAMPAIGN_ID, offer_id=CROWN_ID,
                beneficiary_category="CGHS", consent_given=True, actor="staff-a",
            ).id
        except Exception as exc:  # asserted on by the test
            outcome["error"] = exc
        finally:
            db.close()

    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


@postgres_only
def test_registration_waits_for_a_concurrent_removal_and_is_then_rejected(client):
    """A removal that is in flight when a registration arrives wins once it commits."""
    admin = SessionLocal()
    try:
        admin.execute(campaign_offers.delete().where(
            campaign_offers.c.campaign_id == CAMPAIGN_ID, campaign_offers.c.offer_id == CROWN_ID,
        ))
        thread, outcome = _register_in_thread("9811200011")
        thread.join(timeout=1)
        assert thread.is_alive() and not outcome  # blocked on the uncommitted removal
        admin.commit()
        thread.join(timeout=30)
    finally:
        admin.rollback()
        admin.close()

    assert isinstance(outcome.get("error"), OfferUnavailableError)
    assert _count(PatientOffer) == 0 and _count(Patient) == 0


@postgres_only
def test_removal_waits_for_an_in_flight_registration(client):
    """A registration that has already verified the service is never cut off by a removal."""
    registering = SessionLocal()
    admin = SessionLocal()
    try:
        assert _offer_assigned(registering, CAMPAIGN_ID, CROWN_ID) is True  # holds the share lock
        admin.execute(text("SET LOCAL lock_timeout = '500ms'"))
        with pytest.raises(OperationalError):
            admin.execute(campaign_offers.delete().where(
                campaign_offers.c.campaign_id == CAMPAIGN_ID, campaign_offers.c.offer_id == CROWN_ID,
            ))
        admin.rollback()
        # Other registrations for the same service are not held up by that lock.
        thread, outcome = _register_in_thread("9811200012")
        thread.join(timeout=30)
        assert outcome.get("coupon_id") is not None
        registering.rollback()
        assert admin.execute(campaign_offers.delete().where(
            campaign_offers.c.campaign_id == CAMPAIGN_ID, campaign_offers.c.offer_id == CROWN_ID,
        )).rowcount == 1
        admin.commit()
    finally:
        registering.rollback(); registering.close()
        admin.rollback(); admin.close()
    assert _assigned_ids() == {2}


ALIGNER_ID = 2


def _set_campaign_status(status, campaign_id=CAMPAIGN_ID):
    db = SessionLocal()
    try:
        db.get(Campaign, campaign_id).status = status
        db.commit()
    finally:
        db.close()


def test_inactive_but_attached_service_is_hidden_and_rejected(client):
    stale_form = client.get("/patients/register").text  # opened before the deactivation
    assert "Free In-House Aligner Scan" in stale_form

    client.post(f"/admin/services/{ALIGNER_ID}/toggle")

    assert ALIGNER_ID in _assigned_ids()  # still attached to the campaign
    assert "Free In-House Aligner Scan" not in client.get("/patients/register").text
    response = client.post("/patients/register", data=_registration("9811200020", ALIGNER_ID))
    assert response.status_code == 422 and "no longer available" in response.text
    assert "Free In-House Aligner Scan" not in response.text  # the re-rendered form hides it too

    db = SessionLocal()
    try:
        with pytest.raises(OfferUnavailableError):
            register_patient_offer(
                db, full_name="Direct Call", mobile="9811200021", email="", age="", gender="", city="",
                doctor_name="", campaign_name="", campaign_id=CAMPAIGN_ID, offer_id=ALIGNER_ID,
                beneficiary_category="CGHS", consent_given=True, actor="admin",
            )
    finally:
        db.close()

    _login_as_staff(client)
    assert "Free In-House Aligner Scan" not in client.get("/staff/register").text
    response = client.post("/staff/register", data=_registration("9811200022", ALIGNER_ID))
    assert response.status_code == 422 and "no longer available" in response.text
    assert _count(PatientOffer) == 0 and _count(Patient) == 0


def test_reactivated_service_is_registrable_again(client):
    client.post(f"/admin/services/{ALIGNER_ID}/toggle")
    client.post(f"/admin/services/{ALIGNER_ID}/toggle")
    response = client.post("/patients/register", data=_registration("9811200023", ALIGNER_ID), follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize("status", ["DRAFT", "ACTIVE", "PAUSED"])
def test_services_can_be_managed_in_every_open_status(client, status):
    _set_campaign_status(status)
    assert _add_new(client, "Open Status Service").status_code == 303
    new_id = _offer_id("Open Status Service")
    _remove(client, new_id)
    client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(new_id)})
    _remove(client, CROWN_ID)

    assert _assigned_ids() == {ALIGNER_ID, new_id}
    assert len(_audits("CAMPAIGN_SERVICE_ADDED")) == 2 and len(_audits("CAMPAIGN_SERVICE_REMOVED")) == 2
    assert "Create &amp; add to campaign" in client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text


def test_closed_campaign_services_are_read_only(client):
    client.post("/patients/register", data=_registration("9811200024", CROWN_ID))
    client.post("/patients/register", data=_registration("9811200025", ALIGNER_ID))
    client.post("/admin/services", data={"name": "Catalog Only Service", "description": "test"})
    spare_id = _offer_id("Catalog Only Service")
    _remove(client, ALIGNER_ID)  # removed while open: has history, so it is listed with "Add back"
    assert "Add back" in client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text
    assert client.post(f"/admin/campaigns/{CAMPAIGN_ID}/close", follow_redirects=False).status_code == 303
    before, audit_rows_before, offers_before = _snapshot(), _count(AuditLog), _count(Offer)

    responses = [
        _add_new(client, "Too Late Service"),
        client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(spare_id)}, follow_redirects=False),
        client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(ALIGNER_ID)}, follow_redirects=False),  # re-add
        _remove(client, CROWN_ID),
    ]

    for response in responses:
        assert response.status_code == 303
        assert "campaign%20is%20closed" in response.headers["location"]
    assert "This campaign is closed" in client.get(responses[0].headers["location"]).text
    assert _assigned_ids() == {CROWN_ID}
    assert _count(Offer) == offers_before and _count(AuditLog) == audit_rows_before
    assert _snapshot() == before  # registrations, coupons and the catalog are untouched

    detail = client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text
    assert f"{SERVICES_URL}" not in detail  # no add, attach, remove or add-back forms
    assert "Create &amp; add to campaign" not in detail and "Add back" not in detail
    assert "Free In-House Zirconia Crown" in detail and "Free In-House Aligner Scan" in detail  # history still shown
    assert "REMOVED" in detail


def _nav_link(html, href):
    sidebar = html.split('<nav class="side-nav"', 1)[1].split("</nav>", 1)[0]
    return next((line for line in sidebar.splitlines() if f'href="{href}"' in line), None)


def test_admin_sidebar_has_a_campaigns_link_with_correct_active_state(client):
    for url in ("/admin/campaigns", f"/admin/campaigns/{CAMPAIGN_ID}"):
        page = client.get(url).text
        campaigns, dashboard = _nav_link(page, "/admin/campaigns"), _nav_link(page, "/admin/dashboard")
        assert "Campaigns</a>" in campaigns
        assert 'class="active"' in campaigns and 'aria-current="page"' in campaigns
        assert "Admin CRM</a>" in dashboard
        assert 'class="active"' not in dashboard and "aria-current" not in dashboard

    page = client.get("/admin/dashboard").text
    assert 'class="active"' in _nav_link(page, "/admin/dashboard")
    assert 'class="active"' not in _nav_link(page, "/admin/campaigns")
    page = client.get("/patients").text
    assert 'class="active"' not in _nav_link(page, "/admin/campaigns")


def test_staff_sidebar_has_no_campaign_navigation(client):
    _login_as_staff(client)
    for url in ("/staff/home", "/staff/register", "/staff/patients"):
        assert "/admin/campaigns" not in client.get(url).text
    assert client.get("/admin/campaigns", follow_redirects=False).status_code == 403


def _campaign_status(campaign_id=CAMPAIGN_ID):
    db = SessionLocal()
    try:
        return db.get(Campaign, campaign_id).status
    finally:
        db.close()


@pytest.mark.parametrize("action", ["start", "pause", "close"])
def test_closed_campaign_status_cannot_be_changed_by_crafted_post(client, action):
    assert client.post(f"/admin/campaigns/{CAMPAIGN_ID}/close", follow_redirects=False).status_code == 303
    assert _campaign_status() == "CLOSED"
    audit_rows_before = _count(AuditLog)

    response = client.post(f"/admin/campaigns/{CAMPAIGN_ID}/{action}", follow_redirects=False)

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/admin/campaigns/{CAMPAIGN_ID}?message=") and "campaign%20is%20closed" in location
    assert _campaign_status() == "CLOSED"
    assert _count(AuditLog) == audit_rows_before  # no CAMPAIGN_ACTIVE / CAMPAIGN_PAUSED / second CAMPAIGN_CLOSED
    assert "This campaign is closed. Its status can no longer be changed" in client.get(location).text


def test_closed_campaign_stays_closed_and_read_only_after_reopen_attempts(client):
    client.post("/patients/register", data=_registration("9811200030", CROWN_ID))
    client.post(f"/admin/campaigns/{CAMPAIGN_ID}/close")
    before, audit_rows_before = _snapshot(), _count(AuditLog)

    client.post(f"/admin/campaigns/{CAMPAIGN_ID}/start")
    client.post(f"/admin/campaigns/{CAMPAIGN_ID}/pause")
    # With the campaign still closed, every service route keeps refusing.
    responses = [
        _add_new(client, "After Reopen Attempt"),
        client.post(f"{SERVICES_URL}/attach", data={"offer_id": str(ALIGNER_ID)}, follow_redirects=False),
        _remove(client, CROWN_ID),
    ]

    assert _campaign_status() == "CLOSED"
    for response in responses:
        assert response.status_code == 303 and "campaign%20is%20closed" in response.headers["location"]
    assert _assigned_ids() == {CROWN_ID, ALIGNER_ID}
    assert _count(AuditLog) == audit_rows_before and _snapshot() == before
    detail = client.get(f"/admin/campaigns/{CAMPAIGN_ID}").text
    assert SERVICES_URL not in detail and "Create &amp; add to campaign" not in detail
    for action in ("start", "pause", "close"):
        assert f"/admin/campaigns/{CAMPAIGN_ID}/{action}" not in detail  # no status buttons either
    # And it takes no registrations: admin form does not list it, a crafted POST is rejected.
    assert "Test Active Campaign" not in client.get("/patients/register").text
    response = client.post("/patients/register", data=_registration("9811200031", CROWN_ID))
    assert response.status_code == 422 and "active campaign" in response.text
    assert _count(PatientOffer) == 1


@pytest.mark.parametrize("status, action, expected", [
    ("DRAFT", "start", "ACTIVE"), ("ACTIVE", "pause", "PAUSED"), ("PAUSED", "start", "ACTIVE"), ("PAUSED", "close", "CLOSED"),
])
def test_open_campaign_status_changes_still_work(client, status, action, expected):
    _set_campaign_status(status)
    response = client.post(f"/admin/campaigns/{CAMPAIGN_ID}/{action}", follow_redirects=False)
    assert response.status_code == 303 and "message" not in response.headers["location"]
    assert _campaign_status() == expected
    assert len(_audits_plain(f"CAMPAIGN_{expected}")) == 1


def _audits_plain(action):
    db = SessionLocal()
    try:
        return db.query(AuditLog.id).filter_by(action=action).all()
    finally:
        db.close()
