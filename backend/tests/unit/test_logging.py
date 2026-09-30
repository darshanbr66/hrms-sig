import json
import logging

import pytest

from app.platform.logging import REDACTED, JsonFormatter, is_sensitive_key, redact, request_id_var

FIXTURE_SECRETS = {
    "password": "fixture-password-value",
    "access_token": "fixture-access-token",
    "totp_code": "123456",
    "authorization": "Bearer fixture-bearer",
    "account_number": "000011112222",
    "salary_amount": "150000.00",
}


def render(record_extra: dict[str, object], message: str = "test.event") -> dict[str, object]:
    record = logging.LogRecord("app.test", logging.INFO, __file__, 1, message, None, None)
    for key, value in record_extra.items():
        setattr(record, key, value)
    output: dict[str, object] = json.loads(JsonFormatter().format(record))
    return output


@pytest.mark.parametrize(
    ("key", "sensitive"),
    [
        ("password", True),
        ("new_password", True),
        ("refresh-token", True),
        ("totp_code", True),
        ("Cookie", True),
        ("account_last4", True),
        ("status", False),
        ("company", False),
        ("user_id", False),
        ("duration_ms", False),
        ("route", False),
    ],
)
def test_sensitive_key_detection(key: str, sensitive: bool) -> None:
    assert is_sensitive_key(key) is sensitive


def test_fixture_secrets_never_reach_log_output() -> None:
    output = render({**FIXTURE_SECRETS, "context": {"nested": dict(FIXTURE_SECRETS)}, "user_id": "u-1"})
    text = json.dumps(output)
    for value in FIXTURE_SECRETS.values():
        assert value not in text
    assert output["password"] == REDACTED
    assert output["user_id"] == "u-1"


def test_redact_walks_lists_and_nested_dicts() -> None:
    assert redact([{"otp": "1"}, {"items": [{"secret": "x", "id": 2}]}]) == [
        {"otp": REDACTED},
        {"items": [{"secret": REDACTED, "id": 2}]},
    ]


def test_records_are_json_with_request_id() -> None:
    token = request_id_var.set("req-123")
    try:
        output = render({"status": 200})
    finally:
        request_id_var.reset(token)
    assert output["event"] == "test.event"
    assert output["request_id"] == "req-123"
    assert output["status"] == 200
    assert output["level"] == "INFO"


def test_extra_cannot_overwrite_base_fields() -> None:
    output = render({"level": "FORGED"})
    assert output["level"] == "INFO"
