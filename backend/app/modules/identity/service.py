"""Sign-in, sessions, step-up, password and MFA management, invite activation.

docs/security-architecture.md §3, docs/api-architecture.md §5. Rules that shape the code:

- MFA is mandatory. The password step never creates a session; it returns a 5-minute,
  single-use MFA token. A TOTP code then creates a full session, a recovery code an
  enrolment-only one.
- Every sign-in failure answers the same way (unknown email, wrong password, locked or
  disabled account, wrong code), with the timing of a real password check.
- Failed password and MFA attempts count towards the account lockout, kept in PostgreSQL.
  Per-IP throttling uses the Redis rate limiter, which fails closed for these endpoints.
- Password hashing and verification run in a worker thread, outside any transaction.
  Failure counters are committed before the error is raised, so a refused attempt still
  counts.
- Refresh tokens are single-use. Presenting a used one revokes the whole session.
- Audit and security records are written in the same transaction as the change.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import defaults, devices, notifications
from app.modules.identity import repository as repo
from app.modules.identity.history import LoginHistoryRow, login_events
from app.modules.identity.models import (
    AuthSession,
    ClientType,
    DeviceRevokedReason,
    FactorType,
    MfaFactor,
    OneTimePurpose,
    OneTimeToken,
    RevokedReason,
    SessionToken,
    TokenKind,
    TrustedDevice,
    User,
    UserStatus,
)
from app.modules.people import public as people
from app.platform import settings as policy
from app.platform.audit.records import (
    AuditActor,
    AuditEvent,
    RequestContext,
    SecurityEvent,
    SecurityEventType,
    Severity,
    ValueChange,
)
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor, SessionScope
from app.platform.authz.engine import Authorizer
from app.platform.authz.sod import SodRule, refuse_self
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter, RateLimitExceeded
from app.platform.security import passwords, tokens, totp
from app.platform.security.crypto import DecryptionError, Encrypted, FieldCipher
from app.platform.security.emails import EmailHasher

SECRET_PURPOSE: Final = b"identity.mfa_factors.secret:"

type Sleep = Callable[[float], Awaitable[None]]


class InvalidCredentials(ProblemError):
    def __init__(self, code: str | None = None) -> None:
        super().__init__(ProblemType.INVALID_CREDENTIALS, extensions={"code": code} if code else None)


class Unauthenticated(ProblemError):
    def __init__(self) -> None:
        super().__init__(ProblemType.UNAUTHENTICATED)


def conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def field_error(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


@dataclass(frozen=True, slots=True)
class IssuedSession:
    session_id: uuid.UUID
    scope: SessionScope
    access_token: str
    access_expires_at: datetime
    refresh_token: str
    refresh_expires_at: datetime
    # A new trusted-device cookie to set, when this sign-in came from a new browser.
    device: devices.IssuedDevice | None = None


@dataclass(frozen=True, slots=True)
class FactorSetup:
    factor_id: uuid.UUID
    secret: str
    otpauth_uri: str
    qr_code: str


@dataclass(frozen=True, slots=True)
class Activation:
    session: IssuedSession
    recovery_codes: Sequence[str]


@dataclass(frozen=True, slots=True)
class SessionActor:
    """The parts of the acting session the identity service needs."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    scope: SessionScope


class IdentityService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        cipher: FieldCipher,
        breach_checker: passwords.BreachedPasswordChecker,
        rate_limiter: RateLimiter,
        audit: AuditWriter,
        authorizer: Authorizer,
        email_hasher: EmailHasher,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._db = database
        self._clock = clock
        self._cipher = cipher
        self._breach = breach_checker
        self._limiter = rate_limiter
        self._audit = audit
        self._authz = authorizer
        self._email_hasher = email_hasher
        self._sleep = sleep

    # --- sign-in: password step ----------------------------------------------------------

    async def login(self, context: RequestContext, email: str, password: str) -> str:
        """Check email and password; return a single-use MFA token. Never a session."""
        await self._check_ip_block(context)
        async with self._db.unit_of_work() as session:
            values = await policy.load(session)
            user = await repo.user_by_email(session, email)
            credential = await repo.credential(session, user.id) if user else None
        now = self._clock.now()

        if user is None or credential is None:
            await passwords.burn_verification_time(password)
            await self._record_ip_failure(context, values)
            await self._record_unknown_account_failure(context, email)
            raise InvalidCredentials

        if user.locked_until is not None and user.locked_until > now:
            await passwords.burn_verification_time(password)
            await self._record_ip_failure(context, values)
            await self._record_failure(context, user.id, stage="password", values=values, count=False)
            raise InvalidCredentials

        await self._progressive_delay(user, now, values)
        verification = await passwords.verify_password(credential.password_hash, password)
        if not verification.valid or user.status != UserStatus.ACTIVE:
            await self._record_ip_failure(context, values)
            await self._record_failure(context, user.id, stage="password", values=values)
            raise InvalidCredentials

        new_hash = await passwords.hash_password(password) if verification.needs_rehash else None
        raw = tokens.new_token()
        async with self._db.unit_of_work() as session:
            if new_hash is not None:
                await repo.store_password(session, user.id, new_hash, now, keep_changed_at=True)
            await repo.retire_one_time_tokens(session, user.id, OneTimePurpose.LOGIN_MFA, now)
            session.add(
                OneTimeToken(
                    user_id=user.id,
                    purpose=OneTimePurpose.LOGIN_MFA.value,
                    token_hash=tokens.token_digest(raw),
                    expires_at=now + defaults.LOGIN_MFA_TOKEN_TTL,
                )
            )
        return raw

    # --- sign-in: MFA step ---------------------------------------------------------------

    async def complete_login(
        self,
        context: RequestContext,
        mfa_token: str,
        *,
        code: str | None,
        recovery_code: str | None,
        device_token: str | None = None,
    ) -> IssuedSession:
        """TOTP code -> full session; recovery code -> enrolment-only session. A sign-in from
        a browser that is not a trusted device of the account is reported by email."""
        await self._check_ip_block(context)
        now = self._clock.now()
        if not tokens.looks_like_token(mfa_token):
            raise InvalidCredentials("login.challenge_expired")
        # The TOTP secrets are read and checked inside the transaction because the factor's
        # last-used step must be updated atomically with the check (replay protection).
        failure: str | None = None
        issued: IssuedSession | None = None
        async with self._db.unit_of_work() as session:
            values = await policy.load(session)
            challenge = await repo.lock_one_time_token(
                session, tokens.token_digest(mfa_token), OneTimePurpose.LOGIN_MFA, now
            )
            if challenge is None or challenge.failed_attempts >= defaults.LOGIN_MFA_MAX_ATTEMPTS:
                failure = "login.challenge_expired"
            else:
                user = _required(await repo.lock_user(session, challenge.user_id))
                if user.status != UserStatus.ACTIVE or (
                    user.locked_until is not None and user.locked_until > now
                ):
                    challenge.used_at = now
                    failure = "login.failed"
                else:
                    scope = await self._verify_second_factor(session, user, code, recovery_code, now)
                    if scope is None:
                        challenge.failed_attempts += 1
                        await self._failure_records(
                            session, context, user.id, stage="mfa", now=now, values=values
                        )
                        failure = "login.failed"
                    else:
                        challenge.used_at = now
                        issued = await self._open_session(
                            session, user, scope, context, now=now, values=values
                        )
                        await self._signed_in(session, context, user, scope, issued)
                        issued = await self._recognize_device(
                            session,
                            context,
                            user,
                            issued,
                            device_token=device_token,
                            now=now,
                            values=values,
                            report_new=True,
                        )
        if failure is not None or issued is None:
            if failure == "login.failed":
                await self._record_ip_failure(context, values)
                raise InvalidCredentials
            raise InvalidCredentials("login.challenge_expired")
        return issued

    async def _verify_second_factor(
        self,
        session: AsyncSession,
        user: User,
        code: str | None,
        recovery_code: str | None,
        now: datetime,
    ) -> SessionScope | None:
        if code is not None and await self._accept_totp(session, user, code, now):
            return SessionScope.FULL
        if recovery_code is not None:
            digest = tokens.recovery_code_digest(recovery_code)
            if digest is not None and await repo.use_recovery_code(session, user.id, digest, now):
                return SessionScope.MFA_ENROLMENT
        return None

    async def _accept_totp(self, session: AsyncSession, user: User, code: str, now: datetime) -> bool:
        if not totp.is_code_shape(code):
            return False
        for factor in await repo.confirmed_factors(session, user.id):
            step = totp.match_step(self._secret(factor), code, now)
            if step is not None:
                return await repo.record_factor_use(session, factor.id, step, now)
        return False

    async def _signed_in(
        self,
        session: AsyncSession,
        context: RequestContext,
        user: User,
        scope: SessionScope,
        issued: IssuedSession,
    ) -> None:
        await repo.clear_failures(session, user.id, self._clock.now())
        await self._audit.record_security_event(
            session,
            SecurityEvent(
                SecurityEventType.LOGIN_SUCCEEDED,
                Severity.INFO,
                context=context,
                user_id=user.id,
                session_id=issued.session_id,
                details={"second_factor": "totp" if scope is SessionScope.FULL else "recovery_code"},
            ),
        )
        if scope is SessionScope.MFA_ENROLMENT:
            remaining = await repo.unused_recovery_code_count(session, user.id)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.MFA_RECOVERY_CODE_USED,
                    Severity.HIGH,
                    context=context,
                    user_id=user.id,
                    session_id=issued.session_id,
                    details={"remaining_codes": remaining},
                ),
            )
            await notifications.recovery_code_used(
                session, user.id, issued.session_id, remaining, self._clock.now()
            )

    async def _recognize_device(
        self,
        session: AsyncSession,
        context: RequestContext,
        user: User,
        issued: IssuedSession,
        *,
        device_token: str | None,
        now: datetime,
        values: policy.Settings,
        report_new: bool,
    ) -> IssuedSession:
        """Recognize the browser or trust it as a new device (reported by email when asked)."""
        recognition = await devices.recognize(
            session,
            user_id=user.id,
            device_token=device_token,
            user_agent=context.user_agent,
            now=now,
            lifetime=timedelta(days=values[policy.TRUSTED_DEVICE_LIFETIME]),
        )
        if not recognition.new:
            return issued
        await self._audit.record_security_event(
            session,
            SecurityEvent(
                SecurityEventType.DEVICE_TRUSTED,
                Severity.INFO,
                context=context,
                user_id=user.id,
                session_id=issued.session_id,
                details={"device_id": recognition.device_id},
            ),
        )
        if report_new:
            await notifications.new_device(session, user.id, recognition.device_id, context.user_agent, now)
        return replace(issued, device=recognition.issued)

    # --- throttling and lockout ----------------------------------------------------------

    async def _check_ip_block(self, context: RequestContext) -> None:
        if context.ip is None:
            return
        decision = await self._limiter.peek(defaults.LOGIN_IP_BLOCK, str(context.ip))
        if not decision.allowed:
            raise RateLimitExceeded(decision.retry_after_seconds)

    async def _record_ip_failure(self, context: RequestContext, values: policy.Settings) -> None:
        if context.ip is None:
            return
        subject = str(context.ip)
        rule = defaults.login_ip_failures(values[policy.LOGIN_IP_FAILURES])
        decision = await self._limiter.record(rule, subject)
        if decision.remaining == 0:
            block = await self._limiter.hit(defaults.LOGIN_IP_BLOCK, subject)
            if block.allowed:
                await self._audit.record_separately(
                    self._db,
                    SecurityEvent(
                        SecurityEventType.RATELIMIT_TRIPPED,
                        Severity.WARNING,
                        context=context,
                        details={"rule": defaults.LOGIN_IP_BLOCK.name},
                    ),
                )

    async def _progressive_delay(self, user: User, now: datetime, values: policy.Settings) -> None:
        """1, 2, 4 ... 30 s between attempts once an account has recent failures."""
        delay_after = values[policy.LOCKOUT_DELAY_AFTER]
        if user.failed_login_count < delay_after or user.last_failed_login_at is None:
            return
        window = timedelta(minutes=values[policy.LOCKOUT_WINDOW])
        if user.failed_login_window_started_at is None or user.failed_login_window_started_at <= now - window:
            return
        exponent = user.failed_login_count - delay_after
        delay = timedelta(seconds=min(2**exponent, defaults.MAX_DELAY_SECONDS))
        remaining = (user.last_failed_login_at + delay - now).total_seconds()
        if remaining > 0:
            await self._sleep(remaining)

    async def _record_failure(
        self,
        context: RequestContext,
        user_id: uuid.UUID,
        *,
        stage: str,
        values: policy.Settings,
        count: bool = True,
    ) -> None:
        async with self._db.unit_of_work() as session:
            await self._failure_records(
                session, context, user_id, stage=stage, now=self._clock.now(), values=values, count=count
            )

    async def _count_failure(
        self,
        session: AsyncSession,
        context: RequestContext,
        user_id: uuid.UUID,
        now: datetime,
        values: policy.Settings,
    ) -> repo.FailureState:
        """Count a failed attempt; at the lockout threshold, record the lock and queue one
        lockout email (one per lock, however many attempts follow)."""
        lockout = repo.LockoutPolicy(
            threshold=values[policy.LOCKOUT_THRESHOLD],
            window=timedelta(minutes=values[policy.LOCKOUT_WINDOW]),
            duration=timedelta(minutes=values[policy.LOCKOUT_DURATION]),
        )
        state = await repo.register_failure(session, user_id, now, lockout)
        if state.count == lockout.threshold and state.locked_until is not None:
            minutes = values[policy.LOCKOUT_DURATION]
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.ACCOUNT_LOCKED,
                    Severity.HIGH,
                    context=context,
                    user_id=user_id,
                    details={"locked_minutes": minutes},
                ),
            )
            await notifications.account_locked(session, user_id, state.locked_until, minutes, now)
        return state

    async def _failure_records(
        self,
        session: AsyncSession,
        context: RequestContext,
        user_id: uuid.UUID,
        *,
        stage: str,
        now: datetime,
        values: policy.Settings,
        count: bool = True,
    ) -> None:
        state = await self._count_failure(session, context, user_id, now, values) if count else None
        await self._audit.record_security_event(
            session,
            SecurityEvent(
                SecurityEventType.LOGIN_FAILED,
                Severity.WARNING,
                context=context,
                user_id=user_id,
                details={"stage": stage, "failed_attempts": state.count if state else None},
            ),
        )

    async def _record_unknown_account_failure(self, context: RequestContext, email: str) -> None:
        """Recorded with the keyed hash of the attempted email, never the email itself."""
        await self._audit.record_separately(
            self._db,
            SecurityEvent(
                SecurityEventType.LOGIN_FAILED,
                Severity.WARNING,
                context=context,
                email_attempted_hash=self._email_hasher.digest(email),
                details={"stage": "password", "known_user": False},
            ),
        )

    # --- sessions ------------------------------------------------------------------------

    async def _open_session(
        self,
        session: AsyncSession,
        user: User,
        scope: SessionScope,
        context: RequestContext,
        *,
        now: datetime,
        values: policy.Settings,
    ) -> IssuedSession:
        absolute = now + timedelta(hours=values[policy.SESSION_ABSOLUTE])
        record = AuthSession(
            user_id=user.id,
            client_type=ClientType.WEB.value,
            scope=scope.value,
            ip=str(context.ip) if context.ip else None,
            user_agent=context.user_agent,
            last_activity_at=now,
            idle_expires_at=min(now + timedelta(minutes=values[policy.SESSION_IDLE]), absolute),
            absolute_expires_at=absolute,
        )
        session.add(record)
        await session.flush()
        return await self._issue_tokens(session, record, now, replaces=None)

    async def _issue_tokens(
        self, session: AsyncSession, record: AuthSession, now: datetime, *, replaces: SessionToken | None
    ) -> IssuedSession:
        access_raw, refresh_raw = tokens.new_token(), tokens.new_token()
        access_expires = min(now + defaults.ACCESS_TOKEN_TTL, record.absolute_expires_at)
        access = SessionToken(
            session_id=record.id,
            kind=TokenKind.ACCESS.value,
            token_hash=tokens.token_digest(access_raw),
            expires_at=access_expires,
        )
        refresh = SessionToken(
            session_id=record.id,
            kind=TokenKind.REFRESH.value,
            token_hash=tokens.token_digest(refresh_raw),
            expires_at=record.absolute_expires_at,
        )
        session.add_all([access, refresh])
        await session.flush()
        if replaces is not None:
            replaces.replaced_by_id = refresh.id
        return IssuedSession(
            session_id=record.id,
            scope=SessionScope(record.scope),
            access_token=access_raw,
            access_expires_at=access_expires,
            refresh_token=refresh_raw,
            refresh_expires_at=record.absolute_expires_at,
        )

    async def rotate(self, session: AsyncSession, record: AuthSession, now: datetime) -> IssuedSession:
        """New access and refresh tokens for a session; the previous ones stop working."""
        await repo.expire_tokens(session, now=now, session_id=record.id)
        return await self._issue_tokens(session, record, now, replaces=None)

    async def refresh(self, context: RequestContext, refresh_token: str) -> IssuedSession:
        """Single-use rotation; a used token revokes the session (token theft signal).

        Locks the session row before the token row, the same order every revocation uses, so a
        refresh racing a sign-out or a revocation waits instead of deadlocking.
        """
        if not tokens.looks_like_token(refresh_token):
            raise Unauthenticated
        now = self._clock.now()
        digest = tokens.token_digest(refresh_token)
        issued: IssuedSession | None = None
        async with self._db.unit_of_work() as session:
            found = await repo.find_refresh_token(session, digest)
            record = (
                await session.get(AuthSession, found.session_id, with_for_update=True, populate_existing=True)
                if found
                else None
            )
            token = await repo.lock_refresh_token(session, digest) if record else None
            user = await repo.get_user(session, record.user_id) if record else None
            if token is None or record is None or user is None:
                pass
            elif token.used_at is not None:
                if record.revoked_at is None:
                    await repo.revoke_sessions(
                        session,
                        user_id=user.id,
                        session_id=record.id,
                        reason=RevokedReason.TOKEN_REUSE,
                        now=now,
                    )
                    await self._audit.record_security_event(
                        session,
                        SecurityEvent(
                            SecurityEventType.TOKEN_REUSE_DETECTED,
                            Severity.HIGH,
                            context=context,
                            user_id=user.id,
                            session_id=record.id,
                        ),
                    )
            elif _session_usable(record, user, now) and token.expires_at > now:
                token.used_at = now
                await repo.expire_tokens(session, now=now, session_id=record.id)
                issued = await self._issue_tokens(session, record, now, replaces=token)
        if issued is None:
            raise Unauthenticated
        return issued

    async def logout(self, actor: SessionActor, context: RequestContext) -> None:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            revoked = await repo.revoke_sessions(
                session,
                user_id=actor.user_id,
                session_id=actor.session_id,
                reason=RevokedReason.LOGOUT,
                now=now,
            )
            if revoked:
                await self._session_revoked_event(
                    session,
                    context=context,
                    user_id=actor.user_id,
                    actor=actor,
                    revoked=revoked,
                    reason="logout",
                )

    async def list_sessions(self, user_id: uuid.UUID) -> Sequence[AuthSession]:
        async with self._db.unit_of_work() as session:
            return await repo.active_sessions(session, user_id, self._clock.now())

    async def revoke_own_session(
        self, actor: SessionActor, context: RequestContext, session_id: uuid.UUID
    ) -> None:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            revoked = await repo.revoke_sessions(
                session,
                user_id=actor.user_id,
                session_id=session_id,
                reason=RevokedReason.REVOKED_BY_USER,
                now=now,
            )
            if not revoked:
                raise ProblemError(ProblemType.NOT_FOUND)
            await self._session_revoked_event(
                session,
                context=context,
                user_id=actor.user_id,
                actor=actor,
                revoked=revoked,
                reason="revoked_by_user",
            )

    async def revoke_other_sessions(self, actor: SessionActor, context: RequestContext) -> int:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            revoked = await repo.revoke_sessions(
                session,
                user_id=actor.user_id,
                except_session_id=actor.session_id,
                reason=RevokedReason.REVOKED_BY_USER,
                now=now,
            )
            if revoked:
                await self._session_revoked_event(
                    session,
                    context=context,
                    user_id=actor.user_id,
                    actor=actor,
                    revoked=revoked,
                    reason="revoked_by_user",
                )
            return len(revoked)

    async def _session_revoked_event(
        self,
        session: AsyncSession,
        *,
        context: RequestContext,
        user_id: uuid.UUID,
        actor: SessionActor,
        revoked: Sequence[uuid.UUID],
        reason: str,
    ) -> None:
        await self._audit.record_security_event(
            session,
            SecurityEvent(
                SecurityEventType.SESSION_REVOKED,
                Severity.INFO,
                context=context,
                user_id=user_id,
                session_id=actor.session_id if actor.user_id == user_id else None,
                details={"reason": reason, "sessions": len(revoked), "by_user_id": actor.user_id},
            ),
        )

    # --- step-up -------------------------------------------------------------------------

    async def step_up(self, actor: SessionActor, context: RequestContext, code: str) -> IssuedSession:
        """Re-verify the authenticator (never a password or recovery code) and rotate tokens."""
        await self._limiter.enforce(defaults.STEP_UP_PER_SESSION, str(actor.session_id))
        now = self._clock.now()
        issued: IssuedSession | None = None
        async with self._db.unit_of_work() as session:
            values = await policy.load(session)
            user = _required(await repo.lock_user(session, actor.user_id))
            record = _required(
                await session.get(AuthSession, actor.session_id, with_for_update=True, populate_existing=True)
            )
            if await self._accept_totp(session, user, code, now):
                record.step_up_at = now
                issued = await self.rotate(session, record, now)
                event = SecurityEvent(
                    SecurityEventType.STEP_UP_SUCCEEDED,
                    Severity.INFO,
                    context=context,
                    user_id=user.id,
                    session_id=record.id,
                )
            else:
                await self._count_failure(session, context, user.id, now, values)
                event = SecurityEvent(
                    SecurityEventType.STEP_UP_FAILED,
                    Severity.WARNING,
                    context=context,
                    user_id=user.id,
                    session_id=record.id,
                )
            await self._audit.record_security_event(session, event)
        if issued is None:
            raise field_error("code", "mfa.invalid_code", "That code is not correct. Enter the current code.")
        return issued

    # --- password ------------------------------------------------------------------------

    async def change_password(
        self,
        actor: SessionActor,
        context: RequestContext,
        current_password: str,
        new_password: str,
        *,
        device_token: str | None = None,
    ) -> None:
        """Current password + a policy-compliant new one. Revokes every other session and every
        other trusted device, ends pending sign-in challenges, and tells the user by email."""
        async with self._db.unit_of_work() as session:
            values = await policy.load(session)
            credential = _required(await repo.credential(session, actor.user_id))
        if not (await passwords.verify_password(credential.password_hash, current_password)).valid:
            await self._record_failure(context, actor.user_id, stage="password_change", values=values)
            raise field_error(
                "current_password", "password.incorrect", "Your current password is not correct."
            )
        await self._check_policy(new_password, field="new_password")
        new_hash = await passwords.hash_password(new_password)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await repo.lock_user(session, actor.user_id)
            await repo.store_password(session, actor.user_id, new_hash, now)
            revoked = await repo.revoke_sessions(
                session,
                user_id=actor.user_id,
                except_session_id=actor.session_id,
                reason=RevokedReason.PASSWORD_CHANGED,
                now=now,
            )
            await repo.retire_one_time_tokens(session, actor.user_id, OneTimePurpose.LOGIN_MFA, now)
            await devices.revoke_all(
                session,
                user_id=actor.user_id,
                reason=DeviceRevokedReason.PASSWORD_CHANGED,
                now=now,
                keep_token=device_token,
            )
            await notifications.password_changed(session, actor.user_id, now, by_reset=False)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.PASSWORD_CHANGED,
                    Severity.INFO,
                    context=context,
                    user_id=actor.user_id,
                    session_id=actor.session_id,
                    details={"sessions_revoked": len(revoked)},
                ),
            )

    async def _check_policy(self, password: str, *, field: str) -> None:
        problem = await self._breach.problem(password)
        if problem is not None:
            raise field_error(field, problem.value, passwords.PROBLEM_MESSAGES[problem])

    # --- MFA factors ---------------------------------------------------------------------

    def _secret(self, factor: MfaFactor) -> str:
        try:
            return self._cipher.decrypt(
                Encrypted(factor.secret_key_version, factor.secret_ciphertext), _secret_aad(factor.user_id)
            ).decode()
        except DecryptionError as exc:
            raise RuntimeError("an MFA secret could not be decrypted") from exc

    async def list_factors(self, user_id: uuid.UUID) -> Sequence[MfaFactor]:
        async with self._db.unit_of_work() as session:
            return await repo.confirmed_factors(session, user_id)

    async def setup_factor(self, user_id: uuid.UUID, label: str) -> FactorSetup:
        """A new, unconfirmed TOTP factor. Its secret is shown only in this response."""
        async with self._db.unit_of_work() as session:
            return await self._setup_factor(session, user_id, label)

    async def _setup_factor(self, session: AsyncSession, user_id: uuid.UUID, label: str) -> FactorSetup:
        now = self._clock.now()
        user = _required(await repo.lock_user(session, user_id))
        if len(await repo.confirmed_factors(session, user_id)) >= defaults.MAX_ACTIVE_FACTORS:
            raise conflict(
                "mfa.too_many_factors",
                f"You can have at most {defaults.MAX_ACTIVE_FACTORS} authenticators. Remove one first.",
            )
        await repo.revoke_pending_factors(session, user_id, now)
        secret = totp.new_secret()
        encrypted = self._cipher.encrypt(secret.encode(), _secret_aad(user_id))
        factor = MfaFactor(
            user_id=user_id,
            type=FactorType.TOTP.value,
            label=label,
            secret_ciphertext=encrypted.ciphertext,
            secret_key_version=encrypted.key_version,
        )
        session.add(factor)
        await session.flush()
        uri = totp.provisioning_uri(secret, user.email)
        return FactorSetup(factor.id, secret, uri, totp.qr_svg_data_uri(uri))

    async def confirm_factor(
        self,
        actor: SessionActor,
        context: RequestContext,
        factor_id: uuid.UUID,
        code: str,
        *,
        device_token: str | None = None,
    ) -> IssuedSession | None:
        """Confirm a new factor. In an enrolment-only session this completes the recovery:
        the session becomes a full one, with new tokens."""
        now = self._clock.now()
        issued: IssuedSession | None = None
        async with self._db.unit_of_work() as session:
            await self._confirm_factor(session, actor.user_id, factor_id, code, now)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.MFA_ENROLLED,
                    Severity.INFO,
                    context=context,
                    user_id=actor.user_id,
                    session_id=actor.session_id,
                ),
            )
            await self._mfa_changed(session, actor.user_id, "authenticator_added", now, device_token)
            if actor.scope is SessionScope.MFA_ENROLMENT:
                record = _required(
                    await session.get(
                        AuthSession, actor.session_id, with_for_update=True, populate_existing=True
                    )
                )
                record.scope = SessionScope.FULL.value
                issued = await self.rotate(session, record, now)
        return issued

    async def _confirm_factor(
        self, session: AsyncSession, user_id: uuid.UUID, factor_id: uuid.UUID, code: str, now: datetime
    ) -> None:
        await repo.lock_user(session, user_id)
        factor = await repo.pending_factor(
            session, user_id, factor_id, now - defaults.FACTOR_CONFIRMATION_TTL
        )
        if factor is None:
            raise conflict(
                "mfa.setup_expired",
                "This authenticator setup has expired or was replaced. Start the setup again.",
            )
        step = totp.match_step(self._secret(factor), code, now)
        if step is None:
            raise field_error("code", "mfa.invalid_code", "That code is not correct. Enter the current code.")
        factor.confirmed_at = now
        factor.last_used_step = step
        factor.last_used_at = now
        factor.updated_at = now

    async def _mfa_changed(
        self, session: AsyncSession, user_id: uuid.UUID, change: str, now: datetime, device_token: str | None
    ) -> None:
        """Any MFA change re-evaluates trust: every other trusted device is revoked, and the
        user is told by email."""
        await devices.revoke_all(
            session, user_id=user_id, reason=DeviceRevokedReason.MFA_CHANGED, now=now, keep_token=device_token
        )
        await notifications.mfa_changed(session, user_id, change, now)

    async def remove_factor(
        self,
        actor: SessionActor,
        context: RequestContext,
        factor_id: uuid.UUID,
        *,
        device_token: str | None = None,
    ) -> None:
        """Refused for the last confirmed factor; the database refuses it too."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await repo.lock_user(session, actor.user_id)
            factors = await repo.confirmed_factors(session, actor.user_id)
            factor = next((f for f in factors if f.id == factor_id), None)
            if factor is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if len(factors) == 1:
                raise conflict(
                    "mfa.last_factor",
                    "This is your only authenticator. Add another one before removing it.",
                )
            factor.revoked_at = now
            factor.updated_at = now
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.MFA_FACTOR_REMOVED,
                    Severity.WARNING,
                    context=context,
                    user_id=actor.user_id,
                    session_id=actor.session_id,
                ),
            )
            await self._mfa_changed(session, actor.user_id, "authenticator_removed", now, device_token)

    async def regenerate_recovery_codes(
        self, actor: SessionActor, context: RequestContext, *, device_token: str | None = None
    ) -> list[str]:
        async with self._db.unit_of_work() as session:
            await repo.lock_user(session, actor.user_id)
            codes = await self._new_recovery_codes(session, actor.user_id)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.MFA_RECOVERY_CODES_REGENERATED,
                    Severity.WARNING,
                    context=context,
                    user_id=actor.user_id,
                    session_id=actor.session_id,
                ),
            )
            await self._mfa_changed(
                session, actor.user_id, "recovery_codes_replaced", self._clock.now(), device_token
            )
        return codes

    async def _new_recovery_codes(self, session: AsyncSession, user_id: uuid.UUID) -> list[str]:
        codes = [tokens.new_recovery_code() for _ in range(defaults.RECOVERY_CODE_COUNT)]
        digests = [tokens.recovery_code_digest(code) for code in codes]
        await repo.replace_recovery_codes(
            session, user_id, [digest for digest in digests if digest is not None], self._clock.now()
        )
        return codes

    # --- invitations ---------------------------------------------------------------------

    async def _invited_user(self, session: AsyncSession, invite_token: str, *, lock: bool) -> User | None:
        if not tokens.looks_like_token(invite_token):
            return None
        now = self._clock.now()
        digest = tokens.token_digest(invite_token)
        token = (
            await repo.lock_one_time_token(session, digest, OneTimePurpose.INVITE, now)
            if lock
            else await repo.find_one_time_token(session, digest, OneTimePurpose.INVITE, now)
        )
        if token is None:
            return None
        user = await (
            repo.lock_user(session, token.user_id) if lock else repo.get_user(session, token.user_id)
        )
        return user if user is not None and user.status == UserStatus.INVITED else None

    async def first_name_of(self, user: User) -> str | None:
        """The linked employee's preferred or legal first name, for the invite greeting."""
        if user.employee_id is None:
            return None
        async with self._db.unit_of_work() as session:
            return await people.first_name(session, user.employee_id)

    async def check_invite(self, context: RequestContext, invite_token: str) -> User | None:
        await self._limit_invite(context)
        async with self._db.unit_of_work() as session:
            return await self._invited_user(session, invite_token, lock=False)

    async def accept_invite_password(self, context: RequestContext, invite_token: str, password: str) -> str:
        """Step 1: set the password. Returns a 30-minute enrolment token for steps 2 and 3."""
        await self._limit_invite(context)
        # A cheap check first: an invalid link never costs an Argon2 hash (each uses 64 MiB
        # and a limited slot), so garbage links cannot slow down sign-in for everyone.
        async with self._db.unit_of_work() as session:
            if await self._invited_user(session, invite_token, lock=False) is None:
                raise _invite_invalid()
        await self._check_policy(password, field="password")
        password_hash = await passwords.hash_password(password)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            user = await self._invited_user(session, invite_token, lock=True)
            if user is None:
                raise _invite_invalid()
            await repo.store_password(session, user.id, password_hash, now)
            await repo.retire_one_time_tokens(session, user.id, OneTimePurpose.INVITE_ENROLMENT, now)
            raw = tokens.new_token()
            session.add(
                OneTimeToken(
                    user_id=user.id,
                    purpose=OneTimePurpose.INVITE_ENROLMENT.value,
                    token_hash=tokens.token_digest(raw),
                    expires_at=now + defaults.INVITE_ENROLMENT_TTL,
                )
            )
        return raw

    async def _enrolling_user(
        self, session: AsyncSession, invite_token: str, enrolment_token: str
    ) -> tuple[User, OneTimeToken]:
        user = await self._invited_user(session, invite_token, lock=True)
        enrolment = (
            await repo.lock_one_time_token(
                session,
                tokens.token_digest(enrolment_token),
                OneTimePurpose.INVITE_ENROLMENT,
                self._clock.now(),
            )
            if tokens.looks_like_token(enrolment_token)
            else None
        )
        if user is None or enrolment is None or enrolment.user_id != user.id:
            raise _invite_invalid()
        return user, enrolment

    async def invite_setup_factor(
        self, context: RequestContext, invite_token: str, enrolment_token: str
    ) -> FactorSetup:
        """Step 2: an authenticator for the new account."""
        await self._limit_invite(context)
        async with self._db.unit_of_work() as session:
            user, _ = await self._enrolling_user(session, invite_token, enrolment_token)
            return await self._setup_factor(session, user.id, "Authenticator app")

    async def invite_confirm_factor(
        self,
        context: RequestContext,
        invite_token: str,
        enrolment_token: str,
        factor_id: uuid.UUID,
        code: str,
        *,
        device_token: str | None = None,
    ) -> Activation:
        """Step 3: confirm the authenticator. The account becomes active with its recovery
        codes (shown once) and a full session; the invite is consumed."""
        await self._limit_invite(context)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            values = await policy.load(session)
            user, enrolment = await self._enrolling_user(session, invite_token, enrolment_token)
            await self._confirm_factor(session, user.id, factor_id, code, now)
            codes = await self._new_recovery_codes(session, user.id)
            user.status = UserStatus.ACTIVE.value
            user.email_verified_at = now
            user.updated_at = now
            user.version += 1
            enrolment.used_at = now
            await repo.retire_one_time_tokens(session, user.id, OneTimePurpose.INVITE, now)
            issued = await self._open_session(
                session, user, SessionScope.FULL, context, now=now, values=values
            )
            # The activating browser is the account's first device: trusted, not reported.
            issued = await self._recognize_device(
                session,
                context,
                user,
                issued,
                device_token=device_token,
                now=now,
                values=values,
                report_new=False,
            )
            await repo.clear_failures(session, user.id, now)
            for event_type in (SecurityEventType.ACCOUNT_ACTIVATED, SecurityEventType.MFA_ENROLLED):
                await self._audit.record_security_event(
                    session,
                    SecurityEvent(
                        event_type,
                        Severity.INFO,
                        context=context,
                        user_id=user.id,
                        session_id=issued.session_id,
                    ),
                )
            await self._audit.record(
                session,
                AuditEvent(
                    action="identity.user.activated",
                    actor=AuditActor.user(user.id, session_id=issued.session_id),
                    context=context,
                    target_type="user",
                    target_id=user.id,
                    subject_employee_id=user.employee_id,
                ),
            )
        return Activation(issued, codes)

    async def _limit_invite(self, context: RequestContext) -> None:
        if context.ip is not None:
            await self._limiter.enforce(defaults.INVITE_PER_IP, str(context.ip))

    # --- the actor's own account ----------------------------------------------------------

    async def account(self, user_id: uuid.UUID) -> User:
        async with self._db.unit_of_work() as session:
            return _required(await repo.get_user(session, user_id))

    async def login_history(
        self, user_id: uuid.UUID, *, before: tuple[datetime, uuid.UUID] | None, limit: int
    ) -> tuple[list[LoginHistoryRow], tuple[datetime, uuid.UUID] | None]:
        """Own sign-in events of the last 90 days, newest first, keyset-paginated (AUTH-7)."""
        since = self._clock.now() - defaults.LOGIN_HISTORY_PERIOD
        async with self._db.unit_of_work() as session:
            rows = await login_events(session, user_id, since=since, before=before, limit=limit + 1)
        more = len(rows) > limit
        rows = rows[:limit]
        return rows, ((rows[-1].recorded_at, rows[-1].id) if more and rows else None)

    async def list_devices(self, user_id: uuid.UUID) -> Sequence[TrustedDevice]:
        async with self._db.unit_of_work() as session:
            return await repo.active_devices(session, user_id, self._clock.now())

    async def revoke_device(self, actor: SessionActor, context: RequestContext, device_id: uuid.UUID) -> None:
        """Stop trusting one of the actor's devices; its next sign-in is reported as new."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            count = await repo.revoke_devices(
                session,
                user_id=actor.user_id,
                reason=DeviceRevokedReason.REVOKED_BY_USER.value,
                now=now,
                device_id=device_id,
            )
            if count == 0:
                raise ProblemError(ProblemType.NOT_FOUND)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.DEVICE_REVOKED,
                    Severity.INFO,
                    context=context,
                    user_id=actor.user_id,
                    session_id=actor.session_id,
                    details={"device_id": device_id},
                ),
            )

    # --- administration ------------------------------------------------------------------

    async def list_users(self, *, after: uuid.UUID | None, limit: int) -> repo.UserPage:
        async with self._db.unit_of_work() as session:
            return await repo.list_users(session, after=after, limit=limit)

    async def set_disabled(
        self, admin: Actor, context: RequestContext, user_id: uuid.UUID, *, disabled: bool
    ) -> User:
        """Disable an account, revoking every session in the same transaction, or re-enable
        it. Step-up, audited, never one's own account (SOD-4)."""
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, admin, "user.disable")
            refuse_self(admin, user_id, SodRule.OWN_ACCOUNT)
            user = await repo.lock_user(session, user_id)
            if user is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            before = user.status
            revoked: list[uuid.UUID] = []
            if disabled and user.status != UserStatus.DISABLED:
                user.status = UserStatus.DISABLED.value
                revoked = await repo.revoke_sessions(
                    session, user_id=user.id, reason=RevokedReason.ACCOUNT_DISABLED, now=now
                )
                await devices.revoke_all(
                    session, user_id=user.id, reason=DeviceRevokedReason.ACCOUNT_DISABLED, now=now
                )
                # An outstanding invite stops working too.
                for purpose in (OneTimePurpose.INVITE, OneTimePurpose.INVITE_ENROLMENT):
                    await repo.retire_one_time_tokens(session, user.id, purpose, now)
                await notifications.cancel_invites(session, user.id, now)
                event_type = SecurityEventType.ACCOUNT_DISABLED
            elif not disabled and user.status == UserStatus.DISABLED:
                # An account that was never activated goes back to waiting for its invite.
                user.status = (UserStatus.ACTIVE if user.email_verified_at else UserStatus.INVITED).value
                event_type = SecurityEventType.ACCOUNT_ENABLED
            else:
                return user
            user.updated_at = now
            user.version += 1
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    event_type,
                    Severity.WARNING,
                    context=context,
                    user_id=user.id,
                    details={"by_user_id": admin.user_id, "sessions_revoked": len(revoked)},
                ),
            )
            await self._audit.record(
                session,
                AuditEvent(
                    action="identity.user.disabled" if disabled else "identity.user.enabled",
                    actor=AuditActor.user(admin.user_id, session_id=admin.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user.id,
                    subject_employee_id=user.employee_id,
                    changes={"status": ValueChange(before, user.status)},
                ),
            )
            return user

    async def sessions_of(
        self, admin: Actor, context: RequestContext, user_id: uuid.UUID
    ) -> Sequence[AuthSession]:
        """Another user's open sessions. Every read is audited."""
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, admin, "auth.session.read.all")
            if await repo.get_user(session, user_id) is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            sessions = await repo.active_sessions(session, user_id, self._clock.now())
            await self._audit.record(
                session,
                AuditEvent(
                    action="identity.sessions.read",
                    actor=AuditActor.user(admin.user_id, session_id=admin.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user_id,
                ),
            )
            return sessions

    async def revoke_sessions_of(self, admin: Actor, context: RequestContext, user_id: uuid.UUID) -> int:
        """End every session of another user. Step-up, audited."""
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, admin, "auth.session.revoke.all")
            if await repo.lock_user(session, user_id) is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            now = self._clock.now()
            revoked = await repo.revoke_sessions(
                session, user_id=user_id, reason=RevokedReason.REVOKED_BY_ADMIN, now=now
            )
            # A security response: every browser is reported as new at its next sign-in.
            await devices.revoke_all(
                session, user_id=user_id, reason=DeviceRevokedReason.REVOKED_BY_ADMIN, now=now
            )
            if revoked:
                await self._session_revoked_event(
                    session,
                    context=context,
                    user_id=user_id,
                    actor=SessionActor(admin.user_id, admin.session_id, admin.session_scope),
                    revoked=revoked,
                    reason="revoked_by_admin",
                )
            await self._audit.record(
                session,
                AuditEvent(
                    action="identity.sessions.revoked",
                    actor=AuditActor.user(admin.user_id, session_id=admin.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user_id,
                    changes={"sessions_revoked": ValueChange(None, len(revoked))},
                ),
            )
            return len(revoked)


def _required[T](value: T | None) -> T:
    """A row the caller's own session or a foreign key guarantees exists."""
    if value is None:
        raise RuntimeError("an expected identity row is missing")
    return value


def _session_usable(record: AuthSession, user: User, now: datetime) -> bool:
    return (
        record.revoked_at is None
        and record.idle_expires_at > now
        and record.absolute_expires_at > now
        and user.status == UserStatus.ACTIVE
    )


def _secret_aad(user_id: uuid.UUID) -> bytes:
    return SECRET_PURPOSE + str(user_id).encode()


def _invite_invalid() -> ProblemError:
    return ProblemError(
        ProblemType.CONFLICT,
        detail="This invite link has expired or was already used. Ask HR for a new invite.",
        extensions={"code": "invite.invalid"},
    )
