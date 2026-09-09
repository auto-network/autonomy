"""The vault comes up at unlock, or it does not come up at all.

The generation keys are opened in the BROWSER — the personal root never
reaches this process — so bringing the vault up means receiving content keys
over a route and installing the read and write seams with them. That route is
the difference between a vault that is built and a vault that works.

Every refusal here is about not half-installing: a wrong-length secret, an
empty set, or an unauthenticated caller each leave the process with no vault
rather than a vault that fails later at a read, where it would look like a
key-agreement problem instead of a wiring one.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import unlock_routes
from tools.graph import settings_ops
from tools.vault.personal_object import derive_delegate_audited_recipient
from tools.vault.store import VaultStore


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {})
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)
    app = Starlette(routes=[
        Route("/api/identity/unlock/vault-keys",
              unlock_routes.post_unlock_vault_keys, methods=["POST"]),
    ])
    with TestClient(app) as c:
        yield c
    settings_ops.set_vault_sealer(None)
    settings_ops.set_vault_key_holder(None)
    settings_ops.set_personal_delegate_audited_key(None)


@pytest.fixture
def unlocked(monkeypatch):
    monkeypatch.setattr(unlock_routes, "session_from_request",
                        lambda request: {"sid": "s-1", "method": "password"})


def _keys(n=1):
    return {f"state-{i}": ("%02x" % i) * 32 for i in range(n)}


# ── the gate ─────────────────────────────────────────────────


def test_without_a_session_the_vault_does_not_come_up(client, monkeypatch):
    """THE ONE THAT MATTERS. This runs AFTER proving possession of the root;
    a caller with no session has proved nothing, and the keys it offers could
    be anything."""
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda r: None)

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys()})

    assert response.status_code == 401
    assert settings_ops._vault_key_holder is None, "a read seam was installed"
    assert settings_ops._vault_sealer is None, "a write seam was installed"


# ── what it refuses rather than half-installing ──────────────


def test_an_empty_set_is_refused_when_the_store_HAS_sealed_content(
        client, unlocked, monkeypatch):
    """The browser opening no grants is a failure to report, not a vault to
    bring up half-way — but only where there were grants to open."""
    monkeypatch.setattr(unlock_routes, "_personal_store_has_generations",
                        lambda: True)
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {}})

    assert response.status_code == 400
    assert settings_ops._vault_key_holder is None


def test_an_empty_set_is_ACCEPTED_on_a_first_unlock(client, unlocked, monkeypatch):
    """THE ONE THAT MATTERS, and the reason a fresh identity could never have a
    vault at all.

    On a store where nothing has ever been sealed there are legitimately no
    grants: the sealer mints the first generation on the first write. The old
    rule could not tell that apart from a browser that failed to open grants it
    should have, so it refused the only state a new store can be in — and the
    single path to bringing the vault up rejected every new identity.
    """
    monkeypatch.setattr(unlock_routes, "_personal_store_has_generations",
                        lambda: False)
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {}})

    assert response.status_code == 200, response.text
    assert settings_ops._vault_key_holder is not None


def test_an_unreadable_key_control_store_does_not_block_a_first_unlock():
    """Fails to False deliberately: an unreadable store must not be the thing
    that stops a fresh identity from ever having a vault."""
    import tools.vault.db_content_store as dcs

    original = dcs.vault_db_path_for
    try:
        dcs.vault_db_path_for = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        assert unlock_routes._personal_store_has_generations() is False
    finally:
        dcs.vault_db_path_for = original


def test_a_wrong_length_secret_is_refused(client, unlocked):
    """Caching a 16-byte 'generation key' succeeds here and fails at a READ,
    where it looks like key agreement rather than a bad hand-off."""
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-1": "ab" * 16}})

    assert response.status_code == 400
    assert "32" in response.json()["error"]
    assert settings_ops._vault_key_holder is None


def test_non_hex_is_refused(client, unlocked):
    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-1": "zz" * 32}})

    assert response.status_code == 400
    assert settings_ops._vault_key_holder is None


# ── what it installs ─────────────────────────────────────────


def test_a_good_hand_off_installs_both_seams(client, unlocked):
    """Before: neither a read nor a write is possible in this process."""
    assert settings_ops._vault_key_holder is None
    assert settings_ops._vault_sealer is None

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys(2)})

    assert response.status_code == 200, response.text
    # `snapshot_persisted` is FALSE here, and truthfully so: this unlock sends
    # only generation_keys, so `_VAULT_CACHE["kem_private"]` is never set
    # (unlock_routes.py:1255 is inside `if kem_private_hex is not None`) and
    # `save_vault_across_hot_reload` requires all three parts. The process is
    # warm and the hand-off is NOT durable — previously indistinguishable from
    # outside, which is how the operator was told "unlocked" while the next
    # process booted locked.
    assert response.json() == {
        "ok": True, "generations": 2, "snapshot_persisted": False,
    }
    assert settings_ops._vault_key_holder is not None, "no read path"
    assert settings_ops._vault_sealer is not None, "no write path"


def test_unlock_publishes_and_warms_the_audited_delegate(
        client, unlocked, monkeypatch, tmp_path):
    """The browser-derived pair closes the cold-write/warm-read bootstrap."""
    db = tmp_path / "personal.db"
    monkeypatch.setattr("tools.vault.key_holder._scoped_db", lambda *_: db)
    private_hex, public_hex = derive_delegate_audited_recipient(bytes(range(32)))
    body = {
        "generation_keys": _keys(),
        "delegate_audited_private_key": private_hex,
        "delegate_audited_public_key": public_hex,
    }

    first = client.post("/api/identity/unlock/vault-keys", json=body)
    second = client.post("/api/identity/unlock/vault-keys", json=body)

    assert first.status_code == second.status_code == 200
    with VaultStore(db) as store:
        assert store.get_delegate_audited_recipient() == public_hex
    assert settings_ops._personal_delegate_audited_key == private_hex
    assert unlock_routes._VAULT_CACHE["audited_delegate"] == private_hex


def test_unlock_refuses_a_mismatched_audited_delegate_before_warming(
        client, unlocked, monkeypatch, tmp_path):
    db = tmp_path / "personal.db"
    monkeypatch.setattr("tools.vault.key_holder._scoped_db", lambda *_: db)
    private_hex, _ = derive_delegate_audited_recipient(bytes(range(32)))
    _, other_public = derive_delegate_audited_recipient(bytes(range(1, 33)))

    response = client.post("/api/identity/unlock/vault-keys", json={
        "generation_keys": _keys(),
        "delegate_audited_private_key": private_hex,
        "delegate_audited_public_key": other_public,
    })

    assert response.status_code == 400
    assert "does not match" in response.json()["error"]
    assert settings_ops._personal_delegate_audited_key is None
    assert "audited_delegate" not in unlock_routes._VAULT_CACHE


def test_unlock_refuses_root_rotation_before_any_warm_seam_changes(
        client, unlocked, monkeypatch, tmp_path):
    db = tmp_path / "personal.db"
    monkeypatch.setattr("tools.vault.key_holder._scoped_db", lambda *_: db)
    _, old_public = derive_delegate_audited_recipient(bytes(range(32)))
    new_private, new_public = derive_delegate_audited_recipient(bytes(range(1, 33)))
    with VaultStore(db) as store:
        store.put_delegate_audited_recipient(old_public)

    response = client.post("/api/identity/unlock/vault-keys", json={
        "generation_keys": _keys(),
        "delegate_audited_private_key": new_private,
        "delegate_audited_public_key": new_public,
    })

    assert response.status_code == 400
    assert "root rotation" in response.json()["error"]
    assert settings_ops._personal_delegate_audited_key is None
    assert settings_ops._vault_key_holder is None
    assert settings_ops._vault_sealer is None
    with VaultStore(db) as store:
        assert store.get_delegate_audited_recipient() == old_public


def test_a_second_organization_adds_to_the_same_cache(client, unlocked):
    """Unlocking another org must not drop the first one's keys — that would
    surface as an audited read failing for one org after unlocking another."""
    client.post("/api/identity/unlock/vault-keys",
                json={"generation_keys": {"state-a": "aa" * 32}})

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": {"state-b": "bb" * 32}})

    assert response.json()["generations"] == 2


def test_an_unlock_without_the_audited_recipient_still_fails_closed(
        client, unlocked):
    """Legacy clients cannot make audited writes look warm without the pair."""
    from tools.vault.errors import VaultError
    import tools.graph.schemas  # noqa: F401

    client.post("/api/identity/unlock/vault-keys",
                json={"generation_keys": _keys()})

    with pytest.raises(VaultError, match="delegate recipient"):
        settings_ops.add_setting(
            "autonomy.vault.audited", 1, "github.token",
            {"value": "x" * 40}, org=None,
        )


# ── every refusal names its gate in the log (auto-uhdxm) ─────────────
#
# The 2026-09-06 incident: a bring-up refusal left no server trace at all,
# so a completed sign-in with a dead vault was undiagnosable for an hour.
# Each pre-seat 400 must emit a warning naming its gate.


def _refusal_log(caplog):
    return [r.getMessage() for r in caplog.records
            if "vault bring-up refused" in r.getMessage()]


def test_wrong_length_refusal_is_logged(client, unlocked, caplog):
    with caplog.at_level("WARNING", logger="tools.dashboard.unlock_routes"):
        client.post("/api/identity/unlock/vault-keys",
                    json={"generation_keys": {"state-1": "ab" * 16}})
    assert any("generation-key-wrong-length" in m for m in _refusal_log(caplog))


def test_non_hex_refusal_is_logged(client, unlocked, caplog):
    with caplog.at_level("WARNING", logger="tools.dashboard.unlock_routes"):
        client.post("/api/identity/unlock/vault-keys",
                    json={"generation_keys": {"state-1": "zz" * 32}})
    assert any("generation-key-not-hex" in m for m in _refusal_log(caplog))


def test_body_shape_refusals_are_logged(client, unlocked, caplog):
    with caplog.at_level("WARNING", logger="tools.dashboard.unlock_routes"):
        client.post("/api/identity/unlock/vault-keys", json={"generation_keys": []})
        client.post("/api/identity/unlock/vault-keys",
                    json={"generation_keys": {"state-1": 7}})
        client.post("/api/identity/unlock/vault-keys",
                    json={"generation_keys": {},
                          "persona_kem_private_key": 12})
        client.post("/api/identity/unlock/vault-keys",
                    json={"generation_keys": {},
                          "delegate_signing_key": 12})
    messages = _refusal_log(caplog)
    assert any("generation-keys-shape" in m for m in messages)
    assert any("generation-keys-entry-shape" in m for m in messages)
    assert any("persona-kem-key-shape" in m for m in messages)
    assert any("delegate-signing-key-shape" in m for m in messages)


def test_audited_recipient_refusal_is_logged(client, unlocked, caplog):
    with caplog.at_level("WARNING", logger="tools.dashboard.unlock_routes"):
        response = client.post(
            "/api/identity/unlock/vault-keys",
            json={"generation_keys": _keys(),
                  "delegate_audited_private_key": "ab" * 32})
    assert response.status_code == 400
    assert any("audited-recipient" in m for m in _refusal_log(caplog))


def test_empty_set_with_sealed_content_refusal_is_logged(
    client, unlocked, monkeypatch, caplog,
):
    monkeypatch.setattr(unlock_routes, "_personal_store_has_generations",
                        lambda: True)
    with caplog.at_level("WARNING", logger="tools.dashboard.unlock_routes"):
        response = client.post("/api/identity/unlock/vault-keys",
                               json={"generation_keys": {}})
    assert response.status_code == 400
    assert any("no-generation-keys" in m for m in _refusal_log(caplog))


# ── Snapshot lifecycle (auto-0908 review requirements) ──────────────────


def _snapshot_env(monkeypatch, tmp_path):
    from tools.dashboard import unlock_routes as ur

    monkeypatch.setattr(ur, "_keycache_mount", lambda: tmp_path, raising=False)
    return ur


def test_a_successful_restore_consumes_the_snapshot(monkeypatch, tmp_path):
    """It must CLEAR on success. The clears were briefly moved out of `finally`
    and left after the success `return True`, making them unreachable — a
    consumed snapshot would then be re-applied by the next process."""
    ur = _snapshot_env(monkeypatch, tmp_path)
    cleared = []
    # 32 BYTES of hex: the audited half is fed to X25519PrivateKey, which
    # rejects anything shorter. A 1-byte fixture made the real function return
    # False and the test read as a consume failure.
    monkeypatch.setattr(ur, "_keycache_read", lambda name: b"aa" * 32)
    monkeypatch.setattr(ur, "_keycache_clear", lambda name: cleared.append(name))
    monkeypatch.setattr(ur, "_bring_vault_up", lambda *a, **k: None)
    monkeypatch.setattr(ur, "_install_personal_audited_delegate", lambda *a: None)
    monkeypatch.setattr(ur, "_ensure_sealed_settings_pepper", lambda: None)
    monkeypatch.setattr(
        ur, "_validate_audited_delegate_pair", lambda a, b: (a, b))

    class _KC:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        accepted_grants = staticmethod(lambda: {})
        states = {}

    import sys, types
    fake = types.ModuleType("m")
    monkeypatch.setitem(sys.modules, "tools.network.storagekit.keycontrol",
                        types.SimpleNamespace(KeyControlStore=lambda p: _KC()))
    monkeypatch.setitem(sys.modules, "tools.vault.db_content_store",
                        types.SimpleNamespace(vault_db_path_for=lambda o: ":memory:"))
    monkeypatch.setitem(sys.modules, "tools.vault.unlock",
                        types.SimpleNamespace(open_generation_keys=lambda *a: {}))

    assert ur.restore_vault_across_hot_reload() is True
    assert len(cleared) == 3, "a consumed snapshot must be cleared"


def test_a_failed_apply_retains_a_complete_snapshot(monkeypatch, tmp_path):
    """THE ONE THAT MATTERS. A COMPLETE snapshot whose application failed must
    survive: the failure can be transient, and clearing it destroyed the only
    carrier and made it permanent."""
    ur = _snapshot_env(monkeypatch, tmp_path)
    cleared = []
    monkeypatch.setattr(ur, "_keycache_read", lambda name: b"aa" * 32)
    monkeypatch.setattr(ur, "_keycache_clear", lambda name: cleared.append(name))

    import sys, types
    def _boom(_p):
        raise RuntimeError("grants store briefly unavailable")
    monkeypatch.setitem(sys.modules, "tools.network.storagekit.keycontrol",
                        types.SimpleNamespace(KeyControlStore=_boom))
    monkeypatch.setitem(sys.modules, "tools.vault.db_content_store",
                        types.SimpleNamespace(vault_db_path_for=lambda o: ":memory:"))
    monkeypatch.setitem(sys.modules, "tools.vault.unlock",
                        types.SimpleNamespace(open_generation_keys=lambda *a: {}))

    assert ur.restore_vault_across_hot_reload() is False
    assert cleared == [], "a complete snapshot must survive a failed apply"


def test_a_partial_snapshot_is_still_cleared(monkeypatch, tmp_path):
    """NEGATIVE CONTROL: a partial set is not a hand-off and must never be
    left for a later process to mistake for one."""
    ur = _snapshot_env(monkeypatch, tmp_path)
    cleared = []
    monkeypatch.setattr(
        ur, "_keycache_read",
        lambda name: b"aa" if name == ur._HOTRELOAD_DELEGATE else None)
    monkeypatch.setattr(ur, "_keycache_clear", lambda name: cleared.append(name))

    assert ur.restore_vault_across_hot_reload() is False
    assert len(cleared) == 3, "a partial snapshot must be cleared"


def test_the_unlock_call_writes_the_snapshot_and_reports_it(
    monkeypatch, client, unlocked
):
    """Root's requirement: prove the UNLOCK path itself writes and reports it,
    not merely that existing tests stay green.

    Before this, the only production writer was the graceful-shutdown hook, so
    an unlock left nothing behind and the response said `ok` either way. Both
    halves are asserted: that save was invoked, and that the response tells the
    caller whether it succeeded.
    """
    from tools.dashboard import unlock_routes as ur

    calls = []
    monkeypatch.setattr(
        ur, "save_vault_across_hot_reload",
        lambda: (calls.append(True) or True))

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys(2)})

    assert response.status_code == 200, response.text
    assert calls == [True], "the unlock must persist the carrier itself"
    assert response.json()["snapshot_persisted"] is True


def test_a_failed_snapshot_write_is_reported_not_hidden(
    monkeypatch, client, unlocked
):
    """NEGATIVE CONTROL. A warm-but-not-durable unlock must say so. The UI
    reporting plain success while nothing was persisted is what made repeated
    unlock requests look reasonable to everyone involved."""
    from tools.dashboard import unlock_routes as ur

    monkeypatch.setattr(ur, "save_vault_across_hot_reload", lambda: False)

    response = client.post("/api/identity/unlock/vault-keys",
                           json={"generation_keys": _keys(2)})

    assert response.status_code == 200
    assert response.json()["snapshot_persisted"] is False
