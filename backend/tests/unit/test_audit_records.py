"""Validation of audit and security records before they reach the database."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.platform.audit.records import (
    MAX_CHANGED_FIELDS,
    MAX_DETAIL_KEYS,
    ActorType,
    AuditActor,
    AuditEvent,
    RedactedChange,
    RequestContext,
    SecurityEvent,
    SecurityEventType,
    Severity,
    ValueChange,
)

SYSTEM = AuditActor.system()
# Deliberately without a time zone: the records must refuse it.
NAIVE = datetime(2030, 1, 1)  # noqa: DTZ001


def event(**overrides: Any) -> AuditEvent:
    values: dict[str, Any] = {"action": "leave.request.approved", "actor": SYSTEM}
    values.update(overrides)
    return AuditEvent(**values)


def test_actor_rules() -> None:
    user_id = uuid.uuid4()
    assert AuditActor.user(user_id).type is ActorType.USER
    with pytest.raises(ValueError, match="needs user_id"):
        AuditActor(ActorType.USER)
    with pytest.raises(ValueError, match="needs user_id"):
        AuditActor(ActorType.JOB, user_id=user_id)
    with pytest.raises(ValueError, match="session or under a grant request"):
        AuditActor(ActorType.SYSTEM, session_id=uuid.uuid4())
    with pytest.raises(ValueError, match="session or under a grant request"):
        AuditActor(ActorType.JOB, grant_request_id=uuid.uuid4())


@pytest.mark.parametrize(
    "action", ["approved", "Leave.Request", "leave..approved", "leave.request-approved", "x" * 101]
)
def test_action_must_be_a_dotted_lower_case_name(action: str) -> None:
    with pytest.raises(ValueError, match="action"):
        event(action=action)


@pytest.mark.parametrize("permission", ["leave", "leave.request.approve.team.extra", "Leave.request.read"])
def test_permission_used_must_be_a_permission_key(permission: str) -> None:
    with pytest.raises(ValueError, match="permission_used"):
        event(permission_used=permission)


@pytest.mark.parametrize("permission", ["user.invite", "org.read", "leave.request.approve.team"])
def test_permission_keys_of_two_to_four_parts_are_accepted(permission: str) -> None:
    assert event(permission_used=permission).permission_used == permission


def test_target_type_and_id_come_together() -> None:
    event(target_type="leave_request", target_id=uuid.uuid4())
    event(target_type="setting", target_id="session.idle_timeout")
    with pytest.raises(ValueError, match="together"):
        event(target_type="leave_request")
    with pytest.raises(ValueError, match="together"):
        event(target_id=uuid.uuid4())
    with pytest.raises(ValueError, match="target_type"):
        event(target_type="Leave Request", target_id="x")
    with pytest.raises(ValueError, match="target_id"):
        event(target_type="setting", target_id="x" * 201)


@pytest.mark.parametrize("reason", ["", "x" * 1001])
def test_reason_length(reason: str) -> None:
    with pytest.raises(ValueError, match="reason"):
        event(reason=reason)


@pytest.mark.parametrize(
    "field",
    ["password", "password_hash", "totp_secret", "access_token", "recovery_code", "bank_account", "salary"],
)
def test_sensitive_looking_fields_cannot_carry_values(field: str) -> None:
    with pytest.raises(ValueError, match="RedactedChange"):
        event(changes={field: ValueChange("old", "new")})
    recorded = event(changes={field: RedactedChange()})
    assert recorded.changes_document() == {field: {"redacted": True}}


@pytest.mark.parametrize(
    "field",
    ["date_of_birth", "personal_email", "personal_phone", "current_address", "emergency_contact_name"],
)
def test_personal_fields_cannot_carry_values(field: str) -> None:
    with pytest.raises(ValueError, match="RedactedChange"):
        event(changes={field: ValueChange("old", "new")})


def test_public_internal_fields_carry_values() -> None:
    recorded = event(changes={"work_email": ValueChange("old@dev.example", "new@dev.example")})
    assert recorded.changes_document() == {"work_email": {"old": "old@dev.example", "new": "new@dev.example"}}


def test_text_with_nul_is_refused() -> None:
    with pytest.raises(ValueError, match="NUL"):
        event(changes={"notes": ValueChange(None, "a\x00b")})
    with pytest.raises(ValueError, match="NUL"):
        event(reason="a\x00b")
    with pytest.raises(ValueError, match="NUL"):
        event(target_type="setting", target_id="a\x00b")


@pytest.mark.parametrize("value", [1.5, [1], {"a": 1}, b"bytes", NAIVE])
def test_values_must_be_json_safe_scalars(value: object) -> None:
    with pytest.raises(ValueError, match=r"changes\.status"):
        event(changes={"status": ValueChange(None, value)})  # type: ignore[arg-type]


def test_long_text_values_are_refused() -> None:
    with pytest.raises(ValueError, match="longer than"):
        event(changes={"notes": ValueChange(None, "x" * 501)})


def test_change_list_bounds_and_names() -> None:
    with pytest.raises(ValueError, match="at least one field"):
        event(changes={})
    with pytest.raises(ValueError, match="at most"):
        event(changes={f"field_{i}": ValueChange(i, i + 1) for i in range(MAX_CHANGED_FIELDS + 1)})
    with pytest.raises(ValueError, match="identifier"):
        event(changes={"Status": ValueChange("a", "b")})
    with pytest.raises(ValueError, match="ValueChange or RedactedChange"):
        event(changes={"status": ("a", "b")})


def test_changes_document_encodes_each_type() -> None:
    changed_at = datetime(2030, 1, 2, 3, 4, tzinfo=UTC)
    recorded = event(
        changes={
            "status": ValueChange("pending", "approved"),
            "days": ValueChange(Decimal("1.50"), Decimal("2.00")),
            "start_date": ValueChange(date(2030, 1, 1), None),
            "decided_at": ValueChange(None, changed_at),
            "approver_id": ValueChange(None, uuid.UUID(int=5)),
            "is_paid": ValueChange(True, False),
            "step": ValueChange(1, 2),
        }
    )
    assert recorded.changes_document() == {
        "status": {"old": "pending", "new": "approved"},
        "days": {"old": "1.50", "new": "2.00"},
        "start_date": {"old": "2030-01-01", "new": None},
        "decided_at": {"old": None, "new": "2030-01-02T03:04:00+00:00"},
        "approver_id": {"old": None, "new": str(uuid.UUID(int=5))},
        "is_paid": {"old": True, "new": False},
        "step": {"old": 1, "new": 2},
    }
    assert event().changes_document() is None


def test_occurred_at_must_be_aware() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        event(occurred_at=NAIVE)
    with pytest.raises(ValueError, match="timezone-aware"):
        SecurityEvent(
            SecurityEventType.LOGIN_FAILED,
            Severity.INFO,
            occurred_at=NAIVE,
        )


def test_user_agent_is_bounded() -> None:
    with pytest.raises(ValueError, match="user_agent"):
        RequestContext(user_agent="x" * 513)


def security_event(**overrides: Any) -> SecurityEvent:
    values: dict[str, Any] = {"event_type": SecurityEventType.LOGIN_FAILED, "severity": Severity.WARNING}
    values.update(overrides)
    return SecurityEvent(**values)


def test_security_event_types_and_severity_are_catalogued() -> None:
    with pytest.raises(ValueError, match="SecurityEventType"):
        security_event(event_type="login.failed")
    with pytest.raises(ValueError, match="Severity"):
        security_event(severity="warning")


def test_email_is_stored_only_as_a_digest() -> None:
    security_event(email_attempted_hash=bytes(32))
    with pytest.raises(ValueError, match="32-byte"):
        security_event(email_attempted_hash=b"person@dev.example")


def test_session_needs_a_user() -> None:
    with pytest.raises(ValueError, match="user_id"):
        security_event(session_id=uuid.uuid4())


@pytest.mark.parametrize(
    "key",
    ["password", "otp", "totp_code", "token_hash", "authorization", "email", "attempted_email", "phone"],
)
def test_details_hold_no_secrets_or_contact_details(key: str) -> None:
    with pytest.raises(ValueError, match="looks sensitive"):
        security_event(details={key: "x"})


def test_details_never_hold_an_email_address_under_any_key() -> None:
    with pytest.raises(ValueError, match="email address"):
        security_event(details={"login": "Person@Dev.Example"})


def test_details_bounds() -> None:
    with pytest.raises(ValueError, match="at least one"):
        security_event(details={})
    with pytest.raises(ValueError, match="at most"):
        security_event(details={f"key_{i}": i for i in range(MAX_DETAIL_KEYS + 1)})
    with pytest.raises(ValueError, match=r"details\.attempts"):
        security_event(details={"attempts": 1.0})
    recorded = security_event(details={"attempts": 3, "user_id": uuid.UUID(int=1)})
    assert recorded.details_document() == {"attempts": 3, "user_id": str(uuid.UUID(int=1))}


def test_json_documents_stay_within_the_database_limit() -> None:
    wide = {f"field_{i}": ValueChange("x" * 100, "y" * 100) for i in range(MAX_CHANGED_FIELDS)}
    with pytest.raises(ValueError, match="larger than"):
        event(changes=wide)
    with pytest.raises(ValueError, match="larger than"):
        security_event(details={f"key_{i}": "z" * 500 for i in range(MAX_DETAIL_KEYS)})
