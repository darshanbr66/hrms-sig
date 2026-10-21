"""The Resend HTTPS sender: one authenticated request per email, failures reported without
server text (ADR-030)."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.platform.email import RESEND_EMAILS_URL, EmailDeliveryError, OutgoingEmail, ResendSender

KEY = SecretStr("re_test_not_a_real_key")
EMAIL = OutgoingEmail(to="person@example.com", subject="Your invite", body="Open the link to set up.")


def sender(handler: httpx.MockTransport) -> tuple[ResendSender, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=handler)
    return ResendSender(client=client, api_key=KEY, sender="Sigvitas HRMS <hrms@example.com>"), client


async def test_sends_one_plain_text_email_with_the_api_key() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "4ef9a417-02e9-4d39-ad75-9611e0fcc33c"})

    resend, client = sender(httpx.MockTransport(handle))
    async with client:
        await resend.send(EMAIL)
    [request] = seen
    assert str(request.url) == RESEND_EMAILS_URL
    assert request.method == "POST"
    assert request.headers["Authorization"] == "Bearer re_test_not_a_real_key"
    assert json.loads(request.content) == {
        "from": "Sigvitas HRMS <hrms@example.com>",
        "to": ["person@example.com"],
        "subject": "Your invite",
        "text": "Open the link to set up.",
        "headers": {"Auto-Submitted": "auto-generated"},
    }


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429, 500, 503])
async def test_a_refused_request_reports_only_its_status(status: int) -> None:
    body = {"name": "validation_error", "message": "person@example.com is not allowed"}
    resend, client = sender(httpx.MockTransport(lambda _: httpx.Response(status, json=body)))
    async with client:
        with pytest.raises(EmailDeliveryError) as error:
            await resend.send(EMAIL)
    assert error.value.reason == f"ResendHttp{status}"
    assert "person@example.com" not in str(error.value)


async def test_a_network_failure_reports_only_the_error_class() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out connecting to api.resend.com", request=request)

    resend, client = sender(httpx.MockTransport(fail))
    async with client:
        with pytest.raises(EmailDeliveryError) as error:
            await resend.send(EMAIL)
    assert error.value.reason == "ConnectTimeout"
