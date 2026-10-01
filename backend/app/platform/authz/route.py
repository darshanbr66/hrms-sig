"""Route access declarations (docs/api-architecture.md §6).

Every route declares exactly one of:

- `requires(permission)`: a full session holding the permission at some scope. This is the
  fast fail; the service makes the resource-level decision (scope, SoD, state, step-up).
- `account(...)`: any full session, for the actor's own account (`/me`, sign-out, step-up).
  Allowed only on the paths in ACCOUNT_ROUTES. With `allow_enrolment=True` an enrolment-only
  session is accepted too (the MFA enrolment endpoints).
- `public_route()`: no session. Allowed only on the paths in PUBLIC_ROUTES.

The route-coverage test fails for a route with none of these, or with a public or account
declaration on a path outside its allow-list. Each dependency returns the Actor, so handlers
take it as a parameter.
"""

from collections.abc import Awaitable, Callable
from typing import Any, Final, Protocol

from fastapi import Depends, Request

from app.platform.audit.records import RequestContext, SecurityEvent, SecurityEventType, Severity
from app.platform.audit.writer import AuditWriter
from app.platform.authz.catalog import PERMISSIONS
from app.platform.authz.context import Actor
from app.platform.authz.engine import AccessDenied, Authorizer, MfaEnrolmentRequired
from app.platform.db import Database

ACCESS_ATTRIBUTE: Final = "__hrms_route_access__"

# docs/security-architecture.md §12.
PUBLIC_ROUTES: Final = frozenset(
    {
        "/api/v1/auth/login",
        "/api/v1/auth/login/mfa",
        "/api/v1/auth/refresh",
        "/api/v1/auth/invite/{token}",
        "/api/v1/auth/invite/{token}/password",
        "/api/v1/auth/invite/{token}/mfa/totp/setup",
        "/api/v1/auth/invite/{token}/mfa/totp/confirm",
        "/api/v1/auth/password-reset",
        "/api/v1/auth/password-reset/{token}",
        "/api/health/live",
        "/api/health/ready",
    }
)

# The actor's own account. Self-service never takes a user or employee ID (CLAUDE.md rule 3).
ACCOUNT_ROUTES: Final = frozenset(
    {
        "/api/v1/auth/logout",
        "/api/v1/auth/step-up",
        "/api/v1/me",
        "/api/v1/me/password",
        "/api/v1/me/mfa/factors",
        "/api/v1/me/mfa/factors/{factor_id}",
        "/api/v1/me/mfa/totp/setup",
        "/api/v1/me/mfa/totp/confirm",
        "/api/v1/me/mfa/recovery-codes",
    }
)


class ActorProvider(Protocol):
    async def actor_for(self, request: Request) -> Actor:
        """The actor of a request with a valid session; raises a 401 problem otherwise."""
        ...


def authorizer(request: Request) -> Authorizer:
    value: Authorizer = request.app.state.authorizer
    return value


async def current_actor(request: Request) -> Actor:
    cached = getattr(request.state, "actor", None)
    if isinstance(cached, Actor):
        return cached
    provider: ActorProvider = request.app.state.actor_provider
    actor = await provider.actor_for(request)
    request.state.actor = actor
    return actor


def _declare(dependency: Callable[..., Awaitable[Any]], access: tuple[str, ...]) -> Any:
    setattr(dependency, ACCESS_ATTRIBUTE, access)
    return Depends(dependency)


def requires(permission: str) -> Any:
    Authorizer.candidate_keys(permission)  # refuses an unknown permission at import time

    async def dependency(request: Request) -> Actor:
        actor = await current_actor(request)
        if actor.enrolment_only:
            raise MfaEnrolmentRequired
        engine = authorizer(request)
        if not engine.holds_any(actor, permission):
            await _record_denial(request, actor, permission)
            raise AccessDenied
        return actor

    return _declare(dependency, ("permission", permission))


def account(*, allow_enrolment: bool = False) -> Any:
    async def dependency(request: Request) -> Actor:
        actor = await current_actor(request)
        if actor.enrolment_only and not allow_enrolment:
            raise MfaEnrolmentRequired
        return actor

    return _declare(dependency, ("account", "enrolment" if allow_enrolment else "full"))


def public_route() -> Any:
    async def dependency() -> None:
        return None

    return _declare(dependency, ("public",))


async def _record_denial(request: Request, actor: Actor, permission: str) -> None:
    """A denied attempt at a permission whose uses are audited leaves a trace that survives
    the request failing (docs/security-architecture.md §8, guarantee 2). It runs before the
    handler opens its own transaction, so it never needs a second pooled connection."""
    if not any(PERMISSIONS[key].audit_reads for key in Authorizer.candidate_keys(permission)):
        return
    database: Database = request.app.state.database
    writer: AuditWriter = request.app.state.audit_writer
    event = SecurityEvent(
        event_type=SecurityEventType.ACCESS_DENIED_SENSITIVE,
        severity=Severity.WARNING,
        context=RequestContext.from_request(request),
        user_id=actor.user_id,
        session_id=actor.session_id,
        details={"permission": permission},
    )
    await writer.record_separately(database, event)
