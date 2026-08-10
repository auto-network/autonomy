"""Host-only secure-setting resolver for the restricted browser login action.

Workspace binding (v2): each provisioned credential carries one ciphertext
per allowed workspace, sealed under
``autonomy.secure-setting.v2|<org>|<target_key>|<nonce>|workspace=<ws>``.
This module RECONSTRUCTS that label from the caller's host-derived
workspace — it never trusts a purpose string stored beside the ciphertext
(the revision-1 design did, which made the binding advisory: anyone able
to write the Setting could widen it and the ciphertext stayed valid). A
caller whose workspace was not in the allowlist at sealing time gets a
label the record simply does not open under; widening an allowlist always
means re-sealing through a fresh operator approval.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey


SECURE_SETTING_SET_ID = "autonomy.secure.setting"
SECURE_SETTING_V2_REVISION = 2
PURPOSE_PREFIX = "autonomy.secure-setting.v2"

_NONCE_RE = re.compile(r"^[0-9a-f]{64}$")


def purpose_for(org: str, target_key: str, nonce: str, workspace: str) -> str:
    """The v2 HPKE purpose label. Must stay byte-identical to
    ``tools.dashboard.secure_setting_approvals.purpose_for``."""
    return f"{PURPOSE_PREFIX}|{org}|{target_key}|{nonce}|workspace={workspace}"


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
                     target_key: str, expected_origin: str,
                     caller_workspace: str) -> dict[str, str]:
    root = str(autonomy_root.resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    from tools.graph import ops as graph_ops
    from tools.network.idkit import seal_open

    if not caller_workspace or not isinstance(caller_workspace, str):
        raise PermissionError("secure login requires a derived caller workspace")
    # min_revision=2 fails closed against revision-1 rows: a v1 record has
    # no workspace binding, so accepting it would be the bypass.
    members = graph_ops.read_set(
        SECURE_SETTING_SET_ID, org=org, peers=[],
        min_revision=SECURE_SETTING_V2_REVISION)
    match: Any = next((member for member in members.members
                       if member.key == target_key), None)
    if match is None:
        raise LookupError(
            f"secure setting is not provisioned (workspace-bound v2): {target_key}")
    payload = match.payload
    if payload.get("origin") != expected_origin:
        raise ValueError("secure setting origin does not match login target")
    nonce = payload.get("nonce")
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ValueError("secure setting has no valid provisioning nonce")
    ciphertexts = payload.get("ciphertexts_hex")
    if not isinstance(ciphertexts, dict):
        raise ValueError("secure setting is not a workspace-bound record")
    record_hex = ciphertexts.get(caller_workspace)
    if not isinstance(record_hex, str) or not record_hex:
        raise PermissionError(
            f"secure setting {target_key} is not sealed for workspace "
            f"{caller_workspace}")
    private_hex, key_id = _private_key(key_file)
    if payload.get("key_id") != key_id:
        raise ValueError("secure setting was encrypted to a different repl_login key")
    purpose = purpose_for(org, target_key, nonce, caller_workspace)
    try:
        plaintext = seal_open(bytes.fromhex(record_hex), private_hex, purpose)
    except Exception as exc:
        raise PermissionError(
            f"secure setting {target_key} did not decrypt for workspace "
            f"{caller_workspace}: the sealed record was not created for this "
            "workspace's label") from exc
    values = json.loads(plaintext)
    if not isinstance(values, dict) or not values:
        raise ValueError("secure setting did not contain a credential object")
    if set(values) != set(payload.get("payload_keys") or []):
        raise ValueError("secure setting payload keys do not match its manifest")
    if not all(isinstance(key, str) and isinstance(value, str)
               for key, value in values.items()):
        raise ValueError("secure setting credentials must be strings")
    return values
