"""org_ops.create_org_shell — the no-password org shell for the browser founding
ceremony (I1), idempotent on an un-founded shell so a failed founding never
strands a half-org (auto-jdba4 flag 2)."""
from __future__ import annotations

import pytest

from tools.graph import org_ops
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
