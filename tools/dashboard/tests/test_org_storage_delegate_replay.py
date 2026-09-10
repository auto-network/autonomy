"""Exact signed handoff replay uses the real ledger and existing Settings API.

Settings payloads are in memory here to count writes; the companion browser
ceremony regression covers real audited sealing and decryption.
"""
import copy
import time
from types import SimpleNamespace

import pytest

from tools.dashboard import org_storage_delegate as osd
from tools.graph.schemas.network_identity import NETWORK_STORAGE_DELEGATE_SET_ID as INDEX
from tools.graph.schemas.vault_credential import VAULT_AUDITED_SET_ID as SECRET
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import Event, HLC, LedgerStore, make_event, mint_grant_nonce, sign_delegate_proof
from tools.network.ledger.found import found_org_ledger


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    path = tmp_path / "org.db"
    root = KeyPair.generate()
    seed = bytes.fromhex(KeyPair.generate().private_hex)
    with LedgerStore(path) as store:
        founded = found_org_ledger(store, org_id="org", org_root=root,
                                  personal_root_seed=seed, now=int(time.time() * 1000) - 1000)
    persona = derive_persona(seed, founded.genesis_id)
    monkeypatch.setattr(osd, "org_ledger_db_path", lambda org: path)
    rows, writes = {}, []

    def read(set_id, key, **kwargs):
        return copy.deepcopy(rows.get((set_id, key)))

    def add(set_id, revision, key, payload, **kwargs):
        assert (set_id, key) not in rows, "duplicate secret write"
        rows[set_id, key] = {"id": key, "payload": dict(payload)}
        writes.append(("add", set_id, key))

    def override(row_id, patch, **kwargs):
        next(row for row in rows.values() if row["id"] == row_id)["payload"].update(patch)
        writes.append(("override", row_id))

    def upsert(set_id, revision, key, payload, **kwargs):
        rows[set_id, key] = {"id": key, "payload": dict(payload)}
        writes.append(("upsert", set_id, key))

    monkeypatch.setattr(osd.settings_ops, "read_set_key", read)
    monkeypatch.setattr(osd.settings_ops, "chain_setting", read)
    monkeypatch.setattr(osd.settings_ops, "add_setting", add)
    monkeypatch.setattr(osd.settings_ops, "override_setting", override)
    monkeypatch.setattr(osd.settings_ops, "upsert_by_key", upsert)

    def mint():
        context = osd.prepare("org")
        key = KeyPair.generate()
        nonce = mint_grant_nonce()
        scope = context["scope"]
        with LedgerStore(path) as store:
            ts = max(int(time.time() * 1000), max(e.hlc.ts for e in store.events()) + 1)
        event = make_event(persona, {
            "type": "delegate", "child_pub": key.public_hex, "scope": scope,
            "can_redelegate": False, "ttl": osd.TTL_MS, "grant_nonce": nonce,
            "proof": sign_delegate_proof(key, founded.genesis_id, persona.public_hex, scope,
                                         can_redelegate=False, ttl=osd.TTL_MS, grant_nonce=nonce),
        }, context["parents"], HLC(ts, 0))
        return {"organization": "org", "action": "new", "private_key": key.private_hex,
                "event": event.to_json().decode()}

    return SimpleNamespace(path=path, rows=rows, writes=writes, mint=mint,
                           genesis=founded.genesis_id)


def test_exact_replay_adds_no_event_secret_revision_or_metadata_write(handoff):
    item = handoff.mint()
    osd.accept(item)
    rows, writes = copy.deepcopy(handoff.rows), list(handoff.writes)
    with LedgerStore(handoff.path) as store:
        ids = store.ledger.all_ids()
    osd.accept(item)
    assert handoff.rows == rows
    assert handoff.writes == writes
    with LedgerStore(handoff.path) as store:
        assert store.ledger.all_ids() == ids


@pytest.mark.parametrize("cut", ["append", "index"])
def test_repeat_partial_handoff_keeps_old_key_consistent_then_completes(handoff, monkeypatch, cut):
    osd.accept(handoff.mint())
    old = osd.signing_key("org").public_hex
    old_index = copy.deepcopy(handoff.rows[INDEX, handoff.genesis])
    item = handoff.mint()
    with monkeypatch.context() as patch:
        def refuse(*args, **kwargs):
            raise RuntimeError("handoff stopped before " + cut)
        if cut == "append":
            patch.setattr(LedgerStore, "append", refuse)
        else:
            patch.setattr(osd.settings_ops, "upsert_by_key", refuse)
        with pytest.raises(RuntimeError, match="handoff stopped"):
            osd.accept(item)
    assert handoff.rows[INDEX, handoff.genesis] == old_index
    assert osd.signing_key("org").public_hex == old
    osd.accept(item)
    assert osd.signing_key("org").public_hex == KeyPair.from_private_hex(item["private_key"]).public_hex
    writes = list(handoff.writes)
    osd.accept(item)
    assert handoff.writes == writes


def test_old_replay_does_not_replace_newer_delegate(handoff):
    old = handoff.mint()
    osd.accept(old)
    new = handoff.mint()
    osd.accept(new)
    rows, writes = copy.deepcopy(handoff.rows), list(handoff.writes)
    osd.accept(old)
    assert handoff.rows == rows
    assert handoff.writes == writes


def test_unaccepted_sibling_still_requires_current_heads(handoff):
    first, sibling = handoff.mint(), handoff.mint()
    osd.accept(first)
    with pytest.raises(ValueError, match="current heads"):
        osd.accept(sibling)
