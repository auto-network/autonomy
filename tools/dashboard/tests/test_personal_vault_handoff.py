"""Real browser handoff, Settings encryption, and reload with no personal ledger.

For old data, an in-memory ledger constructs a historical storage-format value.
Only its ciphertext, descriptors, credential, and grants reach personal.db.
Every LedgerStore open is then forbidden during sign-in and recovery.
"""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import unlock_routes as u
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import LedgerStore
from tools.network.ledger.found import found_org_ledger
from tools.network.storagekit.credentials import derive_kem_seed
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault import personal_object
from tools.vault.key_holder import VaultKeyCache, build_key_holder
from tools.vault.key_sealer import build_vault_sealer
from tools.vault.storage_object import open_revision_for_member


JS = r"""
import { readFileSync } from 'node:fs';
const input = JSON.parse(readFileSync(0, 'utf8'));
const { wakeVault } = await import(input.module);
let handoff;
const fetchImpl = async (url, options = {}) => {
  const reply = value => ({ ok: true, status: 200, json: async () => value });
  if (url === '/api/identity/vault-anchors') return reply({
    anchors: [], classes: [{ governance: { form: 'root-reachable' } }],
  });
  if (url !== '/api/identity/unlock/vault-keys') throw Error('unexpected call: ' + url);
  if (options.method !== 'POST') return reply(input.recovery);
  handoff = JSON.parse(options.body);
  return reply({ ok: true });
};
const result = await wakeVault({
  personalRootSeed: Uint8Array.from(Buffer.from(input.seed, 'hex')), fetchImpl,
});
if (!result.ready) throw Error(JSON.stringify(result));
console.log(JSON.stringify(handoff));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("old_data", [False, True])
def test_personal_signin_and_reload_without_a_ledger(tmp_path, monkeypatch, old_data):
    GraphDB.close_all_pooled()
    db = tmp_path / "personal.db"
    GraphDB.create_org_db("personal", type_="personal", path=db).close()
    monkeypatch.setattr("tools.vault.db_content_store.vault_db_path_for", lambda *_: db)
    monkeypatch.setattr("tools.vault.key_holder._scoped_db", lambda *_: db)
    monkeypatch.setenv("AUTONOMY_KEYCACHE_MOUNT", str(tmp_path / "ramfs"))
    (tmp_path / "ramfs").mkdir()
    monkeypatch.setattr("tools.network.storagekit.memory_cache.assert_memory_backed",
                        lambda *_: None)
    monkeypatch.setattr(u, "_VAULT_CACHE", {})
    monkeypatch.setattr(settings_ops, "_vault_key_holder", None)
    monkeypatch.setattr(settings_ops, "_vault_sealer", None)
    monkeypatch.setattr(settings_ops, "_personal_delegate_audited_key", None)
    monkeypatch.setattr(u, "session_from_request", lambda _: {"sid": "test"})
    monkeypatch.setattr(u, "_ensure_sealed_settings_pepper", lambda: None)
    monkeypatch.setattr("tools.dashboard.service_certificate_manager.request_reconcile",
                        lambda: None)
    root = KeyPair.generate()
    seed = bytes.fromhex(root.private_hex)
    old_locator = None
    if old_data:
        # Fixture construction only: recreate what the retired writer persisted.
        with LedgerStore() as ledger:
            founded = found_org_ledger(
                ledger, org_id="historical-personal", org_root=root,
                personal_root_seed=seed, now=1_800_000_000_000,
                kem_seed=derive_kem_seed(seed),
            )
            founder = derive_persona(seed, founded.genesis_id)
            with KeyControlStore(db) as kc:
                kc.accept_credential(founded.kem_credential)
            seal = build_vault_sealer(
                VaultKeyCache(), lambda: founder,
                lambda *_: (ledger.fold(),
                            lambda heads: ledger.fold(heads=list(heads)),
                            ledger.ledger.ancestry),
            )
            old_locator = seal(
                set_id="autonomy.vault.audited", schema_revision=1,
                key="old", setting_id="old-row", payload={"value": "old secret"},
                tier="audited", org=None,
            )

    ledger_calls = []
    def no_ledger(*args, **kwargs):
        ledger_calls.append((args, kwargs))
        raise AssertionError("personal sign-in must not open an authority ledger")

    monkeypatch.setattr("tools.network.ledger.LedgerStore", no_ledger)
    monkeypatch.setattr("tools.network.ledger.store.LedgerStore", no_ledger)
    app = Starlette(routes=[
        Route("/api/identity/unlock/vault-keys", u.get_personal_vault_recovery,
              methods=["GET"]),
        Route("/api/identity/unlock/vault-keys", u.post_unlock_vault_keys,
              methods=["POST"]),
    ])
    module = Path(__file__).resolve().parents[1] / "static/js/ceremony/vault-unlock.js"
    with TestClient(app) as client:
        recovery = client.get("/api/identity/unlock/vault-keys")
        assert recovery.status_code == 200, recovery.text
        result = subprocess.run(
            ["node", "--input-type=module", "-e", JS],
            input=json.dumps({"module": module.as_uri(), "seed": root.private_hex,
                              "recovery": recovery.json()}),
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        handoff = json.loads(result.stdout)
        # The actual headless client must supply exactly the browser's keys.
        from tools.vault import warm_client
        monkeypatch.setattr(warm_client, "unlock", lambda _: root)
        def headless_call(path, body=None, **kwargs):
            assert path == "/api/identity/unlock/vault-keys"
            return recovery.json() if body is None else body
        monkeypatch.setattr(warm_client, "call", headless_call)
        assert warm_client.warm("fixture-password") == handoff
        assert "delegate_signing_key" not in handoff
        assert "kem_credential" not in handoff
        assert bool(handoff.get("persona_kem_private_key")) == old_data
        response = client.post("/api/identity/unlock/vault-keys", json=handoff)
        assert response.status_code == 200, response.text
        assert response.json()["snapshot_persisted"] is True

    locator = settings_ops._seal_vault_payload(
        set_id="autonomy.vault.audited", schema_revision=1, key="new",
        setting_id="new-row", payload={"value": "new secret"}, tier="audited", org=None,
    )
    u._VAULT_CACHE.clear()
    settings_ops.set_personal_delegate_audited_key(None)
    assert u.restore_vault_across_hot_reload() is True
    assert "delegate" not in u._VAULT_CACHE
    assert personal_object.open_audited_revision(
        locator, set_id="autonomy.vault.audited", key="new", setting_id="new-row",
        delegate_private_hex=settings_ops._personal_delegate_audited_key,
    ) == {"value": "new secret"}
    if old_locator:
        control = build_key_holder(u._VAULT_CACHE["cache"])(
            set_id="autonomy.vault.audited", org=None,
        )
        assert open_revision_for_member(
            old_locator, holdings=control.holdings, content_store=control.content_store,
        ) == {"value": "old secret"}
    assert ledger_calls == []
    GraphDB.close_all_pooled()


def test_recovery_metadata_requires_session(monkeypatch):
    monkeypatch.setattr(u, "session_from_request", lambda _: None)
    with TestClient(Starlette(routes=[Route("/keys", u.get_personal_vault_recovery)])) as c:
        assert c.get("/keys").status_code == 401
