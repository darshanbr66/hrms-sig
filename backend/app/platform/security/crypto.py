"""Application-level encryption for `secret` and `sensitive` fields (docs/security-architecture.md §6.2).

AES-256-GCM with versioned keys. Each ciphertext records the key version it was made with,
so keys can be rotated: new values use the active version, old values stay readable while
their version is configured. The associated data binds a ciphertext to its purpose and owner
(for example `identity.mfa_factors.secret` and the user ID), so a ciphertext copied into
another row or column fails to decrypt.

Ciphertext layout: 12-byte random nonce followed by the GCM output (ciphertext + 16-byte tag).
"""

import os
from dataclasses import dataclass
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12


class DecryptionError(Exception):
    """The ciphertext, its key version or its associated data is wrong."""


@dataclass(frozen=True, slots=True)
class Encrypted:
    key_version: int
    ciphertext: bytes


class FieldCipher:
    def __init__(self, keys: dict[int, bytes], active_version: int) -> None:
        if active_version not in keys:
            raise ValueError("the active key version must be one of the configured keys")
        if any(len(key) != KEY_BYTES for key in keys.values()):
            raise ValueError(f"every field encryption key must be {KEY_BYTES} bytes")
        self._keys = {version: AESGCM(key) for version, key in keys.items()}
        self._active = active_version

    def encrypt(self, plaintext: bytes, associated_data: bytes) -> Encrypted:
        nonce = os.urandom(NONCE_BYTES)
        sealed = self._keys[self._active].encrypt(nonce, plaintext, associated_data)
        return Encrypted(self._active, nonce + sealed)

    def decrypt(self, encrypted: Encrypted, associated_data: bytes) -> bytes:
        key = self._keys.get(encrypted.key_version)
        if key is None or len(encrypted.ciphertext) <= NONCE_BYTES:
            raise DecryptionError
        nonce, sealed = encrypted.ciphertext[:NONCE_BYTES], encrypted.ciphertext[NONCE_BYTES:]
        try:
            return key.decrypt(nonce, sealed, associated_data)
        except InvalidTag as exc:
            raise DecryptionError from exc
