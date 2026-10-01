"""Password hashing and policy (docs/security-architecture.md §3.2).

- Argon2id through argon2-cffi, m=64 MiB, t=3, p=1. The parameters are stored in the hash
  string, so a hash made with older parameters is detected and replaced at the next sign-in.
- Hashing and verification are CPU-bound (~100-250 ms) and use 64 MiB each, so they run in
  a worker thread (never blocking the event loop or inside an open database transaction),
  at most MAX_CONCURRENT_HASHES at a time per process. A flood of sign-in attempts then
  queues instead of exhausting memory.
- Policy: 12-128 characters, any Unicode, no composition rules, not a known breached
  password. The breached check uses the Have I Been Pwned range API (k-anonymity: only the
  first five hex characters of the SHA-1 hash leave the server) when enabled, and the
  bundled list of common passwords when it is disabled or unreachable.
"""

import asyncio
import hashlib
import logging
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from importlib import resources
from typing import Final

import httpx
from argon2 import PasswordHasher, Type
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

logger = logging.getLogger(__name__)

MIN_LENGTH: Final = 12
MAX_LENGTH: Final = 128

MAX_CONCURRENT_HASHES: Final = 4
_hash_slots = asyncio.Semaphore(MAX_CONCURRENT_HASHES)

HIBP_RANGE_URL: Final = "https://api.pwnedpasswords.com/range/"
HIBP_TIMEOUT_SECONDS: Final = 2.0

_HASHER: Final = PasswordHasher(time_cost=3, memory_cost=64 * 1024, parallelism=1, type=Type.ID)


class PasswordProblem(StrEnum):
    """Machine codes for a password the policy refuses (used in validation errors)."""

    TOO_SHORT = "password.too_short"
    TOO_LONG = "password.too_long"
    BREACHED = "password.breached"


PROBLEM_MESSAGES: Final = {
    PasswordProblem.TOO_SHORT: f"Use at least {MIN_LENGTH} characters.",
    PasswordProblem.TOO_LONG: f"Use at most {MAX_LENGTH} characters.",
    PasswordProblem.BREACHED: (
        "This password has appeared in a data breach, so it is easy to guess. Choose a different one."
    ),
}


def normalize(password: str) -> str:
    """NFKC, so the same password typed on different devices hashes the same way."""
    return unicodedata.normalize("NFKC", password)


def hash_password_sync(password: str) -> str:
    return _HASHER.hash(normalize(password))


async def hash_password(password: str) -> str:
    async with _hash_slots:
        return await asyncio.to_thread(hash_password_sync, password)


@dataclass(frozen=True, slots=True)
class Verification:
    valid: bool
    needs_rehash: bool = False


def verify_password_sync(password_hash: str, password: str) -> Verification:
    try:
        _HASHER.verify(password_hash, normalize(password))
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return Verification(valid=False)
    return Verification(valid=True, needs_rehash=_HASHER.check_needs_rehash(password_hash))


async def verify_password(password_hash: str, password: str) -> Verification:
    async with _hash_slots:
        return await asyncio.to_thread(verify_password_sync, password_hash, password)


@cache
def _dummy_hash() -> str:
    return hash_password_sync("dummy password for constant-time sign-in failures")


async def burn_verification_time(password: str) -> None:
    """Spend the time of a real verification, so an unknown account answers as slowly as a
    wrong password (docs/security-architecture.md §3.4)."""
    await verify_password(_dummy_hash(), password)


def length_problem(password: str) -> PasswordProblem | None:
    length = len(normalize(password))
    if length < MIN_LENGTH:
        return PasswordProblem.TOO_SHORT
    if length > MAX_LENGTH:
        return PasswordProblem.TOO_LONG
    return None


@cache
def _common_passwords() -> frozenset[str]:
    text = resources.files(__package__).joinpath("common_passwords.txt").read_text(encoding="utf-8")
    return frozenset(line for line in text.splitlines() if line and not line.startswith("#"))


def is_common_password(password: str) -> bool:
    return normalize(password).casefold() in _common_passwords()


class BreachedPasswordChecker:
    """Breached-password check with the bundled list as the fallback."""

    def __init__(self, *, hibp_enabled: bool, client: httpx.AsyncClient | None = None) -> None:
        self._hibp_enabled = hibp_enabled
        self._client = client

    async def is_breached(self, password: str) -> bool:
        if is_common_password(password):
            return True
        if not self._hibp_enabled or self._client is None:
            return False
        # The range API is keyed by SHA-1 (its protocol); this is a lookup key, not a protection.
        digest = (
            hashlib.sha1(  # nosemgrep (SHA-1 is the range API protocol; usedforsecurity=False)
                normalize(password).encode(), usedforsecurity=False
            )
            .hexdigest()
            .upper()
        )
        prefix, suffix = digest[:5], digest[5:]
        try:
            response = await self._client.get(
                HIBP_RANGE_URL + prefix,
                headers={"Add-Padding": "true"},
                timeout=HIBP_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            # The bundled list already ran; an unreachable range API is not a reason to
            # refuse the password.
            logger.warning("password.breach_check_unavailable", extra={"error_type": type(exc).__name__})
            return False
        for line in response.text.splitlines():
            candidate, _, count = line.partition(":")
            if candidate.strip() == suffix and count.strip() not in ("", "0"):
                return True
        return False

    async def problem(self, password: str) -> PasswordProblem | None:
        """The first policy problem with the password, or None if it is acceptable."""
        if (problem := length_problem(password)) is not None:
            return problem
        if await self.is_breached(password):
            return PasswordProblem.BREACHED
        return None
