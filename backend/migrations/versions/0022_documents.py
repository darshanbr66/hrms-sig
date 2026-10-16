"""Documents: categories, documents and versions (docs/database-design.md §4.8,
product-requirements DOC-1 to DOC-7).

Revision ID: 0022
Revises: 0021

Expand only: a new `docs` schema with the module-schema grants of revision 0001.

- `document_categories`: Sigvitas's catalogue; nothing is seeded. Each category has a
  sensitivity (`internal`, `personal`, `sensitive`) that decides who may read its documents,
  and says whether employees see and may upload documents in it, and whether documents need
  an expiry date.
- `documents`: one per employee document (`owner_employee_id`) or company document (no
  owner). Archived, never deleted.
- `document_versions`: one per upload, stored privately under a random key. A version is
  downloadable only once the scanner has marked it `clean`; until then it is `pending`, and
  `infected` or `failed` versions never are. Rows are append-only except the scan result,
  which is recorded once (trigger).
"""

from collections.abc import Sequence

from alembic import op

from migrations.support import RUNTIME_ROLES, quote_ident

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "docs"


def _roles() -> str:
    return ", ".join(quote_ident(role) for role in RUNTIME_ROLES)


def upgrade() -> None:
    op.execute(f"CREATE SCHEMA {SCHEMA}")
    op.execute(f"GRANT USAGE ON SCHEMA {SCHEMA} TO {_roles()}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {_roles()}"
    )
    op.execute(
        """
        CREATE TABLE docs.document_categories (
            id uuid NOT NULL DEFAULT uuidv7(),
            code citext NOT NULL,
            name text NOT NULL,
            description text,
            sensitivity text NOT NULL,
            employee_visible boolean NOT NULL,
            employee_can_upload boolean NOT NULL,
            requires_expiry boolean NOT NULL,
            status text NOT NULL DEFAULT 'active',
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_document_categories PRIMARY KEY (id),
            CONSTRAINT uq_document_categories_code UNIQUE (code),
            CONSTRAINT ck_document_categories_code CHECK (
                char_length(code) BETWEEN 1 AND 32 AND code !~ '\\s'
            ),
            CONSTRAINT ck_document_categories_name CHECK (char_length(name) BETWEEN 1 AND 200),
            CONSTRAINT ck_document_categories_description CHECK (char_length(description) BETWEEN 1 AND 1000),
            CONSTRAINT ck_document_categories_sensitivity CHECK (
                sensitivity IN ('internal', 'personal', 'sensitive')
            ),
            CONSTRAINT ck_document_categories_upload CHECK (employee_visible OR NOT employee_can_upload),
            CONSTRAINT ck_document_categories_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_document_categories_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE docs.documents (
            id uuid NOT NULL DEFAULT uuidv7(),
            owner_employee_id uuid,
            category_id uuid NOT NULL,
            title text NOT NULL,
            expires_on date,
            status text NOT NULL DEFAULT 'active',
            created_by uuid NOT NULL,
            version integer NOT NULL DEFAULT 1,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_documents PRIMARY KEY (id),
            CONSTRAINT fk_documents_owner_employee_id FOREIGN KEY (owner_employee_id)
                REFERENCES people.employees (id) ON DELETE RESTRICT,
            CONSTRAINT fk_documents_category_id FOREIGN KEY (category_id)
                REFERENCES docs.document_categories (id) ON DELETE RESTRICT,
            CONSTRAINT fk_documents_created_by FOREIGN KEY (created_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT ck_documents_title CHECK (char_length(title) BETWEEN 1 AND 200),
            CONSTRAINT ck_documents_status CHECK (status IN ('active', 'archived')),
            CONSTRAINT ck_documents_version CHECK (version >= 1)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_documents_owner_employee_id_category_id "
        "ON docs.documents (owner_employee_id, category_id)"
    )
    op.execute("CREATE INDEX ix_documents_category_id ON docs.documents (category_id)")
    op.execute(
        """
        CREATE TABLE docs.document_versions (
            id uuid NOT NULL DEFAULT uuidv7(),
            document_id uuid NOT NULL,
            version_no integer NOT NULL,
            storage_key text NOT NULL,
            original_filename text NOT NULL,
            content_type text NOT NULL,
            size_bytes bigint NOT NULL,
            sha256 bytea NOT NULL,
            scan_status text NOT NULL DEFAULT 'pending',
            scan_attempts smallint NOT NULL DEFAULT 0,
            scanned_at timestamptz,
            uploaded_by uuid NOT NULL,
            uploaded_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_document_versions PRIMARY KEY (id),
            CONSTRAINT fk_document_versions_document_id FOREIGN KEY (document_id)
                REFERENCES docs.documents (id) ON DELETE RESTRICT,
            CONSTRAINT fk_document_versions_uploaded_by FOREIGN KEY (uploaded_by)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_document_versions_document_id_version_no UNIQUE (document_id, version_no),
            CONSTRAINT uq_document_versions_storage_key UNIQUE (storage_key),
            CONSTRAINT ck_document_versions_version_no CHECK (version_no >= 1),
            CONSTRAINT ck_document_versions_storage_key CHECK (storage_key ~ '^documents/[0-9a-f-]{36}$'),
            CONSTRAINT ck_document_versions_filename CHECK (char_length(original_filename) BETWEEN 1 AND 255),
            CONSTRAINT ck_document_versions_content_type CHECK (
                content_type IN ('application/pdf', 'image/png', 'image/jpeg')
            ),
            CONSTRAINT ck_document_versions_size CHECK (size_bytes BETWEEN 1 AND 10485760),
            CONSTRAINT ck_document_versions_sha256 CHECK (octet_length(sha256) = 32),
            CONSTRAINT ck_document_versions_scan_status CHECK (
                scan_status IN ('pending', 'clean', 'infected', 'failed')
            ),
            CONSTRAINT ck_document_versions_scanned CHECK ((scan_status = 'pending') = (scanned_at IS NULL)),
            CONSTRAINT ck_document_versions_scan_attempts CHECK (scan_attempts BETWEEN 0 AND 100)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_document_versions_pending ON docs.document_versions (uploaded_at) "
        "WHERE scan_status = 'pending'"
    )
    op.execute(
        """
        CREATE FUNCTION docs.guard_versions() RETURNS trigger
        LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF OLD.scan_status <> 'pending'
                   OR (to_jsonb(NEW) - 'scan_status' - 'scan_attempts' - 'scanned_at')
                      IS DISTINCT FROM (to_jsonb(OLD) - 'scan_status' - 'scan_attempts' - 'scanned_at') THEN
                    RAISE EXCEPTION 'a document version records its scan result once, and nothing else changes';
                END IF;
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'document versions are kept: % is not allowed', TG_OP;
        END
        $$
        """  # noqa: E501 - PostgreSQL keeps a function body as written
    )
    op.execute(
        "CREATE TRIGGER guard_versions_rows BEFORE UPDATE OR DELETE ON docs.document_versions "
        "FOR EACH ROW EXECUTE FUNCTION docs.guard_versions()"
    )
    op.execute(
        "CREATE TRIGGER guard_versions_truncate BEFORE TRUNCATE ON docs.document_versions "
        "FOR EACH STATEMENT EXECUTE FUNCTION docs.guard_versions()"
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE docs.document_versions")
    op.execute("DROP FUNCTION docs.guard_versions()")
    op.execute("DROP TABLE docs.documents")
    op.execute("DROP TABLE docs.document_categories")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES IN SCHEMA {SCHEMA} "
        f"REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {_roles()}"
    )
    op.execute(f"DROP SCHEMA {SCHEMA}")
