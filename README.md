# Smriti Raj Dentistry — QR Campaign Management

FastAPI application for registering campaign patients, issuing secure QR offers, managing clinic staff, and redeeming offers at the clinic.

It supports SQLite for local development and PostgreSQL for deployment.

## What it does

- Creates patient registrations and one QR coupon per registration.
- Allows deliberate repeat registrations while protecting against accidental recent duplicates.
- Generates QR images on demand from the signed coupon token, without permanent QR-file storage.
- Supports any practical number of services per campaign. Admins can add, remove, and re-add services without rewriting historical registrations.
- Lets admins assign one or more campaigns to staff. Those assignments scope staff registration, patient lists, and patient-detail access.
- Lets any authenticated staff member validate and redeem a valid QR, so reception staff can handle patients registered by another staff member.
- Shows full patient details only after an authenticated QR scan; the QR itself contains no patient data.
- Provides campaign-aware admin dashboards, lists, CSV exports, delivery activity, and audit logs.
- Uses n8n for actual email/WhatsApp delivery dispatch.

## QR and privacy model

The QR payload is an opaque signed token in the form `SRD-<coupon-id>.<signature>`. It contains no name, mobile number, email address, URL, or other patient data.

- Logged-out visitors cannot access QR validation, QR images, downloads, or printable coupons.
- Authenticated staff and admins can validate and redeem valid QR offers.
- Expired and cancelled QR codes cannot be redeemed.
- The printable coupon contains campaign/service/coupon information only, not patient details.
- Successful patient-detail views and redemptions are recorded in the audit log.

`QR_SIGNING_KEY` is an environment secret and must be set independently in production. QR
IDs are random, not sequential. Existing QR tokens are tied to the signing key used when
they were issued, so rotating that key requires a reviewed QR reissue/key-rotation plan.

## Local setup

### Linux / Arch

```bash
cd /home/pratham/Dental_QR
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
```

### Windows PowerShell

```powershell
py -m venv .venv
.\.venv\Scripts\activate
py -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Generate secure administrator credentials, then copy the generated values into `.env`:

```bash
.venv/bin/python scripts/create_clinic_credentials.py
```

Never commit `.env`, database files, backups, or real passwords.

## Database migrations and startup

Always apply migrations before starting an existing local database:

```bash
.venv/bin/python -m alembic upgrade head
```

For a new or important local database, make a copy before migrating:

```bash
cp smritiraj.db smritiraj.db.before-migration.backup
.venv/bin/python -m alembic upgrade head
```

Start the local server:

```bash
.venv/bin/python -m uvicorn app.main:app --reload --port 8001
```

Open [http://127.0.0.1:8001](http://127.0.0.1:8001). API documentation is at [http://127.0.0.1:8001/docs](http://127.0.0.1:8001/docs).

`alembic stamp` is only for an already-existing unversioned database whose exact schema version is known. Do not use it on a production database without a backup and a reviewed migration plan.

## Roles and access

### Admin

The environment-configured administrator can manage campaigns, services, staff, assignments, reporting, exports, audit logs, and all patient records.

### Staff

Staff accounts are created by an admin. A staff member with no campaign assignment cannot register patients or see campaign patient lists. Assignments take effect on the next request; no re-login is needed.

Campaign assignments restrict:

- registration choices;
- staff home metrics;
- patient lists and direct patient-record access;
- delivery resend actions.

They do not restrict QR scanning: any authenticated staff member can validate and redeem any valid QR at reception.

## Core workflows

### Campaign and service management

Go to **Admin CRM → Campaigns** to create and manage campaigns. A campaign can have any practical number of services.

- Adding a service makes it available for future registrations.
- Removing a service stops future registrations only.
- Existing registrations, QR coupons, redemptions, and reports remain unchanged.
- Closed campaigns are immutable.

### Patient registration

1. Select an active campaign and an active service assigned to it.
2. Enter patient details and register.
3. The system creates a separate coupon and QR for the registration.
4. If the same mobile, campaign, and service were registered recently, the UI asks for confirmation rather than silently creating an accidental duplicate.

### QR validation and redemption

1. An authenticated staff member opens **QR Scanner**.
2. Scan or enter the opaque QR token.
3. A valid QR shows the authorized patient, campaign, service, and validity details.
4. Redeem once. A later scan shows the already-redeemed status and cannot redeem again.

## Filters and exports

Admins can filter the dashboard, patients, issued coupons, redemptions, delivery activity, and CSV exports by campaign. Relevant pages also support service, status, search, date, delivery channel, and delivery-status filters.

Historical rows remain reachable when campaigns are closed or services are removed/deactivated. CSV exports neutralize spreadsheet formula prefixes to prevent formula injection when opened in Excel or Sheets.

## n8n delivery

FastAPI records durable delivery intents; the external delivery worker sends them to n8n. See [n8n/README.md](n8n/README.md) and [docs/blueprint/n8n-delivery-contract.md](docs/blueprint/n8n-delivery-contract.md) for setup and the callback contract.

Scheduled jobs:

```bash
.venv/bin/python -m app.delivery_job
.venv/bin/python -m app.expiry_job
.venv/bin/python -m app.backup_job
```

Run the delivery job once per minute in production-like environments. Run the expiry job every few minutes. The SQLite backup job refuses to create a plaintext backup when `BACKUP_ENCRYPTION_KEY` is absent; use managed PostgreSQL backups in production.

## Testing

Run the normal suite from the repository root:

```bash
.venv/bin/python -m pytest tests
```

Use `pytest tests`, not bare `pytest`, if a nested local clone named `Dental_QR/` exists.

QR image decoding tests use the optional development dependency:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
```

The production requirements and deployment image do not install this development-only decoder.

## Deployment

Use PostgreSQL in deployment and run Alembic migrations as part of the release/start command:

```bash
alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

See [RENDER_DEPLOY.md](RENDER_DEPLOY.md) for the current Render deployment configuration. Before an event, assign campaigns to the required staff accounts, verify n8n callbacks, check backups, and run a controlled load test.

## Launch gates and known follow-up work

Before a production event, verify HTTPS, production secrets, PostgreSQL, encrypted backups,
n8n callbacks, staff campaign assignments, and a controlled load test. Redemption has true
parallel PostgreSQL coverage: one redemption and one audit event must win per coupon. The
scheduled expiry sweep also conditionally expires only active coupons, preserving concurrent
cancellations.

Remaining follow-up work includes moving expiry reminders into the durable delivery outbox,
a documented QR key-rotation/reissue plan before changing `QR_SIGNING_KEY`, rate-limit
hardening, pagination for large all-campaign lists, and event-scale load testing.

## Important repository hygiene

Do not commit any of the following:

- `.env` or real credentials;
- `smritiraj.db` or database backups;
- `generated_qr/` legacy files;
- editor backup files such as `*.save`;
- nested local clones such as `Dental_QR/`.
