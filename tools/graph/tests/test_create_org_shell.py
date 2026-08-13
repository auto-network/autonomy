"""org_ops.create_org_shell — the no-password org shell for the browser founding
ceremony (I1), idempotent on an un-founded shell so a failed founding never
strands a half-org (auto-jdba4 flag 2)."""
from __future__ import annotations

import pytest

from tools.graph import org_ops, settings_ops
from tools.network.idkit import KeyPair
from tools.network.ledger.found import found_org_ledger
from tools.network.ledger.store import LedgerStore, org_ledger_db_path


@pytest.fixture
def orgs(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(tmp_path / "orgs"))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    yield
    from tools.graph.db import GraphDB
    GraphDB.close_all_pooled()


def test_fresh_shell_creates_db_and_identity(orgs):
    ref = org_ops.create_org_shell(
        "anchore", type_="shared", identity_payload={"name": "Anchore"}
    )
    assert ref.slug == "anchore"
    assert ref.id
    import os
    assert os.path.exists(ref.db_path)


def test_idempotent_on_unfounded_shell(orgs):
    a = org_ops.create_org_shell("anchore", type_="shared")
    # No password, no server-side founding: the ledger is NOT folded yet.
    b = org_ops.create_org_shell("anchore", type_="shared")
    assert a.id == b.id  # same shell returned, no OrgExistsError


def test_refuses_once_ledger_is_founded(orgs):
    ref = org_ops.create_org_shell("anchore", type_="shared")
    root, personal = KeyPair.generate(), KeyPair.generate()
    with LedgerStore(org_ledger_db_path("anchore")) as store:
        found_org_ledger(
            store, org_id=ref.id, org_root=root,
            personal_root_seed=bytes.fromhex(personal.private_hex), now=1,
        )
    with pytest.raises(org_ops.OrgExistsError):
        org_ops.create_org_shell("anchore", type_="shared")


def _real_sealed_blob():
    """A schema-valid browser-shaped sealed org-key blob (real idkit seal)."""
    from tools.network.idkit import KeyPair
    from tools.network.idkit.sealing import derive_encapsulation_keypair, seal
    from tools.graph.schemas.network_identity import ORG_ROOT_ARMOR_PURPOSE

    org_root, personal = KeyPair.generate(), KeyPair.generate()
    _, recipient_pub = derive_encapsulation_keypair(
        bytes.fromhex(personal.private_hex), ORG_ROOT_ARMOR_PURPOSE
    )
    sealed = seal(bytes.fromhex(org_root.private_hex), recipient_pub, ORG_ROOT_ARMOR_PURPOSE)
    return org_root.public_hex, {
        "root_pub": org_root.public_hex,
        "sealed_root_key": sealed.hex(),
        "owner_kem_pub": recipient_pub,
        "seal_purpose": ORG_ROOT_ARMOR_PURPOSE,
    }


def test_store_sealed_org_key_at_raw_and_idempotent(orgs):
    import sqlite3
    from tools.graph.schemas.network_identity import NETWORK_ORG_KEY_SET_ID

    ref = org_ops.create_org_shell("anchore", type_="shared")
    root_pub, blob = _real_sealed_blob()
    org_ops.store_sealed_org_key("anchore", blob)
    org_ops.store_sealed_org_key("anchore", blob)  # seal-first-then-fold retry
    m = settings_ops.read_owned_set(NETWORK_ORG_KEY_SET_ID, org="anchore").members
    assert len(m) == 1  # idempotent upsert, not a second base
    assert m[0].payload["root_pub"] == root_pub
    conn = sqlite3.connect(ref.db_path)
    state = conn.execute(
        "SELECT publication_state FROM settings WHERE set_id=? AND deprecated=0",
        (NETWORK_ORG_KEY_SET_ID,),
    ).fetchall()
    conn.close()
    assert state == [("raw",)]  # a secret, off the cross-org read-through surface


def test_store_sealed_org_key_rejects_bad_payload_and_unknown_org(orgs):
    org_ops.create_org_shell("anchore", type_="shared")
    _root_pub, blob = _real_sealed_blob()
    with pytest.raises(org_ops.OrgError):
        org_ops.store_sealed_org_key("anchore", {**blob, "seal_purpose": "wrong"})
    with pytest.raises(org_ops.OrgError):
        org_ops.store_sealed_org_key("anchore", {**blob, "root_pub": "nothex!"})
    with pytest.raises(org_ops.OrgNotFoundError):
        org_ops.store_sealed_org_key("ghost", blob)


def test_store_sealed_org_key_locked_once_founded(orgs):
    from tools.network.idkit import KeyPair
    from tools.network.ledger.found import found_org_ledger
    from tools.network.ledger.store import LedgerStore, org_ledger_db_path

    ref = org_ops.create_org_shell("anchore", type_="shared")
    _root_pub, blob = _real_sealed_blob()
    org_ops.store_sealed_org_key("anchore", blob)  # un-founded: allowed
    # Found the ledger -> the root is now committed and locked.
    r, personal = KeyPair.generate(), KeyPair.generate()
    with LedgerStore(org_ledger_db_path("anchore")) as store:
        found_org_ledger(
            store, org_id=ref.id, org_root=r,
            personal_root_seed=bytes.fromhex(personal.private_hex), now=1,
        )
    with pytest.raises(org_ops.OrgExistsError):
        org_ops.store_sealed_org_key("anchore", blob)  # founded: refused
