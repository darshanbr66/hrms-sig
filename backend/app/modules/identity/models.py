"""Accounts, credentials, factors, sessions and tokens (docs/database-design.md §4.1).

Allowed values and lengths are part of each collection's validator. Email addresses and token
digests are unique across all organizations (an account is found by its email, a request by
its token, before its organization is known); everything else is owned by the account's
organization. The two MFA rules that span collections (an active account has a confirmed
factor; a full session needs one) are checked before every commit that could break them
(`invariants.py`). No model here is ever serialized directly into a response.
"""

import dataclasses
import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

from app.platform.db import CASE_INSENSITIVE, Document, Index, Record, allowed, asc, utcnow


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class FactorType(StrEnum):
    TOTP = "totp"


class ClientType(StrEnum):
    WEB = "web"


class RevokedReason(StrEnum):
    LOGOUT = "logout"
    REVOKED_BY_USER = "revoked_by_user"
    REVOKED_BY_ADMIN = "revoked_by_admin"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 (a reason name, not a secret)
    ACCOUNT_DISABLED = "account_disabled"
    TOKEN_REUSE = "token_reuse"  # noqa: S105 (a reason name, not a secret)
    PASSWORD_RESET = "password_reset"  # noqa: S105 (a reason name, not a secret)
    MFA_RESET = "mfa_reset"


class TokenKind(StrEnum):
    ACCESS = "access"
    REFRESH = "refresh"


class OneTimePurpose(StrEnum):
    INVITE = "invite"
    INVITE_ENROLMENT = "invite_enrolment"
    LOGIN_MFA = "login_mfa"
    PASSWORD_RESET = "password_reset"  # noqa: S105 (a purpose name, not a secret)
    # The emailed link of an administrator's MFA reset (24 h by default, single use).
    MFA_REENROLMENT = "mfa_reenrolment"


class DeviceRevokedReason(StrEnum):
    REVOKED_BY_USER = "revoked_by_user"
    REVOKED_BY_ADMIN = "revoked_by_admin"
    PASSWORD_CHANGED = "password_changed"  # noqa: S105 (a reason name, not a secret)
    PASSWORD_RESET = "password_reset"  # noqa: S105 (a reason name, not a secret)
    MFA_CHANGED = "mfa_changed"
    ACCOUNT_DISABLED = "account_disabled"


_USER_AGENT = {"maxLength": 512}


@dataclasses.dataclass(kw_only=True, eq=False)
class User(Record):
    __collection__ = "users"
    __indexes__ = (
        # One account per email address across every organization: sign-in needs no organization.
        Index(asc("email"), name="uq_users_email", unique=True, collation=CASE_INSENSITIVE),
        Index(
            asc("organization_id", "employee_id"),
            name="uq_users_employee_id",
            unique=True,
            partial={"employee_id": {"$type": "binData"}},
        ),
        Index(asc("organization_id", "status"), name="ix_users_status"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "email": {"maxLength": 254, "pattern": r"^[^@\s]+@[^@\s]+$"},
        "status": allowed(UserStatus),
        "failed_login_count": {"minimum": 0},
        "version": {"minimum": 1},
    }
    __expr_rules__ = ({"$or": [{"$ne": ["$status", "active"]}, {"$ne": ["$email_verified_at", None]}]},)

    email: str
    email_verified_at: datetime | None = None
    status: str
    employee_id: uuid.UUID | None = None
    last_login_at: datetime | None = None
    failed_login_count: int = 0
    failed_login_window_started_at: datetime | None = None
    last_failed_login_at: datetime | None = None
    locked_until: datetime | None = None
    # Set by an administrator's MFA reset: the account has no factor and can reach only the
    # re-enrolment flow until a new authenticator is confirmed.
    mfa_reenrolment_required: bool = False
    version: int = 1
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)
    # When the person's data was permanently erased: the account is disabled for good, its
    # email is a placeholder, and nothing to sign in with remains.
    erased_at: datetime | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class Credential(Document):
    """`credentials`: the account's password hash. Its `_id` is the account's."""

    __collection__ = "credentials"
    __id_field__ = "user_id"
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "password_hash": {"maxLength": 512, "pattern": r"^\$argon2id\$"},
    }

    user_id: uuid.UUID
    organization_id: uuid.UUID | None = None
    password_hash: str
    password_changed_at: datetime
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class MfaFactor(Record):
    __collection__ = "mfa_factors"
    __indexes__ = (Index(asc("organization_id", "user_id", "revoked_at"), name="ix_mfa_factors_user_id"),)
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "type": allowed(FactorType),
        "label": {"minLength": 1, "maxLength": 64},
        "secret_key_version": {"minimum": 1},
    }

    user_id: uuid.UUID
    type: str
    label: str
    secret_ciphertext: bytes
    secret_key_version: int
    last_used_step: int | None = None
    last_used_at: datetime | None = None
    confirmed_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    updated_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class RecoveryCode(Record):
    __collection__ = "recovery_codes"
    __indexes__ = (
        Index(asc("organization_id", "user_id", "code_hash"), name="uq_recovery_codes_code", unique=True),
    )

    user_id: uuid.UUID
    code_hash: bytes
    used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class AuthSession(Record):
    """A signed-in session (`sessions`)."""

    __collection__ = "sessions"
    __indexes__ = (Index(asc("organization_id", "user_id", "revoked_at"), name="ix_sessions_user_id"),)
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "client_type": allowed(ClientType),
        "scope": allowed(("full", "mfa_enrolment")),
        "user_agent": _USER_AGENT,
        "revoked_reason": allowed(RevokedReason),
    }
    __expr_rules__ = (
        {"$lte": ["$idle_expires_at", "$absolute_expires_at"]},
        {"$eq": [{"$eq": ["$revoked_at", None]}, {"$eq": ["$revoked_reason", None]}]},
    )

    user_id: uuid.UUID
    client_type: str
    scope: str
    ip: str | None = None
    user_agent: str | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    last_activity_at: datetime
    idle_expires_at: datetime
    absolute_expires_at: datetime
    step_up_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_reason: str | None = None


@dataclasses.dataclass(kw_only=True, eq=False)
class SessionToken(Record):
    __collection__ = "session_tokens"
    __indexes__ = (
        Index(asc("token_hash"), name="uq_session_tokens_token_hash", unique=True),
        Index(asc("organization_id", "session_id"), name="ix_session_tokens_session_id"),
        # Expired tokens go once a day after their expiry; nothing reads them after that.
        Index(asc("expires_at"), name="ttl_session_tokens_expires_at", expire_after_seconds=86400),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "kind": allowed(TokenKind),
    }
    __expr_rules__ = ({"$or": [{"$eq": ["$used_at", None]}, {"$eq": ["$kind", "refresh"]}]},)

    session_id: uuid.UUID
    kind: str
    token_hash: bytes
    expires_at: datetime
    used_at: datetime | None = None
    replaced_by_id: uuid.UUID | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class OneTimeToken(Record):
    __collection__ = "one_time_tokens"
    __indexes__ = (
        Index(asc("token_hash"), name="uq_one_time_tokens_token_hash", unique=True),
        Index(asc("organization_id", "user_id", "purpose"), name="ix_one_time_tokens_user_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "purpose": allowed(OneTimePurpose),
        "failed_attempts": {"minimum": 0},
    }

    user_id: uuid.UUID
    purpose: str
    token_hash: bytes
    expires_at: datetime
    used_at: datetime | None = None
    failed_attempts: int = 0
    created_at: datetime = dataclasses.field(default_factory=utcnow)


@dataclasses.dataclass(kw_only=True, eq=False)
class TrustedDevice(Record):
    """A browser the user has signed in from (docs/security-architecture.md §3.4).

    Identified by a random 256-bit cookie, stored only as its SHA-256 digest. Trust never
    replaces MFA or step-up; it decides only whether a sign-in is reported as from a new device.
    """

    __collection__ = "trusted_devices"
    __indexes__ = (
        Index(asc("token_hash"), name="uq_trusted_devices_token_hash", unique=True),
        Index(asc("organization_id", "user_id", "revoked_at"), name="ix_trusted_devices_user_id"),
    )
    __field_rules__: ClassVar[dict[str, dict[str, Any]]] = {
        "user_agent": _USER_AGENT,
        "revoked_reason": allowed(DeviceRevokedReason),
    }
    __expr_rules__ = (
        {"$gt": ["$expires_at", "$created_at"]},
        {"$eq": [{"$eq": ["$revoked_at", None]}, {"$eq": ["$revoked_reason", None]}]},
    )

    user_id: uuid.UUID
    token_hash: bytes
    user_agent: str | None = None
    created_at: datetime = dataclasses.field(default_factory=utcnow)
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoked_reason: str | None = None
