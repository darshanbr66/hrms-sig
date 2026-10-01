"""Passwords, tokens, recovery codes, TOTP and field encryption (docs/security-architecture.md §3, §6)."""

import asyncio
import base64
import hashlib
import threading
import time
from datetime import UTC, datetime

import httpx
import pyotp
import pytest
from argon2 import PasswordHasher

from app.platform.security import passwords, tokens, totp
from app.platform.security.crypto import DecryptionError, Encrypted, FieldCipher

# --- passwords ---------------------------------------------------------------------------


def test_hash_is_argon2id_with_the_documented_parameters() -> None:
    password_hash = passwords.hash_password_sync("a long enough passphrase")
    assert password_hash.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert passwords.verify_password_sync(password_hash, "a long enough passphrase").valid
    assert not passwords.verify_password_sync(password_hash, "a long enough passphrasf").valid


def test_verification_never_raises_on_a_malformed_hash() -> None:
    assert not passwords.verify_password_sync("not a hash", "anything at all").valid


def test_weaker_parameters_are_flagged_for_rehash() -> None:
    old = PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1).hash("a long enough passphrase")
    result = passwords.verify_password_sync(old, "a long enough passphrase")
    assert result.valid
    assert result.needs_rehash


def test_unicode_is_normalized_before_hashing() -> None:
    composed, decomposed = "café passphrase!", "café passphrase!"
    assert passwords.verify_password_sync(passwords.hash_password_sync(composed), decomposed).valid


@pytest.mark.parametrize(
    ("password", "problem"),
    [
        ("x" * 11, passwords.PasswordProblem.TOO_SHORT),
        ("x" * 129, passwords.PasswordProblem.TOO_LONG),
        ("passwordpassword", passwords.PasswordProblem.BREACHED),
        ("PasswordPassword", passwords.PasswordProblem.BREACHED),
    ],
)
async def test_policy(password: str, problem: passwords.PasswordProblem) -> None:
    checker = passwords.BreachedPasswordChecker(hibp_enabled=False)
    assert await checker.problem(password) is problem


async def test_policy_has_no_composition_rules() -> None:
    checker = passwords.BreachedPasswordChecker(hibp_enabled=False)
    assert await checker.problem("only lower case words here") is None
    assert await checker.problem("q7" * 6) is None
    # The bundled list refuses common ones regardless of how they look.
    assert await checker.problem("x" * 12) is passwords.PasswordProblem.BREACHED


async def test_breached_check_sends_only_a_five_character_prefix() -> None:
    seen: list[httpx.Request] = []
    password = "a passphrase in a breach"
    digest = hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text=f"{digest[5:]}:3\r\n0000000000000000000000000000000000A:0")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        checker = passwords.BreachedPasswordChecker(hibp_enabled=True, client=client)
        assert await checker.problem(password) is passwords.PasswordProblem.BREACHED
    [request] = seen
    assert request.url.path.endswith("/" + digest[:5])
    assert password not in str(request.url)
    assert digest[5:] not in str(request.url)


async def test_an_unreachable_range_api_falls_back_to_the_bundled_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("down", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        checker = passwords.BreachedPasswordChecker(hibp_enabled=True, client=client)
        assert await checker.problem("a passphrase nobody uses") is None
        assert await checker.problem("passwordpassword") is passwords.PasswordProblem.BREACHED


# --- tokens and recovery codes -----------------------------------------------------------


def test_tokens_are_256_bit_and_stored_only_as_digests() -> None:
    token = tokens.new_token()
    assert len(base64.urlsafe_b64decode(token + "=")) == 32
    assert tokens.looks_like_token(token)
    assert tokens.token_digest(token) != token.encode()
    assert len(tokens.token_digest(token)) == 32
    assert tokens.new_token() != token


@pytest.mark.parametrize("value", ["", "short", "x" * 43 + "y", "é" * 43, "a b" + "c" * 40])
def test_malformed_tokens_are_refused_before_lookup(value: str) -> None:
    assert not tokens.looks_like_token(value)


def test_recovery_codes_are_80_bit_and_forgiving_about_format() -> None:
    code = tokens.new_recovery_code()
    assert len(code) == 19
    assert code.count("-") == 3
    digest = tokens.recovery_code_digest(code)
    assert digest is not None
    assert tokens.recovery_code_digest(code.lower().replace("-", " ")) == digest
    assert tokens.recovery_code_digest("0000-1111-OOOO-IIII") is None
    assert tokens.recovery_code_digest("ABCD") is None


# --- TOTP --------------------------------------------------------------------------------

# RFC 6238 appendix B, SHA-1 secret "12345678901234567890", last six digits of each code.
RFC_SECRET = base64.b32encode(b"12345678901234567890").decode()
# "123456" in full-width digits: digits to str.isdigit(), but not ASCII.
FULLWIDTH_DIGITS = "".join(chr(0xFF11 + i) for i in range(6))


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [
        (59, "287082"),
        (1111111109, "081804"),
        (1111111111, "050471"),
        (1234567890, "005924"),
        (2000000000, "279037"),
    ],
)
def test_rfc_6238_vectors(unix_time: int, code: str) -> None:
    moment = datetime.fromtimestamp(unix_time, UTC)
    assert totp.match_step(RFC_SECRET, code, moment) == unix_time // 30


def test_one_step_of_tolerance_either_side() -> None:
    moment = datetime.fromtimestamp(1234567890, UTC)
    step = 1234567890 // 30
    generator = pyotp.TOTP(RFC_SECRET)
    for offset in (-1, 0, 1):
        assert totp.match_step(RFC_SECRET, generator.generate_otp(step + offset), moment) == step + offset
    for offset in (-2, 2):
        assert totp.match_step(RFC_SECRET, generator.generate_otp(step + offset), moment) is None


@pytest.mark.parametrize("code", ["", "12345", "1234567", "12345a", FULLWIDTH_DIGITS])
def test_codes_must_be_six_ascii_digits(code: str) -> None:
    assert totp.match_step(RFC_SECRET, code, datetime.now(UTC)) is None


def test_provisioning_uri_and_qr_code() -> None:
    secret = totp.new_secret()
    assert len(base64.b32decode(secret + "=" * (-len(secret) % 8))) == 20
    uri = totp.provisioning_uri(secret, "person@dev.example")
    assert uri.startswith("otpauth://totp/Sigvitas%20HRMS:person%40dev.example?")
    qr = totp.qr_svg_data_uri(uri)
    assert qr.startswith("data:image/svg+xml;base64,")
    assert b"<svg" in base64.b64decode(qr.split(",", 1)[1])


# --- field encryption --------------------------------------------------------------------


def test_round_trip_and_key_rotation() -> None:
    old = FieldCipher({1: b"a" * 32}, 1)
    sealed = old.encrypt(b"secret value", b"purpose:owner")
    assert sealed.key_version == 1
    assert b"secret value" not in sealed.ciphertext
    rotated = FieldCipher({1: b"a" * 32, 2: b"b" * 32}, 2)
    assert rotated.decrypt(sealed, b"purpose:owner") == b"secret value"
    assert rotated.encrypt(b"secret value", b"purpose:owner").key_version == 2


def test_ciphertext_is_bound_to_its_owner_and_cannot_be_tampered_with() -> None:
    cipher = FieldCipher({1: b"a" * 32}, 1)
    sealed = cipher.encrypt(b"secret value", b"purpose:owner-1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(sealed, b"purpose:owner-2")
    tampered = Encrypted(1, sealed.ciphertext[:-1] + bytes([sealed.ciphertext[-1] ^ 1]))
    with pytest.raises(DecryptionError):
        cipher.decrypt(tampered, b"purpose:owner-1")
    with pytest.raises(DecryptionError):
        cipher.decrypt(Encrypted(9, sealed.ciphertext), b"purpose:owner-1")


def test_nonces_are_never_reused() -> None:
    cipher = FieldCipher({1: b"a" * 32}, 1)
    nonces = {cipher.encrypt(b"same", b"aad").ciphertext[:12] for _ in range(200)}
    assert len(nonces) == 200


def test_keys_must_be_256_bit() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        FieldCipher({1: b"short"}, 1)
    with pytest.raises(ValueError, match="active key version"):
        FieldCipher({1: b"a" * 32}, 2)


async def test_concurrent_hashing_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each Argon2 hash uses 64 MiB; a flood of sign-in attempts must queue, not exhaust memory."""
    running = peak = 0
    lock = threading.Lock()

    def slow_verify(password_hash: str, password: str) -> passwords.Verification:
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
        time.sleep(0.05)
        with lock:
            running -= 1
        return passwords.Verification(valid=False)

    monkeypatch.setattr(passwords, "verify_password_sync", slow_verify)
    await asyncio.gather(*(passwords.verify_password("hash", "password") for _ in range(12)))
    assert peak == passwords.MAX_CONCURRENT_HASHES
