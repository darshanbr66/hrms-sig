"""Print a new Ed25519 key pair for signing audit checkpoints, as environment values.

    uv run python -m scripts.generate_audit_signing_key [--key-id ID]

For local development, or for an operator creating a key to store in the secret manager. The
private key is printed once and stored nowhere; put it in the worker's secrets only.
"""

import argparse
import base64
import json
import sys
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m scripts.generate_audit_signing_key")
    parser.add_argument("--key-id", default=f"audit-{datetime.now(UTC):%Y%m%d}")
    key_id = parser.parse_args().key_id
    private_key = Ed25519PrivateKey.generate()
    private_raw = private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    sys.stdout.write(f"AUDIT_SIGNING_KEY_ID={key_id}\n")
    sys.stdout.write(f"AUDIT_SIGNING_PRIVATE_KEY={base64.b64encode(private_raw).decode()}\n")
    public = json.dumps({key_id: base64.b64encode(public_raw).decode()})
    sys.stdout.write(f"AUDIT_VERIFY_PUBLIC_KEYS='{public}'\n")


if __name__ == "__main__":
    main()
