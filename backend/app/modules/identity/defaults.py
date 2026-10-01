"""Security settings for sign-in and sessions, at the values in docs/security-architecture.md §3.

These are technical security defaults, not HR policy. The settings registry
(`security.settings.manage`) makes the adjustable ones configurable when it arrives; until
then they are fixed here, in one place.
"""

from datetime import timedelta
from typing import Final

from app.platform.ratelimit import RateLimitRule, RateLimitScope

# §3.5 sessions and tokens.
ACCESS_TOKEN_TTL: Final = timedelta(minutes=15)
SESSION_IDLE_TIMEOUT: Final = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT: Final = timedelta(hours=12)
# last_activity_at is written at most this often, so a busy session is not a write per request.
ACTIVITY_WRITE_INTERVAL: Final = timedelta(minutes=1)

# docs/api-architecture.md §5: the password step returns a 5-minute, single-use MFA token.
LOGIN_MFA_TOKEN_TTL: Final = timedelta(minutes=5)
LOGIN_MFA_MAX_ATTEMPTS: Final = 5

# §3.1 account lifecycle.
INVITE_TTL: Final = timedelta(hours=72)
INVITE_ENROLMENT_TTL: Final = timedelta(minutes=30)
# An authenticator set up but not confirmed within this time must be set up again.
FACTOR_CONFIRMATION_TTL: Final = timedelta(minutes=15)

# §3.4 throttling and lockout (failed password and failed MFA attempts both count).
FAILURE_WINDOW: Final = timedelta(minutes=15)
DELAY_AFTER_FAILURES: Final = 5
MAX_DELAY_SECONDS: Final = 30
LOCK_AFTER_FAILURES: Final = 10
LOCK_DURATION: Final = timedelta(minutes=15)

LOGIN_IP_FAILURES: Final = RateLimitRule(RateLimitScope.AUTH, "login.ip_failures", 20, timedelta(minutes=5))
LOGIN_IP_BLOCK: Final = RateLimitRule(RateLimitScope.AUTH, "login.ip_block", 1, timedelta(minutes=15))
# docs/api-architecture.md §9.
STEP_UP_PER_SESSION: Final = RateLimitRule(RateLimitScope.AUTH, "step_up.session", 5, timedelta(minutes=5))
# Invite links are bearer secrets; bound how fast one address can try them.
INVITE_PER_IP: Final = RateLimitRule(RateLimitScope.AUTH, "invite.ip", 20, timedelta(minutes=5))

# §3.3 MFA.
MAX_ACTIVE_FACTORS: Final = 5
RECOVERY_CODE_COUNT: Final = 10

# AUTH-7: own login history for the last 90 days.
LOGIN_HISTORY_PERIOD: Final = timedelta(days=90)
