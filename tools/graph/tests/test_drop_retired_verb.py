"""``graph maintenance drop-retired-tables`` purges retired-table catalog
addresses per store and never stops at one store's failure."""

from __future__ import annotations

import json
import types

import pytest

from tools.graph.db import GraphDB
from tools.graph.maintenance import drop_retired
from tools.network.fleet_sync.codec import encode_value
from tools.network.fleet_sync.tests.test_retired_entity_tables import LEGACY_ENTITY_DDL


@pytest.fixture
def stores(tmp_path, monkeypatch):
    orgs = tmp_path / "orgs"
    orgs.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(orgs))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.create_org_db("acme", type_="shared").close()
    yield orgs
    GraphDB.close_all_pooled()


def _seed_retired(db: GraphDB) -> bytes:
    db.conn.executescript(LEGACY_ENTITY_DDL)
    db.conn.execute("INSERT INTO entities(id,name,canonical_name) VALUES('e1','X','x')")
    db.conn.execute(
        "CREATE TABLE IF NOT EXISTS fleet_sync_catalog("
        "address BLOB PRIMARY KEY, timestamp_ns INTEGER NOT NULL,"
        "tombstone INTEGER NOT NULL, transaction_ref INTEGER NOT NULL,"
        "operation_index INTEGER NOT NULL) WITHOUT ROWID"
    )
    keep = encode_value(["thoughts", ["t1"]])
    for address in (encode_value(["entities", ["e1"]]),
                    encode_value(["entity_mentions", ["e1", "t1"]]), keep):
        db.conn.execute("INSERT INTO fleet_sync_catalog VALUES(?,1,0,1,0)", (address,))
    db.conn.commit()
    return keep


def test_the_verb_purges_every_store_and_reports_counts(stores, capsys):
    keep = _seed_retired(GraphDB.for_org("personal", mode="rw"))
    report = drop_retired.run_drop_retired()
    assert report.by_store["personal"]["addresses_purged"] == 2
    assert sorted(report.by_store["personal"]["dropped"]) == ["entities", "entity_mentions"]
    assert report.by_store["acme"] == {"dropped": [], "addresses_purged": 0}
    remaining = [bytes(r[0]) for r in GraphDB.for_org("personal").conn.execute(
        "SELECT address FROM fleet_sync_catalog")]
    assert remaining == [keep]
    # Idempotent: a second pass is a no-op.
    assert drop_retired.run_drop_retired(org="personal").by_store["personal"] == {
        "dropped": [], "addresses_purged": 0}

    drop_retired.cmd_drop_retired(types.SimpleNamespace(org="personal", batch=5))
    out = json.loads(capsys.readouterr().out)
    assert out["addresses_purged"] == 0 and out["errors"] == {}


def test_an_unknown_store_is_refused(stores):
    with pytest.raises(SystemExit):
        drop_retired.run_drop_retired(org="nope")


def test_one_failing_store_does_not_stop_the_others(stores, monkeypatch):
    real = GraphDB.for_org

    def broken(slug, mode="ro"):
        if slug == "acme":
            raise RuntimeError("locked")
        return real(slug, mode=mode)

    monkeypatch.setattr(GraphDB, "for_org", staticmethod(broken))
    report = drop_retired.run_drop_retired()
    assert "acme" in report.errors and "RuntimeError" in report.errors["acme"]
    assert "personal" in report.by_store
