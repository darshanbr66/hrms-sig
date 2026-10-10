"""Checkpoint signatures (docs/security-architecture.md §8.1).

Each checkpoint hash is signed with an Ed25519 key that only the worker holds, from the
secret store (`AUDIT_SIGNING_PRIVATE_KEY`, never in the database, never in the API process).
Verification uses the public keys by key ID (`AUDIT_VERIFY_PUBLIC_KEYS`), so a rotated key's
old checkpoints still verify. A database superuser can recompute every SHA-256 in the chain
but cannot produce a valid signature.

Keys are raw 32-byte Ed25519 keys, base64-encoded in the environment.
"""

import base64
import binascii
import json
import re
from collections.abc import Mapping
from typing import Final, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

KEY_ID: Final = re.compile(r"[A-Za-z0-9._-]{1,64}")
SIGNATURE_BYTES: Final = 64


def _raw_key(value: str, name: str) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{name} must be base64") from exc
    if len(raw) != 32:
        raise ValueError(f"{name} must be a raw 32-byte Ed25519 key")
    return raw


def public_key_bytes(key: Ed25519PublicKey) -> bytes:
    return key.public_bytes(Encoding.Raw, PublicFormat.Raw)


class CheckpointSigner:
    """The worker's signing key."""

    def __init__(self, key_id: str, private_key: Ed25519PrivateKey) -> None:
        if not KEY_ID.fullmatch(key_id):
            raise ValueError("the signing key ID is 1 to 64 letters, digits, '.', '_' or '-'")
        self.key_id = key_id
        self._key = private_key

    @classmethod
    def from_base64(cls, key_id: str, private_key: str) -> Self:
        raw = _raw_key(private_key, "AUDIT_SIGNING_PRIVATE_KEY")
        return cls(key_id, Ed25519PrivateKey.from_private_bytes(raw))

    @property
    def public_key(self) -> Ed25519PublicKey:
        return self._key.public_key()

    def sign(self, checkpoint_hash: bytes) -> bytes:
        return self._key.sign(checkpoint_hash)


class SignatureVerifier:
    """Public keys by key ID."""

    def __init__(self, keys: Mapping[str, Ed25519PublicKey]) -> None:
        if not keys:
            raise ValueError("at least one audit verification key is needed")
        for key_id in keys:
            if not KEY_ID.fullmatch(key_id):
                raise ValueError("an audit verification key ID is 1 to 64 letters, digits, '.', '_' or '-'")
        self._keys = dict(keys)

    @classmethod
    def from_json(cls, value: str) -> Self:
        """`{"<key id>": "<base64 raw public key>", ...}`"""
        try:
            raw = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(
                'AUDIT_VERIFY_PUBLIC_KEYS must be a JSON object {"<key id>": "<base64 key>"}'
            ) from exc
        if not isinstance(raw, dict) or not all(isinstance(item, str) for item in raw.values()):
            raise ValueError('AUDIT_VERIFY_PUBLIC_KEYS must be a JSON object {"<key id>": "<base64 key>"}')
        return cls(
            {
                str(key_id): Ed25519PublicKey.from_public_bytes(_raw_key(key, "AUDIT_VERIFY_PUBLIC_KEYS"))
                for key_id, key in raw.items()
            }
        )

    def knows(self, key_id: str) -> bool:
        return key_id in self._keys

    def matches(self, signer: CheckpointSigner) -> bool:
        """The signer's key is the public key listed under its ID."""
        key = self._keys.get(signer.key_id)
        return key is not None and public_key_bytes(key) == public_key_bytes(signer.public_key)

    def verify(self, key_id: str, checkpoint_hash: bytes, signature: bytes) -> bool:
        key = self._keys.get(key_id)
        if key is None or len(signature) != SIGNATURE_BYTES:
            return False
        try:
            key.verify(signature, checkpoint_hash)
        except InvalidSignature:
            return False
        return True
