"""Workspace binding is cryptographic: the v2 record decrypts only under a
label reconstructed from the caller's derived workspace.

The pivotal case is the widened allowlist: an attacker with Setting write
access copies the sealed record under an extra workspace key. The lookup
then succeeds — and decryption fails, because the record was sealed under
a label naming the original workspace. That failure mode (InvalidTag, not
a policy check) is the whole point of the design.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from tools.connectors import repl_login
from tools.graph import settings_ops
from tools.graph.schemas.secure_setting import (
    SECURE_SETTING_SET_ID,
    SECURE_SETTING_REVISION,
    SECURE_SETTING_V2_REVISION,
)
from tools.network.idkit import seal

ORG = "acme"
TARGET_KEY = "connector.eversource.login"
ORIGIN = "eversource.com"
ALLOWED_WS = "finance-ws"
OTHER_WS = "ops-ws"
NONCE = "ab" * 32
VALUES = {"username": "alex@example.com", "password": "hunter2"}


@pytest.fixture
def env(tmp_path, monkeypatch):
    from tools.graph.db import GraphDB

    GraphDB.close_all_pooled()
    orgs_dir = tmp_path / "orgs"
    orgs_dir.mkdir()
    GraphDB(orgs_dir / f"{ORG}.db").close()
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))

    private = X25519PrivateKey.generate()
    private_hex = private.private_bytes_raw().hex()
    public = private.public_key().public_bytes_raw()
    key_file = tmp_path / "repl-login.key"
    key_file.write_text(private_hex)
    key_file.chmod(0o600)
    key_id = hashlib.sha256(public).hexdigest()[:16]
    yield tmp_path, key_file, public.hex(), key_id
    GraphDB.close_all_pooled()


def _provision_v2(public_hex: str, key_id: str,
                  workspaces: list[str]) -> dict:
    plaintext = json.dumps(VALUES).encode()
    payload = {
        "ciphertexts_hex": {
            ws: seal(plaintext, public_hex,
                     repl_login.purpose_for(ORG, TARGET_KEY, NONCE, ws)).hex()
            for ws in workspaces
        },
        "workspaces": sorted(workspaces),
        "nonce": NONCE,
        "key_id": key_id,
        "origin": ORIGIN,
        "payload_keys": ["username", "password"],
        "provisioned_at": time.time(),
    }
    settings_ops.upsert_by_key(
        SECURE_SETTING_SET_ID, SECURE_SETTING_V2_REVISION,
        TARGET_KEY, payload, org=ORG)
    return payload


def _load(env, *, caller_workspace: str, target_key: str = TARGET_KEY,
          origin: str = ORIGIN) -> dict:
    tmp_path, key_file, _public, _key_id = env
    return repl_login.load_credentials(
        autonomy_root=tmp_path, key_file=key_file, org=ORG,
        target_key=target_key, expected_origin=origin,
        caller_workspace=caller_workspace)


def test_allowed_workspace_decrypts(env):
    _tmp, _kf, public_hex, key_id = env
    _provision_v2(public_hex, key_id, [ALLOWED_WS])
    assert _load(env, caller_workspace=ALLOWED_WS) == VALUES


def test_workspace_outside_allowlist_has_no_record(env):
    _tmp, _kf, public_hex, key_id = env
    _provision_v2(public_hex, key_id, [ALLOWED_WS])
    with pytest.raises(PermissionError, match="not sealed for workspace"):
        _load(env, caller_workspace=OTHER_WS)


def test_widened_allowlist_fails_to_decrypt_not_merely_a_check(env):
    _tmp, _kf, public_hex, key_id = env
    payload = _provision_v2(public_hex, key_id, [ALLOWED_WS])
    # Attacker with Setting write access "widens" the allowlist by copying
    # the sealed record under the excluded workspace's key.
    widened = dict(payload)
    widened["ciphertexts_hex"] = {
        **payload["ciphertexts_hex"],
        OTHER_WS: payload["ciphertexts_hex"][ALLOWED_WS],
    }
    widened["workspaces"] = sorted([ALLOWED_WS, OTHER_WS])
    settings_ops.upsert_by_key(
        SECURE_SETTING_SET_ID, SECURE_SETTING_V2_REVISION,
        TARGET_KEY, widened, org=ORG)
    # The lookup now SUCCEEDS — and decryption fails, because the label
    # reconstructed for OTHER_WS is not the label the record was sealed to.
    with pytest.raises(PermissionError, match="did not decrypt"):
        _load(env, caller_workspace=OTHER_WS)
    # The legitimate workspace is unaffected.
    assert _load(env, caller_workspace=ALLOWED_WS) == VALUES


def test_v1_record_is_refused(env):
    tmp_path, key_file, public_hex, key_id = env
    purpose = f"autonomy.secure-setting.v1|{ORG}|{TARGET_KEY}|{NONCE}"
    settings_ops.upsert_by_key(
        SECURE_SETTING_SET_ID, SECURE_SETTING_REVISION, TARGET_KEY, {
            "ciphertext_hex": seal(json.dumps(VALUES).encode(),
                                   public_hex, purpose).hex(),
            "key_id": key_id,
            "purpose": purpose,
            "origin": ORIGIN,
            "payload_keys": ["username", "password"],
            "provisioned_at": time.time(),
        }, org=ORG)
    with pytest.raises(LookupError, match="not provisioned"):
        _load(env, caller_workspace=ALLOWED_WS)


def test_origin_mismatch_is_refused(env):
    _tmp, _kf, public_hex, key_id = env
    _provision_v2(public_hex, key_id, [ALLOWED_WS])
    with pytest.raises(ValueError, match="origin does not match"):
        _load(env, caller_workspace=ALLOWED_WS, origin="evil.example")


def test_wrong_recipient_key_is_refused(env):
    tmp_path, key_file, public_hex, key_id = env
    _provision_v2(public_hex, key_id, [ALLOWED_WS])
    other = X25519PrivateKey.generate().private_bytes_raw().hex()
    key_file.write_text(other)
    key_file.chmod(0o600)
    with pytest.raises(ValueError, match="different repl_login key"):
        _load(env, caller_workspace=ALLOWED_WS)
