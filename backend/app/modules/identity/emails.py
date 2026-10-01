"""The identity emails, built by the worker at send time (notify.public.Renderer).

Each renderer reads the account, decides whether the email still applies, and returns the
message, or None to cancel it (the invite was accepted, the account was disabled). The invite
and reset renderers create their link token here, in the dispatcher's transaction: only its
SHA-256 digest is stored, earlier links of the same kind are retired, and the lifetime is the
current setting. Links are built from APP_BASE_URL, never from a request (threat model T16).

Emails carry no secret except the one link they exist to deliver, and no personal data beyond
the recipient's own first name.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import repository as repo
from app.modules.identity.models import OneTimePurpose, OneTimeToken, User, UserStatus
from app.modules.notify.public import EmailTemplate, OutboxItem, Renderer
from app.modules.people import public as people
from app.platform import settings
from app.platform.clock import Clock
from app.platform.email import OutgoingEmail
from app.platform.security import tokens

PRODUCT = "Sigvitas HRMS"
SIGN_OFF = f"\n\n{PRODUCT}\nThis is an automatic message. Replies are not read."
NOT_YOU = "If this wasn't you, contact your HR team straight away."

_MFA_CHANGES = {
    "authenticator_added": "An authenticator app was added to your account.",
    "authenticator_removed": "An authenticator app was removed from your account.",
    "recovery_codes_replaced": "New recovery codes were made for your account. The old codes no longer work.",
}


class IdentityEmails:
    def __init__(self, *, app_base_url: str, clock: Clock) -> None:
        self._base = app_base_url
        self._clock = clock

    def renderers(self) -> Mapping[EmailTemplate, Renderer]:
        return {
            EmailTemplate.INVITE: self.invite,
            EmailTemplate.PASSWORD_RESET: self.password_reset,
            EmailTemplate.PASSWORD_CHANGED: self.password_changed,
            EmailTemplate.NEW_DEVICE: self.new_device,
            EmailTemplate.ACCOUNT_LOCKED: self.account_locked,
            EmailTemplate.RECOVERY_CODE_USED: self.recovery_code_used,
            EmailTemplate.MFA_CHANGED: self.mfa_changed,
        }

    async def _link_token(
        self, session: AsyncSession, user: User, purpose: OneTimePurpose, ttl: timedelta, now: datetime
    ) -> str:
        raw = tokens.new_token()
        await repo.retire_one_time_tokens(session, user.id, purpose, now)
        session.add(
            OneTimeToken(
                user_id=user.id,
                purpose=purpose.value,
                token_hash=tokens.token_digest(raw),
                expires_at=now + ttl,
            )
        )
        await session.flush()
        return raw

    async def invite(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await repo.lock_user(session, item.user_id)
        if user is None or user.status != UserStatus.INVITED:
            return None
        hours = (await settings.load(session))[settings.INVITE_TTL]
        now = self._clock.now()
        token = await self._link_token(session, user, OneTimePurpose.INVITE, timedelta(hours=hours), now)
        name = await people.first_name(session, user.employee_id) if user.employee_id else None
        greeting = f"Hello {name}," if name else "Hello,"
        return OutgoingEmail(
            to=user.email,
            subject=f"Set up your {PRODUCT} account",
            body=(
                f"{greeting}\n\nAn account has been created for you in {PRODUCT}. Use this link to set "
                f"your password and set up an authenticator app:\n\n{self._base}/invite/{token}\n\n"
                f"The link works once and stops working after {hours} hours. If you weren't expecting "
                f"this email, you can ignore it.{SIGN_OFF}"
            ),
        )

    async def password_reset(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await repo.lock_user(session, item.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            return None
        minutes = (await settings.load(session))[settings.PASSWORD_RESET_TTL]
        now = self._clock.now()
        token = await self._link_token(
            session, user, OneTimePurpose.PASSWORD_RESET, timedelta(minutes=minutes), now
        )
        return OutgoingEmail(
            to=user.email,
            subject=f"Reset your {PRODUCT} password",
            body=(
                f"Hello,\n\nSomeone asked to reset the password of your {PRODUCT} account. Use this "
                f"link to choose a new password:\n\n{self._base}/password-reset/{token}\n\n"
                f"The link works once and stops working after {minutes} minutes. Resetting your "
                f"password does not change your authenticator app: you will still need it to sign "
                f"in.\n\nIf you didn't ask for this, you can ignore this email; your password stays "
                f"the same.{SIGN_OFF}"
            ),
        )

    async def _account(self, session: AsyncSession, item: OutboxItem) -> User | None:
        user = await repo.get_user(session, item.user_id)
        return user if user is not None and user.status == UserStatus.ACTIVE else None

    async def password_changed(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await self._account(session, item)
        if user is None:
            return None
        how = "reset with a reset link" if item.data.get("by_reset") else "changed from your account settings"
        return OutgoingEmail(
            to=user.email,
            subject=f"Your {PRODUCT} password was changed",
            body=(
                f"Hello,\n\nThe password of your {PRODUCT} account was {how}. You have been signed "
                f"out on other devices.\n\n{NOT_YOU}{SIGN_OFF}"
            ),
        )

    async def new_device(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await self._account(session, item)
        if user is None:
            return None
        browser = str(item.data.get("browser") or "an unknown browser")
        when = str(item.data.get("signed_in_at") or "")
        return OutgoingEmail(
            to=user.email,
            subject=f"New sign-in to your {PRODUCT} account",
            body=(
                f"Hello,\n\nYour {PRODUCT} account was signed in to from a browser it had not seen "
                f"before:\n\n{browser}\n{when} (UTC)\n\nIf this was you, there is nothing to do. If not, "
                f"change your password and sign out of that session from your account security page. "
                f"{NOT_YOU}{SIGN_OFF}"
            ),
        )

    async def account_locked(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await self._account(session, item)
        if user is None:
            return None
        minutes = item.data.get("locked_minutes")
        return OutgoingEmail(
            to=user.email,
            subject=f"Your {PRODUCT} account is temporarily locked",
            body=(
                f"Hello,\n\nAfter several failed sign-in attempts, your {PRODUCT} account is locked "
                f"for {minutes} minutes. It unlocks by itself; you don't need to do anything.\n\n"
                f"If these attempts weren't yours, someone may know your password: change it once "
                f"you can sign in, and contact your HR team.{SIGN_OFF}"
            ),
        )

    async def recovery_code_used(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await self._account(session, item)
        if user is None:
            return None
        remaining = item.data.get("remaining_codes")
        return OutgoingEmail(
            to=user.email,
            subject=f"A recovery code was used on your {PRODUCT} account",
            body=(
                f"Hello,\n\nA recovery code was used to sign in to your {PRODUCT} account. "
                f"{remaining} unused recovery codes are left. You will be asked to set up a new "
                f"authenticator app.\n\n{NOT_YOU}{SIGN_OFF}"
            ),
        )

    async def mfa_changed(self, session: AsyncSession, item: OutboxItem) -> OutgoingEmail | None:
        user = await self._account(session, item)
        if user is None:
            return None
        change = _MFA_CHANGES.get(str(item.data.get("change")), "Your sign-in settings were changed.")
        return OutgoingEmail(
            to=user.email,
            subject=f"Your {PRODUCT} sign-in settings were changed",
            body=(
                f"Hello,\n\n{change} Other browsers will be reported as new at their next sign-in."
                f"\n\n{NOT_YOU}{SIGN_OFF}"
            ),
        )
