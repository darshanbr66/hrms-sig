"""Inviting people (docs/security-architecture.md §3.1, docs/api-architecture.md §8 Administration).

`user.invite` creates an invited account and queues its invite email; resending and revoking
an invite need the same permission. All are audited.

- An invite grants no role. Access comes later, through role assignment (with its own
  permission, step-up and separation-of-duties rules) or from the linked employee record.
- Linking the account to an employee record gives the account that employee's self-service
  access, so it needs more than `user.invite`: the inviter must hold
  `employee.lifecycle.manage` covering that employee (HR, not system administrators), and the
  invite must go to the employee's own work email. Without the second rule, someone allowed
  to invite could route another employee's account, and its self-scope access, to a mailbox
  they control.
- The link token is created by the worker when the email is sent (only its hash is stored).
  Resending cancels an invite still waiting to be sent and retires the outstanding link at
  once; revoking retires it and cancels any waiting email. A retired, expired or used link
  fails at every activation step.
- Responses here are for administrators who may list accounts anyway (`user.read.all`), so an
  email already in use is reported (409); public endpoints never reveal it.
"""

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.identity import notifications
from app.modules.identity import public as identity
from app.modules.identity import repository as repo
from app.modules.identity.models import OneTimePurpose, User, UserStatus
from app.modules.people import public as people
from app.platform.audit.records import AuditActor, AuditEvent, RequestContext, ValueChange
from app.platform.audit.writer import AuditWriter
from app.platform.authz.context import Actor
from app.platform.authz.engine import Authorizer
from app.platform.clock import Clock
from app.platform.db import Database
from app.platform.errors import ProblemError, ProblemType
from app.platform.security.emails import canonical_email


def _conflict(code: str, detail: str) -> ProblemError:
    return ProblemError(ProblemType.CONFLICT, detail=detail, extensions={"code": code})


def _field(field: str, code: str, message: str) -> ProblemError:
    return ProblemError(
        ProblemType.VALIDATION_ERROR,
        extensions={"errors": [{"field": field, "code": code, "message": message}]},
    )


class InvitationService:
    def __init__(
        self, *, database: Database, clock: Clock, audit: AuditWriter, authorizer: Authorizer
    ) -> None:
        self._db = database
        self._clock = clock
        self._audit = audit
        self._authz = authorizer

    async def invite(
        self, admin: Actor, context: RequestContext, email: str, employee_id: uuid.UUID | None
    ) -> User:
        address = canonical_email(email)
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, admin, "user.invite")
            if employee_id is not None:
                await self._check_employee_link(session, admin, address, employee_id)
            if await identity.email_in_use(session, address):
                raise _conflict("user.email_in_use", "An account with this email already exists.")
            user_id = await self._create(session, address, employee_id)
            await notifications.invite(session, user_id, now)
            await self._audit.record(
                session,
                AuditEvent(
                    action="identity.user.invited",
                    actor=AuditActor.user(admin.user_id, session_id=admin.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user_id,
                    subject_employee_id=employee_id,
                    changes={"employee_id": ValueChange(None, employee_id)},
                ),
            )
            user = await repo.get_user(session, user_id)
            if user is None:
                raise RuntimeError("an invited account vanished in its own transaction")
            return user

    async def _create(self, session: AsyncSession, address: str, employee_id: uuid.UUID | None) -> uuid.UUID:
        try:
            async with session.begin_nested():
                return await identity.create_invited_account(session, address, employee_id=employee_id)
        except IntegrityError as exc:
            constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
            if constraint == "uq_users_email":
                raise _conflict("user.email_in_use", "An account with this email already exists.") from exc
            if constraint == "uq_users_employee_id":
                raise _conflict("user.employee_has_account", "This employee already has an account.") from exc
            raise

    async def _check_employee_link(
        self, session: AsyncSession, admin: Actor, address: str, employee_id: uuid.UUID
    ) -> None:
        # Linking gives the account the employee's self-service access: HR authority over
        # that employee is required, and existence is not confirmed to anyone without it.
        await self._authz.require(
            session, admin, "employee.lifecycle.manage", subject_employee_id=employee_id, hide_existence=True
        )
        exists, work_email = await people.work_email(session, employee_id)
        if not exists:
            raise ProblemError(ProblemType.NOT_FOUND)
        if work_email is None:
            raise _field(
                "employee_id",
                "user.employee_without_work_email",
                "This employee has no work email. Add it to their record first.",
            )
        if canonical_email(work_email) != address:
            raise _field(
                "email",
                "user.email_not_work_email",
                "An account linked to an employee must use that employee's work email.",
            )

    async def resend(self, admin: Actor, context: RequestContext, user_id: uuid.UUID) -> None:
        await self._change(admin, context, user_id, action="identity.user.invite_resent", resend=True)

    async def revoke(self, admin: Actor, context: RequestContext, user_id: uuid.UUID) -> None:
        await self._change(admin, context, user_id, action="identity.user.invite_revoked", resend=False)

    async def _change(
        self, admin: Actor, context: RequestContext, user_id: uuid.UUID, *, action: str, resend: bool
    ) -> None:
        now = self._clock.now()
        async with self._db.unit_of_work() as session:
            granted = await self._authz.require(session, admin, "user.invite")
            user = await repo.lock_user(session, user_id)
            if user is None:
                raise ProblemError(ProblemType.NOT_FOUND)
            if user.status != UserStatus.INVITED:
                raise _conflict("user.not_invited", "This account is not waiting for an invite.")
            # The outstanding link and any step already taken with it stop working now.
            for purpose in (OneTimePurpose.INVITE, OneTimePurpose.INVITE_ENROLMENT):
                await repo.retire_one_time_tokens(session, user.id, purpose, now)
            if resend:
                await notifications.invite(session, user.id, now)
            else:
                await notifications.cancel_invites(session, user.id, now)
            await self._audit.record(
                session,
                AuditEvent(
                    action=action,
                    actor=AuditActor.user(admin.user_id, session_id=admin.session_id),
                    context=context,
                    permission_used=granted.key,
                    target_type="user",
                    target_id=user.id,
                    subject_employee_id=user.employee_id,
                ),
            )
