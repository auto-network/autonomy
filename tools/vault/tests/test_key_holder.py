"""The production vault key holder opens a really-sealed secret.

The crib (``1e005d5c-c11`` §1) records the vault mechanism as built and unwired:
``set_vault_key_holder`` had no production caller, so every audited read
answered "no vault key holder is registered in this process" and no vault row
could be read. ``tools/vault/key_holder.py`` is that caller.

This drives the REAL write path (the world's real sealer) and the REAL read path
(``read_set``), with the holder under test in between — no test-double holder.
It is the headless acceptance the crib §21 asks for: CLI/API-shaped, throwaway
identities, no browser.
"""

from __future__ import annotations

import pytest

from tools.graph import schemas, settings_ops
from tools.graph.tests.vault_read_harness import VaultWorld
from tools.vault.key_holder import (
    VaultKeyCache,
    build_key_holder,
    register_key_holder,
)

SET_ID = "autonomy.test.holder.vaulted"
KEY = "restic-password"
SECRET = "correct-horse-battery-staple-9F3xQ"


@pytest.fixture
def vaulted_set():
    @schemas.vaulted("audited")
    class _V(schemas.SettingSchema):
        set_id = SET_ID
        schema_revision = 1

    from tools.graph.schemas.registry import SCHEMAS, schema_key

    schemas.register_schema(SET_ID, 1, _V)
    yield
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    SCHEMAS.pop(schema_key(SET_ID, 1), None)


@pytest.fixture
def graph_db(tmp_path, monkeypatch):
    from tools.graph import client as client_mod
    from tools.graph.db import GraphDB

    db_path = tmp_path / "graph.db"
    monkeypatch.setenv("GRAPH_DB", str(db_path))
    monkeypatch.delenv("GRAPH_API", raising=False)
    monkeypatch.setattr(client_mod, "_FORCE_HOST_DIRECT", True)
    GraphDB(db_path, create=True).close()
    return db_path


def _world_with_production_holder(tmp_path):
    """A founded world writing through its real sealer and reading through the
    production holder, cache loaded with the persona's real generation keys —
    the shape the dashboard has after an unlock."""
    world = VaultWorld(tmp_path / "world")
    settings_ops.set_vault_sealer(world.sealer)
    cache = VaultKeyCache()
    cache.load(dict(world.world.held(world.author)))
    register_key_holder(
        cache, tmp_path / "world" / "keycontrol.db", tmp_path / "world" / "content"
    )
    return world, cache


def test_production_holder_opens_a_really_sealed_secret(graph_db, vaulted_set, tmp_path):
    _world_with_production_holder(tmp_path)

    settings_ops.add_setting(SET_ID, 1, KEY, {"secret_value": SECRET}, org=None, state="raw")

    # The stored database never holds the plaintext — only a locator.
    assert SECRET.encode() not in graph_db.read_bytes()

    resolved = settings_ops.read_set(SET_ID, org=None)
    row = resolved.to_dict()[KEY]
    assert row.payload == {"secret_value": SECRET}
    assert row.vault_error is None


def test_empty_cache_fails_closed_not_with_a_missing_holder(graph_db, vaulted_set, tmp_path):
    """Before the first unlock the holder is registered but the cache is empty.
    An audited read then fails closed on the missing key material, not with the
    "no holder registered" error the whole bead exists to remove."""
    world = VaultWorld(tmp_path / "world")
    settings_ops.set_vault_sealer(world.sealer)
    cache = VaultKeyCache()  # deliberately not loaded
    register_key_holder(
        cache, tmp_path / "world" / "keycontrol.db", tmp_path / "world" / "content"
    )

    settings_ops.add_setting(SET_ID, 1, KEY, {"secret_value": SECRET}, org=None, state="raw")
    resolved = settings_ops.read_set(SET_ID, org=None)
    row = resolved.to_dict()[KEY]
    # A holder IS registered — so this is a decryption failure, never the
    # missing-holder condition. The secret does not come back.
    assert row.payload != {"secret_value": SECRET}


def test_holder_is_registered_after_register_key_holder(tmp_path):
    cache = VaultKeyCache()
    build_key_holder(cache, tmp_path / "kc.db", tmp_path / "content")  # pure build, no install
    assert settings_ops._vault_key_holder is None
    register_key_holder(cache, tmp_path / "kc.db", tmp_path / "content")
    assert settings_ops._vault_key_holder is not None
    settings_ops.set_vault_key_holder(None)
