"""The key-control store, opened before its database is fleet-activated,
captures the write it makes after another connection activates it (the
ActivationWatch left by attach_active_production_catalog; live failure and
proof shape in tools/vault/tests/test_store_activation_race.py)."""

from __future__ import annotations

from tools.network.fleet_sync.catalog import ActivationWatch, MutationCatalog
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault.tests.test_store_activation_race import activate, assert_live_captures


def test_credential_write_after_activation_by_another_connection(world, tmp_path):
    path = tmp_path / "org.db"
    recipient = world.member(1)
    credential = world.principals[recipient.public_hex]["credential"]
    with KeyControlStore(path) as store:
        assert isinstance(store.db._hook(), ActivationWatch)
        activate(path)
        store.accept_credential(credential)
        assert isinstance(store.db._hook(), MutationCatalog)
        assert store.get_credential(credential.kem_key_id) == credential
    assert_live_captures(path, at_least=1, distinct_transactions=1)
