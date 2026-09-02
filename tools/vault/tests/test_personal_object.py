"""Personal owner-at-rest secured Settings: no organization outer layer."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3

import pytest

from tools.graph import schemas, settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.registry import (
    SCHEMAS,
    UPCONVERTERS,
    SettingSchema,
    home,
    vaulted,
)
from tools.network.idkit.canonical import canonical_json
from tools.vault import ClassOpenError, PASSWORD_POLICY, create_class
from tools.vault.factors import create_password_factor, open_password_seed
from tools.vault import key_holder
from tools.vault.personal_object import LOCATOR_PREFIX, is_personal_locator
from tools.vault.store import VaultStore
from tools.vault.testkit import enroll_test_anchor


SET_ID = "autonomy.test.personal-secured"
SECRET = "-----BEGIN OPENSSH PRIVATE KEY-----\nfake-only\n-----END OPENSSH PRIVATE KEY-----"


@pytest.fixture(autouse=True)
def isolated_registry_and_seams():
    schemas_snapshot = dict(SCHEMAS)
    upconverters_snapshot = dict(UPCONVERTERS)
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    try:
        yield
    finally:
        SCHEMAS.clear()
        SCHEMAS.update(schemas_snapshot)
        UPCONVERTERS.clear()
        UPCONVERTERS.update(upconverters_snapshot)
        settings_ops.set_vault_sealer(None)
        settings_ops.set_vault_key_holder(None)


@pytest.fixture
def personal_world(tmp_path, monkeypatch):
    db_path = tmp_path / "personal.db"
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setattr(key_holder, "_scoped_db", lambda _set_id, _org: db_path)

    @home("personal")
    @vaulted("secured")
    class PersonalSecuredV1(SettingSchema):
        set_id = SET_ID
        schema_revision = 1

    schemas.register_schema(SET_ID, 1, PersonalSecuredV1)
    GraphDB(db_path).close()

    password = "operator-held-test-password"
    factor = create_password_factor(password, factor_id="operator-password")
    seed = open_password_seed(factor.armor, password)
    with VaultStore(db_path) as store:
        _, anchor = enroll_test_anchor(store)
        policy_class = create_class(
            PASSWORD_POLICY, [factor.published],
            created_at="2026-08-24T00:00:00Z", recovery=anchor,
        )
        store.put_class(policy_class)
    return db_path, policy_class, seed


def test_personal_secured_setting_seals_cold_and_opens_only_with_factor(
    personal_world,
):
    db_path, policy_class, seed = personal_world

    setting_id = settings_ops.add_setting(
        SET_ID,
        1,
        "autonomy:mac.ssh.disposable-proof",
        {"value": SECRET},
        org=None,
        vault_policy_class_id=policy_class.class_id,
    )

    with sqlite3.connect(db_path) as connection:
        stored = connection.execute(
            "SELECT payload FROM settings WHERE id = ?", (setting_id,)
        ).fetchone()[0]
        outer_objects = connection.execute(
            "SELECT COUNT(*) FROM vault_content_objects"
        ).fetchone()[0]
    locator = json.loads(stored)
    assert is_personal_locator(locator)
    _wire = locator[len(LOCATOR_PREFIX):]
    _envelope = json.loads(base64.urlsafe_b64decode(_wire + "=" * (-len(_wire) % 4)))
    assert _envelope["body_suite_id"] == "chacha20-poly1305", (
        "a secured body seals the browser-openable ChaCha20-Poly1305 suite (A-2)"
    )
    assert SECRET not in stored
    assert outer_objects == 0, "personal secured writes must bypass the org outer layer"

    member = settings_ops.read_set(SET_ID, org=None, peers=[]).members[0]
    assert member.payload is None
    assert member.vault_error is None
    assert member.sealed_content_key["policy_class_id"] == policy_class.class_id

    digest = hashlib.sha256(canonical_json(member.sealed_content_key)).hexdigest()
    with pytest.raises(ClassOpenError):
        settings_ops.open_secured_setting(
            SET_ID,
            member.key,
            setting_id=setting_id,
            sealed_content_key_digest=digest,
            opener_seeds={},
            org=None,
        )
    opened = settings_ops.open_secured_setting(
        SET_ID,
        member.key,
        setting_id=setting_id,
        sealed_content_key_digest=digest,
        opener_seeds={"operator-password": seed},
        org=None,
    )
    assert opened == {"value": SECRET}


def test_personal_locator_cannot_be_moved_to_another_setting(personal_world):
    db_path, policy_class, _seed = personal_world
    settings_ops.add_setting(
        SET_ID,
        1,
        "one",
        {"value": SECRET},
        org=None,
        vault_policy_class_id=policy_class.class_id,
    )
    member = settings_ops.read_set(SET_ID, org=None, peers=[]).members[0]
    assert member.sealed_content_key is not None

    with sqlite3.connect(db_path) as db:
        db.execute("UPDATE settings SET key = 'two' WHERE id = ?", (member.id,))

    moved = settings_ops.read_set(SET_ID, org=None, peers=[]).members[0]
    assert moved.payload is None
    assert moved.sealed_content_key is None
    assert moved.vault_error.reason == settings_ops.VAULT_DECRYPTION_FAILED


def test_personal_audited_seals_cold_to_delegate_and_opens_with_delegate_private():
    """A-1: a personal audited revision seals COLD to the delegate's public key
    (no key holder, no factor) and opens only with the delegate private half."""
    from tools.vault import personal_object as pobj
    from tools.vault.errors import VaultError

    root_seed = bytes(range(32))
    priv_hex, pub_hex = pobj.derive_delegate_audited_recipient(root_seed)
    assert len(priv_hex) == 64 and len(pub_hex) == 64 and priv_hex != pub_hex

    # COLD write: only the published public key is needed — no VaultKeyCache, no factor.
    locator = pobj.seal_audited_revision(
        set_id="autonomy.vault.audited",
        key="anchore:scale-harness.ssh",
        setting_id="setting-abc",
        payload={"value": SECRET},
        delegate_public_hex=pub_hex,
    )
    assert pobj.is_personal_locator(locator)
    wire = locator[len(pobj.LOCATOR_PREFIX):]
    env = json.loads(base64.urlsafe_b64decode(wire + "=" * (-len(wire) % 4)))
    assert env["tier"] == "audited"
    assert env["body_suite_id"] == "aes-256-gcm-siv", "audited keeps the nonce-misuse-resistant body"
    assert "delegate_sealed_cek" in env and "sealed_cek" not in env
    assert SECRET not in locator

    # WARM read: the delegate private half opens it.
    opened = pobj.open_audited_revision(
        locator,
        set_id="autonomy.vault.audited",
        key="anchore:scale-harness.ssh",
        setting_id="setting-abc",
        delegate_private_hex=priv_hex,
    )
    assert opened == {"value": SECRET}

    # A different delegate key fails closed — never plaintext.
    other_priv, _ = pobj.derive_delegate_audited_recipient(bytes(range(1, 33)))
    with pytest.raises(VaultError):
        pobj.open_audited_revision(
            locator,
            set_id="autonomy.vault.audited",
            key="anchore:scale-harness.ssh",
            setting_id="setting-abc",
            delegate_private_hex=other_priv,
        )
