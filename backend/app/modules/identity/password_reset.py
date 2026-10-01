"""Password reset (AUTH-5, docs/security-architecture.md §3.3, threat model T16).

Request (`POST /auth/password-reset`): always answers 202, whether or not the email belongs to
an account, and writes a security event either way (with the keyed hash of the email when
there is no account), so the work done is similar in both cases. Limited to 3 requests per
hour per email and 10 per hour per IP address (failing closed when the rate-limit store is
down). For an active account, a reset email is queued in the outbox; the worker creates the
link token when it sends the email, so the token exists only in the email and as a hash.
A newer request cancels a reset email still waiting to be sent, and each sent link retires
the earlier ones.

Completion (`POST /auth/password-reset/{token}`): the new password must meet the policy and
not be breached (checked before any lock is taken; hashing happens outside the transaction).
Then, in one transaction with the token and the account locked: the token is consumed,
the password stored, every session and pending sign-in challenge ended, other reset links
retired, every trusted device revoked, and a security event and a "password changed" email
recorded. MFA is untouched and the user is not signed in: the next sign-in still needs the
second factor (§3.3). A used, expired or replaced link fails with 409 `password_reset.invalid`.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import defaults, devices, notifications
from app.modules.identity import repository as repo
from app.modules.identity.models import DeviceRevokedReason, OneTimePurpose, RevokedReason, UserStatus
from app.platform.audit.records import RequestContext, SecurityEvent, SecurityEventType, Severity
from app.platform.audit.writer import AuditWriter
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.ratelimit import RateLimiter
from app.platform.security import passwords, tokens
from app.platform.security.emails import EmailHasher, canonical_email


def invalid_link() -> ProblemError:
    return ProblemError(
        ProblemType.CONFLICT,
        detail="This password reset link has expired or was already used. Ask for a new one.",
        extensions={"code": "password_reset.invalid"},
    )


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


class PasswordResetService:
    def __init__(
        self,
        *,
        database: Database,
        clock: Clock,
        breach_checker: passwords.BreachedPasswordChecker,
        rate_limiter: RateLimiter,
        audit: AuditWriter,
        email_hasher: EmailHasher,
    ) -> None:
        self._db = database
        self._clock = clock
        self._breach = breach_checker
        self._limiter = rate_limiter
        self._audit = audit
        self._email_hasher = email_hasher

    async def request(self, context: RequestContext, email: str) -> None:
        """Queue a reset email if the address belongs to an active account. Same answer either way."""
        canonical = canonical_email(email)
        if context.ip is not None:
            await self._limiter.enforce(defaults.PASSWORD_RESET_PER_IP, str(context.ip))
        await self._limiter.enforce(defaults.PASSWORD_RESET_PER_EMAIL, canonical)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            user = await repo.user_by_email(session, canonical)
            known = user is not None and user.status == UserStatus.ACTIVE
            if user is not None and known:
                await notifications.password_reset(session, user.id, now)
            await self._audit.record_security_event(
                session,
                SecurityEvent(
                    SecurityEventType.PASSWORD_RESET_REQUESTED,
                    Severity.INFO,
                    context=context,
                    user_id=user.id if user is not None and known else None,
                    email_attempted_hash=None if known else self._email_hasher.digest(canonical),
                    details={"known_user": known},
                ),
            )

    async def complete(self, context: RequestContext, token: str, new_password: str) -> None:
        if context.ip is not None:
            await self._limiter.enforce(defaults.PASSWORD_RESET_COMPLETE_PER_IP, str(context.ip))
        if not tokens.looks_like_token(token):
            raise invalid_link()
        digest = tokens.token_digest(token)
        async with self._db.unit_of_work() as session:
            usable = await repo.find_one_time_token(
                session, digest, OneTimePurpose.PASSWORD_RESET, self._clock.now()
            )
        if usable is None:
            raise invalid_link()
        problem = await self._breach.problem(new_password)
        if problem is not None:
            raise _field("password", problem.value, passwords.PROBLEM_MESSAGES[problem])
        password_hash = await passwords.hash_password(new_password)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            await self._apply(session, context, digest, password_hash, now)

    async def _apply(
        self, session: AsyncSession, context: RequestContext, digest: bytes, password_hash: str, now: datetime
    ) -> None:
        # Re-checked under the lock: a concurrent completion, a newer link or expiry since the
        # first check makes this one fail.
        reset = await repo.lock_one_time_token(session, digest, OneTimePurpose.PASSWORD_RESET, now)
        if reset is None:
            raise invalid_link()
        user = await repo.lock_user(session, reset.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise invalid_link()
        reset.used_at = now
        await repo.store_password(session, user.id, password_hash, now)
        for purpose in (OneTimePurpose.PASSWORD_RESET, OneTimePurpose.LOGIN_MFA):
            await repo.retire_one_time_tokens(session, user.id, purpose, now)
        revoked = await repo.revoke_sessions(
            session, user_id=user.id, reason=RevokedReason.PASSWORD_RESET, now=now
        )
        devices_revoked = await devices.revoke_all(
            session, user_id=user.id, reason=DeviceRevokedReason.PASSWORD_RESET, now=now
        )
        await self._audit.record_security_event(
            session,
            SecurityEvent(
                SecurityEventType.PASSWORD_RESET_COMPLETED,
                Severity.WARNING,
                context=context,
                user_id=user.id,
                details={"sessions_revoked": len(revoked), "devices_revoked": devices_revoked},
            ),
        )
        await notifications.password_changed(session, user.id, now, by_reset=True)
