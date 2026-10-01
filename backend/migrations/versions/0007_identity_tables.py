"""Identity: accounts, credentials, MFA factors, recovery codes, sessions and tokens.

Revision ID: 0007
Revises: 0006

Expand/contract: new tables, functions and triggers only.

docs/database-design.md §4.1 and docs/security-architecture.md §3:

- An account (`users`) is separate from the employee record and linked to at most one
  (`employee_id UNIQUE`). The password hash lives in `credentials`, so account queries never
  load it.
- Secrets are never stored in a usable form: token and recovery-code digests (SHA-256 of
  256- and 80-bit random values), Argon2id password hashes, AES-256-GCM TOTP secrets.
- MFA is mandatory. A deferred constraint trigger refuses to commit any change that leaves
  an `active` account without a confirmed, unrevoked factor. It locks the account row first,
  so two concurrent removals of different factors cannot both pass.
- `trusted_devices` arrives with new-device notifications, which need the email outbox.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE identity.users (
            id uuid NOT NULL DEFAULT uuidv7(),
            email citext NOT NULL,
            email_verified_at timestamptz,
            status text NOT NULL,
            employee_id uuid,
            last_login_at timestamptz,
            failed_login_count smallint NOT NULL DEFAULT 0,
            failed_login_window_started_at timestamptz,
            last_failed_login_at timestamptz,
            locked_until timestamptz,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_users PRIMARY KEY (id),
            CONSTRAINT uq_users_email UNIQUE (email),
            CONSTRAINT uq_users_employee_id UNIQUE (employee_id),
            CONSTRAINT fk_users_employee_id FOREIGN KEY (employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT ck_users_email CHECK (
                char_length(email) <= 254 AND email ~ '^[^@[:space:]]+@[^@[:space:]]+$'
            ),
            CONSTRAINT ck_users_status CHECK (status IN ('invited', 'active', 'disabled')),
            CONSTRAINT ck_users_active_is_verified CHECK (
                status <> 'active' OR email_verified_at IS NOT NULL
            ),
            CONSTRAINT ck_users_failed_login_count CHECK (failed_login_count >= 0),
            CONSTRAINT ck_users_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.credentials (
            user_id uuid NOT NULL,
            password_hash text NOT NULL,
            password_changed_at timestamptz NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_credentials PRIMARY KEY (user_id),
            CONSTRAINT fk_credentials_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_credentials_password_hash CHECK (
                char_length(password_hash) <= 512 AND password_hash LIKE '$argon2id$%'
            )
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.mfa_factors (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            type text NOT NULL,
            label text NOT NULL,
            secret_ciphertext bytea NOT NULL,
            secret_key_version smallint NOT NULL,
            last_used_step bigint,
            last_used_at timestamptz,
            confirmed_at timestamptz,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_mfa_factors PRIMARY KEY (id),
            CONSTRAINT fk_mfa_factors_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_mfa_factors_type CHECK (type IN ('totp')),
            CONSTRAINT ck_mfa_factors_label CHECK (char_length(label) BETWEEN 1 AND 64),
            CONSTRAINT ck_mfa_factors_secret_ciphertext CHECK (
                octet_length(secret_ciphertext) BETWEEN 29 AND 256
            ),
            CONSTRAINT ck_mfa_factors_secret_key_version CHECK (secret_key_version >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_mfa_factors_user_id ON identity.mfa_factors (user_id) WHERE revoked_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE identity.recovery_codes (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            code_hash bytea NOT NULL,
            used_at timestamptz,
            revoked_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_recovery_codes PRIMARY KEY (id),
            CONSTRAINT fk_recovery_codes_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_recovery_codes_user_id_code_hash UNIQUE (user_id, code_hash),
            CONSTRAINT ck_recovery_codes_code_hash CHECK (octet_length(code_hash) = 32)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE identity.sessions (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            client_type text NOT NULL,
            scope text NOT NULL,
            ip inet,
            user_agent text,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_activity_at timestamptz NOT NULL,
            idle_expires_at timestamptz NOT NULL,
            absolute_expires_at timestamptz NOT NULL,
            step_up_at timestamptz,
            revoked_at timestamptz,
            revoked_reason text,
            CONSTRAINT pk_sessions PRIMARY KEY (id),
            CONSTRAINT fk_sessions_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_sessions_client_type CHECK (client_type IN ('web')),
            CONSTRAINT ck_sessions_scope CHECK (scope IN ('full', 'mfa_enrolment')),
            CONSTRAINT ck_sessions_user_agent CHECK (char_length(user_agent) <= 512),
            CONSTRAINT ck_sessions_expiry CHECK (idle_expires_at <= absolute_expires_at),
            CONSTRAINT ck_sessions_revoked CHECK ((revoked_at IS NULL) = (revoked_reason IS NULL)),
            CONSTRAINT ck_sessions_revoked_reason CHECK (
                revoked_reason IN (
                    'logout', 'revoked_by_user', 'revoked_by_admin', 'password_changed',
                    'account_disabled', 'token_reuse'
                )
            )
        )
        """
    )
    op.execute("CREATE INDEX ix_sessions_user_id ON identity.sessions (user_id) WHERE revoked_at IS NULL")
    op.execute(
        """
        CREATE TABLE identity.session_tokens (
            id uuid NOT NULL DEFAULT uuidv7(),
            session_id uuid NOT NULL,
            kind text NOT NULL,
            token_hash bytea NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            replaced_by_id uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_session_tokens PRIMARY KEY (id),
            CONSTRAINT fk_session_tokens_session_id FOREIGN KEY (session_id)
                REFERENCES identity.sessions (id) ON DELETE CASCADE,
            CONSTRAINT fk_session_tokens_replaced_by_id FOREIGN KEY (replaced_by_id)
                REFERENCES identity.session_tokens (id) ON DELETE SET NULL,
            CONSTRAINT uq_session_tokens_token_hash UNIQUE (token_hash),
            CONSTRAINT ck_session_tokens_kind CHECK (kind IN ('access', 'refresh')),
            CONSTRAINT ck_session_tokens_token_hash CHECK (octet_length(token_hash) = 32),
            CONSTRAINT ck_session_tokens_only_refresh_is_used CHECK (used_at IS NULL OR kind = 'refresh')
        )
        """
    )
    op.execute("CREATE INDEX ix_session_tokens_session_id ON identity.session_tokens (session_id)")
    op.execute(
        """
        CREATE TABLE identity.one_time_tokens (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            purpose text NOT NULL,
            token_hash bytea NOT NULL,
            expires_at timestamptz NOT NULL,
            used_at timestamptz,
            failed_attempts smallint NOT NULL DEFAULT 0,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_one_time_tokens PRIMARY KEY (id),
            CONSTRAINT fk_one_time_tokens_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_one_time_tokens_token_hash UNIQUE (token_hash),
            CONSTRAINT ck_one_time_tokens_purpose CHECK (
                purpose IN ('invite', 'invite_enrolment', 'login_mfa')
            ),
            CONSTRAINT ck_one_time_tokens_token_hash CHECK (octet_length(token_hash) = 32),
            CONSTRAINT ck_one_time_tokens_failed_attempts CHECK (failed_attempts >= 0)
        )
        """
    )
    op.execute("CREATE INDEX ix_one_time_tokens_user_id ON identity.one_time_tokens (user_id)")

    op.execute(
        """
        CREATE FUNCTION identity.require_mfa_for_active_users() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            account uuid;
            account_status text;
        BEGIN
            IF TG_TABLE_NAME = 'users' THEN
                account := NEW.id;
            ELSE
                account := OLD.user_id;
            END IF;
            -- Lock the account so concurrent factor changes are checked one after another.
            SELECT status INTO account_status FROM identity.users WHERE id = account FOR UPDATE;
            IF account_status = 'active' AND NOT EXISTS (
                SELECT FROM identity.mfa_factors
                WHERE user_id = account AND confirmed_at IS NOT NULL AND revoked_at IS NULL
            ) THEN
                RAISE EXCEPTION 'an active account must keep a confirmed MFA factor'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER users_require_mfa AFTER INSERT OR UPDATE OF status ON identity.users "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION identity.require_mfa_for_active_users()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER mfa_factors_require_mfa "
        "AFTER UPDATE OF confirmed_at, revoked_at OR DELETE ON identity.mfa_factors "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION identity.require_mfa_for_active_users()"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE identity.one_time_tokens")
    op.execute("DROP TABLE identity.session_tokens")
    op.execute("DROP TABLE identity.sessions")
    op.execute("DROP TABLE identity.recovery_codes")
    op.execute("DROP TABLE identity.mfa_factors")
    op.execute("DROP TABLE identity.credentials")
    op.execute("DROP TABLE identity.users")
    op.execute("DROP FUNCTION identity.require_mfa_for_active_users()")
