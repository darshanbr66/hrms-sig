"""HTTP routes for sign-in, the actor's own account, invites and account administration.

docs/api-architecture.md §5 and §8. Session tokens are set only as HttpOnly cookies: the
access token as `__Host-sv_at` (SameSite=Lax, path /) and the refresh token as
`__Secure-sv_rt` (SameSite=Strict, sent only to the refresh endpoint).
"""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.modules.identity import public
from app.modules.identity import schemas as s
from app.modules.identity.models import AuthSession, MfaFactor, User
from app.modules.identity.service import IdentityService, IssuedSession, SessionActor
from app.platform.audit.records import RequestContext
from app.platform.authz.context import Actor
from app.platform.authz.engine import STEP_UP_WINDOW, Authorizer
from app.platform.authz.route import account, authorizer, public_route, requires
from app.platform.clock import Clock
from app.platform.errors import ProblemError, ProblemType
from app.platform.pagination import decode_cursor, encode_cursor

router = APIRouter(prefix="/api/v1", tags=["identity"])


def service(request: Request) -> IdentityService:
    value: IdentityService = request.app.state.identity_service
    return value


def clock(request: Request) -> Clock:
    value: Clock = request.app.state.clock
    return value


Service = Annotated[IdentityService, Depends(service)]
Engine = Annotated[Authorizer, Depends(authorizer)]


def _session_actor(actor: Actor) -> SessionActor:
    return SessionActor(actor.user_id, actor.session_id, actor.session_scope)


def _set_session_cookies(response: Response, issued: IssuedSession, now: datetime) -> None:
    response.set_cookie(
        public.ACCESS_COOKIE,
        issued.access_token,
        max_age=max(int((issued.access_expires_at - now).total_seconds()), 0),
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    response.set_cookie(
        public.REFRESH_COOKIE,
        issued.refresh_token,
        max_age=max(int((issued.refresh_expires_at - now).total_seconds()), 0),
        path=public.REFRESH_COOKIE_PATH,
        secure=True,
        httponly=True,
        samesite="strict",
    )


def _clear_session_cookies(response: Response) -> None:
    response.delete_cookie(public.ACCESS_COOKIE, path="/", secure=True, httponly=True, samesite="lax")
    response.delete_cookie(
        public.REFRESH_COOKIE, path=public.REFRESH_COOKIE_PATH, secure=True, httponly=True, samesite="strict"
    )


# --- sign-in ------------------------------------------------------------------------------


@router.post("/auth/login", response_model=s.LoginChallenge, dependencies=[public_route()])
async def login(body: s.LoginRequest, request: Request, identity: Service) -> s.LoginChallenge:
    token = await identity.login(RequestContext.from_request(request), body.email, body.password)
    return s.LoginChallenge(mfa_token=token)


@router.post("/auth/login/mfa", response_model=s.SignedIn, dependencies=[public_route()])
async def login_mfa(
    body: s.LoginMfaRequest, request: Request, response: Response, identity: Service
) -> s.SignedIn:
    issued = await identity.complete_login(
        RequestContext.from_request(request), body.mfa_token, code=body.code, recovery_code=body.recovery_code
    )
    _set_session_cookies(response, issued, clock(request).now())
    return s.SignedIn(session_scope=issued.scope.value)


@router.post("/auth/refresh", response_model=s.SignedIn, dependencies=[public_route()])
async def refresh(request: Request, response: Response, identity: Service) -> s.SignedIn:
    token = request.cookies.get(public.REFRESH_COOKIE)
    if not token:
        raise ProblemError(ProblemType.UNAUTHENTICATED)
    issued = await identity.refresh(RequestContext.from_request(request), token)
    _set_session_cookies(response, issued, clock(request).now())
    return s.SignedIn(session_scope=issued.scope.value)


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    identity: Service,
    actor: Annotated[Actor, account(allow_enrolment=True)],
) -> None:
    await identity.logout(_session_actor(actor), RequestContext.from_request(request))
    _clear_session_cookies(response)


@router.post("/auth/step-up", response_model=s.StepUpResult)
async def step_up(
    body: s.StepUpRequest,
    request: Request,
    response: Response,
    identity: Service,
    actor: Annotated[Actor, account()],
) -> s.StepUpResult:
    now = clock(request).now()
    issued = await identity.step_up(_session_actor(actor), RequestContext.from_request(request), body.code)
    _set_session_cookies(response, issued, now)
    return s.StepUpResult(step_up_expires_at=now + STEP_UP_WINDOW)


# --- the actor's own account --------------------------------------------------------------


@router.get("/me", response_model=s.Me)
async def me(identity: Service, actor: Annotated[Actor, account(allow_enrolment=True)]) -> s.Me:
    user = await identity.account(actor.user_id)
    return s.Me(
        user_id=user.id,
        email=user.email,
        employee_id=user.employee_id,
        session_id=actor.session_id,
        session_scope=actor.session_scope.value,
        step_up_expires_at=actor.step_up_at + STEP_UP_WINDOW if actor.step_up_at else None,
        roles=sorted(actor.role_keys),
        permissions=sorted(actor.permission_keys),
    )


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    body: s.PasswordChange,
    request: Request,
    identity: Service,
    engine: Engine,
    actor: Annotated[Actor, account()],
) -> None:
    engine.require_step_up(actor)
    await identity.change_password(
        _session_actor(actor), RequestContext.from_request(request), body.current_password, body.new_password
    )


def _factor_view(factor: MfaFactor) -> s.Factor:
    return s.Factor(
        id=factor.id,
        type="totp",
        label=factor.label,
        created_at=factor.created_at,
        last_used_at=factor.last_used_at,
    )


@router.get("/me/mfa/factors", response_model=s.FactorList)
async def list_factors(
    identity: Service, actor: Annotated[Actor, account(allow_enrolment=True)]
) -> s.FactorList:
    return s.FactorList(items=[_factor_view(f) for f in await identity.list_factors(actor.user_id)])


@router.post("/me/mfa/totp/setup", response_model=s.TotpSetup)
async def setup_totp(
    body: s.TotpSetupRequest,
    identity: Service,
    engine: Engine,
    actor: Annotated[Actor, account(allow_enrolment=True)],
) -> s.TotpSetup:
    # The recovery-code sign-in of an enrolment-only session is its verification;
    # a full session must step up (docs/security-architecture.md §3.3, §3.6).
    if not actor.enrolment_only:
        engine.require_step_up(actor)
    setup = await identity.setup_factor(actor.user_id, body.label)
    return s.TotpSetup(
        factor_id=setup.factor_id, secret=setup.secret, otpauth_uri=setup.otpauth_uri, qr_code=setup.qr_code
    )


@router.post("/me/mfa/totp/confirm", response_model=s.TotpConfirmed)
async def confirm_totp(
    *,
    body: s.TotpConfirmRequest,
    request: Request,
    response: Response,
    identity: Service,
    engine: Engine,
    actor: Annotated[Actor, account(allow_enrolment=True)],
) -> s.TotpConfirmed:
    if not actor.enrolment_only:
        engine.require_step_up(actor)
    issued = await identity.confirm_factor(
        _session_actor(actor), RequestContext.from_request(request), body.factor_id, body.code
    )
    if issued is not None:
        _set_session_cookies(response, issued, clock(request).now())
        return s.TotpConfirmed(session_scope=issued.scope.value)
    return s.TotpConfirmed(session_scope=actor.session_scope.value)


@router.delete("/me/mfa/factors/{factor_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_factor(
    factor_id: uuid.UUID,
    request: Request,
    identity: Service,
    engine: Engine,
    actor: Annotated[Actor, account()],
) -> None:
    engine.require_step_up(actor)
    await identity.remove_factor(_session_actor(actor), RequestContext.from_request(request), factor_id)


@router.post("/me/mfa/recovery-codes", response_model=s.RecoveryCodes)
async def regenerate_recovery_codes(
    request: Request, identity: Service, engine: Engine, actor: Annotated[Actor, account()]
) -> s.RecoveryCodes:
    engine.require_step_up(actor)
    codes = await identity.regenerate_recovery_codes(
        _session_actor(actor), RequestContext.from_request(request)
    )
    return s.RecoveryCodes(codes=codes)


def _session_view(record: AuthSession, current_session_id: uuid.UUID) -> s.SessionView:
    return s.SessionView(
        id=record.id,
        current=record.id == current_session_id,
        client_type="web",
        ip=str(record.ip) if record.ip else None,
        user_agent=record.user_agent,
        created_at=record.created_at,
        last_activity_at=record.last_activity_at,
    )


@router.get("/me/sessions", response_model=s.SessionList)
async def my_sessions(
    identity: Service, actor: Annotated[Actor, requires("auth.session.read.self")]
) -> s.SessionList:
    sessions = await identity.list_sessions(actor.user_id)
    return s.SessionList(items=[_session_view(record, actor.session_id) for record in sessions])


@router.delete("/me/sessions", response_model=s.RevokedCount)
async def revoke_my_other_sessions(
    request: Request, identity: Service, actor: Annotated[Actor, requires("auth.session.revoke.self")]
) -> s.RevokedCount:
    """Sign out everywhere else: every session except this one."""
    count = await identity.revoke_other_sessions(_session_actor(actor), RequestContext.from_request(request))
    return s.RevokedCount(revoked=count)


@router.delete("/me/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_my_session(
    session_id: uuid.UUID,
    request: Request,
    response: Response,
    identity: Service,
    actor: Annotated[Actor, requires("auth.session.revoke.self")],
) -> None:
    await identity.revoke_own_session(_session_actor(actor), RequestContext.from_request(request), session_id)
    if session_id == actor.session_id:
        _clear_session_cookies(response)


@router.get("/me/login-history", response_model=s.LoginHistory)
async def my_login_history(
    identity: Service,
    actor: Annotated[Actor, requires("auth.session.read.self")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(max_length=200)] = None,
) -> s.LoginHistory:
    rows, next_position = await identity.login_history(
        actor.user_id, before=decode_cursor(cursor), limit=limit
    )
    return s.LoginHistory(
        items=[
            s.LoginHistoryItem(
                occurred_at=row.occurred_at,
                event_type=row.event_type,
                ip=row.ip,
                user_agent=row.user_agent,
            )
            for row in rows
        ],
        next_cursor=encode_cursor(next_position),
    )


# --- invitations --------------------------------------------------------------------------


@router.get("/auth/invite/{token}", response_model=s.InviteStatus, dependencies=[public_route()])
async def check_invite(token: str, request: Request, identity: Service) -> s.InviteStatus:
    user = await identity.check_invite(RequestContext.from_request(request), token)
    if user is None:
        return s.InviteStatus(valid=False, first_name=None)
    first_name = await identity.first_name_of(user) if user.employee_id else None
    return s.InviteStatus(valid=True, first_name=first_name)


@router.post("/auth/invite/{token}/password", response_model=s.InviteEnrolment, dependencies=[public_route()])
async def invite_password(
    token: str, body: s.InvitePasswordRequest, request: Request, identity: Service
) -> s.InviteEnrolment:
    enrolment = await identity.accept_invite_password(
        RequestContext.from_request(request), token, body.password
    )
    return s.InviteEnrolment(enrolment_token=enrolment)


@router.post("/auth/invite/{token}/mfa/totp/setup", response_model=s.TotpSetup, dependencies=[public_route()])
async def invite_setup_totp(
    token: str, body: s.InviteSetupRequest, request: Request, identity: Service
) -> s.TotpSetup:
    setup = await identity.invite_setup_factor(
        RequestContext.from_request(request), token, body.enrolment_token
    )
    return s.TotpSetup(
        factor_id=setup.factor_id, secret=setup.secret, otpauth_uri=setup.otpauth_uri, qr_code=setup.qr_code
    )


@router.post(
    "/auth/invite/{token}/mfa/totp/confirm", response_model=s.InviteActivated, dependencies=[public_route()]
)
async def invite_confirm_totp(
    token: str, body: s.InviteConfirmRequest, request: Request, response: Response, identity: Service
) -> s.InviteActivated:
    activation = await identity.invite_confirm_factor(
        RequestContext.from_request(request), token, body.enrolment_token, body.factor_id, body.code
    )
    _set_session_cookies(response, activation.session, clock(request).now())
    return s.InviteActivated(recovery_codes=list(activation.recovery_codes))


# --- administration -----------------------------------------------------------------------


def _user_view(user: User, now: datetime) -> s.UserView:
    return s.UserView(
        id=user.id,
        email=user.email,
        status=user.status,
        employee_id=user.employee_id,
        locked=user.locked_until is not None and user.locked_until > now,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
    )


@router.get("/users", response_model=s.UserPage)
async def list_users(
    request: Request,
    identity: Service,
    _actor: Annotated[Actor, requires("user.read.all")],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
) -> s.UserPage:
    page = await identity.list_users(after=cursor, limit=limit)
    now = clock(request).now()
    return s.UserPage(items=[_user_view(user, now) for user in page.users], next_cursor=page.next_cursor)


@router.post("/users/{user_id}/disable", response_model=s.UserView)
async def disable_user(
    user_id: uuid.UUID, request: Request, identity: Service, actor: Annotated[Actor, requires("user.disable")]
) -> s.UserView:
    user = await identity.set_disabled(actor, RequestContext.from_request(request), user_id, disabled=True)
    return _user_view(user, clock(request).now())


@router.post("/users/{user_id}/enable", response_model=s.UserView)
async def enable_user(
    user_id: uuid.UUID, request: Request, identity: Service, actor: Annotated[Actor, requires("user.disable")]
) -> s.UserView:
    user = await identity.set_disabled(actor, RequestContext.from_request(request), user_id, disabled=False)
    return _user_view(user, clock(request).now())


@router.get("/users/{user_id}/sessions", response_model=s.AdminSessionList)
async def user_sessions(
    user_id: uuid.UUID,
    request: Request,
    identity: Service,
    actor: Annotated[Actor, requires("auth.session.read.all")],
) -> s.AdminSessionList:
    sessions = await identity.sessions_of(actor, RequestContext.from_request(request), user_id)
    return s.AdminSessionList(
        items=[
            s.AdminSessionView(
                id=record.id,
                client_type="web",
                ip=str(record.ip) if record.ip else None,
                user_agent=record.user_agent,
                created_at=record.created_at,
                last_activity_at=record.last_activity_at,
            )
            for record in sessions
        ]
    )


@router.delete("/users/{user_id}/sessions", response_model=s.RevokedCount)
async def revoke_user_sessions(
    user_id: uuid.UUID,
    request: Request,
    identity: Service,
    actor: Annotated[Actor, requires("auth.session.revoke.all")],
) -> s.RevokedCount:
    count = await identity.revoke_sessions_of(actor, RequestContext.from_request(request), user_id)
    return s.RevokedCount(revoked=count)
