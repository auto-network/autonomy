"""Personal owner-at-rest secured Settings: no organization outer layer."""

from __future__ import annotations

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
from tools.vault.personal_object import is_personal_locator
from tools.vault.store import VaultStore


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
    policy_class = create_class(
        PASSWORD_POLICY, [factor.published], created_at="2026-08-24T00:00:00Z"
    )
    with VaultStore(db_path) as store:
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
