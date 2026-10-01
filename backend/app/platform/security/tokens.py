"""Opaque random tokens (docs/security-architecture.md §3.5).

Tokens are 256-bit random values in base64url. Only their SHA-256 digest is stored, so a
copy of the database does not contain usable tokens. A plain SHA-256 is enough because the
input has 256 bits of entropy; there is nothing to brute-force.
"""

import hashlib
import secrets
import string
from typing import Final

TOKEN_BYTES: Final = 32
# base64url of 32 bytes without padding is 43 characters.
TOKEN_LENGTH: Final = 43
_TOKEN_ALPHABET: Final = frozenset(string.ascii_letters + string.digits + "-_")

# Recovery codes: 16 characters from a 32-letter alphabet (80 bits), shown as four groups.
# The alphabet leaves out 0, 1, I and O, which are easy to confuse when typed from paper.
RECOVERY_CODE_ALPHABET: Final = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
RECOVERY_CODE_LENGTH: Final = 16
RECOVERY_CODE_GROUP: Final = 4


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def looks_like_token(value: str) -> bool:
    """Cheap shape check before any database lookup."""
    return len(value) == TOKEN_LENGTH and all(c in _TOKEN_ALPHABET for c in value)


def new_recovery_code() -> str:
    raw = "".join(secrets.choice(RECOVERY_CODE_ALPHABET) for _ in range(RECOVERY_CODE_LENGTH))
    return "-".join(
        raw[i : i + RECOVERY_CODE_GROUP] for i in range(0, RECOVERY_CODE_LENGTH, RECOVERY_CODE_GROUP)
    )


def normalize_recovery_code(value: str) -> str | None:
    """Uppercase, without separators or spaces; None if it cannot be a recovery code."""
    compact = "".join(ch for ch in value.upper() if ch not in "- ")
    if len(compact) != RECOVERY_CODE_LENGTH or any(ch not in RECOVERY_CODE_ALPHABET for ch in compact):
        return None
    return compact


def recovery_code_digest(value: str) -> bytes | None:
    compact = normalize_recovery_code(value)
    if compact is None:
        return None
    return hashlib.sha256(compact.encode()).digest()
