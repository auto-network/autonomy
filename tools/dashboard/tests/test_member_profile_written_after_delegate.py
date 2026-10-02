"""The member's own directory row is written once the storage delegate is
held, signed by it, never at founding or at the join install (where this
process holds no signer and the unsigned row is refused by every other
member's store as settings_unsigned: simulation 2026-10-02, Bob never saw
Alice's photo).

Real organization store and ledger; the personal-store rows the delegate
index and audited secret live in are in memory, as in the replay test
(writing the audited set needs a warm vault)."""

from __future__ import annotations

import copy
import sqlite3
import time

import pytest

from tools.dashboard import member_directory, org_storage_delegate as osd
from tools.graph import settings_ops
from tools.graph.db import GraphDB
from tools.graph.schemas.org_member_profile import MEMBER_PROFILE_SET_ID
from tools.network.idkit import KeyPair, derive_persona
from tools.network.ledger import HLC, LedgerStore, make_event, mint_grant_nonce, sign_delegate_proof
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import org_ledger_db_path
from tools.network.settingskit import authority
from tools.network.settingskit.envelope import record_from_row, verify_record

ORG = "acme"
ORG_UUID = "22222222-2222-4222-8222-222222222222"
AVATAR = "0192a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"


@pytest.fixture
def founded(tmp_path, monkeypatch):
    orgs_dir = tmp_path / "orgs"
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs_dir))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal", path=orgs_dir / "personal.db").close()
    GraphDB.create_org_db(ORG, root=orgs_dir, org_id=ORG_UUID).close()
    root = KeyPair.generate()
    seed = bytes.fromhex(KeyPair.generate().private_hex)
    with LedgerStore(org_ledger_db_path(ORG)) as store:
        founding = found_org_ledger(store, org_id=ORG_UUID, org_root=root,
                                    personal_root_seed=seed, now=int(time.time() * 1000) - 1000)
    persona = derive_persona(seed, founding.genesis_id)

    # Personal-store rows (org=None) in memory; organization rows (org=ORG)
    # reach the real store and its signing boundary.
    personal: dict = {}
    real = {name: getattr(settings_ops, name)
            for name in ("read_set_key", "chain_setting", "add_setting", "upsert_by_key")}

    def read_set_key(set_id, key, *, org=None, **kwargs):
        if org is None:
            return copy.deepcopy(personal.get((set_id, key)))
        return real["read_set_key"](set_id, key, org=org, **kwargs)

    def add_setting(set_id, revision, key, payload, *, org=None, **kwargs):
        if org is None:
            personal[set_id, key] = {"id": key, "payload": dict(payload)}
            return key
        return real["add_setting"](set_id, revision, key, payload, org=org, **kwargs)

    def upsert_by_key(set_id, revision, key, payload, *, org=None, **kwargs):
        if org is None:
            personal[set_id, key] = {"id": key, "payload": dict(payload)}
            return key
        return real["upsert_by_key"](set_id, revision, key, payload, org=org, **kwargs)

    monkeypatch.setattr(settings_ops, "read_set_key", read_set_key)
    monkeypatch.setattr(settings_ops, "chain_setting", read_set_key)
    monkeypatch.setattr(settings_ops, "add_setting", add_setting)
    monkeypatch.setattr(settings_ops, "upsert_by_key", upsert_by_key)
    # The dashboard's signer: this process's storage delegates, resolved
    # from the index accept() writes. Nothing is pre-installed.
    monkeypatch.setattr(settings_ops, "_SIGNER_PROVIDER", None)
    monkeypatch.setattr(settings_ops, "_UNSIGNED_WARNED", set())
    monkeypatch.setattr(osd, "_SIGNING_CONTEXTS", {})
    authority.forget()
    osd.install_settings_signer()
    monkeypatch.setattr(
        member_directory, "presentation_from_personal_profile",
        lambda slug: {"display_name": "Alice", "byline": "Founder", "avatar": AVATAR, "color": ""},
    )

    def mint():
        context = osd.prepare(ORG)
        key = KeyPair.generate()
        nonce = mint_grant_nonce()
        scope = context["scope"]
        with LedgerStore(org_ledger_db_path(ORG)) as store:
            ts = max(int(time.time() * 1000), max(e.hlc.ts for e in store.events()) + 1)
        event = make_event(persona, {
            "type": "delegate", "child_pub": key.public_hex, "scope": scope,
            "can_redelegate": False, "ttl": osd.TTL_MS, "grant_nonce": nonce,
            "proof": sign_delegate_proof(key, founding.genesis_id, persona.public_hex, scope,
                                         can_redelegate=False, ttl=osd.TTL_MS, grant_nonce=nonce),
        }, context["parents"], HLC(ts, 0))
        return {"organization": ORG, "action": "new", "private_key": key.private_hex,
                "event": event.to_json().decode()}, key

    yield {"mint": mint, "persona": persona.public_hex, "genesis": founding.genesis_id}
    authority.forget()
    GraphDB.close_all_pooled()


def _profile_rows() -> list[dict]:
    conn = sqlite3.connect(org_ledger_db_path(ORG))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM settings WHERE set_id=? ORDER BY rowid", (MEMBER_PROFILE_SET_ID,))]
    finally:
        conn.close()


def test_the_own_row_is_written_signed_when_the_delegate_is_accepted_and_only_once(founded):
    # Founding wrote no directory row: nothing here could have signed it.
    assert _profile_rows() == []

    item, delegate = founded["mint"]()
    osd.accept(item)

    rows = _profile_rows()
    assert [r["key"] for r in rows] == [founded["persona"]]
    row = rows[0]
    assert row["signature"] is not None, "the row written after acceptance must be signed"
    assert row["signing_key"] == delegate.public_hex
    assert row["terminal_persona"] == founded["persona"]
    verify_record(record_from_row(row, founded["genesis"]), str(row["signature"]))
    assert '"avatar": "%s"' % AVATAR in row["payload"]

    # The next sign-on accepts a NEW grant: the row stands as it is.
    again, _ = founded["mint"]()
    osd.accept(again)
    assert _profile_rows() == rows


def test_a_presentation_chosen_for_the_organization_is_never_overwritten(founded, monkeypatch):
    item, _ = founded["mint"]()
    osd.accept(item)
    monkeypatch.setattr(
        member_directory, "presentation_from_personal_profile",
        lambda slug: {"display_name": "Alice (renamed)", "byline": "", "avatar": "", "color": ""},
    )
    assert member_directory.write_own(ORG, founded["persona"]) is False
    assert '"display_name": "Alice"' in _profile_rows()[0]["payload"]
