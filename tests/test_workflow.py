from datetime import timedelta
from app.database import SessionLocal
from app.models import PatientOffer
from app.qr.service import token_for, token_hash

def registration(name, mobile, offer="1", consent="true", beneficiary_category="CGHS"):
    return {"full_name": name, "mobile": mobile, "campaign_id": "1", "offer_id": offer, "consent_given": consent, "beneficiary_category": beneficiary_category}

def test_patient_registration_and_qr(client):
    r = client.post("/patients/register", data={"full_name":"Test Patient", "mobile":"9999999999", "email":"", "age":"30", "gender":"Male", "city":"Delhi", "doctor_name":"", "campaign_name":"Test", "campaign_id":"1", "offer_id":"1", "beneficiary_category":"CGHS", "consent_given":"true"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/patients/")
    db = SessionLocal(); coupon = db.query(PatientOffer).first()
    assert coupon and len(coupon.secure_token_hash) == 64
    assert coupon.secure_token_hash == token_hash(token_for(coupon.coupon_uid))
    assert coupon.expires_at - coupon.created_at == timedelta(days=10)
    assert coupon.status == "ACTIVE" and coupon.patient.consent_given is True
    db.close()

def test_valid_then_redeemed_then_second_attempt_fails(client):
    client.post("/patients/register", data=registration("Double Use", "9999999998"))
    db = SessionLocal(); coupon = db.query(PatientOffer).first(); token = token_for(coupon.coupon_uid); cid = coupon.id; db.close()
    assert "OFFER VALID" in client.post("/validate", data={"token":token}).text
    # The redirect straight after a successful redemption shows the success receipt, not
    # "already used" -- that state is reserved for a later, separate validation attempt.
    assert "Offer redeemed successfully" in client.post(f"/redeem/{cid}", follow_redirects=True).text
    db = SessionLocal(); assert db.get(PatientOffer, cid).status == "REDEEMED"; db.close()
    assert "OFFER ALREADY USED" in client.post("/validate", data={"token":token}).text

def test_invalid_token(client):
    assert "INVALID QR" in client.post("/validate", data={"token":"SRD-NOT-REAL"}).text

def test_visible_qr_id_can_be_validated_by_authenticated_staff(client):
    client.post("/patients/register", data=registration("Manual ID", "9999999985"))
    db = SessionLocal(); coupon_uid = db.query(PatientOffer).first().coupon_uid; db.close()
    response = client.post("/validate", data={"token": coupon_uid.lower()})
    assert "OFFER VALID" in response.text

def test_different_offers_are_stored(client):
    client.post("/patients/register", data=registration("Crown", "9999999991", "1"))
    client.post("/patients/register", data=registration("Aligner", "9999999992", "2"))
    db = SessionLocal(); rows = db.query(PatientOffer).order_by(PatientOffer.id).all()
    assert [x.offer_id for x in rows] == [1, 2]
    assert rows[0].secure_token_hash != rows[1].secure_token_hash
    db.close()

def test_patient_search(client):
    client.post("/patients/register", data=registration("Unique Search Person", "9999999990"))
    assert "Unique Search Person" in client.get("/patients?q=Unique+Search").text

def test_consent_is_required(client):
    r = client.post("/patients/register", data=registration("No Consent", "9999999981", consent=""))
    assert r.status_code == 422 and "consent is required" in r.text

def test_same_mobile_can_register_again_in_same_campaign_week(client):
    client.post("/patients/register", data=registration("First", "9999999982", offer="1"))
    r = client.post("/patients/register", data=registration("Second", "9999999982", offer="2"), follow_redirects=False)
    assert r.status_code == 303
    db = SessionLocal(); rows = db.query(PatientOffer).order_by(PatientOffer.id).all(); db.close()
    assert len(rows) == 2 and rows[0].patient_id != rows[1].patient_id

def test_token_never_appears_in_url_or_database(client):
    client.post("/patients/register", data=registration("Private Token", "9999999983"))
    db = SessionLocal(); coupon = db.query(PatientOffer).first(); token = token_for(coupon.coupon_uid)
    assert coupon.secure_token_hash != token; db.close()
    r = client.post("/validate", data={"token":token})
    assert r.status_code == 200 and token not in str(r.url) and token not in r.text

def test_active_offer_can_be_cancelled_and_cannot_be_redeemed(client):
    client.post("/patients/register", data=registration("Cancelled", "9999999984"))
    db = SessionLocal(); coupon = db.query(PatientOffer).first(); patient_id = coupon.patient_id; cid = coupon.id; token = token_for(coupon.coupon_uid); db.close()
    assert client.post(f"/patients/{patient_id}/cancel", data={"reason":"Patient requested cancellation"}, follow_redirects=False).status_code == 303
    assert "OFFER CANCELLED" in client.post("/validate", data={"token":token}).text
    client.post(f"/redeem/{cid}")
    db = SessionLocal(); coupon = db.get(PatientOffer, cid)
    assert coupon.status == "CANCELLED" and coupon.cancellation_reason == "Patient requested cancellation"
    db.close()
