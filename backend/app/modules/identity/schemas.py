"""Request and response models for sign-in, the actor's own account and account administration.

Requests reject unknown fields. Responses never carry a password hash, a token digest or a
stored TOTP secret; the only secrets in a response are the ones shown once by design (a new
authenticator's setup key, new recovery codes, the single-use tokens of the sign-in and
invite steps). Session tokens travel only in HttpOnly cookies.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, EmailStr, Field, StringConstraints, model_validator

# Long enough for any policy-compliant password, short enough that nobody can make the
# server hash megabytes.
type PasswordInput = Annotated[str, StringConstraints(min_length=1, max_length=1024)]
type TokenInput = Annotated[str, StringConstraints(min_length=1, max_length=64)]
type CodeInput = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]

SessionScopeValue = Literal["full", "mfa_enrolment"]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- sign-in ------------------------------------------------------------------------------


class LoginRequest(_Request):
    email: Annotated[EmailStr, StringConstraints(max_length=254)]
    password: PasswordInput


class LoginChallenge(_Response):
    """Proof that the password step passed. Single-use, valid for 5 minutes."""

    mfa_token: str


class LoginMfaRequest(_Request):
    mfa_token: TokenInput
    code: CodeInput | None = None
    recovery_code: CodeInput | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> Self:
        if (self.code is None) == (self.recovery_code is None):
            raise ValueError("send either code or recovery_code")
        return self


class SignedIn(_Response):
    """Session cookies are set. `mfa_enrolment` allows only setting up a new authenticator."""

    session_scope: SessionScopeValue


class StepUpRequest(_Request):
    code: CodeInput


class StepUpResult(_Response):
    step_up_expires_at: datetime


# --- the actor's own account ----------------------------------------------------------------


class Me(_Response):
    user_id: uuid.UUID
    email: str
    employee_id: uuid.UUID | None
    session_id: uuid.UUID
    session_scope: SessionScopeValue
    step_up_expires_at: datetime | None
    roles: list[str]
    permissions: list[str]


class PasswordChange(_Request):
    current_password: PasswordInput
    new_password: PasswordInput


class Factor(_Response):
    id: uuid.UUID
    type: Literal["totp"]
    label: str
    created_at: datetime
    last_used_at: datetime | None


class FactorList(_Response):
    items: list[Factor]


class TotpSetupRequest(_Request):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)] = (
        "Authenticator app"
    )


class TotpSetup(_Response):
    """Shown once. The secret is never returned again."""

    factor_id: uuid.UUID
    secret: str
    otpauth_uri: str
    qr_code: str = Field(description="SVG QR code of otpauth_uri as a data: URI, for an <img> element")


class TotpConfirmRequest(_Request):
    factor_id: uuid.UUID
    code: CodeInput


class TotpConfirmed(_Response):
    session_scope: SessionScopeValue
    # Shown once: new recovery codes when this confirmation finished a re-enrolment after an
    # administrator's MFA reset (the reset revoked the old codes). Null otherwise.
    recovery_codes: list[str] | None = None


class RecoveryCodes(_Response):
    """Shown once. Each code works once; the previous unused codes stop working."""

    codes: list[str]


class SessionView(_Response):
    id: uuid.UUID
    current: bool
    client_type: Literal["web"]
    ip: str | None
    user_agent: str | None
    created_at: datetime
    last_activity_at: datetime


class SessionList(_Response):
    items: list[SessionView]


class RevokedCount(_Response):
    revoked: int


class LoginHistoryItem(_Response):
    occurred_at: datetime
    event_type: Literal["login.succeeded", "login.failed", "account.locked"]
    ip: str | None
    user_agent: str | None


class LoginHistory(_Response):
    items: list[LoginHistoryItem]
    next_cursor: str | None


# --- invitations ----------------------------------------------------------------------------


class InviteStatus(_Response):
    valid: bool
    first_name: str | None


class InvitePasswordRequest(_Request):
    password: PasswordInput


class InviteEnrolment(_Response):
    """Binds steps 2 and 3 to the browser that set the password. Valid for 30 minutes."""

    enrolment_token: str


class InviteSetupRequest(_Request):
    enrolment_token: TokenInput


class InviteConfirmRequest(_Request):
    enrolment_token: TokenInput
    factor_id: uuid.UUID
    code: CodeInput


class InviteActivated(_Response):
    """The account is active and signed in. The recovery codes are shown only here."""

    recovery_codes: list[str]


# --- administration -------------------------------------------------------------------------


class UserView(_Response):
    id: uuid.UUID
    email: str
    status: Literal["invited", "active", "disabled"]
    employee_id: uuid.UUID | None
    locked: bool
    # After an MFA reset, until the person sets up a new authenticator through the emailed link.
    mfa_reenrolment_required: bool
    last_login_at: datetime | None
    created_at: datetime


class UserPage(_Response):
    items: list[UserView]
    next_cursor: uuid.UUID | None


class AdminSessionView(_Response):
    id: uuid.UUID
    client_type: Literal["web"]
    ip: str | None
    user_agent: str | None
    created_at: datetime
    last_activity_at: datetime


class AdminSessionList(_Response):
    items: list[AdminSessionView]


# --- password reset ---------------------------------------------------------------------------


class PasswordResetRequest(_Request):
    email: Annotated[EmailStr, StringConstraints(max_length=254)]


class PasswordResetComplete(_Request):
    password: PasswordInput


# --- MFA reset and re-enrolment ---------------------------------------------------------------


class MfaResetRequest(_Request):
    """Why the reset is needed, recorded in the audit log. Record the identity check that was
    done (docs/runbooks), not personal details."""

    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class MfaReenrolmentRequest(_Request):
    password: PasswordInput


# --- trusted devices --------------------------------------------------------------------------


class DeviceView(_Response):
    """A browser the account has signed in from. Recognizing it never skips MFA or step-up."""

    id: uuid.UUID
    current: bool
    user_agent: str | None
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


class DeviceList(_Response):
    items: list[DeviceView]


# --- invitations (administration) -------------------------------------------------------------


class InviteCreate(_Request):
    email: Annotated[EmailStr, StringConstraints(max_length=254)]
    # Links the account to an employee record: needs HR authority over that employee, and the
    # email must be the employee's work email.
    employee_id: uuid.UUID | None = None
