def test_post_without_csrf_is_rejected(client):
    response = client.post("/logout", include_csrf=False)
    assert response.status_code == 403


def test_post_with_wrong_csrf_is_rejected(client):
    response = client.post("/logout", include_csrf=False, data={"_csrf_token": "wrong"})
    assert response.status_code == 403


def test_security_headers_are_present(client):
    response = client.get("/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"


def test_unknown_host_is_rejected(client):
    response = client.get("/health", headers={"host": "attacker.example"})
    assert response.status_code == 400


# --- ALLOWED_HOSTS / TrustedHostMiddleware for local Docker n8n callbacks ------------
#
# ALLOWED_HOSTS is baked into `app.main`'s TrustedHostMiddleware at import time, so these
# tests build small standalone apps wired the exact same way (same middleware, same
# allowed_hosts list shape) rather than reloading the real app module under different
# env vars — that keeps the tests fast and isolated while still exercising the real
# Starlette TrustedHostMiddleware behavior the production app relies on.

def _make_trusted_host_app(allowed_hosts):
    from fastapi import FastAPI
    from starlette.middleware.trustedhost import TrustedHostMiddleware
    from starlette.testclient import TestClient

    app = FastAPI()
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.get("/health")
    def health():
        return {"status": "ok"}

    return TestClient(app)


def test_docker_host_header_succeeds_when_added_to_dev_allowed_hosts():
    """A local Docker n8n callback arrives with Host: host.docker.internal:8000. Once a
    developer appends it to their dev ALLOWED_HOSTS (per n8n/README.md section G), the
    request must be accepted."""
    dev_client = _make_trusted_host_app([
        "127.0.0.1", "localhost", "healthcheck.railway.app", "host.docker.internal",
    ])
    response = dev_client.get("/health", headers={"host": "host.docker.internal:8000"})
    assert response.status_code == 200


def test_untrusted_host_still_rejected_even_with_docker_host_allowed():
    """Adding host.docker.internal must not loosen the allowlist for anything else."""
    dev_client = _make_trusted_host_app([
        "127.0.0.1", "localhost", "healthcheck.railway.app", "host.docker.internal",
    ])
    response = dev_client.get("/health", headers={"host": "attacker.example"})
    assert response.status_code == 400


def test_production_shaped_allowed_hosts_does_not_silently_trust_docker_host():
    """A production ALLOWED_HOSTS value that was never told about host.docker.internal
    must keep rejecting it — the dev-only entry is never implied or defaulted in."""
    prod_client = _make_trusted_host_app([
        "crm.smritirajdentistry.example",
    ])
    response = prod_client.get("/health", headers={"host": "host.docker.internal:8000"})
    assert response.status_code == 400


def test_registration_rejects_invalid_contact_data(client):
    response = client.post(
        "/patients/register",
        data={"full_name": "Test Patient", "mobile": "12", "email": "not-an-email", "offer_id": "1"},
    )
    assert response.status_code == 422
    assert "Mobile" in response.text or "Email" in response.text


def test_registration_does_not_expose_internal_error(client, monkeypatch):
    from app.registrations import service as registration_service

    def fail_generation(*_args, **_kwargs):
        raise RuntimeError("SECRET INTERNAL FAILURE")

    monkeypatch.setattr(registration_service, "queue_registration_deliveries", fail_generation)
    response = client.post(
        "/patients/register",
        data={"full_name": "Test Patient", "mobile": "9999999999", "campaign_id": "1", "offer_id": "1", "beneficiary_category": "CGHS", "consent_given": "true"},
    )
    assert response.status_code == 500
    assert "Registration could not be completed" in response.text
    assert "SECRET INTERNAL FAILURE" not in response.text
