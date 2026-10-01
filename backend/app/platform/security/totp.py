"""TOTP (RFC 6238) with replay protection (docs/security-architecture.md §3.3).

30-second steps, 6 digits, SHA-1 (what authenticator apps support), one step of tolerance
either side. `match_step` returns the step a code belongs to; the caller stores it as the
factor's `last_used_step` and accepts only later steps, so a code works once.
"""

import base64
import hmac
import io
import secrets
from datetime import datetime
from typing import Final

import pyotp
import segno

STEP_SECONDS: Final = 30
DIGITS: Final = 6
TOLERANCE_STEPS: Final = 1
SECRET_BYTES: Final = 20  # 160 bits, the RFC 4226 recommendation
ISSUER: Final = "Sigvitas HRMS"


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode().rstrip("=")


def step_at(moment: datetime) -> int:
    return int(moment.timestamp()) // STEP_SECONDS


def is_code_shape(code: str) -> bool:
    return len(code) == DIGITS and code.isascii() and code.isdigit()


def match_step(secret: str, code: str, moment: datetime) -> int | None:
    """The step within tolerance whose code equals `code`, or None."""
    if not is_code_shape(code):
        return None
    totp = pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS)
    current = step_at(moment)
    matched: int | None = None
    # Check every candidate, so the time taken does not depend on which one matches.
    for step in range(current - TOLERANCE_STEPS, current + TOLERANCE_STEPS + 1):
        if hmac.compare_digest(totp.generate_otp(step), code) and matched is None:
            matched = step
    return matched


def provisioning_uri(secret: str, account_name: str) -> str:
    return pyotp.TOTP(secret, digits=DIGITS, interval=STEP_SECONDS).provisioning_uri(
        name=account_name, issuer_name=ISSUER
    )


def qr_svg_data_uri(uri: str) -> str:
    """The provisioning URI as an SVG QR code in a data: URI, for an <img> element."""
    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", scale=4, border=2, xmldecl=False, svgns=True)
    return "data:image/svg+xml;base64," + base64.b64encode(buffer.getvalue()).decode()
