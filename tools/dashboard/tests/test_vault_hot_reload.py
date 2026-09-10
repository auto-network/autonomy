"""Personal decryption keys survive graceful reload through the RAM carrier.

Old-format storage grants recover with the optional KEM key; new personal
values need only the audited recipient. Neither requires a signing delegate.
"""

from __future__ import annotations

import pytest

from tools.dashboard import unlock_routes as u
from tools.graph import settings_ops
from tools.network.idkit import KeyPair


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
