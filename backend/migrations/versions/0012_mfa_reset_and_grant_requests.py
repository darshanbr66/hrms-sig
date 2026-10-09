"""Admin MFA reset and re-enrolment; role grant requests (elevation, super admin assignment, break-glass).

Revision ID: 0012
Revises: 0011

Expand/contract: a new column with a constant default (metadata-only in PostgreSQL 11+), a
new table, a new unique constraint on the small catalog table `access.roles`, CHECK
constraints widened to accept new values (every existing row satisfies them; the tables are
small), one CHECK replaced by a wider one, and functions and triggers. Code deployed before
this revision keeps working: it never writes the new column, table or values.

docs/security-architecture.md §3.3 and §9, docs/authorization-model.md §4.3 and §6,
docs/database-design.md §4.1-4.2:

- `identity.users.mfa_reenrolment_required`: set by an administrator's MFA reset. The MFA
  invariant now reads: an active account keeps a confirmed factor unless it is waiting to
  re-enrol, and an account waiting to re-enrol has no open full session. A new deferred
  trigger on `identity.sessions` refuses a full session for an account that has no
  confirmed factor or is waiting to re-enrol, so no code path can open one.
- `identity.one_time_tokens.purpose` gains `mfa_reenrolment` (the emailed link);
  `identity.sessions.revoked_reason` gains `mfa_reset`; the outbox gains the templates
  `identity.mfa_reset` and `access.grant_request`.
- `access.role_grant_requests`: requests that need a second person (an elevation, a super
  admin assignment) and break-glass self-activations. Limits live in CHECK constraints
  (elevation at most 8 hours, break-glass only `system_admin` for at most 1 hour, nobody
  decides their own request or one about themselves); `role_key` is tied to `role_id` by a
  composite foreign key so the role rules can be checked declaratively. Rows are history: a
  trigger refuses DELETE and TRUNCATE, refuses changes to what was requested, allows only the
  forward status transitions, and lets each decision, revocation and acknowledgement be
  recorded once. At most one pending request per person and kind, and at most one active
  elevation or break-glass per person.
- `access.user_roles.grant_request_id`: the request an assignment came from. The SOD-3 check
  allows a self-granted row only with a request, and an insert trigger checks the request:
  same person, role and constraint; an approved super admin assignment with no end and the
  approver as grantor; an active elevation with exactly its window and the approver as
  grantor; an active break-glass with exactly its window, granted by its own requester. A
  `super_admin` assignment is refused unless it comes from an approved request or is the
  installation bootstrap (no grantor). Each request yields at most one assignment.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURPOSES = ("invite", "invite_enrolment", "login_mfa", "password_reset")
SESSION_REASONS = (
    "logout",
    "revoked_by_user",
    "revoked_by_admin",
    "password_changed",
    "account_disabled",
    "token_reuse",
    "password_reset",
)
TEMPLATES = (
    "identity.invite",
    "identity.password_reset",
    "identity.password_changed",
    "identity.new_device",
    "identity.account_locked",
    "identity.recovery_code_used",
    "identity.mfa_changed",
)


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def _replace_check(table: str, name: str, condition: str) -> None:
    op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")
    op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({condition})")


def _identity_upgrade() -> None:
    op.execute(
        "ALTER TABLE identity.users ADD COLUMN mfa_reenrolment_required boolean NOT NULL DEFAULT false"
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity.require_mfa_for_active_users() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            account uuid;
            account_status text;
            reenrolment boolean;
        BEGIN
            IF TG_TABLE_NAME = 'users' THEN
                account := NEW.id;
            ELSE
                account := OLD.user_id;
            END IF;
            -- Lock the account so concurrent factor changes are checked one after another.
            SELECT status, mfa_reenrolment_required INTO account_status, reenrolment
            FROM identity.users WHERE id = account FOR UPDATE;
            IF account_status = 'active' AND NOT reenrolment AND NOT EXISTS (
                SELECT FROM identity.mfa_factors
                WHERE user_id = account AND confirmed_at IS NOT NULL AND revoked_at IS NULL
            ) THEN
                RAISE EXCEPTION 'an active account must keep a confirmed MFA factor'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF reenrolment AND EXISTS (
                SELECT FROM identity.sessions
                WHERE user_id = account AND scope = 'full' AND revoked_at IS NULL
            ) THEN
                RAISE EXCEPTION 'an account waiting to re-enrol MFA cannot keep a full session'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute("DROP TRIGGER users_require_mfa ON identity.users")
    op.execute(
        "CREATE CONSTRAINT TRIGGER users_require_mfa "
        "AFTER INSERT OR UPDATE OF status, mfa_reenrolment_required ON identity.users "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION identity.require_mfa_for_active_users()"
    )
    op.execute(
        """
        CREATE FUNCTION identity.require_mfa_for_full_sessions() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            reenrolment boolean;
        BEGIN
            IF NEW.scope <> 'full' OR NEW.revoked_at IS NOT NULL THEN
                RETURN NULL;
            END IF;
            -- The same lock the factor invariant takes, so the two checks see one state.
            SELECT mfa_reenrolment_required INTO reenrolment
            FROM identity.users WHERE id = NEW.user_id FOR UPDATE;
            IF reenrolment OR NOT EXISTS (
                SELECT FROM identity.mfa_factors
                WHERE user_id = NEW.user_id AND confirmed_at IS NOT NULL AND revoked_at IS NULL
            ) THEN
                RAISE EXCEPTION 'a full session needs a confirmed MFA factor'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER sessions_require_mfa AFTER INSERT OR UPDATE OF scope ON identity.sessions "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION identity.require_mfa_for_full_sessions()"
    )
    _replace_check(
        "identity.one_time_tokens",
        "ck_one_time_tokens_purpose",
        f"purpose IN ({_in((*PURPOSES, 'mfa_reenrolment'))})",
    )
    _replace_check(
        "identity.sessions",
        "ck_sessions_revoked_reason",
        f"revoked_reason IN ({_in((*SESSION_REASONS, 'mfa_reset'))})",
    )
    _replace_check(
        "notify.email_outbox",
        "ck_email_outbox_template",
        f"template IN ({_in((*TEMPLATES, 'identity.mfa_reset', 'access.grant_request'))})",
    )


def _grant_requests_upgrade() -> None:
    op.execute("ALTER TABLE access.roles ADD CONSTRAINT uq_roles_id_key UNIQUE (id, key)")
    op.execute(
        """
        CREATE TABLE access.role_grant_requests (
            id uuid NOT NULL DEFAULT uuidv7(),
            kind text NOT NULL,
            subject_user_id uuid NOT NULL,
            role_id uuid NOT NULL,
            role_key text NOT NULL,
            department_id uuid,
            location_id uuid,
            reason text NOT NULL,
            requested_duration interval,
            requested_by uuid NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            status text NOT NULL,
            decided_by uuid,
            decided_at timestamptz,
            decision_note text,
            starts_at timestamptz,
            ends_at timestamptz,
            revoked_by uuid,
            revoked_at timestamptz,
            acknowledged_by uuid,
            acknowledged_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_role_grant_requests PRIMARY KEY (id),
            CONSTRAINT fk_role_grant_requests_subject_user_id FOREIGN KEY (subject_user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_role FOREIGN KEY (role_id, role_key)
                REFERENCES access.roles (id, key) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_department_id FOREIGN KEY (department_id)
                REFERENCES org.departments (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_requested_by FOREIGN KEY (requested_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_decided_by FOREIGN KEY (decided_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_revoked_by FOREIGN KEY (revoked_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_grant_requests_acknowledged_by FOREIGN KEY (acknowledged_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_role_grant_requests_kind CHECK (
                kind IN ('elevation', 'super_admin_assignment', 'break_glass')
            ),
            CONSTRAINT ck_role_grant_requests_status CHECK (
                (kind = 'elevation'
                    AND status IN ('pending', 'active', 'rejected', 'expired', 'ended', 'revoked'))
                OR (kind = 'super_admin_assignment'
                    AND status IN ('pending', 'approved', 'rejected', 'expired', 'revoked'))
                OR (kind = 'break_glass' AND status IN ('active', 'ended', 'revoked'))
            ),
            CONSTRAINT ck_role_grant_requests_role CHECK (
                (kind = 'elevation' AND role_key NOT IN ('super_admin', 'employee', 'manager'))
                OR (kind = 'super_admin_assignment' AND role_key = 'super_admin')
                OR (kind = 'break_glass' AND role_key = 'system_admin'
                    AND department_id IS NULL AND location_id IS NULL)
            ),
            CONSTRAINT ck_role_grant_requests_requester CHECK (
                (kind IN ('elevation', 'break_glass') AND requested_by = subject_user_id)
                OR (kind = 'super_admin_assignment' AND requested_by <> subject_user_id)
            ),
            CONSTRAINT ck_role_grant_requests_decider CHECK (
                decided_by IS NULL OR (decided_by <> requested_by AND decided_by <> subject_user_id)
            ),
            CONSTRAINT ck_role_grant_requests_decision CHECK (
                (decided_by IS NULL) = (decided_at IS NULL)
                AND (decision_note IS NULL OR decided_by IS NOT NULL)
                AND (kind <> 'break_glass' OR decided_by IS NULL)
                AND (status NOT IN ('approved', 'rejected') OR decided_by IS NOT NULL)
                AND (kind <> 'elevation' OR status <> 'active' OR decided_by IS NOT NULL)
            ),
            CONSTRAINT ck_role_grant_requests_duration CHECK (
                (kind = 'elevation' AND requested_duration > interval '0'
                    AND requested_duration <= interval '8 hours')
                OR (kind = 'break_glass' AND requested_duration > interval '0'
                    AND requested_duration <= interval '1 hour')
                OR (kind = 'super_admin_assignment' AND requested_duration IS NULL)
            ),
            CONSTRAINT ck_role_grant_requests_window CHECK (
                (kind = 'super_admin_assignment' AND ends_at IS NULL
                    AND (status <> 'approved') = (starts_at IS NULL))
                OR (kind IN ('elevation', 'break_glass')
                    AND (starts_at IS NULL) = (ends_at IS NULL)
                    AND (starts_at IS NULL) = (status IN ('pending', 'rejected', 'expired')
                        OR (status = 'revoked' AND decided_by IS NULL AND kind = 'elevation'))
                    AND (ends_at IS NULL OR ends_at - starts_at = requested_duration))
            ),
            CONSTRAINT ck_role_grant_requests_revoked CHECK (
                (revoked_by IS NULL) = (revoked_at IS NULL)
                AND (status = 'revoked') = (revoked_at IS NOT NULL)
            ),
            CONSTRAINT ck_role_grant_requests_acknowledged CHECK (
                (acknowledged_by IS NULL) = (acknowledged_at IS NULL)
                AND (acknowledged_by IS NULL OR (kind = 'break_glass' AND acknowledged_by <> requested_by))
            ),
            CONSTRAINT ck_role_grant_requests_reason CHECK (char_length(reason) BETWEEN 1 AND 500),
            CONSTRAINT ck_role_grant_requests_decision_note CHECK (
                char_length(decision_note) BETWEEN 1 AND 500
            )
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_role_grant_requests_pending ON access.role_grant_requests "
        "(subject_user_id, kind) WHERE status = 'pending'"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_role_grant_requests_active ON access.role_grant_requests "
        "(subject_user_id) WHERE status = 'active'"
    )
    op.execute(
        "CREATE INDEX ix_role_grant_requests_status ON access.role_grant_requests (status) "
        "WHERE status IN ('pending', 'active')"
    )
    op.execute(
        """
        CREATE FUNCTION access.guard_grant_requests() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP <> 'UPDATE' THEN
                RAISE EXCEPTION 'grant requests are history: % is not allowed on %', TG_OP, TG_TABLE_NAME;
            END IF;
            IF (to_jsonb(NEW) - ARRAY['status', 'decided_by', 'decided_at', 'decision_note', 'starts_at',
                    'ends_at', 'revoked_by', 'revoked_at', 'acknowledged_by', 'acknowledged_at',
                    'updated_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status', 'decided_by', 'decided_at', 'decision_note', 'starts_at',
                    'ends_at', 'revoked_by', 'revoked_at', 'acknowledged_by', 'acknowledged_at',
                    'updated_at']) THEN
                RAISE EXCEPTION 'what a grant request asked for cannot change';
            END IF;
            IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                (OLD.status = 'pending'
                    AND NEW.status IN ('approved', 'active', 'rejected', 'expired', 'revoked'))
                OR (OLD.status = 'active' AND NEW.status IN ('ended', 'revoked'))
            ) THEN
                RAISE EXCEPTION 'a grant request cannot move from % to %', OLD.status, NEW.status;
            END IF;
            IF (OLD.decided_by IS NOT NULL AND NEW.decided_by IS DISTINCT FROM OLD.decided_by)
               OR (OLD.decided_at IS NOT NULL AND NEW.decided_at IS DISTINCT FROM OLD.decided_at)
               OR (OLD.decision_note IS NOT NULL AND NEW.decision_note IS DISTINCT FROM OLD.decision_note)
               OR (OLD.starts_at IS NOT NULL AND NEW.starts_at IS DISTINCT FROM OLD.starts_at)
               OR (OLD.ends_at IS NOT NULL AND NEW.ends_at IS DISTINCT FROM OLD.ends_at)
               OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at)
               OR (OLD.revoked_by IS NOT NULL AND NEW.revoked_by IS DISTINCT FROM OLD.revoked_by)
               OR (OLD.acknowledged_at IS NOT NULL
                   AND NEW.acknowledged_at IS DISTINCT FROM OLD.acknowledged_at)
               OR (OLD.acknowledged_by IS NOT NULL
                   AND NEW.acknowledged_by IS DISTINCT FROM OLD.acknowledged_by)
            THEN
                RAISE EXCEPTION 'a recorded decision, revocation or acknowledgement cannot change';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_grant_request_rows BEFORE UPDATE OR DELETE ON access.role_grant_requests "
        "FOR EACH ROW EXECUTE FUNCTION access.guard_grant_requests()"
    )
    op.execute(
        "CREATE TRIGGER guard_grant_request_truncate BEFORE TRUNCATE ON access.role_grant_requests "
        "FOR EACH STATEMENT EXECUTE FUNCTION access.guard_grant_requests()"
    )

    op.execute("ALTER TABLE access.user_roles ADD COLUMN grant_request_id uuid")
    op.execute(
        "ALTER TABLE access.user_roles ADD CONSTRAINT fk_user_roles_grant_request_id "
        "FOREIGN KEY (grant_request_id) REFERENCES access.role_grant_requests (id) ON DELETE RESTRICT"
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_roles_grant_request_id ON access.user_roles (grant_request_id) "
        "WHERE grant_request_id IS NOT NULL"
    )
    _replace_check(
        "access.user_roles",
        "ck_user_roles_not_self_granted",
        "granted_by IS NULL OR granted_by <> user_id OR grant_request_id IS NOT NULL",
    )
    op.execute(
        """
        CREATE FUNCTION access.check_assignment_grant() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        DECLARE
            request access.role_grant_requests%ROWTYPE;
            assigned_role text;
        BEGIN
            IF NEW.grant_request_id IS NULL THEN
                SELECT key INTO assigned_role FROM access.roles WHERE id = NEW.role_id;
                IF assigned_role = 'super_admin' AND NEW.granted_by IS NOT NULL THEN
                    RAISE EXCEPTION 'super_admin is assigned only through an approved request (SOD-8)'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            SELECT * INTO request FROM access.role_grant_requests WHERE id = NEW.grant_request_id;
            IF request.subject_user_id IS DISTINCT FROM NEW.user_id
               OR request.role_id IS DISTINCT FROM NEW.role_id
               OR request.department_id IS DISTINCT FROM NEW.department_id
               OR request.location_id IS DISTINCT FROM NEW.location_id THEN
                RAISE EXCEPTION 'an assignment must match the grant request it comes from'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NOT (
                (request.kind = 'super_admin_assignment' AND request.status = 'approved'
                    AND NEW.valid_until IS NULL AND NEW.granted_by = request.decided_by)
                OR (request.kind = 'elevation' AND request.status = 'active'
                    AND NEW.valid_from = request.starts_at AND NEW.valid_until = request.ends_at
                    AND NEW.granted_by = request.decided_by)
                OR (request.kind = 'break_glass' AND request.status = 'active'
                    AND NEW.valid_from = request.starts_at AND NEW.valid_until = request.ends_at
                    AND NEW.granted_by = NEW.user_id)
            ) THEN
                RAISE EXCEPTION 'the grant request does not allow this assignment'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER check_assignment_grant BEFORE INSERT ON access.user_roles "
        "FOR EACH ROW EXECUTE FUNCTION access.check_assignment_grant()"
    )


def upgrade() -> None:
    _identity_upgrade()
    _grant_requests_upgrade()


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TRIGGER check_assignment_grant ON access.user_roles")
    op.execute("DROP FUNCTION access.check_assignment_grant()")
    _replace_check(
        "access.user_roles", "ck_user_roles_not_self_granted", "granted_by IS NULL OR granted_by <> user_id"
    )
    op.execute("DROP INDEX access.uq_user_roles_grant_request_id")
    op.execute("ALTER TABLE access.user_roles DROP COLUMN grant_request_id")
    op.execute("DROP TABLE access.role_grant_requests")
    op.execute("DROP FUNCTION access.guard_grant_requests()")
    op.execute("ALTER TABLE access.roles DROP CONSTRAINT uq_roles_id_key")

    _replace_check("notify.email_outbox", "ck_email_outbox_template", f"template IN ({_in(TEMPLATES)})")
    _replace_check(
        "identity.sessions", "ck_sessions_revoked_reason", f"revoked_reason IN ({_in(SESSION_REASONS)})"
    )
    _replace_check("identity.one_time_tokens", "ck_one_time_tokens_purpose", f"purpose IN ({_in(PURPOSES)})")
    op.execute("DROP TRIGGER sessions_require_mfa ON identity.sessions")
    op.execute("DROP FUNCTION identity.require_mfa_for_full_sessions()")
    op.execute("DROP TRIGGER users_require_mfa ON identity.users")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION identity.require_mfa_for_active_users() RETURNS trigger
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
    op.execute("ALTER TABLE identity.users DROP COLUMN mfa_reenrolment_required")
