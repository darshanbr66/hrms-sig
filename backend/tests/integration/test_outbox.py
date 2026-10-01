"""The email outbox (notify): transactional writes, idempotency, retries, failure, concurrency."""

import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta

import httpx
import pytest
from testcontainers.core.container import DockerContainer
from testcontainers.core.wait_strategies import LogMessageWaitStrategy

from app.modules.notify.dispatcher import OutboxDispatcher
from app.modules.notify.models import EmailTemplate
from app.modules.notify.public import enqueue
from app.platform.config import SmtpSecurity
from app.platform.email import EmailDeliveryError, OutgoingEmail, SmtpSender
from tests.api_support import Api, RecordingSender

MAILPIT_IMAGE = "axllent/mailpit:v1.31.3"


async def queue(
    api: Api, user_id: uuid.UUID, key: str, template: EmailTemplate = EmailTemplate.NEW_DEVICE
) -> bool:
    async with api.database.unit_of_work() as session:
        return await enqueue(
            session,
            user_id=user_id,
            template=template,
            idempotency_key=key,
            now=api.clock.now(),
            data={"browser": "Test browser", "signed_in_at": "2030-01-01T00:00"},
        )


async def row(api: Api, key: str) -> tuple[str, int, str | None]:
    [(status, attempts, error)] = await api.fetch(
        "SELECT status, attempts, last_error FROM notify.email_outbox WHERE idempotency_key = :k", k=key
    )
    return status, attempts, error


async def test_an_email_exists_only_if_its_transaction_commits(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"

    class Aborted(Exception):
        pass

    async def change_that_fails() -> None:
        async with api.database.unit_of_work() as session:
            await enqueue(
                session,
                user_id=account.user_id,
                template=EmailTemplate.NEW_DEVICE,
                idempotency_key=key,
                now=api.clock.now(),
            )
            raise Aborted

    with pytest.raises(Aborted):
        await change_that_fails()
    assert await api.fetch("SELECT 1 FROM notify.email_outbox WHERE idempotency_key = :k", k=key) == []


async def test_the_same_event_is_queued_once(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    assert await queue(api, account.user_id, key)
    assert not await queue(api, account.user_id, key)
    rows = await api.fetch("SELECT count(*) FROM notify.email_outbox WHERE idempotency_key = :k", k=key)
    assert rows == [(1,)]


@pytest.mark.parametrize(
    "data",
    [
        {"email": "x"},
        {"token": "abc"},
        {"browser": "someone@dev.example"},
        {"note": "x" * 301},
        {"Bad-Key": "x"},
        {"value": [1, 2]},
    ],
)
async def test_template_data_cannot_carry_secrets_or_addresses(api: Api, data: dict[str, object]) -> None:
    account = await api.create_account()
    async with api.database.unit_of_work() as session:
        with pytest.raises(ValueError, match="template data"):
            await enqueue(
                session,
                user_id=account.user_id,
                template=EmailTemplate.NEW_DEVICE,
                idempotency_key=f"test:{uuid.uuid4()}",
                now=api.clock.now(),
                data=data,  # type: ignore[arg-type]
            )


async def test_delivery_resolves_the_address_at_send_time(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    await queue(api, account.user_id, key)
    stored = await api.fetch(
        "SELECT to_jsonb(o)::text FROM notify.email_outbox o WHERE idempotency_key = :k", k=key
    )
    assert account.email not in stored[0][0]
    await api.deliver()
    [message] = api.mail_to(account.email)
    assert "Test browser" in message.body
    assert await row(api, key) == ("sent", 1, None)


async def test_failures_retry_with_backoff_then_stop_as_failed(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    await queue(api, account.user_id, key)
    failing = RecordingSender(failures=100)
    dispatcher = api.dispatcher(failing)
    for attempt in range(1, 6):
        await dispatcher.run_once()
        status, attempts, error = await row(api, key)
        assert attempts == attempt
        assert error == "ConnectionRefusedError"
        if attempt < 5:
            assert status == "pending"
            # Not due again until the backoff passes: 1, 2, 4, 8 minutes.
            await dispatcher.run_once()
            assert (await row(api, key))[1] == attempt
            api.clock.advance(timedelta(minutes=2 ** (attempt - 1), seconds=1))
    assert await row(api, key) == ("failed", 5, "ConnectionRefusedError")
    # A failed email stays recorded and is not retried.
    api.clock.advance(timedelta(hours=2))
    await dispatcher.run_once()
    assert (await row(api, key))[0] == "failed"


async def test_an_email_that_no_longer_applies_is_cancelled(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    await queue(api, account.user_id, key)
    await api.execute("UPDATE identity.users SET status = 'disabled' WHERE id = :id", id=account.user_id)
    await api.deliver()
    assert api.mail_to(account.email) == []
    assert (await row(api, key))[0] == "cancelled"


async def test_an_abandoned_claim_is_taken_again_after_its_lease(api: Api) -> None:
    """A worker that stopped mid-send leaves the row `sending`; it is retried after the lease."""
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    await queue(api, account.user_id, key)
    await api.execute(
        "UPDATE notify.email_outbox SET status = 'sending', attempts = 1, lease_expires_at = :t "
        "WHERE idempotency_key = :k",
        t=api.clock.now() + timedelta(minutes=5),
        k=key,
    )
    await api.deliver()
    assert api.mail_to(account.email) == []
    api.clock.advance(timedelta(minutes=5, seconds=1))
    await api.deliver()
    assert len(api.mail_to(account.email)) == 1
    assert await row(api, key) == ("sent", 2, None)


async def test_concurrent_dispatchers_send_each_email_once(api: Api) -> None:
    accounts = [await api.create_account() for _ in range(6)]
    keys = [f"test:{uuid.uuid4()}" for _ in accounts]
    for account, key in zip(accounts, keys, strict=True):
        await queue(api, account.user_id, key)
    senders = [RecordingSender() for _ in range(4)]
    await asyncio.gather(*(api.dispatcher(sender).run_once() for sender in senders))
    delivered = [message.to for sender in senders for message in sender.sent]
    ours = sorted(email for email in delivered if email in {a.email for a in accounts})
    assert ours == sorted(account.email for account in accounts)
    for key in keys:
        assert (await row(api, key))[0] == "sent"


# --- real SMTP -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def mailpit() -> Iterator[tuple[str, int, int]]:
    container = (
        DockerContainer(MAILPIT_IMAGE)
        .with_exposed_ports(1025, 8025)
        .waiting_for(LogMessageWaitStrategy("accessible via"))
    )
    with container:
        yield (
            container.get_container_host_ip(),
            int(container.get_exposed_port(1025)),
            int(container.get_exposed_port(8025)),
        )


async def test_smtp_delivery_to_a_mail_server(mailpit: tuple[str, int, int]) -> None:
    host, smtp_port, http_port = mailpit
    sender = SmtpSender(
        host=host, port=smtp_port, sender="HRMS <hrms@dev.example>", security=SmtpSecurity.NONE
    )
    await sender.send(OutgoingEmail(to="person@dev.example", subject="Test subject", body="Test body"))
    async with httpx.AsyncClient(base_url=f"http://{host}:{http_port}") as client:
        [message] = (await client.get("/api/v1/messages")).json()["messages"]
        assert message["Subject"] == "Test subject"
        assert message["To"][0]["Address"] == "person@dev.example"
        detail = (await client.get(f"/api/v1/message/{message['ID']}")).json()
        assert "Test body" in detail["Text"]


async def test_smtp_failure_reports_only_an_error_class(mailpit: tuple[str, int, int]) -> None:
    host, smtp_port, _ = mailpit
    # STARTTLS is required but the server does not offer it: refused, never sent in the clear.
    sender = SmtpSender(host=host, port=smtp_port, sender="hrms@dev.example", security=SmtpSecurity.STARTTLS)
    with pytest.raises(EmailDeliveryError) as failed:
        await sender.send(OutgoingEmail(to="person@dev.example", subject="x", body="x"))
    assert "@" not in failed.value.reason
    unreachable = SmtpSender(host="127.0.0.1", port=1, sender="hrms@dev.example", security=SmtpSecurity.NONE)
    with pytest.raises(EmailDeliveryError):
        await unreachable.send(OutgoingEmail(to="person@dev.example", subject="x", body="x"))


async def test_a_row_that_cannot_be_rendered_fails_without_stopping_the_batch(api: Api) -> None:
    """Regression: a renderer error escaped the run, left the batch waiting and retried for ever."""
    poisoned, healthy = await api.create_account(), await api.create_account()
    bad_key, good_key = f"test:{uuid.uuid4()}", f"test:{uuid.uuid4()}"
    await queue(api, poisoned.user_id, bad_key, EmailTemplate.MFA_CHANGED)
    await queue(api, healthy.user_id, good_key)
    renderers = dict(api.dispatcher()._renderers)

    async def broken(session: object, item: object) -> None:
        raise RuntimeError("renderer bug")

    renderers[EmailTemplate.MFA_CHANGED] = broken
    dispatcher = OutboxDispatcher(
        database=api.worker_database, sender=api.mail, renderers=renderers, clock=api.clock
    )
    await dispatcher.run_once()
    assert await row(api, bad_key) == ("pending", 1, "RuntimeError")
    assert (await row(api, good_key))[0] == "sent"
    for _ in range(6):
        api.clock.advance(timedelta(hours=1))
        await dispatcher.run_once()
    assert await row(api, bad_key) == ("failed", 5, "RuntimeError")


async def test_an_abandoned_last_attempt_is_marked_failed(api: Api) -> None:
    account = await api.create_account()
    key = f"test:{uuid.uuid4()}"
    await queue(api, account.user_id, key)
    await api.execute(
        "UPDATE notify.email_outbox SET status = 'sending', attempts = 5, lease_expires_at = :t "
        "WHERE idempotency_key = :k",
        t=api.clock.now() - timedelta(seconds=1),
        k=key,
    )
    await api.deliver()
    assert await row(api, key) == ("failed", 5, "LeaseExpired")
    assert api.mail_to(account.email) == []
