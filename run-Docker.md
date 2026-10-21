# Docker
"/c/Program Files/Docker/Docker/Docker Desktop.exe" &

# backend
uv run --env-file ../.env uvicorn app.main:app_factory --factory --loop asyncio:SelectorEventLoop

POSTGRES_SUPERUSER_PASSWORD=Hid
HRMS_DB_MIGRATOR_PASSWORD=Hid
HRMS_DB_APP_PASSWORD=Hid
HRMS_DB_WORKER_PASSWORD=Hid
HRMS_DB_AUDIT_RETENTION_PASSWORD=Hid
REDIS_RATELIMIT_PASSWORD=Hid
APP_ENV=Hid
LOG_LEVEL=Hid
API_DOCS_ENABLED=Hid
TRUSTED_PROXY_CIDRSHid
DATABASE_URL_APP=Hid
DATABASE_URL_WORKER=Hid
DATABASE_URL_MIGRATOR=Hid
REDIS_URL=Hid
RATE_LIMIT_KEY_HMAC_KEY=Hid

# --- Added for Checkpoints C and D (local development values) ---
APP_BASE_URL=Hid
FIELD_ENCRYPTION_KEYS=Hid
FIELD_ENCRYPTION_ACTIVE_VERSION=Hid
HIBP_ENABLED=Hid
EMAIL_LOOKUP_HMAC_KEY=Hid
SMTP_HOST=Hid
SMTP_PORT=Hid
SMTP_FROM=Hid
SMTP_SECURITY=Hid
SMTP_USERNAMEHid
SMTP_PASSWORDHid

# --- Worker: audit integrity (added for Checkpoint F; local values only) ---
DATABASE_URL_AUDIT_RETENTION=Hid0.0.1:5433/hrms
AUDIT_SIGNING_KEY_ID=Hid
AUDIT_SIGNING_PRIVATE_KEY=Hid
AUDIT_VERIFY_PUBLIC_KEYS=Hid
S3_ENDPOINT_URL=Hid
S3_REGION=Hid
AUDIT_ANCHOR_BUCKET=Hid
AUDIT_ANCHOR_ACCESS_KEY_ID=Hid
AUDIT_ANCHOR_SECRET_ACCESS_KEY=Hid
AUDIT_LOG_RETENTION_MONTHSHid
SECURITY_EVENTS_RETENTION_MONTHSHid

# --- Documents and exports, virus scanning (added 2026-10-15; local values only) ---
# Private files bucket on the Compose RustFS. Locally it uses the emulator's root credentials,
# like the anchor bucket. run-backend-local-test.cmd overrides the bucket for hrms_local_test.
FILES_BUCKET=Hid
FILES_ACCESS_KEY_ID=Hid
FILES_SECRET_ACCESS_KEY=Hid
# ClamAV from the Compose `scan` profile.
CLAMAV_HOST=Hid
CLAMAV_PORT=Hid

RESEND_API_KEY=Hid
