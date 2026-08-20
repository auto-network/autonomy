"""Schema metadata lives in the machine store, not every organization's.

Acceptance for auto-n77vh (design of record graph://21a0da9e-1c2). Schema
metadata is a projection of the code THIS PROCESS runs: no member authors
it, nothing can sign it, and two machines on different code versions have
no single true answer per organization. Flushing it into shared org
databases made whoever restarted last win, and was the last unsigned
ingress into databases where every row must be signed. The flush now
writes ONE store — the machine store, resolved by name — and sweeps the
rows earlier code versions wrote everywhere else.

The read path depends on auto-9uj7i's explicit rule: ``resolve_peers``
names the operator's own stores unconditionally, so the machine store's
``canonical`` rows participate in every organization's read on this
machine — including an organization whose pinned peer subscription names
nobody. That participation is the failure mode that makes this change
unshippable if it breaks: before this bead an under-subscribed org lost
only OTHER orgs' schema discovery; after it, a dropped machine store
would leave that org with no schema metadata at all. It is asserted
directly here, not reported either way.
"""

from __future__ import annotations

import pytest

from tools.graph import cross_org, settings_ops
from tools.graph import db as graph_db_mod
from tools.graph.db import GraphDB, _org_db_path
from tools.graph.schemas.registry import (
    SCHEMAS,
    SCHEMA_META_SET_ID,
    SYNOPSIS_META_SET_ID,
    flush_schema_meta_machine_store,
)


@pytest.fixture
def orgs_root(tmp_path, monkeypatch):
    root = tmp_path / "orgs"
    root.mkdir()
    monkeypatch.setenv("AUTONOMY_ORGS_DIR", str(root))
    monkeypatch.delenv("GRAPH_DB", raising=False)
    monkeypatch.delenv("GRAPH_ORG", raising=False)
    GraphDB.close_all_pooled()
    yield root
    GraphDB.close_all_pooled()


def _schema_meta_rows(db_path) -> int:
    db = GraphDB(str(db_path), mode="rw")
    try:
        return db.conn.execute(
            "SELECT COUNT(*) FROM settings WHERE set_id IN (?, ?)",
            (SCHEMA_META_SET_ID, SYNOPSIS_META_SET_ID),
        ).fetchone()[0]
    finally:
        db.close()


def test_flush_writes_one_store_and_no_organization_database(orgs_root):
    """One store, not N: after the flush, the machine store holds the whole
    registry and every organization database holds ZERO schema-meta rows."""
    acme = GraphDB.create_org_db("acme")
    acme_path = acme.db_path
    acme.close()
    beta = GraphDB.create_org_db("beta")
    beta_path = beta.db_path
    beta.close()
    GraphDB.close_all_pooled()

    assert flush_schema_meta_machine_store() == 1

    machine_path = _org_db_path("machine")
    assert machine_path == orgs_root.parent / "machine.db", (
        "the machine store must resolve by name to its own home, never an "
        "org-namespace path"
    )
    machine = GraphDB(str(machine_path), mode="rw")
    try:
        keys = {
            r[0] for r in machine.conn.execute(
                "SELECT key FROM settings WHERE set_id = ?",
                (SCHEMA_META_SET_ID,),
            ).fetchall()
        }
    finally:
        machine.close()
    assert set(SCHEMAS) <= keys, "the whole registry lands in the machine store"

    assert _schema_meta_rows(acme_path) == 0
    assert _schema_meta_rows(beta_path) == 0

    # First creation writes the typed bootstrap row: list_orgs is the
    # operator's STORE INVENTORY and silently skips files without one, so
    # a bare machine store would exist, hold the whole registry, serve
    # reads — and report as absent (the absent-versus-broken shape,
    # inverted). Local stores carry type='personal'; the slug tells the
    # two apart.
    from tools.graph import org_ops

    inventory = {ref.slug: ref.type for ref in org_ops.list_orgs()}
    assert inventory.get("machine") == "personal", (
        f"machine store missing from the store inventory: {inventory}"
    )


def test_flush_sweeps_rows_previous_versions_wrote_into_shared_stores(
    orgs_root,
):
    """Rows a pre-n77vh dashboard flushed into organization databases and
    the personal store are removed — exactly one copy exists per machine.
    These are startup-refreshed projections of the running code, not
    authored content, so the removal is a hard delete and idempotent."""
    acme = GraphDB.create_org_db("acme")
    acme_path = acme.db_path
    acme.close()
    personal = GraphDB.create_org_db("personal", type_="personal")
    personal_path = personal.db_path
    personal.close()
    GraphDB.close_all_pooled()

    for i, path in enumerate([acme_path, personal_path]):
        db = GraphDB(str(path), mode="rw")
        db.conn.execute(
            "INSERT INTO settings (id, set_id, schema_revision, key,"
            " payload, publication_state) VALUES (?, ?, 1,"
            " 'stale.legacy#1', '{}', 'canonical')",
            (f"stale-{i}", SCHEMA_META_SET_ID,),
        )
        db.conn.commit()
        db.close()
    GraphDB.close_all_pooled()

    flush_schema_meta_machine_store()

    assert _schema_meta_rows(acme_path) == 0, "org database not swept"
    assert _schema_meta_rows(personal_path) == 0, "personal store not swept"
    # Idempotent once clean.
    flush_schema_meta_machine_store()
    assert _schema_meta_rows(acme_path) == 0


def test_a_pinned_subscription_org_resolves_every_schema_identically(
    orgs_root,
):
    """THE unshippable failure mode, asserted directly: an organization
    whose peer subscription pins an empty list — naming neither the
    machine store nor any other organization — resolves 100% of registered
    schemas, identically to an unpinned organization. A subscription opts
    out of other organizations; it may not remove the operator's own
    stores (auto-9uj7i), and schema discovery now rides entirely on that
    rule."""
    GraphDB.create_org_db("pinned").close()
    GraphDB.create_org_db("unpinned").close()
    GraphDB.create_org_db("personal", type_="personal").close()
    GraphDB.close_all_pooled()

    assert flush_schema_meta_machine_store() == 1

    settings_ops.add_setting(
        cross_org.PEER_SUBSCRIPTION_SET_ID, 1, key="pinned",
        payload={"peers": []}, org="personal", state="canonical",
    )
    # The pin is live: the pinned org's peer set drops 'unpinned' but
    # keeps the operator's own stores.
    peers = cross_org.resolve_peers("pinned", None)
    assert "unpinned" not in peers and "machine" in peers

    pinned_keys = {
        m.key for m in settings_ops.read_set(SCHEMA_META_SET_ID, org="pinned")
    }
    unpinned_keys = {
        m.key
        for m in settings_ops.read_set(SCHEMA_META_SET_ID, org="unpinned")
    }
    assert pinned_keys == unpinned_keys, (
        "a pinned subscription changed schema discovery — the machine "
        "store fell out of the candidate set"
    )
    assert set(SCHEMAS) <= pinned_keys, (
        f"missing: {sorted(set(SCHEMAS) - pinned_keys)[:5]}"
    )


def test_two_code_versions_no_longer_fight_in_a_shared_database(orgs_root):
    """The whoever-restarted-last fight is structurally over: the flush
    never opens a shared organization database writably, so a second
    machine on different code cannot clobber this one's rows there. The
    machine store replicates nowhere, so its rows are per-machine by
    construction — asserted here as: two flushes with different registry
    contents each land wholly in their own (machine) store and org DBs
    stay empty throughout."""
    acme = GraphDB.create_org_db("acme")
    acme_path = acme.db_path
    acme.close()
    GraphDB.close_all_pooled()

    flush_schema_meta_machine_store()
    assert _schema_meta_rows(acme_path) == 0
    assert _schema_meta_rows(_org_db_path("machine")) >= len(SCHEMAS)
