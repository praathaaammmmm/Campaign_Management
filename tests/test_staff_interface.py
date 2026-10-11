from app.auth import password_hasher
from app.database import SessionLocal
from app.models import AuditLog, Campaign, Patient, StaffUser
from app.routes.auth import _attempts


def _create_staff(username="staff-user", password="staff-password", campaign_ids=None):
    """Create a staff account. Staff only have access to assigned campaigns, so by default
    the account is assigned every campaign that exists at this point; pass an explicit
    ``campaign_ids`` list (possibly empty) to test scoped or unassigned staff.
    """
    db = SessionLocal()
    try:
        user = StaffUser(
            username=username,
            password_hash=password_hasher.hash(password),
            role="staff",
            active=True,
        )
        query = db.query(Campaign)
        if campaign_ids is not None:
            query = query.filter(Campaign.id.in_(campaign_ids))
        user.campaigns = query.all()
        db.add(user)
        db.commit()
        return user.id
    finally:
        db.close()


def _login_as_staff(client, campaign_ids=None):
    _create_staff(campaign_ids=campaign_ids)
    _attempts.clear()
    client.post("/logout")
    response = client.post(
        "/login",
        data={"username": "staff-user", "password": "staff-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/staff/home"


def test_staff_session_is_rejected_from_admin_urls(client):
    """Hidden admin navigation is not the authorization boundary."""
    _login_as_staff(client)
    response = client.get("/admin/dashboard", follow_redirects=False)
    assert response.status_code == 403
    response = client.get("/admin/campaigns", follow_redirects=False)
    assert response.status_code == 403


def test_staff_registration_uses_active_campaign_and_audits_actor(client):
    _login_as_staff(client)
    response = client.post(
        "/staff/register",
        data={
            "full_name": "Staff Registered Patient", "mobile": "9876543210",
            "campaign_id": "1", "offer_id": "1", "beneficiary_category": "ECHS", "consent_given": "true",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/staff/patients/")
    db = SessionLocal()
    try:
        patient = db.query(Patient).filter_by(full_name="Staff Registered Patient").one()
        assert patient.offers[0].campaign_id == 1
        event = db.query(AuditLog).filter_by(patient_id=patient.id, action="PATIENT_REGISTERED").one()
        assert event.user == "staff-user"
    finally:
        db.close()
