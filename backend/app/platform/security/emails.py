"""Canonical email addresses and their keyed hash (docs/security-architecture.md §6.4, §8).

`canonical_email` is the one normalization used everywhere an email address is stored,
compared, rate-limited or hashed: Unicode NFKC, surrounding whitespace removed, lower case.
Lower case (not case folding) matches the database's `citext` comparison, so the canonical
form and the stored account email always agree.

`EmailHasher` records an attempted email (for example a sign-in for an unknown account)
without storing it: HMAC-SHA256 with a dedicated key from the secret manager
(`EMAIL_LOOKUP_HMAC_KEY`). A plain SHA-256 would let anyone with a list of candidate
addresses reverse it; the keyed hash cannot be computed without the key, yet the same
address always gives the same digest, so repeated attempts can still be correlated.
"""

import hashlib
import hmac
import unicodedata
from typing import Final

DIGEST_BYTES: Final = 32
MIN_KEY_BYTES: Final = 32


def canonical_email(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().lower()


class EmailHasher:
    def __init__(self, key: bytes) -> None:
        if len(key) < MIN_KEY_BYTES:
            raise ValueError(f"the email lookup key must be at least {MIN_KEY_BYTES} bytes")
        self._key = key

    def digest(self, email: str) -> bytes:
        return hmac.new(self._key, canonical_email(email).encode(), hashlib.sha256).digest()
