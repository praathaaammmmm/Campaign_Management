# Render Free-Tier Test Deployment

This is a **temporary test deployment**, not the project's planned production hosting
(the blueprint docs in `docs/blueprint/` and `docs/deployment-railway.md` describe the
real Railway plan — this file is separate and does not change that plan). Use it to
confirm the app works on a public phone browser, then tear it down or replace it with the
real deployment later.

Email/n8n delivery and the delivery/expiry schedulers are **intentionally left disabled**
for this first test — only the web app (registration, QR issue, validation, redemption,
admin/staff login) is being verified. Nothing in this guide starts `delivery_job.py`,
`expiry_job.py`, or `backup_job.py` as a Render Cron Job; add those later once the site is
confirmed working and delivery is actually wanted.

Never paste real secret values into this file, a commit, or any chat/output — generate
them locally with the commands below and paste them only into Render's **Environment**
tab, which is not part of the git history.

## 1. Provision a free PostgreSQL database first

1. Render dashboard → **New** → **PostgreSQL**.
2. Name: e.g. `smritiraj-test-db`. Plan: **Free** (Render deletes free databases after
   30 days — acceptable for a temporary test; do not store anything you need to keep).
3. Region: same region you'll use for the web service below.
4. Once created, open it and copy the **Internal Database URL** (starts with
   `postgresql://`). You'll edit this before using it — see the `DATABASE_URL` note below.

## 2. Create the web service

Render dashboard → **New** → **Web Service** → connect this repository.

| Setting | Value |
|---|---|
| Root Directory | `smritiraj_qr_system` |
| Environment | Docker |
| Dockerfile Path | `Dockerfile` (relative to Root Directory) |
| Docker Command (override) | leave blank — do not set one |
| Plan | Free |
| Health Check Path | `/ready` |
| Auto-Deploy | On commit to `main` (or off, your choice) |

Leave the Docker Command override **blank**. The repository's `Dockerfile` already runs
`alembic upgrade head` and then execs `uvicorn` as its own `CMD` — `app/main.py`
deliberately skips `Base.metadata.create_all` when `APP_ENV=production` (schema changes
must go through Alembic in production), and the migration step now lives in the image
itself rather than a dashboard field. Do not paste a Docker Command override here: Render
does not shell-interpret that field the way a local terminal does, so a string containing
`&&` gets passed to `alembic` as a literal argument instead of chaining two commands —
that was the actual cause of an earlier deploy failure, not a Dockerfile problem.

If you'd rather not use Docker, a native Python environment also works with:
- Build Command: `pip install -r requirements.txt`
- Start Command: `alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port $PORT`

## 3. Environment variables

Set these in the web service's **Environment** tab. Placeholders below are safe examples —
generate real values locally (never in a file or chat) with the commands shown, then paste
only into Render.

```env
APP_ENV=production
CLINIC_USERNAME=smritiraj-clinic
CLINIC_PASSWORD_HASH=<paste output of scripts/create_clinic_credentials.py>
SESSION_SECRET_KEY=<paste output of scripts/create_clinic_credentials.py>
SESSION_MAX_AGE_SECONDS=1800
SESSION_HTTPS_ONLY=true
SESSION_VERSION=1
QR_SIGNING_KEY=<run: py -c "import secrets; print(secrets.token_urlsafe(48))">
BACKUP_ENCRYPTION_KEY=<run: py -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">
ALLOWED_HOSTS=<your-service-name>.onrender.com
PUBLIC_BASE_URL=https://<your-service-name>.onrender.com
DATABASE_URL=<Render Postgres Internal Database URL, with postgresql+psycopg:// instead of postgresql://>
DB_POOL_SIZE=3
DB_MAX_OVERFLOW=2
DB_POOL_TIMEOUT_SECONDS=30
HOSPITAL_NAME=Smriti Raj Dentistry
N8N_WEBHOOK_URL=
N8N_WEBHOOK_SECRET=
WHATSAPP_ENABLED=false
VALIDATION_RATE_LIMIT_ATTEMPTS=30
VALIDATION_RATE_LIMIT_WINDOW_SECONDS=60
```

Notes on the values above:

- **`CLINIC_PASSWORD_HASH` / `SESSION_SECRET_KEY`** — run
  `python scripts/create_clinic_credentials.py` locally (inside `smritiraj_qr_system/`,
  with the project's virtualenv active) and paste its two output lines into Render. It
  never writes them to a file.
- **`DATABASE_URL` scheme matters**: Render's copied URL starts with `postgresql://`. This
  project installs the `psycopg` (v3) driver, not `psycopg2`, so you must change the
  scheme to `postgresql+psycopg://` (keep everything else — host, port, user, password,
  database name — exactly as Render gave it) or the app fails to start with a driver
  import error.
- **`ALLOWED_HOSTS`/`PUBLIC_BASE_URL`**: fill in your actual Render subdomain once the
  service is created and you know its name (Render shows it before first deploy, in the
  service's settings page) — the app's `TrustedHostMiddleware` rejects any other `Host`
  header with `400 Invalid host header`, and `validate_security_config()` refuses to start
  at all in production if `ALLOWED_HOSTS` is empty or `*`.
- **`QR_SIGNING_KEY`**: must be set as its own value, separately from
  `SESSION_SECRET_KEY` — production startup validation (`app/config.py`) checks this
  explicitly. Keep it stable across restarts/redeploys and back it up with the
  database: QR images are not stored. `qr_png_bytes()` (`app/qr/service.py`) derives each
  QR PNG in memory from the coupon ID and this key whenever it is viewed, downloaded,
  printed or delivered — so a changed key makes existing QRs unavailable.
- **`DB_POOL_SIZE`/`DB_MAX_OVERFLOW`** are set low (3/2) because Render's free Postgres
  plan caps total concurrent connections; this is a deliberate, safe choice for a small
  phone test, not a code change.
- Leave `N8N_WEBHOOK_URL`, `N8N_WEBHOOK_SECRET` empty and `WHATSAPP_ENABLED=false` — this
  keeps delivery/WhatsApp fully disabled, per the current test scope. `trigger_delivery`
  already reports a clear "not configured" result rather than erroring when these are
  unset, and nothing on Render calls it anyway since no delivery job is scheduled.

## 4. Known limitation for this free-tier test

Render's **free** web service plan has no persistent disk. QR images no longer depend on
it: patient/coupon records live in Postgres and every QR PNG is generated on demand from
the coupon ID and `QR_SIGNING_KEY`. The one exception is legacy coupons issued before
hashed tokens (their QR cannot be regenerated): those are served read-only from an
existing `generated_qr/` file, so on a disk-less plan they show "QR file unavailable"
and need a replacement coupon.

## 5. Post-deploy phone test checklist

Do this from an actual phone browser (not just desktop), after the first deploy succeeds:

1. Open `https://<your-service-name>.onrender.com/health` → expect `{"status":"ok"}`.
2. Open `https://<your-service-name>.onrender.com/ready` → expect
   `{"status":"ready"}` (confirms the app actually reached Postgres, not just that it's
   running).
3. Open `/login`, sign in with `CLINIC_USERNAME` and the password you set (not the hash)
   in `scripts/create_clinic_credentials.py`.
4. Register a test patient using your own phone number and a real test email address you
   can check.
5. Confirm you land on the patient's detail page and a QR image renders correctly.
6. Open the QR download link and confirm the PNG downloads/displays.
7. Open `/validate`, submit the same coupon's token or visible ID, confirm **OFFER VALID**.
8. Redeem it, then validate the same coupon again — confirm **OFFER ALREADY USED**.
9. Confirm the CRM Delivery screen shows the row as `PREPARED` (not `SENT`/`FAILED`) since
   no delivery job is running — this is expected for this test, not a bug.
10. Sign out and confirm `/login` is required again for any admin page.
11. Refresh the page a few times over a couple of minutes (Render free services spin down
    after idling and cold-start on the next request) — confirm the app comes back up
    rather than erroring, and that step 4's patient is still there after a cold start
    (proves Postgres, not local disk, is the source of truth).

If any step fails, check the Render service's **Logs** tab first — `validate_security_config()`
raises a clear `RuntimeError` listing every missing/invalid setting if the environment
variables above are incomplete.
