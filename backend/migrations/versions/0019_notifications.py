"""In-app notifications and notification preferences (docs/database-design.md §4.9,
product-requirements NTF-1 to NTF-3).

Revision ID: 0019
Revises: 0018

Expand only, in the `notify` schema of revision 0001 (whose default privileges grant the
runtime roles):

- `notifications`: one row per item for one account. The payload holds a short title, a
  short body and an in-app link: display text only, never a reason, a note, an address or any
  other personal value (checked in code, bounded here). `idempotency_key` is unique, so one
  event creates at most one notification per recipient. Users mark rows read or remove them
  (`deleted_at`); nothing else changes.
- `notification_preferences`: per account, notification type and channel, whether it is on.
  A missing row means on. Security notifications are not listed here and cannot be muted.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE notify.notifications (
            id uuid NOT NULL DEFAULT uuidv7(),
            recipient_user_id uuid NOT NULL,
            type text NOT NULL,
            subject_type text,
            subject_id uuid,
            title text NOT NULL,
            body text,
            link text,
            idempotency_key text NOT NULL,
            read_at timestamptz,
            deleted_at timestamptz,
            created_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_notifications PRIMARY KEY (id),
            CONSTRAINT fk_notifications_recipient_user_id FOREIGN KEY (recipient_user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_notifications_idempotency_key UNIQUE (idempotency_key),
            CONSTRAINT ck_notifications_type CHECK (type ~ '^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$'
                AND char_length(type) <= 100),
            CONSTRAINT ck_notifications_subject CHECK ((subject_type IS NULL) = (subject_id IS NULL)),
            CONSTRAINT ck_notifications_subject_type CHECK (subject_type ~ '^[a-z][a-z0-9_]{0,39}$'),
            CONSTRAINT ck_notifications_title CHECK (char_length(title) BETWEEN 1 AND 120),
            CONSTRAINT ck_notifications_body CHECK (char_length(body) BETWEEN 1 AND 300),
            CONSTRAINT ck_notifications_link CHECK (link ~ '^/[A-Za-z0-9/_?=&.-]{0,199}$'),
            CONSTRAINT ck_notifications_idempotency_key CHECK (char_length(idempotency_key) BETWEEN 1 AND 200)
        )
        """
    )
    op.execute(
        "CREATE INDEX ix_notifications_recipient_user_id_created_at ON notify.notifications "
        "(recipient_user_id, created_at, id) WHERE deleted_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_notifications_recipient_user_id_unread ON notify.notifications "
        "(recipient_user_id) WHERE deleted_at IS NULL AND read_at IS NULL"
    )

    op.execute(
        """
        CREATE TABLE notify.notification_preferences (
            id uuid NOT NULL DEFAULT uuidv7(),
            user_id uuid NOT NULL,
            type text NOT NULL,
            channel text NOT NULL,
            enabled boolean NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now(),
            CONSTRAINT pk_notification_preferences PRIMARY KEY (id),
            CONSTRAINT fk_notification_preferences_user_id FOREIGN KEY (user_id)
                REFERENCES identity.users (id) ON DELETE RESTRICT,
            CONSTRAINT uq_notification_preferences_user_id_type_channel UNIQUE (user_id, type, channel),
            CONSTRAINT ck_notification_preferences_channel CHECK (channel IN ('in_app', 'email')),
            CONSTRAINT ck_notification_preferences_type CHECK (char_length(type) BETWEEN 1 AND 100)
        )
        """
    )


def downgrade() -> None:
    """Local development and CI only. Never run against staging or production."""
    op.execute("DROP TABLE notify.notification_preferences")
    op.execute("DROP TABLE notify.notifications")
