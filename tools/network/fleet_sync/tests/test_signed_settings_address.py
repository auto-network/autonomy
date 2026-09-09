"""Signed settings rows replicate one slot per signer, and ingest verifies
the envelope (auto-y068i; design graph://21a0da9e-1c2)."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from tools.graph.db import GraphDB
from tools.network.fleet_sync import catalog as catalog_module
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.materialize import LEDGER_EVENT_SET_ID
from tools.network.fleet_sync.policies import TABLE_POLICIES, _replication_surface
from tools.network.fleet_sync.snapshot import _logical_address
from tools.network.idkit import KeyPair
from tools.network.settingskit.envelope import build_record, sign_record

SET_ID = "dashboard.example"
KEY = "shared-key"


def _genesis_wire() -> tuple[str, str]:
    wire = json.dumps({"payload": {"type": "genesis"}, "author": "x"}, sort_keys=True)
    return hashlib.sha256(wire.encode()).hexdigest(), wire


def _open(path: Path, origin: str) -> tuple[GraphDB, MutationCatalog]:
    db = GraphDB(path)
    catalog = MutationCatalog(db.conn, origin)
    catalog.install()
    return db, catalog


def _write_genesis(db: GraphDB, catalog: MutationCatalog, ts: int, event_id: str, wire: str) -> None:
    with catalog.transaction(ts, f"genesis-{ts}"):
        db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
            " VALUES (?,?,?,?,?,?)",
            (str(uuid.uuid4()), LEDGER_EVENT_SET_ID, 1, event_id,
             json.dumps({"wire": wire}), "published"),
        )


def _write_signed(db: GraphDB, catalog: MutationCatalog, ts: int, persona: KeyPair,
                  genesis: str, payload: dict, signed_at: int) -> str:
    record = build_record(
        org=genesis, set_id=SET_ID, key=KEY, schema_revision=1,
        publication_state="published", deprecated=False, successor_id=None,
        payload=payload, signed_at=signed_at, signing_key=persona.public_hex,
        witness=None,
    )
    sig = sign_record(persona, record)
    row_id = str(uuid.uuid4())
    with catalog.transaction(ts, f"signed-{ts}"):
        db.conn.execute(
            "INSERT INTO settings (id,set_id,schema_revision,key,payload,"
            "publication_state,deprecated,successor_id,signed_at,signing_key,"
            "signature,witness,terminal_persona) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (row_id, SET_ID, 1, KEY, json.dumps(payload), "published", 0, None,
             signed_at, persona.public_hex, sig, None, persona.public_hex),
        )
    return row_id


def _exchange(src: MutationCatalog, src_origin: str, dst: MutationCatalog) -> list[tuple[int, int]]:
    held = dst.origin_watermarks().get(src_origin, 0)
    position: tuple[int, str | None] = (held, None)
    results = []
    while True:
        page = src.next_transactions_for_origin(src_origin, position[0], position[1], limit=50)
        if not page:
            return results
        for _ref, timestamp, transaction_id, items in page:
            results.append(dst.apply_remote_batch(items))
            position = (timestamp, transaction_id)


def _rows_at_key(db: GraphDB) -> list[tuple[str | None, str]]:
    return [
        (r[0], json.loads(r[1])["v"])
        for r in db.conn.execute(
            "SELECT terminal_persona,payload FROM settings WHERE set_id=? AND key=?"
            " ORDER BY terminal_persona", (SET_ID, KEY),
        )
    ]


def test_tampered_signed_row_is_quarantined_and_never_forwarded(tmp_path: Path) -> None:
    genesis, wire = _genesis_wire()
    p = KeyPair.generate()
    a_db, a = _open(tmp_path / "alpha-a.db", "a" * 64)
    b_db, b = _open(tmp_path / "alpha-b.db", "b" * 64)
    try:
        _write_genesis(a_db, a, 10, genesis, wire)
        _write_genesis(b_db, b, 11, genesis, wire)
        row_id = _write_signed(a_db, a, 100, p, genesis, {"v": "honest"}, 1_000)
        # The payload changes under an unchanged signature: a forgery shape.
        with a.transaction(150, "tamper"):
            a_db.conn.execute(
                "UPDATE settings SET payload=? WHERE id=?",
                (json.dumps({"v": "forged"}), row_id),
            )
        _exchange(a, "a" * 64, b)
        assert _rows_at_key(b_db) == []
        parked = b_db.conn.execute(
            "SELECT reason FROM fleet_sync_quarantine"
        ).fetchall()
        assert [r[0] for r in parked] == ["settings_signature_invalid"]
        # B forwards the transaction to others WITHOUT the forged row.
        ref, = [r[0] for r in b_db.conn.execute(
            "SELECT t.id FROM fleet_sync_transactions t JOIN fleet_sync_origins o"
            " ON o.id=t.origin_id WHERE o.incarnation=? AND t.transaction_id='tamper'",
            ("a" * 64,),
        )]
        assert b.transaction_items(ref, "a" * 64, "tamper") == []
    finally:
        a_db.close()
        b_db.close()


def test_unsigned_rows_keep_the_five_part_address_and_the_policy_key(tmp_path: Path) -> None:
    policy = TABLE_POLICIES["settings"]
    row = {"set_id": SET_ID, "schema_revision": 1, "key": KEY,
           "publication_state": "raw", "supersedes": None, "excludes": None,
           "id": "x", "terminal_persona": None}
    assert _logical_address(policy, row) == (SET_ID, 1, KEY, "raw", "base")
    signed = dict(row, terminal_persona="p" * 64)
    assert _logical_address(policy, signed) == (SET_ID, 1, KEY, "raw", "base", "p" * 64)
    # The policy inventory the compatibility digest hashes is unchanged, so
    # an unsigned-only fleet keeps its digest across this change.
    assert _replication_surface()["policies"]["settings"]["key"] == [
        "set_id", "schema_revision", "key", "publication_state", "row_role",
    ]
    # And the SQL capture agrees with the Python address for both shapes.
    db, catalog = _open(tmp_path / "alpha.db", "a" * 64)
    try:
        with catalog.transaction(10, "unsigned"):
            db.conn.execute(
                "INSERT INTO settings (id,set_id,schema_revision,key,payload,publication_state)"
                " VALUES ('u',?,1,?,'{}','raw')", (SET_ID, KEY),
            )
        addresses = [m.mutation.address for m in catalog.iter_mutations()]
        assert addresses == [(SET_ID, 1, KEY, "raw", "base")]
    finally:
        db.close()


def test_signed_row_waits_for_the_genesis_then_verifies_on_drain(tmp_path: Path) -> None:
    genesis, wire = _genesis_wire()
    p = KeyPair.generate()
    a_db, a = _open(tmp_path / "alpha-a.db", "a" * 64)
    b_db, b = _open(tmp_path / "alpha-b.db", "b" * 64)
    try:
        # A signs before B knows any genesis: the row is parked, not lost.
        _write_signed(a_db, a, 100, p, genesis, {"v": "early"}, 1_000)
        _exchange(a, "a" * 64, b)
        assert _rows_at_key(b_db) == []
        assert [r[0] for r in b_db.conn.execute(
            "SELECT reason FROM fleet_sync_quarantine"
        )] == ["settings_signature_pending"]
        # The parked row is still forwarded onward (signature travels with it).
        ref, = [r[0] for r in b_db.conn.execute(
            "SELECT t.id FROM fleet_sync_transactions t JOIN fleet_sync_origins o"
            " ON o.id=t.origin_id WHERE o.incarnation=? AND t.transaction_id='signed-100'",
            ("a" * 64,),
        )]
        assert len(b.transaction_items(ref, "a" * 64, "signed-100")) == 1
        # The genesis arrives; the drain verifies and lands the row.
        _write_genesis(a_db, a, 200, genesis, wire)
        _exchange(a, "a" * 64, b)
        assert b.drain_pending_signatures() == 1
        assert _rows_at_key(b_db) == [(p.public_hex, "early")]
        assert b_db.conn.execute("SELECT COUNT(*) FROM fleet_sync_quarantine").fetchone()[0] == 0
    finally:
        a_db.close()
        b_db.close()
