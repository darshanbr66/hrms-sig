"""Email outbox, trusted devices, settings; password-reset tokens and session reason.

Revision ID: 0010
Revises: 0009

Expand/contract: new tables, and two CHECK constraints widened to accept a new value
(`one_time_tokens.purpose` gains `password_reset`, `sessions.revoked_reason` gains
`password_reset`). Every existing row satisfies the wider constraints; the identity tables
are small, so re-validating them is quick.

- `notify.email_outbox`: one row per email. It names the recipient account and a template,
  never an address or a secret (the worker resolves the address and builds any link token
  at send time). `idempotency_key` is unique. Status moves pending -> sending -> sent, or
  back to pending with a later `next_attempt_at`, or to failed or cancelled; rows are kept.
- `identity.trusted_devices`: a browser identified by a random cookie stored as its SHA-256
  digest; it only decides whether a sign-in is reported as from a new device. Revoked rows
  are kept (`revoked_at`, `revoked_reason`).
- `app.settings`: overrides of registry settings (app/platform/settings.py); defaults live
  in code. Whole-number values only, for the keys the registry declares.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TEMPLATES = (
    "identity.invite",
    "identity.password_reset",
    "identity.password_changed",
    "identity.new_device",
    "identity.account_locked",
    "identity.recovery_code_used",
    "identity.mfa_changed",
)
SESSION_REASONS = (
    "logout",
    "revoked_by_user",
    "revoked_by_admin",
    "password_changed",
    "account_disabled",
    "token_reuse",
)
PURPOSES = ("invite", "invite_enrolment", "login_mfa")


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE notify.email_outbox (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            template text NOT NULL,
            template_data jsonb,
            idempotency_key text NOT NULL,
            status text NOT NULL DEFAULT 'pending',
            attempts smallint NOT NULL DEFAULT 0,
            next_attempt_at timestamptz NOT NULL DEFAULT now(),
            lease_expires_at timestamptz,
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            sent_at timestamptz,
            CONSTRAINT pk_email_outbox PRIMARY KEY (id),
            CONSTRAINT fk_email_outbox_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_email_outbox_idempotency_key UNIQUE (idempotency_key),
            CONSTRAINT ck_email_outbox_template CHECK (template IN ({_in(TEMPLATES)})),
            CONSTRAINT ck_email_outbox_template_data CHECK (
                jsonb_typeof(template_data) = 'object' AND octet_length(template_data::text) <= 2048
            ),
            CONSTRAINT ck_email_outbox_idempotency_key CHECK (char_length(idempotency_key) BETWEEN 1 AND 200),
            CONSTRAINT ck_email_outbox_status CHECK (
                status IN ('pending', 'sending', 'sent', 'failed', 'cancelled')
            ),
            CONSTRAINT ck_email_outbox_attempts CHECK (attempts >= 0),
            CONSTRAINT ck_email_outbox_sent CHECK ((status = 'sent') = (sent_at IS NOT NULL)),
            CONSTRAINT ck_email_outbox_lease CHECK (status <> 'sending' OR lease_expires_at IS NOT NULL),
            CONSTRAINT ck_email_outbox_last_error CHECK (char_length(last_error) <= 200)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_email_outbox_next_attempt_at ON notify.email_outbox (next_attempt_at) "
        "WHERE status IN ('pending', 'sending')"
    )
    op.execute(
        "CREATE INDEX ix_email_outbox_user_id_template ON notify.email_outbox (user_id, template) "
        "WHERE status = 'pending'"
    )

    op.execute(
        """
        CREATE TABLE identity.trusted_devices (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            token_hash bytea NOT NULL,
            user_agent text,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL,
            expires_at timestamptz NOT NULL,
            revoked_at timestamptz,
            revoked_reason text,
            CONSTRAINT pk_trusted_devices PRIMARY KEY (id),
            CONSTRAINT fk_trusted_devices_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_trusted_devices_token_hash UNIQUE (token_hash),
            CONSTRAINT ck_trusted_devices_token_hash CHECK (octet_length(token_hash) = 32),
            CONSTRAINT ck_trusted_devices_user_agent CHECK (char_length(user_agent) <= 512),
            CONSTRAINT ck_trusted_devices_expiry CHECK (expires_at > created_at),
            CONSTRAINT ck_trusted_devices_revoked CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL)),
            CONSTRAINT ck_trusted_devices_revoked_reason CHECK (
                revoked_reason IN (
                    'revoked_by_user', 'revoked_by_admin', 'password_changed', 'password_reset',
                    'mfa_changed', 'account_disabled'
                )
            )
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_trusted_devices_user_id ON identity.trusted_devices (user_id) "
        "WHERE revoked_at IS NULL"
    )

    op.execute(
        r"""
        CREATE TABLE app.settings (
            key text NOT NULL,
            value jsonb NOT NULL,
            version integer NOT NULL DEFAULT 1,
            updated_by uuid,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_settings PRIMARY KEY (key),
            CONSTRAINT fk_settings_updated_by FOREIGN KEY (updated_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_settings_key CHECK (
                key ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$' AND char_length(key) <= 100
            ),
            CONSTRAINT ck_settings_value CHECK (jsonb_typeof(value) = 'number'),
            CONSTRAINT ck_settings_version CHECK (version >= 1)
        )
        """
    )

    _replace_check(
        "identity.one_time_tokens",
        "ck_one_time_tokens_purpose",
        f"purpose IN ({_in((*PURPOSES, 'password_reset'))})",
    )
    _replace_check(
        "identity.sessions",
        "ck_sessions_revoked_reason",
        f"revoked_reason IN ({_in((*SESSION_REASONS, 'password_reset'))})",
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    _replace_check(
        "identity.sessions", "ck_sessions_revoked_reason", f"revoked_reason IN ({_in(SESSION_REASONS)})"
    )
    _replace_check("identity.one_time_tokens", "ck_one_time_tokens_purpose", f"purpose IN ({_in(PURPOSES)})")
    op.execute("DROP TABLE app.settings")
    op.execute("DROP TABLE identity.trusted_devices")
    op.execute("DROP TABLE notify.email_outbox")
