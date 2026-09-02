"""A graceful hot reload keeps the vault warm; a crash does not (auto-a1pub).

The shutdown hook hands the delegate + the persona KEM private key to the ramfs
key cache; the startup hook re-derives the generation keys from the on-disk
grants with the KEM key, installs the delegate, and clears the files. The KEM
key is held (§12) because it is what opens grants that arrive after unlock —
including ones minted on another machine and synced in.
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
    """Install a warm vault (delegate + KEM key) and capture the re-derivation."""
    delegate = KeyPair.generate()
    kem_private = "e" * 64
    audited_delegate = "d" * 64
    u._VAULT_CACHE["delegate"] = delegate
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
    return delegate, kem_private, audited_delegate, captured


def test_graceful_reload_re_warms_from_the_two_keys(tmp_path, monkeypatch):
    delegate, kem_private, audited_delegate, captured = _warm(monkeypatch)

    assert u.save_vault_across_hot_reload() is True
    u._VAULT_CACHE.clear()  # the reload: the in-memory cache dies
    assert u.restore_vault_across_hot_reload() is True

    # The two keys crossed, and the KEM key drove the generation re-derivation.
    assert captured["delegate_hex"] == delegate.private_hex
    assert captured["kem_hex"] == kem_private
    assert captured["generation_keys"] == {"a" * 64: b"\x01" * 32}
    # And the KEM key is retained for the NEXT reload.
    assert u._VAULT_CACHE.get("kem_private") == kem_private
    assert u._VAULT_CACHE.get("audited_delegate") == audited_delegate
    assert settings_ops._personal_delegate_audited_key == audited_delegate

    # The snapshot files are consumed on load — nothing lingers on ramfs.
    assert u._keycache_read(u._HOTRELOAD_DELEGATE) is None
    assert u._keycache_read(u._HOTRELOAD_KEM) is None
    assert u._keycache_read(u._HOTRELOAD_AUDITED_DELEGATE) is None


def test_a_crash_leaves_nothing_and_boots_locked(monkeypatch):
    # No warm cache (a locked or crashed process): nothing is written, and a
    # boot finds nothing to restore — the vault stays locked.
    assert u.save_vault_across_hot_reload() is False
    assert u.restore_vault_across_hot_reload() is False


def test_without_a_held_kem_key_nothing_is_saved(tmp_path, monkeypatch):
    # A delegate but no KEM key (e.g. a browser-handoff unlock that never sent
    # one) cannot support the fleet reload, so it saves nothing rather than a
    # half-warm snapshot.
    u._VAULT_CACHE["delegate"] = KeyPair.generate()
    assert u.save_vault_across_hot_reload() is False
    assert u._keycache_read(u._HOTRELOAD_DELEGATE) is None
    assert u._keycache_read(u._HOTRELOAD_KEM) is None
