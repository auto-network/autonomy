"""Reusable client-to-host encrypted settings envelopes.

The browser/client encrypts a structured settings map to a recipient public
key.  The host stores only the envelope in the setting and keeps the matching
private key in a capability-owned 0600 file.  This is the interim adapter for
``repl_login``; the envelope is deliberately shaped so a vault recipient can
replace the capability key later without changing the input dialog.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from tools.network.idkit import sealing


FORMAT = "autonomy.secure-setting.v1"
KEY_FILE_MODE = 0o600


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def _purpose(target_setting: str, target_key: str, schema: dict[str, str],
             origin: str | None) -> str:
    binding = _canonical({
        "format": FORMAT,
        "target_setting": target_setting,
        "target_key": target_key,
        "schema": schema,
        "origin": origin or "",
    })
    return f"secure-setting:repl_login:{hashlib.sha256(binding).hexdigest()[:24]}"


def create_keypair() -> tuple[str, bytes]:
    """Return (public-key-hex, raw-private-key-bytes)."""
    private = X25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return public.hex(), private.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )


def write_private_key(path: Path, private_bytes: bytes) -> None:
    """Create a capability key file, refusing unsafe existing permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != KEY_FILE_MODE:
            raise PermissionError(f"key file must be 0600: {path}")
        path.write_bytes(private_bytes)
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(path, flags, KEY_FILE_MODE)
        try:
            os.write(fd, private_bytes)
        finally:
            os.close(fd)
    os.chmod(path, KEY_FILE_MODE)


def read_private_key(path: Path) -> X25519PrivateKey:
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != KEY_FILE_MODE:
        raise PermissionError(f"key file must be a regular 0600 file: {path}")
    return X25519PrivateKey.from_private_bytes(path.read_bytes())


def encrypt_settings(settings: dict[str, Any], *, public_key_b64: str,
                     key_id: str, target_setting: str, target_key: str,
                     schema: dict[str, str], origin: str | None = None) -> dict[str, Any]:
    """Seal a settings map with Autonomy's vault-compatible HPKE suite."""
    if not isinstance(settings, dict) or not settings:
        raise ValueError("settings must be a non-empty object")
    unknown = set(settings) - set(schema.values())
    if unknown:
        raise ValueError(f"settings contain keys outside schema: {sorted(unknown)}")
    purpose = _purpose(target_setting, target_key, schema, origin)
    record = sealing.seal(_canonical(settings), public_key_b64, purpose)
    return {
        "format": FORMAT,
        "key_id": key_id,
        "target_setting": target_setting,
        "target_key": target_key,
        "schema": schema,
        "origin": origin or "",
        "purpose": purpose,
        "sealed_payload": record.hex(),
    }


def decrypt_settings(envelope: dict[str, Any], *, private_key_path: Path,
                     expected_target_setting: str | None = None,
                     expected_target_key: str | None = None) -> dict[str, Any]:
    """Decrypt and validate an envelope in host memory."""
    if envelope.get("format") != FORMAT:
        raise ValueError("unsupported secure-setting envelope")
    if expected_target_setting and envelope.get("target_setting") != expected_target_setting:
        raise ValueError("secure-setting target mismatch")
    if expected_target_key and envelope.get("target_key") != expected_target_key:
        raise ValueError("secure-setting key mismatch")
    schema = envelope.get("schema")
    if not isinstance(schema, dict):
        raise ValueError("secure-setting schema is missing")
    expected_purpose = _purpose(
        str(envelope["target_setting"]), str(envelope["target_key"]), schema,
        str(envelope.get("origin") or ""))
    if envelope.get("purpose") != expected_purpose:
        raise ValueError("secure-setting purpose mismatch")
    private = read_private_key(private_key_path)
    raw_private = private.private_bytes(
        serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
        serialization.NoEncryption()).hex()
    plaintext = sealing.open(bytes.fromhex(envelope["sealed_payload"]),
                             raw_private, expected_purpose)
    values = json.loads(plaintext)
    if not isinstance(values, dict):
        raise ValueError("secure-setting payload is not an object")
    return values
