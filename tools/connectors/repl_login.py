"""Host-only secure-setting resolver for the restricted browser login action."""

from __future__ import annotations

import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


SECURE_SETTING_SET_ID = "autonomy.secure.setting"


def _private_key(path: Path) -> tuple[str, str]:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError(f"repl_login key must be a regular 0600 file: {path}")
    private_hex = path.read_text().strip()
    if len(private_hex) != 64:
        raise ValueError("repl_login key is not a raw 32-byte X25519 scalar")
    private = X25519PrivateKey.from_private_bytes(bytes.fromhex(private_hex))
    public = private.public_key().public_bytes_raw()
    return private_hex, hashlib.sha256(public).hexdigest()[:16]


def load_credentials(*, autonomy_root: Path, key_file: Path, org: str,
                     target_key: str, expected_origin: str) -> dict[str, str]:
    root = str(autonomy_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from tools.graph import ops as graph_ops
    from tools.network.idkit import seal_open

    members = graph_ops.read_set(SECURE_SETTING_SET_ID, org=org, peers=[])
    match: Any = next((member for member in members.members
                       if member.key == target_key), None)
    if match is None:
        raise LookupError(f"secure setting is not provisioned: {target_key}")
    payload = match.payload
    if payload.get("origin") != expected_origin:
        raise ValueError("secure setting origin does not match login target")
    private_hex, key_id = _private_key(key_file)
    if payload.get("key_id") != key_id:
        raise ValueError("secure setting was encrypted to a different repl_login key")
    plaintext = seal_open(bytes.fromhex(payload["ciphertext_hex"]),
                          private_hex, payload["purpose"])
    values = json.loads(plaintext)
    if not isinstance(values, dict) or not values:
        raise ValueError("secure setting did not contain a credential object")
    if set(values) != set(payload.get("payload_keys") or []):
        raise ValueError("secure setting payload keys do not match its manifest")
    if not all(isinstance(key, str) and isinstance(value, str)
               for key, value in values.items()):
        raise ValueError("secure setting credentials must be strings")
    return values
