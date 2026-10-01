"""Fixed security values for sign-in, sessions and account recovery.

These are security floors and protocol limits that are not adjustable at run time. Values
the security architecture allows administrators to tune (lockout thresholds, session limits,
invite and reset link lifetimes, the trusted-device lifetime, the per-IP failure limit) are
registry settings in app/platform/settings.py, with these documents' values as defaults.
"""

from datetime import timedelta
from typing import Final

from app.platform.ratelimit import RateLimitRule, RateLimitScope

# §3.5 sessions and tokens.
ACCESS_TOKEN_TTL: Final = timedelta(minutes=15)
# last_activity_at is written at most this often, so a busy session is not a write per request.
ACTIVITY_WRITE_INTERVAL: Final = timedelta(minutes=1)

# docs/api-architecture.md §5: the password step returns a 5-minute, single-use MFA token.
LOGIN_MFA_TOKEN_TTL: Final = timedelta(minutes=5)
LOGIN_MFA_MAX_ATTEMPTS: Final = 5

# §3.1 account lifecycle.
INVITE_ENROLMENT_TTL: Final = timedelta(minutes=30)
# An authenticator set up but not confirmed within this time must be set up again.
FACTOR_CONFIRMATION_TTL: Final = timedelta(minutes=15)

# §3.4 throttling: the delay between attempts never exceeds this; an IP that reaches the
# failure threshold (a setting) is blocked for LOGIN_IP_BLOCK.
MAX_DELAY_SECONDS: Final = 30
LOGIN_IP_FAILURE_WINDOW: Final = timedelta(minutes=5)
LOGIN_IP_BLOCK: Final = RateLimitRule(RateLimitScope.AUTH, "login.ip_block", 1, timedelta(minutes=15))
# docs/api-architecture.md §9.
STEP_UP_PER_SESSION: Final = RateLimitRule(RateLimitScope.AUTH, "step_up.session", 5, timedelta(minutes=5))
PASSWORD_RESET_PER_EMAIL: Final = RateLimitRule(RateLimitScope.AUTH, "reset.email", 3, timedelta(hours=1))
PASSWORD_RESET_PER_IP: Final = RateLimitRule(RateLimitScope.AUTH, "reset.ip", 10, timedelta(hours=1))
# Link tokens are bearer secrets; bound how fast one address can try them.
INVITE_PER_IP: Final = RateLimitRule(RateLimitScope.AUTH, "invite.ip", 20, timedelta(minutes=5))
PASSWORD_RESET_COMPLETE_PER_IP: Final = RateLimitRule(
    RateLimitScope.AUTH, "reset.complete.ip", 20, timedelta(minutes=5)
)

# §3.3 MFA.
MAX_ACTIVE_FACTORS: Final = 5
RECOVERY_CODE_COUNT: Final = 10

# AUTH-7: own login history for the last 90 days.
LOGIN_HISTORY_PERIOD: Final = timedelta(days=90)


def login_ip_failures(limit: int) -> RateLimitRule:
    """The per-IP failed sign-in rule, with the limit from the settings registry."""
    return RateLimitRule(RateLimitScope.AUTH, "login.ip_failures", limit, LOGIN_IP_FAILURE_WINDOW)
