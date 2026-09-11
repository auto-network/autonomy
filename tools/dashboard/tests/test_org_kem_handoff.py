"""Organization KEM handoff: initial public setup, reuse, and memory-only custody."""
import copy
import time
from dataclasses import replace
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from tools.dashboard import signon_preparation, unlock_routes
from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import LedgerStore, org_ledger_db_path
from tools.network.ledger.found import found_org_ledger
from tools.network.storagekit import credentials
from tools.network.storagekit.errors import StorageError
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.vault.key_holder import VaultKeyCache
from tools.vault.unlock import current_recovery_credentials, recover_organization_generations


@pytest.fixture
def org(tmp_path, monkeypatch, request):
    GraphDB.close_all_pooled()
    monkeypatch.setenv("AUTONOMY_DATA_ROOT", str(tmp_path))
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    monkeypatch.delenv("DASHBOARD_MOCK", raising=False)
    slug = "kem-handoff"
    GraphDB.create_org_db(slug, type_="shared", path=tmp_path / "orgs" / f"{slug}.db").close()
    root = KeyPair.generate()
    with LedgerStore(org_ledger_db_path(slug)) as ledger:
        founded = found_org_ledger(ledger, org_id=slug, org_root=KeyPair.generate(),
            personal_root_seed=bytes.fromhex(root.private_hex), now=int(time.time() * 1000) - 1000,
            kem_seed=(credentials.derive_kem_seed(bytes.fromhex(root.private_hex))
                      if getattr(request, "param", True) else None))
    monkeypatch.setattr(unlock_routes, "_VAULT_CACHE", {"cache": VaultKeyCache()})
    yield slug, founded, root
    GraphDB.close_all_pooled()


def item_for(org):
    slug, founded, _ = org
    return {"organization": slug, "genesis_id": founded.genesis_id,
            "kem_key_id": founded.kem_credential["kem_key_id"],
            "persona_kem_private_key": founded.kem_private_key}


def counts(slug):
    with LedgerStore(org_ledger_db_path(slug)) as ledger:
        events = ledger.ledger.all_ids()
    with KeyControlStore(org_ledger_db_path(slug)) as store:
        return (events, len(store.states), len(store.accepted_grants()),
                store.db.execute("SELECT COUNT(*) FROM keycontrol_credential").fetchone()[0])


@pytest.mark.parametrize("org", [False], indirect=True)
def test_missing_member_credential_is_published_once_by_existing_handoff(org):
    slug, founded, root = org
    persona = derive_persona(bytes.fromhex(root.private_hex), founded.genesis_id)
    before = counts(slug)
    context = signon_preparation.organization_encryption_recovery(slug)
    assert context["provisioning"]["personas"] == [persona.public_hex]
    credential, private = credentials.build(persona, founded.genesis_id,
        credentials.derive_kem_seed(bytes.fromhex(root.private_hex)),
        context["provisioning"]["authority_heads"], context["provisioning"]["created_hlc"])
    item = {"organization": slug, "genesis_id": founded.genesis_id,
            "kem_key_id": credential.kem_key_id, "kem_credential": credential.to_dict(),
            "persona_kem_private_key": private}
    assert unlock_routes._accept_organization_kem_key(item) == 0
    assert unlock_routes._accept_organization_kem_key(item) == 0
    after = counts(slug)
    assert after[:-1] == before[:-1]  # no new membership, generation, or grant
    assert after[-1] == 1
    assert "provisioning" not in signon_preparation.organization_encryption_recovery(slug)


@pytest.mark.parametrize("org", [False], indirect=True)
def test_initial_credential_context_is_reproducible_from_synced_genesis(org, monkeypatch):
    slug, founded, _ = org
    first = signon_preparation.organization_encryption_recovery(slug)["provisioning"]
    later = time.time() + 60
    monkeypatch.setattr(signon_preparation.time, "time", lambda: later)
    second = signon_preparation.organization_encryption_recovery(slug)["provisioning"]
    assert second == first
    with LedgerStore(org_ledger_db_path(slug)) as ledger:
        assert first["authority_heads"] == [founded.genesis_id]
        assert first["created_hlc"] == ledger.get(founded.genesis_id).hlc.to_list()


@pytest.mark.parametrize("org", [False], indirect=True)
@pytest.mark.parametrize("invalid", ["private", "member", "heads", "signature", "key-id"])
def test_invalid_initial_publication_writes_nothing(org, invalid):
    slug, founded, root = org
    persona = (KeyPair.generate() if invalid == "member" else
               derive_persona(bytes.fromhex(root.private_hex), founded.genesis_id))
    context = signon_preparation.organization_encryption_recovery(slug)["provisioning"]
    credential, private = credentials.build(persona, founded.genesis_id,
        credentials.derive_kem_seed(bytes.fromhex(root.private_hex)),
        ["00" * 32] if invalid == "heads" else context["authority_heads"], context["created_hlc"])
    wire = credential.to_dict()
    if invalid == "signature":
        wire["signature"] = "00" * 64
    item = {"organization": slug, "genesis_id": founded.genesis_id,
            "kem_key_id": "00" * 32 if invalid == "key-id" else credential.kem_key_id,
            "kem_credential": wire,
            "persona_kem_private_key": "00" * 32 if invalid == "private" else private}
    before = counts(slug)
    with pytest.raises((ValueError, StorageError)):
        unlock_routes._accept_organization_kem_key(item)
    assert counts(slug) == before
    assert not unlock_routes._VAULT_CACHE.get("organization_kem_keys")


def test_initial_publication_cannot_replace_existing_member_credential(org):
    slug, founded, root = org
    with LedgerStore(org_ledger_db_path(slug)) as ledger:
        credential, private = credentials.build(
            derive_persona(bytes.fromhex(root.private_hex), founded.genesis_id),
            founded.genesis_id, b"n" * 32, ledger.heads(), (int(time.time() * 1000), 0))
    before = counts(slug)
    with pytest.raises(ValueError, match="cannot replace"):
        unlock_routes._accept_organization_kem_key({"organization": slug,
            "genesis_id": founded.genesis_id, "kem_key_id": credential.kem_key_id,
            "kem_credential": credential.to_dict(), "persona_kem_private_key": private})
    assert counts(slug) == before


def test_fold_only_credential_preparation_acceptance_and_replay_write_nothing(org):
    slug, founded, _ = org
    before = counts(slug)
    assert before[-1] == 0  # Never sealed here: credential only in member.claim.
    metadata = signon_preparation.organization_encryption_recovery(slug)
    assert metadata == {"genesis_id": founded.genesis_id, "counter": 0, "credentials": [{
        "kem_key_id": founded.kem_credential["kem_key_id"],
        "kem_public_key": founded.kem_credential["kem_public_key"]}]}
    for _ in range(2):
        assert unlock_routes._accept_organization_kem_key(item_for(org)) == 0
        assert counts(slug) == before
    assert unlock_routes._VAULT_CACHE["organization_kem_keys"] == {
        founded.genesis_id: {founded.kem_credential["kem_key_id"]: founded.kem_private_key}}


@pytest.mark.parametrize("field,value", [
    ("genesis_id", "00" * 32), ("kem_key_id", "00" * 32),
    ("persona_kem_private_key", "00" * 32),
    ("persona_kem_private_key", "malformed"), ("organization", "personal"),
])
def test_invalid_item_does_not_replace_good_memory_or_write_records(org, field, value):
    item = item_for(org)
    unlock_routes._accept_organization_kem_key(item)
    held = copy.deepcopy(unlock_routes._VAULT_CACHE["organization_kem_keys"])
    before = counts(org[0])
    with pytest.raises((ValueError, TypeError)):
        unlock_routes._accept_organization_kem_key({**item, field: value})
    assert unlock_routes._VAULT_CACHE["organization_kem_keys"] == held
    assert counts(org[0]) == before


def test_current_credential_selection_includes_store_and_excludes_removed_member(org):
    slug, founded, root = org
    with LedgerStore(org_ledger_db_path(slug)) as ledger:
        frontier = ledger.fold()
        # A newer valid credential supersedes the claim's original credential.
        new, _ = credentials.build(derive_persona(bytes.fromhex(root.private_hex), founded.genesis_id),
            founded.genesis_id, b"n" * 32, ledger.heads(), (int(time.time() * 1000), 0))
        with KeyControlStore(org_ledger_db_path(slug)) as store:
            store.accept_credential(new)
            selected = current_recovery_credentials(frontier, store, ledger.ledger.ancestry)
            assert [c.kem_key_id for c in selected] == [new.kem_key_id]
            removed = SimpleNamespace(genesis_id=frontier.genesis_id,
                members={k: replace(m, roles=()) for k, m in frontier.members.items()})
            assert current_recovery_credentials(removed, store, ledger.ledger.ancestry) == ()


def test_route_isolates_refusal_and_retains_before_snapshot(org, monkeypatch):
    item = item_for(org)
    snapshots = []
    monkeypatch.setattr(unlock_routes, "session_from_request", lambda r: {"sid": "test"})
    monkeypatch.setattr(unlock_routes, "_personal_store_has_generations", lambda: False)
    monkeypatch.setattr(unlock_routes, "_bring_vault_up", lambda keys: 0)
    def snapshot():
        snapshots.append(copy.deepcopy(unlock_routes._VAULT_CACHE["organization_kem_keys"]))
        return True
    monkeypatch.setattr(unlock_routes, "save_vault_across_hot_reload", snapshot)
    app = Starlette(routes=[Route("/keys", unlock_routes.post_unlock_vault_keys, methods=["POST"])])
    with TestClient(app) as client:
        response = client.post("/keys", json={"generation_keys": {}, "organization_kem_keys": [
            {**item, "organization": "missing-org"}, None, item]})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["snapshot_persisted"] is True
    assert body["organization_recovery"][org[0]] == {"ok": True, "recovered": 0}
    assert not body["organization_recovery"]["missing-org"]["ok"]
    assert not body["organization_recovery"]["unknown"]["ok"]
    assert item["persona_kem_private_key"] not in response.text
    assert snapshots == [unlock_routes._VAULT_CACHE["organization_kem_keys"]]


def test_shared_recovery_skips_held_states_and_does_not_cache_failed_opens(monkeypatch):
    from tools.vault import unlock
    cache = VaultKeyCache()
    cache.add("held", b"h" * 32)
    states = {s: SimpleNamespace(genesis_id="org", state_id=s) for s in ("held", "new")}
    states["foreign"] = SimpleNamespace(genesis_id="another-org", state_id="foreign")
    grants = [SimpleNamespace(storage_state_id=s, recipient_kem_key_id="ours")
              for s in ("held", "new", "new", "foreign")]
    grants.append(SimpleNamespace(storage_state_id="new", recipient_kem_key_id="someone-else"))
    store = SimpleNamespace(states=states, accepted_grants=lambda: grants)
    calls = []
    def opens(private, candidates, descriptors):
        sid = candidates[0].storage_state_id
        calls.append((private, sid))
        return {sid: b"n" * 32} if private == "good" else {}
    monkeypatch.setattr(unlock, "open_generation_keys", opens)
    assert recover_organization_generations("org", {"ours": "bad"}, store, cache) == 0
    assert recover_organization_generations("org", {"ours": "good"}, store, cache) == 1
    assert recover_organization_generations("org", {"ours": "good"}, store, cache) == 0
    assert calls == [("bad", "new"), ("bad", "new"), ("good", "new")]
    assert cache.secrets == {"held": b"h" * 32, "new": b"n" * 32}
