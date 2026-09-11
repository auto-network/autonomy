"""Personal decryption keys survive graceful reload through the RAM carrier.

Old-format storage grants recover with the optional KEM key; new personal
values need only the audited recipient. Neither requires a signing delegate.
"""

from __future__ import annotations

import pytest

from tools.dashboard import unlock_routes as u
from tools.graph import settings_ops
from tools.network.idkit import KeyPair
from tools.network.storagekit.memory_cache import assert_memory_backed as real_memory_guard


@pytest.fixture(autouse=True)
def _ramfs(tmp_path, monkeypatch):
    # A temp dir stands in for the ramfs key cache; neuter the ramfs guard so
    # the test runs headless (the guard itself is covered by memory_cache).
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path))
    # restore opens KeyControlStore(vault_db_path_for(None)); give it a temp
    # personal store so resolution doesn't refuse (open_generation_keys itself
    # is stubbed, so the store's contents don't matter).
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    (tmp_path / "orgs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "tools.network.storagekit.memory_cache.assert_memory_backed",
        lambda *a, **k: None,
    )
    u._VAULT_CACHE.clear()
    settings_ops.set_personal_delegate_audited_key(None)
    yield
    u._VAULT_CACHE.clear()
    settings_ops.set_personal_delegate_audited_key(None)


def _warm(monkeypatch):
    """Install personal decryption material and capture old-data recovery."""
    kem_private = "e" * 64
    audited_delegate = "d" * 64
    u._VAULT_CACHE["kem_private"] = kem_private
    u._VAULT_CACHE["audited_delegate"] = audited_delegate

    captured: dict = {}

    # Restore re-derives the generation keys through open_generation_keys +
    # _bring_vault_up; stub both, capturing what the KEM key drives.
    def fake_open(kem_hex, grants, states):
        captured["kem_hex"] = kem_hex
        return {"a" * 64: b"\x01" * 32}

    def fake_bring_up(generation_keys, delegate_hex=None):
        captured["generation_keys"] = generation_keys
        captured["delegate_hex"] = delegate_hex
        return len(generation_keys)

    monkeypatch.setattr("tools.vault.unlock.open_generation_keys", fake_open)
    monkeypatch.setattr(u, "_bring_vault_up", fake_bring_up)
    return kem_private, audited_delegate, captured


def test_graceful_reload_recovers_old_content_without_a_signing_key(tmp_path, monkeypatch, caplog):
    import logging

    kem_private, audited_delegate, captured = _warm(monkeypatch)

    assert u.save_vault_across_hot_reload() is True
    u._VAULT_CACHE.clear()  # the reload: the in-memory cache dies
    with caplog.at_level(logging.INFO, logger=u.logger.name):
        assert u.restore_vault_across_hot_reload() is True

    # A successful re-warm announces itself, so a "cold key" diagnosis can be
    # checked against whether a hot-reload actually succeeded (no false positive).
    assert any(
        "keys successfully hot-reloaded" in r.getMessage()
        for r in caplog.records
    )

    # No signing key crossed; the KEM key drove old-generation recovery.
    assert captured["delegate_hex"] is None
    assert captured["kem_hex"] == kem_private
    assert captured["generation_keys"] == {"a" * 64: b"\x01" * 32}
    # And the KEM key is retained for the NEXT reload.
    assert u._VAULT_CACHE.get("kem_private") == kem_private
    assert u._VAULT_CACHE.get("audited_delegate") == audited_delegate
    assert settings_ops._personal_delegate_audited_key == audited_delegate

    # The snapshot files are consumed on load — nothing lingers on ramfs.
    assert u._keycache_read("vault.hotreload.delegate") is None
    assert u._keycache_read(u._HOTRELOAD_KEM) is None
    assert u._keycache_read(u._HOTRELOAD_AUDITED_DELEGATE) is None


def test_a_crash_leaves_nothing_and_boots_locked(monkeypatch):
    # No warm cache (a locked or crashed process): nothing is written, and a
    # boot finds nothing to restore — the vault stays locked.
    assert u.save_vault_across_hot_reload() is False
    assert u.restore_vault_across_hot_reload() is False


def test_a_signing_key_without_a_personal_recipient_is_not_a_warm_vault(tmp_path, monkeypatch):
    # An organization signing key is not a personal decryption recipient.
    u._VAULT_CACHE["delegate"] = KeyPair.generate()
    assert u.save_vault_across_hot_reload() is False
    assert u._keycache_read("vault.hotreload.delegate") is None
    assert u._keycache_read(u._HOTRELOAD_KEM) is None


def test_personal_recipient_alone_survives_reload(tmp_path, monkeypatch):
    """New personal vaults have no ledger, signing delegate, or generation KEM."""
    u._VAULT_CACHE["audited_delegate"] = "d" * 64
    monkeypatch.setattr(u, "_personal_store_has_generations", lambda: False)
    assert u.save_vault_across_hot_reload() is True
    u._VAULT_CACHE.clear()
    assert u.restore_vault_across_hot_reload() is True
    assert settings_ops._personal_delegate_audited_key == "d" * 64
    assert "delegate" not in u._VAULT_CACHE
    assert "kem_private" not in u._VAULT_CACHE


def test_org_map_survives_when_one_org_cannot_be_resolved(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools.graph import org_ops
    import json
    u._VAULT_CACHE['audited_delegate'] = 'd' * 64
    keys = {'a' * 64: {'b' * 64: 'c' * 64}, 'e' * 64: {'f' * 64: '1' * 64}}
    u._VAULT_CACHE['organization_kem_keys'] = keys
    monkeypatch.setattr(org_ops, 'list_orgs', lambda: [SimpleNamespace(slug='unavailable')])
    monkeypatch.setattr(u, '_personal_store_has_generations', lambda: False)
    assert u.save_vault_across_hot_reload()
    assert json.loads(u._keycache_read(u._HOTRELOAD_ORGANIZATION_KEM)) == keys
    u._VAULT_CACHE.clear()
    assert u.restore_vault_across_hot_reload()
    assert settings_ops._personal_delegate_audited_key == 'd' * 64
    assert u._VAULT_CACHE['organization_kem_keys'] == keys
    assert u._VAULT_CACHE['cache'].secrets == {}  # No grants yet is normal.
    assert not (tmp_path / 'orgs/unavailable.db').exists()
    assert u._keycache_read(u._HOTRELOAD_ORGANIZATION_KEM) is None


def test_malformed_org_entry_does_not_erase_personal_or_healthy_key(monkeypatch):
    import json
    from tools.graph import org_ops
    monkeypatch.setattr(org_ops, 'list_orgs', lambda: [])
    u._VAULT_CACHE['audited_delegate'] = 'd' * 64
    u._VAULT_CACHE['organization_kem_keys'] = {'a' * 64: {'b' * 64: 'c' * 64}}
    assert u.save_vault_across_hot_reload()
    u._keycache_write(u._HOTRELOAD_ORGANIZATION_KEM, json.dumps({
        'bad-scope': {}, 'a' * 64: {'b' * 64: 'c' * 64, 'f' * 64: 'bad-private'},
    }).encode())
    u._VAULT_CACHE.clear()
    assert u.restore_vault_across_hot_reload()
    assert settings_ops._personal_delegate_audited_key == 'd' * 64
    assert u._VAULT_CACHE['organization_kem_keys'] == {'a' * 64: {'b' * 64: 'c' * 64}}


def test_org_key_snapshot_refuses_disk_with_real_memory_guard(tmp_path, monkeypatch):
    monkeypatch.setattr('tools.network.storagekit.memory_cache.assert_memory_backed', real_memory_guard)
    u._VAULT_CACHE['audited_delegate'] = 'd' * 64
    u._VAULT_CACHE['organization_kem_keys'] = {'a' * 64: {'b' * 64: 'c' * 64}}
    assert not u.save_vault_across_hot_reload()
    assert not list(tmp_path.glob('vault.hotreload.*'))


def test_one_org_store_failure_does_not_block_another_org_restore(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from tools.graph import org_ops
    from tools.vault.tests.test_unlock import _one_grant
    grant, private, descriptor, secret = _one_grant()
    u._VAULT_CACHE['audited_delegate'] = 'd' * 64
    u._VAULT_CACHE['organization_kem_keys'] = {
        descriptor.genesis_id: {grant.recipient_kem_key_id: private}}
    assert u.save_vault_across_hot_reload()
    u._VAULT_CACHE.clear()
    monkeypatch.setattr(u, '_personal_store_has_generations', lambda: False)
    monkeypatch.setattr(u, '_ensure_sealed_settings_pepper', lambda: None)
    monkeypatch.setattr(org_ops, 'list_orgs', lambda: [
        SimpleNamespace(slug='broken'), SimpleNamespace(slug='healthy')])
    for slug in ('broken', 'healthy'):
        (tmp_path / 'orgs' / f'{slug}.db').touch()
    class Ledger:
        def __init__(self, path): self.ledger = SimpleNamespace(genesis_id=descriptor.genesis_id)
        def __enter__(self): return self
        def __exit__(self, *_): pass
    class Store:
        def __init__(self, path):
            if 'broken' in str(path): raise ValueError('unavailable org store')
        def __enter__(self): return self
        def __exit__(self, *_): pass
        states = {descriptor.state_id: descriptor}
        def accepted_grants(self): return (grant,)
    monkeypatch.setattr('tools.network.ledger.LedgerStore', Ledger)
    monkeypatch.setattr('tools.network.storagekit.keycontrol.KeyControlStore', Store)
    assert u.restore_vault_across_hot_reload()
    assert settings_ops._personal_delegate_audited_key == 'd' * 64
    assert u._VAULT_CACHE['cache'].secrets == {descriptor.state_id: secret}
