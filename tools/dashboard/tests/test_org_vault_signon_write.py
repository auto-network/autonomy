"""Root-unlock key handoff must enable an organization-homed Settings write.

Unlike the storage lifecycle tests, this test never supplies the sealer's
author. It runs encrypted preparation and the browser's three sign-in phases, bridges
their requests to real dashboard handlers, then writes through Settings.
Human-factor authentication is a precondition; registry maintenance and the
personal policy-class inventory are fixtures. No real service is contacted.
"""

from __future__ import annotations

import json
import selectors
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import link_channel_key, network_routes, unlock_routes, org_storage_delegate, signon_preparation
from tools.graph import settings_ops, org_ops
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.storagekit.credentials import derive_kem_seed


ORG = "vault-signon-org"
JS_ROOT = Path(__file__).resolve().parents[1] / "static/js"

# JSON-lines transports fetch requests to TestClient without a listening server.
# Private test material stays on pipes, never argv, environment, or disk.
CEREMONY = r"""
import { createInterface } from 'node:readline';
const input = createInterface({ input: process.stdin })[Symbol.asyncIterator]();
const terms = JSON.parse((await input.next()).value);
let rootOpen = false;
let transportQueue = Promise.resolve();
function fetchImpl(url, options = {}) {
  if (rootOpen) throw new Error('network while root is open');
  // The test's single JSON-lines pipe has one response reader. Serialize
  // diagnostic and handoff requests so fire-and-forget reporting cannot
  // consume the next request's reply or fill Python's text read buffer.
  const pending = transportQueue.then(async () => {
    process.stdout.write(JSON.stringify({url, method: options.method || 'GET',
      headers: options.headers || {}, body: options.body || null}) + '\n');
    const response = JSON.parse((await input.next()).value);
    return { status: response.status, ok: response.status < 300,
      json: async () => response.body };
  });
  transportQueue = pending.catch(() => {});
  return pending;
}
globalThis.window = { fetch: fetchImpl };
await import(terms.signonModule);
const phases = await import(terms.phasesModule);
const encrypted = await phases.fetchPreparation(fetchImpl);
const seed = Uint8Array.from(Buffer.from(terms.seed, 'hex'));
let prepared;
rootOpen = true;
const forbidden = () => { throw new Error('network while root is open'); };
globalThis.window.fetch = forbidden;
globalThis.fetch = forbidden;
try {
  prepared = await phases.prepareSignon(seed, encrypted, window.AutonomyNetworkSession);
} finally { seed.fill(0); rootOpen = false; }
globalThis.window.fetch = fetchImpl;
globalThis.fetch = fetchImpl;
await phases.submitSignon(prepared, fetchImpl);
process.stdout.write(JSON.stringify({done: true}) + '\n');
process.exit(0);
"""


def _run_ceremony(client, terms):
    with subprocess.Popen(
        ["node", "--input-type=module", "-e", CEREMONY],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    ) as proc:
        try:
            proc.stdin.write(json.dumps(terms) + "\n")
            proc.stdin.flush()
            deadline = time.monotonic() + 30
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ)
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and selector.select(remaining), "ceremony timed out"
                    line = proc.stdout.readline()
                    if not line:
                        raise AssertionError("ceremony exited before completion: " + proc.stderr.read())
                    message = json.loads(line)
                    if message.get("done"):
                        break
                    response = client.request(
                        message["method"], message["url"],
                        headers=message["headers"], content=message["body"],
                    )
                    # Do not include request bodies: they carry ephemeral keys.
                    assert response.status_code < 300, (
                        f'{message["method"]} {message["url"]}: {response.status_code}'
                    )
                    if message["url"] == "/api/identity/unlock/vault-keys":
                        # Replay the exact browser-signed grants, without
                        # reopening the root or generating another key.
                        for item in json.loads(message["body"]).get("organization_delegates", []):
                            org = item["organization"]
                            before = org_storage_delegate.prepare(org)["delegate_metadata"]
                            from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
                            chain = settings_ops.chain_setting(
                                VAULT_AUDITED_SET_ID, before["key_reference"], org=None)
                            with LedgerStore(org_ledger_db_path(org)) as ledger:
                                ids = ledger.ledger.all_ids()
                            org_storage_delegate.accept(item)
                            assert org_storage_delegate.prepare(org)["delegate_metadata"] == before
                            assert settings_ops.chain_setting(
                                VAULT_AUDITED_SET_ID, before["key_reference"], org=None) == chain
                            with LedgerStore(org_ledger_db_path(org)) as ledger:
                                assert ledger.ledger.all_ids() == ids
                    proc.stdin.write(json.dumps({
                        "status": response.status_code, "body": response.json(),
                    }) + "\n")
                    proc.stdin.flush()
            assert proc.wait(timeout=5) == 0, proc.stderr.read()
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not on PATH")
@pytest.mark.parametrize("unavailable_org", [False, True])
@pytest.mark.parametrize("has_org_key", [False, True])
def test_root_unlock_enables_organization_channel_key_write(tmp_path, monkeypatch, unavailable_org, has_org_key):
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    # Empty initial process state, NOT a supplied signing key or author mock.
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {})
    monkeypatch.setattr(settings_ops, "_vault_sealer", None)
    monkeypatch.setattr(settings_ops, "_vault_key_holder", None)
    monkeypatch.setattr(settings_ops, "_personal_delegate_audited_key", None)
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda request: {"sid": "test"})
    # Reload durability and certificate renewal are outside this regression.
    monkeypatch.setattr(unlock_routes, "save_vault_across_hot_reload", lambda: False)
    from tools.dashboard import service_certificate_manager
    monkeypatch.setattr(service_certificate_manager, "request_reconcile", lambda: None)
    root = KeyPair.generate()
    organization_root = KeyPair.generate()
    seed = bytes.fromhex(root.private_hex)
    now = int(time.time() * 1000) - 1000
    founded = {}
    try:
        for org in ("personal", ORG):
            GraphDB.create_org_db(
                org, type_="personal" if org == "personal" else "shared",
                path=tmp_path / "orgs" / f"{org}.db",
            ).close()
            if org == "personal":
                continue
            with LedgerStore(org_ledger_db_path(org)) as ledger:
                founded[org] = found_org_ledger(
                    ledger, org_id=org, org_root=organization_root,
                    personal_root_seed=seed, now=now,
                    kem_seed=derive_kem_seed(seed),
                )

        async def existing_personal_class(request):
            return JSONResponse({"anchors": [], "classes": [{
                "governance": {"form": "root-reachable"},
            }]})

        async def reported_failure(request):
            return JSONResponse({"ok": True})

        from tools.dashboard import membership_checkpoint as cp
        from tools.network.ledger.membership_commitment import validate_checkpoint
        checkpoints = []

        async def registry_acceptance(request):
            # Registry transport is the fixture; validate the real browser
            # signature and record adoption through the existing helper.
            record = (await request.json())["record"]
            validate_checkpoint(record, root_pub=organization_root.public_hex)
            cp.record_adopted(ORG, record)
            checkpoints.append(record)
            return JSONResponse({"ok": True})

        app = Starlette(routes=[
            Route("/api/identity/unlock/preparation", signon_preparation.get_preparation),
            Route("/api/identity/ceremony-error", reported_failure, methods=["POST"]),
            Route("/api/identity/vault-anchors", existing_personal_class),
            Route("/api/network/ledger/heads", network_routes.get_ledger_heads),
            Route("/api/network/ledger/delegate", network_routes.post_ledger_delegate, methods=["POST"]),
            Route("/api/network/unlock-report", network_routes.post_unlock_maintenance_report, methods=["POST"]),
            Route("/api/network/membership-checkpoint", registry_acceptance, methods=["POST"]),
            Route("/api/identity/unlock/vault-keys", unlock_routes.get_personal_vault_recovery, methods=["GET"]),
            Route("/api/identity/unlock/vault-keys", unlock_routes.post_unlock_vault_keys, methods=["POST"]),
        ])
        from tools.graph.schemas.network_identity import (
            NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION,
            NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION_2, ORG_ROOT_ARMOR_PURPOSE,
        )
        from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
        _, owner_pub = derive_encapsulation_keypair(seed, ORG_ROOT_ARMOR_PURPOSE)
        org_key_row = settings_ops.add_setting(NETWORK_ORG_KEY_SET_ID, NETWORK_ORG_KEY_REVISION_2, "default", {
            "root_pub": organization_root.public_hex,
            "sealed_root_key": seal(bytes.fromhex(organization_root.private_hex), owner_pub,
                                    ORG_ROOT_ARMOR_PURPOSE).hex(),
            "owner_kem_pub": owner_pub, "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
        }, org=ORG)
        if not has_org_key:
            # A second fleet machine has the org ledger and adopted checkpoint,
            # but does not receive the founder's identity-armor Setting.
            settings_ops.remove_setting(org_key_row, org=ORG)
            from tools.network.ledger import membership_commitment as mc
            with LedgerStore(org_ledger_db_path(ORG)) as ledger:
                state = ledger.fold()
                adopted = mc.build_root_checkpoint(
                    org="8a2d6c7a-498c-42ba-a4a6-b3b27a024bac", seq=0,
                    genesis_id=founded[ORG].genesis_id, ledger_head=ledger.heads()[0],
                    members_root_hex=mc.members_root(state),
                    checkpointers_root_hex=mc.checkpointers_root(state),
                    ts=int(time.time()), root=organization_root,
                )
            cp.record_adopted(ORG, adopted)
        settings_ops.add_setting(NETWORK_BINDING_SET_ID, NETWORK_BINDING_REVISION, "default", {
            "org_uuid": "8a2d6c7a-498c-42ba-a4a6-b3b27a024bac",
            "root_pub": organization_root.public_hex, "registry_url": "https://registry.invalid",
            "binding_expires_at": (datetime.now(timezone.utc) + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "recovery_policy": {"mode": "none"},
        }, org=ORG)
        from tools.vault.personal_object import derive_delegate_audited_recipient
        from tools.vault.key_holder import _scoped_db
        from tools.vault.store import VaultStore
        from tools.graph.schemas.vault_policy_class import VAULT_POLICY_CLASS_SET_ID
        _, public = derive_delegate_audited_recipient(seed)
        with VaultStore(_scoped_db(VAULT_POLICY_CLASS_SET_ID, None)) as store:
            store.put_delegate_audited_recipient(public)

        # Exercise the actual collector and persisted persona mapping. Only
        # unrelated fleet, registry and personal policy inventory are fixtures.
        from tools.dashboard import fleet_enrollment_routes as fleet, identity_routes
        from tools.dashboard import vault_routes, link_serving_supervisor
        org_ops._record_persona_setting(ORG, founded[ORG].genesis_id,
                                       founded[ORG].founder_persona_pub, source="found")
        monkeypatch.setattr(identity_routes, "_personal_member",
                            lambda: SimpleNamespace(payload={"root_pub": root.public_hex}))
        monkeypatch.setattr(vault_routes, "root_anchor_inventory", lambda: {
            "anchors": [], "classes": [{"governance": {"form": "root-reachable"}}]})
        monkeypatch.setattr(fleet, "runtime_preparation",
                            lambda: JSONResponse({"enabled": False}))
        monkeypatch.setattr(fleet, "_local_completion_state", lambda: (None, None, None))
        monkeypatch.setattr(link_serving_supervisor, "serve_cert_state", lambda org: {})
        monkeypatch.setattr(link_serving_supervisor, "serve_cert_requirement",
                            lambda org: {"required": False})
        if unavailable_org:
            real_plans = signon_preparation.organization_plans
            def plans_with_unavailable_org():
                yield {"slug": "unavailable-org", "error": "organization-preparation-unavailable"}, None
                yield from real_plans()
            monkeypatch.setattr(signon_preparation, "organization_plans", plans_with_unavailable_org)
        terms = {
            "seed": seed.hex(), "org": ORG,
            "signonModule": (JS_ROOT / "network-signon.mjs").as_uri(),
            "phasesModule": (JS_ROOT / "ceremony/signon-phases.js").as_uri(),
        }
        with TestClient(app) as client:
            _run_ceremony(client, terms)
            expected_checkpoints = int(has_org_key)
            assert len(checkpoints) == expected_checkpoints
            initial = org_storage_delegate.signing_key(ORG).public_hex
            with LedgerStore(org_ledger_db_path(ORG)) as ledger:
                count = len(ledger.events())
            # A fresh process reuses synchronized Settings after warming.
            settings_ops.set_personal_delegate_audited_key(None)
            unlock_routes._VAULT_CACHE.clear()
            _run_ceremony(client, terms)
            assert len(checkpoints) == expected_checkpoints, "adopted roots suppress another checkpoint"
            assert org_storage_delegate.signing_key(ORG).public_hex == initial
            with LedgerStore(org_ledger_db_path(ORG)) as ledger:
                assert len(ledger.events()) == count

            # A subsequent sign-in inside the 30-day window must replace the
            # key, not merely extend its old grant, and append exactly once.
            from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID
            metadata = settings_ops.read_set_key(
                NETWORK_STORAGE_DELEGATE_SET_ID, founded[ORG].genesis_id, org=None)
            settings_ops.override_setting(metadata["id"], {
                "expires_at": int(time.time() * 1000) + 29 * 86400000,
            }, org=None)
            settings_ops.set_personal_delegate_audited_key(None)
            unlock_routes._VAULT_CACHE.clear()
            _run_ceremony(client, terms)
            assert len(checkpoints) == expected_checkpoints, "storage delegation does not change membership"
            assert org_storage_delegate.signing_key(ORG).public_hex != initial
            with LedgerStore(org_ledger_db_path(ORG)) as ledger:
                assert len(ledger.events()) == count + 1
            metadata = settings_ops.read_set_key(
                NETWORK_STORAGE_DELEGATE_SET_ID, founded[ORG].genesis_id, org=None)["payload"]
            assert metadata["expires_at"] - int(time.time() * 1000) > 89 * 86400000

            # Retiring a retained key must cause the next ceremony to mint,
            # not attempt to reuse a lifecycle-hidden base/renewal chain.
            from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID
            retired_key = org_storage_delegate.signing_key(ORG).public_hex
            retained = settings_ops.read_set_key(
                VAULT_AUDITED_SET_ID, metadata["key_reference"], org=None)
            settings_ops.exclude_setting(retained["id"], org=None)
            assert not org_storage_delegate.prepare(ORG)["delegate_metadata"]["key_exists"]
            settings_ops.set_personal_delegate_audited_key(None)
            unlock_routes._VAULT_CACHE.clear()
            _run_ceremony(client, terms)
            assert org_storage_delegate.signing_key(ORG).public_hex != retired_key
            assert len(checkpoints) == expected_checkpoints

        # End-to-end success: the existing Settings write selects the stored
        # organization delegate without a test-supplied signing author.
        token = "7c" * 16
        public = link_channel_key.mint_channel_key(token, ORG)
        assert link_channel_key.channel_key_for(token, ORG).public_hex == public
    finally:
        GraphDB.close_all_pooled()
