"""Shared support for API tests: settings, a controllable clock, accounts and signed-in clients.

Accounts are created directly in the database (an active account with a password, a
confirmed TOTP factor and optional role assignments), then signed in through the real
two-step HTTP flow. Every test client gets its own client IP address, so per-IP throttling
in one test never affects another. All data is obviously fake.
"""

import base64
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import cache
from typing import Any

import httpx
import pyotp
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, text

from app.main import create_app
from app.modules.access.models import Role, UserRole
from app.modules.identity.emails import IdentityEmails
from app.modules.identity.models import Credential, FactorType, MfaFactor, User, UserStatus
from app.modules.notify.dispatcher import DispatchResult, OutboxDispatcher
from app.platform.config import ApiSettings, AppEnv, WorkerSettings
from app.platform.db import Database, create_engine
from app.platform.email import EmailDeliveryError, EmailSender, OutgoingEmail
from app.platform.security import passwords, totp
from app.platform.security.crypto import FieldCipher
from tests.conftest import DATABASE_NAME, PostgresServer, RedisServer

APP_ORIGIN = "https://hrms.test"
BASE_URL = APP_ORIGIN
FIELD_KEY = b"t" * 32
EMAIL_LOOKUP_KEY = b"e" * 32
CSRF_HEADERS = {"X-Requested-With": "sv-web"}
# A password that passes the policy; used for every test account.
PASSWORD = "correct horse battery staple 42"


def api_settings(database_url: SecretStr, **overrides: Any) -> ApiSettings:
    values: dict[str, Any] = {
        "app_env": AppEnv.TEST,
        "database_url_app": database_url,
        "redis_url": SecretStr("redis://hrms_ratelimit:unused@127.0.0.1:1/0"),
        "rate_limit_key_hmac_key": SecretStr(base64.b64encode(b"k" * 32).decode()),
        "email_lookup_hmac_key": SecretStr(base64.b64encode(EMAIL_LOOKUP_KEY).decode()),
        "app_base_url": APP_ORIGIN,
        "field_encryption_keys": SecretStr(f'{{"1": "{base64.b64encode(FIELD_KEY).decode()}"}}'),
        "field_encryption_active_version": 1,
        "hibp_enabled": False,
    }
    values.update(overrides)
    return ApiSettings.model_validate(values)


class FakeClock:
    """Starts at the real time (so database defaults and the clock agree) and moves only when told."""

    def __init__(self) -> None:
        self.current = datetime.now(UTC)

    def now(self) -> datetime:
        return self.current

    def advance(self, delta: timedelta) -> None:
        self.current += delta


@dataclass
class RecordingSleep:
    calls: list[float] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


@cache
def password_hash() -> str:
    return passwords.hash_password_sync(PASSWORD)


def random_ip() -> str:
    return f"10.{secrets.randbelow(250) + 1}.{secrets.randbelow(250) + 1}.{secrets.randbelow(250) + 1}"


def fake_email() -> str:
    return f"user-{secrets.token_hex(5)}@dev.example"


def refresh_cookie(client: httpx.AsyncClient) -> str:
    """The refresh cookie, which the jar holds only for the refresh endpoint's path."""
    value = next((cookie.value for cookie in client.cookies.jar if cookie.name == "__Secure-sv_rt"), None)
    assert value is not None
    return value


@dataclass
class Account:
    user_id: uuid.UUID
    email: str
    totp_secret: str
    employee_id: uuid.UUID | None = None

    def code(self, at: datetime) -> str:
        return pyotp.TOTP(self.totp_secret).at(at)


@dataclass
class RecordingSender:
    """Captures outgoing email instead of sending it; can be told to fail."""

    sent: list[OutgoingEmail] = field(default_factory=list)
    failures: int = 0

    async def send(self, email: OutgoingEmail) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise EmailDeliveryError("ConnectionRefusedError")
        self.sent.append(email)


LINK = re.compile(r"https://hrms\.test/(invite|password-reset)/([A-Za-z0-9_-]{43})")
# More than enough for the emails one test produces (each pass handles up to 20).
DELIVER_PASSES = 50


def link_token(email: OutgoingEmail) -> str:
    match = LINK.search(email.body)
    assert match is not None, email.body
    return match.group(2)


class Api:
    """A running application with a controllable clock, plus helpers to create accounts."""

    def __init__(
        self,
        app: FastAPI,
        clock: FakeClock,
        sleep: RecordingSleep,
        database: Database,
        worker_database: Database,
    ) -> None:
        self.app = app
        self.clock = clock
        self.sleep = sleep
        self.database = database
        self.worker_database = worker_database
        self.cipher = FieldCipher({1: FIELD_KEY}, 1)
        self.mail = RecordingSender()

    def dispatcher(self, sender: EmailSender | None = None) -> OutboxDispatcher:
        """The outbox dispatcher as the worker runs it (worker database role)."""
        return OutboxDispatcher(
            database=self.worker_database,
            sender=sender or self.mail,
            renderers=IdentityEmails(app_base_url=APP_ORIGIN, clock=self.clock).renderers(),
            clock=self.clock,
        )

    async def deliver(self) -> list[OutgoingEmail]:
        """Run the dispatcher until nothing is due; returns the emails sent by this call."""
        before = len(self.mail.sent)
        # Bounded, so a dispatcher that keeps finding work fails the test instead of hanging it.
        for _ in range(DELIVER_PASSES):
            if await self.dispatcher().run_once() == DispatchResult():
                return self.mail.sent[before:]
        raise AssertionError(f"the outbox still had due emails after {DELIVER_PASSES} dispatch passes")

    def mail_to(self, email: str) -> list[OutgoingEmail]:
        return [message for message in self.mail.sent if message.to == email]

    @asynccontextmanager
    async def client(self, *, ip: str | None = None, csrf: bool = True) -> AsyncIterator[httpx.AsyncClient]:
        transport = httpx.ASGITransport(app=self.app, client=(ip or random_ip(), 51000))
        async with httpx.AsyncClient(
            transport=transport, base_url=BASE_URL, headers=CSRF_HEADERS if csrf else {}
        ) as client:
            yield client

    async def create_account(
        self,
        *,
        roles: tuple[str, ...] = (),
        employee_id: uuid.UUID | None = None,
        status: UserStatus = UserStatus.ACTIVE,
        role_scope: dict[str, Any] | None = None,
    ) -> Account:
        email = fake_email()
        secret = totp.new_secret()
        async with self.database.unit_of_work() as session:
            now = self.clock.now()
            user = User(
                email=email,
                status=status.value,
                employee_id=employee_id,
                email_verified_at=now if status != UserStatus.INVITED else None,
            )
            session.add(user)
            await session.flush()
            session.add(Credential(user_id=user.id, password_hash=password_hash(), password_changed_at=now))
            encrypted = self.cipher.encrypt(
                secret.encode(), b"identity.mfa_factors.secret:" + str(user.id).encode()
            )
            session.add(
                MfaFactor(
                    user_id=user.id,
                    type=FactorType.TOTP.value,
                    label="Test authenticator",
                    secret_ciphertext=encrypted.ciphertext,
                    secret_key_version=encrypted.key_version,
                    confirmed_at=now,
                )
            )
            for role_key in roles:
                role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
                session.add(
                    UserRole(
                        user_id=user.id,
                        role_id=role_id,
                        valid_from=now - timedelta(minutes=1),
                        grant_reason="Test assignment",
                        **(role_scope or {}),
                    )
                )
        return Account(user.id, email, secret, employee_id)

    async def sign_in(self, client: httpx.AsyncClient, account: Account) -> httpx.Response:
        """The two-step sign-in. Moves the clock to a fresh TOTP step first, so a code is never
        rejected as a replay of an earlier sign-in in the same test."""
        self.clock.advance(timedelta(seconds=totp.STEP_SECONDS))
        login = await client.post("/api/v1/auth/login", json={"email": account.email, "password": PASSWORD})
        assert login.status_code == 200, login.text
        response = await client.post(
            "/api/v1/auth/login/mfa",
            json={"mfa_token": login.json()["mfa_token"], "code": account.code(self.clock.now())},
        )
        assert response.status_code == 200, response.text
        return response

    async def step_up(self, client: httpx.AsyncClient, account: Account) -> httpx.Response:
        self.clock.advance(timedelta(seconds=totp.STEP_SECONDS))
        response = await client.post("/api/v1/auth/step-up", json={"code": account.code(self.clock.now())})
        assert response.status_code == 200, response.text
        return response

    async def fetch(self, statement: str, **params: Any) -> list[Any]:
        async with self.database.unit_of_work() as session:
            return list((await session.execute(text(statement), params)).all())

    async def execute(self, statement: str, **params: Any) -> None:
        async with self.database.unit_of_work() as session:
            await session.execute(text(statement), params)


@asynccontextmanager
async def running_api(
    postgres: PostgresServer, redis: RedisServer, *, database_name: str = DATABASE_NAME, **settings: Any
) -> AsyncIterator[Api]:
    """The API on `database_name`: the shared test database unless a test needs one of its own."""
    clock, sleep = FakeClock(), RecordingSleep()
    settings.setdefault("redis_url", SecretStr(redis.url()))
    app_url = postgres.url("hrms_app", database_name)
    app = create_app(api_settings(app_url, **settings), clock=clock, sleep=sleep)
    async with app.router.lifespan_context(app):
        database = Database(create_engine(app_url, application_name="test-support"))
        worker = Database(
            create_engine(postgres.url("hrms_worker", database_name), application_name="test-worker")
        )
        try:
            yield Api(app, clock, sleep, database, worker)
        finally:
            await database.dispose()
            await worker.dispose()


def worker_settings(database_url: SecretStr, **overrides: Any) -> WorkerSettings:
    """Worker settings for tests: plain SMTP to an address nothing listens on, unless overridden."""
    values: dict[str, Any] = {
        "app_env": AppEnv.TEST,
        "database_url_worker": database_url,
        "app_base_url": APP_ORIGIN,
        "smtp_host": "127.0.0.1",
        "smtp_port": 1,
        "smtp_from": "HRMS <hrms@dev.example>",
        "smtp_security": "none",
    }
    values.update(overrides)
    return WorkerSettings.model_validate(values)
