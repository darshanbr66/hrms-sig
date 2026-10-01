"""Access: permissions, roles, role permissions and role assignments.

Revision ID: 0008
Revises: 0007

Expand/contract: new tables, a function and triggers only.

docs/database-design.md §4.2 and docs/authorization-model.md §4-6:

- The catalog tables (`permissions`, `roles`, `role_permissions`) are written only by
  migrations: the runtime roles get SELECT and nothing else, so no application bug can
  widen a role. The code catalog is the source of truth; revision 0009 seeds it.
- `user_roles` is history: rows are revoked, never deleted, and a revoked row cannot be
  reopened. Only `revoked_at` and `revoked_by` may change, once.
- Derived roles (`employee`, `manager`) are computed per request and can never be stored:
  the composite foreign key on `(role_id, role_is_derived)` with `role_is_derived = false`
  refuses them.
- SOD-3 in the database: nobody grants a role to themselves (`granted_by <> user_id`).
  `granted_by` is NULL only for the installation bootstrap. Grant requests (elevation,
  super admin assignment, break-glass) and their link from `user_roles` arrive with the
  request workflow.
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import RUNTIME_ROLES, quote_ident

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

KEY = r"^[a-z][a-z0-9_]*$"
PERMISSION_KEY = r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}$"
CATALOG_TABLES = ("access.permissions", "access.roles", "access.role_permissions")


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE access.permissions (
            id uuid NOT NULL DEFAULT uuidv7(),
            key text NOT NULL,
            description text NOT NULL,
            requires_step_up boolean NOT NULL,
            audit_reads boolean NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_permissions PRIMARY KEY (id),
            CONSTRAINT uq_permissions_key UNIQUE (key),
            CONSTRAINT ck_permissions_key CHECK (char_length(key) <= 100 AND key ~ '{PERMISSION_KEY}'),
            CONSTRAINT ck_permissions_description CHECK (char_length(description) BETWEEN 1 AND 200)
        )
        """
    )
    op.execute(
        f"""
        CREATE TABLE access.roles (
            id uuid NOT NULL DEFAULT uuidv7(),
            key text NOT NULL,
            name text NOT NULL,
            description text NOT NULL,
            is_system boolean NOT NULL,
            is_derived boolean NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_roles PRIMARY KEY (id),
            CONSTRAINT uq_roles_key UNIQUE (key),
            CONSTRAINT uq_roles_id_is_derived UNIQUE (id, is_derived),
            CONSTRAINT ck_roles_key CHECK (char_length(key) <= 64 AND key ~ '{KEY}'),
            CONSTRAINT ck_roles_name CHECK (char_length(name) BETWEEN 1 AND 100),
            CONSTRAINT ck_roles_description CHECK (char_length(description) BETWEEN 1 AND 500)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE access.role_permissions (
            role_id uuid NOT NULL,
            permission_id uuid NOT NULL,
            CONSTRAINT pk_role_permissions PRIMARY KEY (role_id, permission_id),
            CONSTRAINT fk_role_permissions_role_id FOREIGN KEY (role_id)
                REFERENCES access.roles (id) ON DELETE RESTRICT,
            CONSTRAINT fk_role_permissions_permission_id FOREIGN KEY (permission_id)
                REFERENCES access.permissions (id) ON DELETE RESTRICT
        )
        """
    )
    op.execute("CREATE INDEX ix_role_permissions_permission_id ON access.role_permissions (permission_id)")
    op.execute(
        """
        CREATE TABLE access.user_roles (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            role_id uuid NOT NULL,
            role_is_derived boolean NOT NULL DEFAULT false,
            department_id uuid,
            location_id uuid,
            valid_from timestamptz NOT NULL DEFAULT now(),
            valid_until timestamptz,
            granted_by uuid,
            grant_reason text NOT NULL,
            revoked_at timestamptz,
            revoked_by uuid,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_user_roles PRIMARY KEY (id),
            CONSTRAINT fk_user_roles_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_user_roles_role_id_role_is_derived FOREIGN KEY (role_id, role_is_derived)
                REFERENCES access.roles (id, is_derived) ON DELETE RESTRICT,
            CONSTRAINT fk_user_roles_department_id FOREIGN KEY (department_id)
                REFERENCES org.departments (id) ON DELETE RESTRICT,
            CONSTRAINT fk_user_roles_location_id FOREIGN KEY (location_id)
                REFERENCES org.locations (id) ON DELETE RESTRICT,
            CONSTRAINT fk_user_roles_granted_by FOREIGN KEY (granted_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT fk_user_roles_revoked_by FOREIGN KEY (revoked_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_user_roles_not_derived CHECK (role_is_derived = false),
            CONSTRAINT ck_user_roles_validity CHECK (valid_until IS NULL OR valid_until > valid_from),
            CONSTRAINT ck_user_roles_not_self_granted CHECK (granted_by IS NULL OR granted_by <> user_id),
            CONSTRAINT ck_user_roles_grant_reason CHECK (char_length(grant_reason) BETWEEN 1 AND 500),
            CONSTRAINT ck_user_roles_revoked_by CHECK (revoked_by IS NULL OR revoked_at IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX uq_user_roles_active_assignment ON access.user_roles "
        "(user_id, role_id, coalesce(department_id, '00000000-0000-0000-0000-000000000000'::uuid), "
        "coalesce(location_id, '00000000-0000-0000-0000-000000000000'::uuid)) WHERE revoked_at IS NULL"
    )
    op.execute("CREATE INDEX ix_user_roles_user_id ON access.user_roles (user_id) WHERE revoked_at IS NULL")
    op.execute("CREATE INDEX ix_user_roles_role_id ON access.user_roles (role_id) WHERE revoked_at IS NULL")

    op.execute(
        """
        CREATE FUNCTION access.guard_role_assignments() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.revoked_at IS NOT NULL THEN
                    RAISE EXCEPTION 'a revoked role assignment cannot change';
                END IF;
                IF (to_jsonb(NEW) - 'revoked_at' - 'revoked_by')
                   IS DISTINCT FROM (to_jsonb(OLD) - 'revoked_at' - 'revoked_by') THEN
                    RAISE EXCEPTION 'role assignments are history: only revocation can change'
                        USING HINT = 'Revoke the assignment and create a new one.';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'role assignments are history: % is not allowed on %', TG_OP, TG_TABLE_NAME;
        END
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER guard_role_assignment_rows BEFORE UPDATE OR DELETE ON access.user_roles "
        "FOR EACH ROW EXECUTE FUNCTION access.guard_role_assignments()"
    )
    op.execute(
        "CREATE TRIGGER guard_role_assignment_truncate BEFORE TRUNCATE ON access.user_roles "
        "FOR EACH STATEMENT EXECUTE FUNCTION access.guard_role_assignments()"
    )

    runtime = ", ".join(quote_ident(role) for role in RUNTIME_ROLES)
    for table in CATALOG_TABLES:
        op.execute(f"REVOKE INSERT, UPDATE, DELETE ON {table} FROM {runtime}")


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE access.user_roles")
    op.execute("DROP FUNCTION access.guard_role_assignments()")
    op.execute("DROP TABLE access.role_permissions")
    op.execute("DROP TABLE access.roles")
    op.execute("DROP TABLE access.permissions")
