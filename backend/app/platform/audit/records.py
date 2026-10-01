"""What an audit or security record contains, validated before it reaches the database.

Rules from docs/security-architecture.md §8 and docs/database-design.md §4.10:

- Records hold IDs, field names and non-sensitive values only. A change to a personal,
  sensitive, restricted or secret field is recorded as `RedactedChange`: the audit row says
  the field changed and where the value lives, never the value. Field names that look
  secret (password, token, code, account, salary, ...) or personal (personal, birth,
  address, ...; docs/security-architecture.md §2) are refused with a value change, as a
  backstop for callers that forget.
- Security event details are flat, non-sensitive scalars (IDs, counts, reasons). They never
  hold an email address or phone number: an email typed at sign-in is recorded only as
  `email_attempted_hash`.
- Text values never contain NUL, which PostgreSQL text and JSONB cannot store.
- Invalid records raise `ValueError`: they are programming errors, not user errors.
"""

import ipaddress
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Final, Self

from starlette.requests import Request

from app.platform.logging import is_sensitive_key

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
type Scalar = str | int | bool | Decimal | date | datetime | uuid.UUID | None

MAX_USER_AGENT_LENGTH: Final = 512
MAX_REASON_LENGTH: Final = 1000
MAX_TARGET_ID_LENGTH: Final = 200
MAX_TEXT_VALUE_LENGTH: Final = 500
MAX_CHANGED_FIELDS: Final = 100
MAX_DETAIL_KEYS: Final = 32
# Same limit as the CHECK constraints on audit_log.changes and security_events.details.
MAX_JSON_BYTES: Final = 16384

_DOTTED_NAME = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+")
# Two to four parts: the resource is omitted when a domain has one (`user.invite`).
_PERMISSION_KEY = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,3}")
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*")
_EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+")

# Personal and sensitive fields (docs/security-architecture.md §2), matched against each
# underscore-separated part of a field name, on top of the secret and pay words that log
# redaction covers. Work email and phone are `public_internal`, so they may carry values.
_PERSONAL_NAME_PARTS: Final = frozenset(
    {
        "personal",
        "birth",
        "dob",
        "address",
        "emergency",
        "medical",
        "passport",
        "ifsc",
        "routing",
        "compensation",
        "payslip",
    }
)
# Security event details also never name contact details: an email typed at sign-in is
# recorded only as `email_attempted_hash`.
_CONTACT_NAME_PARTS: Final = frozenset({"email", "phone", "mobile"})


def _is_sensitive_field(name: str) -> bool:
    return is_sensitive_key(name) or not _PERSONAL_NAME_PARTS.isdisjoint(name.split("_"))


class ActorType(StrEnum):
    USER = "user"
    SYSTEM = "system"
    JOB = "job"


class Outcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILED = "failed"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"


class SecurityEventType(StrEnum):
    """Security event catalogue (docs/database-design.md §4.10). Add types here as flows need them."""

    LOGIN_SUCCEEDED = "login.succeeded"
    LOGIN_FAILED = "login.failed"
    ACCOUNT_LOCKED = "account.locked"
    MFA_ENROLLED = "mfa.enrolled"
    MFA_RESET = "mfa.reset"
    MFA_RECOVERY_CODE_USED = "mfa.recovery_code_used"
    TOKEN_REUSE_DETECTED = "token.reuse_detected"  # noqa: S105 (an event name, not a secret)
    SESSION_REVOKED = "session.revoked"
    RATELIMIT_TRIPPED = "ratelimit.tripped"
    ACCESS_DENIED_SENSITIVE = "access.denied_sensitive"
    ELEVATION_REQUESTED = "elevation.requested"
    ELEVATION_APPROVED = "elevation.approved"
    ELEVATION_BREAK_GLASS = "elevation.break_glass"
    AUDIT_CHAIN_MISMATCH = "audit.chain_mismatch"
    STEP_UP_SUCCEEDED = "step_up.succeeded"
    STEP_UP_FAILED = "step_up.failed"
    PASSWORD_CHANGED = "password.changed"  # noqa: S105 (an event name, not a secret)
    MFA_FACTOR_REMOVED = "mfa.factor_removed"
    MFA_RECOVERY_CODES_REGENERATED = "mfa.recovery_codes_regenerated"
    ACCOUNT_ACTIVATED = "account.activated"
    ACCOUNT_DISABLED = "account.disabled"
    ACCOUNT_ENABLED = "account.enabled"


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RequestContext:
    """Where a recorded action came from. Empty for jobs and system actions."""

    request_id: uuid.UUID | None = None
    ip: IPAddress | None = None
    user_agent: str | None = None

    def __post_init__(self) -> None:
        if self.user_agent is not None and len(self.user_agent) > MAX_USER_AGENT_LENGTH:
            raise ValueError("user_agent is too long; build the context with from_request()")

    @classmethod
    def from_request(cls, request: Request) -> Self:
        """Request ID and client address as resolved by RequestContextMiddleware."""
        request_id = getattr(request.state, "request_id", None)
        user_agent = request.headers.get("user-agent")
        return cls(
            request_id=uuid.UUID(hex=request_id) if request_id else None,
            ip=getattr(request.state, "client_ip", None),
            user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
        )


@dataclass(frozen=True, slots=True)
class AuditActor:
    """Who acted. A user actor may carry the session and the grant request (elevation or
    break-glass) they acted under; system and job actors carry neither."""

    type: ActorType
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    grant_request_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (self.type is ActorType.USER) != (self.user_id is not None):
            raise ValueError("a user actor needs user_id, and only a user actor may have one")
        if self.type is not ActorType.USER and (self.session_id or self.grant_request_id):
            raise ValueError("only a user actor can act in a session or under a grant request")

    @classmethod
    def user(
        cls,
        user_id: uuid.UUID,
        *,
        session_id: uuid.UUID | None = None,
        grant_request_id: uuid.UUID | None = None,
    ) -> Self:
        return cls(ActorType.USER, user_id, session_id, grant_request_id)

    @classmethod
    def system(cls) -> Self:
        return cls(ActorType.SYSTEM)

    @classmethod
    def job(cls) -> Self:
        return cls(ActorType.JOB)


@dataclass(frozen=True, slots=True)
class ValueChange:
    """A non-sensitive field changed from `old` to `new`."""

    old: Scalar
    new: Scalar


@dataclass(frozen=True, slots=True)
class RedactedChange:
    """A field changed; its values are withheld because the field is not safe to copy."""


type FieldChange = ValueChange | RedactedChange


def _check_scalar(value: object, name: str) -> None:
    if value is None or isinstance(value, bool | int | Decimal | uuid.UUID | date):
        if isinstance(value, datetime):
            _require_aware(value, name)
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT_VALUE_LENGTH:
            raise ValueError(f"{name} is longer than {MAX_TEXT_VALUE_LENGTH} characters")
        _check_no_nul(value, name)
        return
    raise ValueError(f"{name} must be a string, integer, boolean, Decimal, date, datetime, UUID or None")


def _check_no_nul(value: str, name: str) -> None:
    if "\x00" in value:
        raise ValueError(f"{name} contains a NUL character")


def _check_name(name: str, what: str) -> None:
    if len(name) > 64 or not _IDENTIFIER.fullmatch(name):
        raise ValueError(f"{what} {name!r} must be a lower-case identifier of at most 64 characters")


def _check_json_size(document: object, name: str) -> None:
    # jsonb renders with the same separators as json.dumps, so this matches the database limit.
    if len(json.dumps(document, ensure_ascii=False).encode()) > MAX_JSON_BYTES:
        raise ValueError(f"{name} is larger than {MAX_JSON_BYTES} bytes as JSON")


def json_scalar(value: Scalar) -> str | int | bool | None:
    """Encode a checked scalar for JSONB. Decimals become strings so no precision is lost."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, date):  # includes datetime
        return value.isoformat()
    return str(value)  # Decimal, UUID


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A business action or sensitive read for `audit.audit_log`."""

    action: str
    actor: AuditActor
    context: RequestContext = field(default_factory=RequestContext)
    outcome: Outcome = Outcome.SUCCESS
    target_type: str | None = None
    target_id: str | uuid.UUID | None = None
    subject_employee_id: uuid.UUID | None = None
    permission_used: str | None = None
    changes: Mapping[str, FieldChange] | None = None
    reason: str | None = None
    # When the action happened, if not now (for example a job recording an earlier event).
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if len(self.action) > 100 or not _DOTTED_NAME.fullmatch(self.action):
            raise ValueError("action must be a dotted lower-case name such as 'leave.request.approved'")
        if self.permission_used is not None and (
            len(self.permission_used) > 100 or not _PERMISSION_KEY.fullmatch(self.permission_used)
        ):
            raise ValueError("permission_used must be a permission key such as 'leave.request.approve.team'")
        if (self.target_type is None) != (self.target_id is None):
            raise ValueError("target_type and target_id are given together or not at all")
        if self.target_type is not None:
            _check_name(self.target_type, "target_type")
        if isinstance(self.target_id, str):
            if not 1 <= len(self.target_id) <= MAX_TARGET_ID_LENGTH:
                raise ValueError(f"target_id must be 1 to {MAX_TARGET_ID_LENGTH} characters")
            _check_no_nul(self.target_id, "target_id")
        if self.reason is not None:
            if not 1 <= len(self.reason) <= MAX_REASON_LENGTH:
                raise ValueError(f"reason must be 1 to {MAX_REASON_LENGTH} characters")
            _check_no_nul(self.reason, "reason")
        if self.occurred_at is not None:
            _require_aware(self.occurred_at, "occurred_at")
        if self.changes is not None:
            self._check_changes(self.changes)
            _check_json_size(self.changes_document(), "changes")

    @staticmethod
    def _check_changes(changes: Mapping[str, FieldChange]) -> None:
        if not changes:
            raise ValueError("changes must name at least one field; use None when nothing changed")
        if len(changes) > MAX_CHANGED_FIELDS:
            raise ValueError(f"changes may name at most {MAX_CHANGED_FIELDS} fields")
        for name, change in changes.items():
            _check_name(name, "changed field")
            if isinstance(change, ValueChange):
                if _is_sensitive_field(name):
                    raise ValueError(f"field {name!r} looks sensitive; record it as RedactedChange()")
                _check_scalar(change.old, f"changes.{name}.old")
                _check_scalar(change.new, f"changes.{name}.new")
            elif not isinstance(change, RedactedChange):
                raise ValueError(f"changes.{name} must be a ValueChange or RedactedChange")

    def changes_document(self) -> dict[str, dict[str, object]] | None:
        if self.changes is None:
            return None
        return {
            name: (
                {"old": json_scalar(change.old), "new": json_scalar(change.new)}
                if isinstance(change, ValueChange)
                else {"redacted": True}
            )
            for name, change in self.changes.items()
        }


@dataclass(frozen=True, slots=True)
class SecurityEvent:
    """An authentication or security signal for `audit.security_events`."""

    event_type: SecurityEventType
    severity: Severity
    context: RequestContext = field(default_factory=RequestContext)
    user_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None
    # Keyed hash of an email typed at sign-in for an unknown account; never the email itself.
    email_attempted_hash: bytes | None = None
    details: Mapping[str, Scalar] | None = None
    occurred_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_type, SecurityEventType):
            raise ValueError("event_type must be a SecurityEventType")
        if not isinstance(self.severity, Severity):
            raise ValueError("severity must be a Severity")
        if self.email_attempted_hash is not None and len(self.email_attempted_hash) != 32:
            raise ValueError("email_attempted_hash must be a 32-byte digest")
        if self.session_id is not None and self.user_id is None:
            raise ValueError("a session belongs to a user; give user_id with session_id")
        if self.occurred_at is not None:
            _require_aware(self.occurred_at, "occurred_at")
        if self.details is not None:
            if not self.details:
                raise ValueError("details must hold at least one entry; use None otherwise")
            if len(self.details) > MAX_DETAIL_KEYS:
                raise ValueError(f"details may hold at most {MAX_DETAIL_KEYS} entries")
            for key, value in self.details.items():
                _check_name(key, "details key")
                if _is_sensitive_field(key) or not _CONTACT_NAME_PARTS.isdisjoint(key.split("_")):
                    raise ValueError(f"details key {key!r} looks sensitive; security events hold no secrets")
                _check_scalar(value, f"details.{key}")
                if isinstance(value, str) and _EMAIL_SHAPE.search(value):
                    raise ValueError(f"details.{key} looks like an email address; use email_attempted_hash")
            _check_json_size(self.details_document(), "details")

    def details_document(self) -> dict[str, str | int | bool | None] | None:
        if self.details is None:
            return None
        return {key: json_scalar(value) for key, value in self.details.items()}
