from pathlib import Path
import hashlib
import os
import sqlite3

import pytest

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.records import record_id
from tools.network.storagekit.tests.conftest import HLC0, World
from tools.network.fleet_sync_sim.alpha import (
    FleetSyncAlpha,
    install_checkpoint,
    transport_checkpoint_via_raptorq,
)
from tools.network.fleet_sync_sim.compaction import AuthoredMutation
from tools.network.fleet_sync_sim.codec import Mutation
from tools.network.fleet_sync_sim.materialize import MaterializationError
from tools.network.fleet_sync_sim.merge import MutationConflictError, MutationInbox
from tools.network.fleet_sync_sim.policies import audit_schema


def _insert_vault_and_keycontrol(conn: sqlite3.Connection) -> tuple:
    ciphertext = b"\x00sealed\xffpersonal-secret"
    header = b'{"order":"is-exact","not":"decoded"}'
    digest = hashlib.sha256(ciphertext).hexdigest()
    world = World()
    descriptor, _ = world.mint_initial_state(world.member(0))
    credential, _ = build_credential(
        world.member(0), world.gen, bytes(range(32)), [world.gen], HLC0
    )
    bridge = bridge_mod.create(
        KeyPair.generate(), genesis_id=os.urandom(32).hex(),
        domain_id=os.urandom(32).hex(),
        child_state_id=os.urandom(32).hex(),
        parent_state_id=os.urandom(32).hex(),
        child_state_secret=os.urandom(32), parent_state_secret=os.urandom(32),
        authority_heads=[os.urandom(32).hex()],
    )
    conn.execute(
        "INSERT INTO vault_content_bodies VALUES(?,?,?,?)",
        (digest, len(ciphertext), ciphertext, 1_787_000_000),
    )
    conn.execute(
        "INSERT INTO vault_content_objects VALUES(?,?,?,?,?,?,?,?)",
        ("object-1", "revision-1", "genesis-1", "personal", descriptor.state_id,
         digest, header, 1_787_000_001),
    )
    conn.execute(
        "INSERT INTO keycontrol_state VALUES(?,?)",
        (descriptor.state_id, descriptor.to_json()),
    )
    grant_wire = b"signed-capability-grant-wire"
    conn.execute(
        "INSERT INTO keycontrol_grant VALUES(?,?,?,?)",
        ("grant-1", descriptor.state_id, credential.kem_key_id, grant_wire),
    )
    conn.execute(
        "INSERT INTO keycontrol_credential VALUES(?,?,?)",
        (credential.kem_key_id, credential.persona, credential.to_json()),
    )
    conn.execute(
        "INSERT INTO keycontrol_bridge VALUES(?,?,?,?)",
        (bridge.bridge_id, bridge.child_state_id, bridge.parent_state_id,
         bridge.to_json()),
    )
    # This value is intentionally wrong. It is local derived state and must
    # not cross the wire; the receiver rebuilds the exact count from objects.
    conn.execute(
        "INSERT INTO vault_state_object_counts VALUES(?,?)",
        (descriptor.state_id, 999),
    )
    return ciphertext, header, descriptor, credential, bridge, grant_wire


def test_vault_and_keycontrol_round_trip_through_real_alpha_checkpoint(
    tmp_path: Path,
) -> None:
    origin_path = tmp_path / "origin.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(origin_path, "machine-a") as origin:
        with origin.author(100, "vault-transaction"):
                ciphertext, header, descriptor, credential, bridge, grant_wire = (
                _insert_vault_and_keycontrol(origin.graph.conn)
            )
        checkpoint = origin.checkpoint(
            tmp_path / "checkpoint", roster_epoch=1,
            active_roster=("machine-a", "machine-b"), target_chunk_bytes=4096,
        )
        assert checkpoint.base_records == 6

    transport_checkpoint_via_raptorq(
        tmp_path / "checkpoint", tmp_path / "received", symbol_size=256
    )
    receiver_local = GraphDB(target_path)
    try:
        receiver_local.conn.execute(
            "INSERT INTO keycontrol_meta VALUES('receiver-only','kept')"
        )
        receiver_local.conn.execute(
            "INSERT INTO keycontrol_pending VALUES(?,?,?,?,?,?,?,?)",
            ("bridge", "pending-id", "state", "missing", 1, "peer", b"wire", 4),
        )
        receiver_local.conn.execute(
            "INSERT INTO keycontrol_pending_usage VALUES(1,1,4)"
        )
        receiver_local.conn.commit()
    finally:
        receiver_local.close()
    install_checkpoint(
        tmp_path / "received", target_path,
        target_origin_incarnation="machine-b", expected_roster_epoch=1,
        expected_active_roster=("machine-a", "machine-b"),
    )
    target = GraphDB(target_path)
    try:
        assert bytes(target.conn.execute(
            "SELECT body FROM vault_content_bodies"
        ).fetchone()[0]) == ciphertext
        assert bytes(target.conn.execute(
            "SELECT header_json FROM vault_content_objects"
        ).fetchone()[0]) == header
        assert [tuple(row) for row in target.conn.execute(
            "SELECT storage_state_id,object_count FROM vault_state_object_counts"
        ).fetchall()] == [(descriptor.state_id, 1)]
        assert bytes(target.conn.execute(
            "SELECT wire FROM keycontrol_state"
        ).fetchone()[0]) == descriptor.to_json()
        assert bytes(target.conn.execute(
            "SELECT wire FROM keycontrol_grant WHERE grant_id='grant-1'"
        ).fetchone()[0]) == grant_wire
        assert bytes(target.conn.execute(
            "SELECT wire FROM keycontrol_credential"
        ).fetchone()[0]) == credential.to_json()
        assert bytes(target.conn.execute(
            "SELECT wire FROM keycontrol_bridge"
        ).fetchone()[0]) == bridge.to_json()
        audit_schema(target.conn)
        assert target.conn.execute("PRAGMA user_version").fetchone()[0] == 8
        assert tuple(target.conn.execute(
            "SELECT key,value FROM keycontrol_meta WHERE key='receiver-only'"
        ).fetchone()) == ("receiver-only", "kept")
        assert target.conn.execute(
            "SELECT COUNT(*) FROM keycontrol_pending"
        ).fetchone()[0] == 1
        assert tuple(target.conn.execute(
            "SELECT pending_rows,pending_bytes FROM keycontrol_pending_usage"
        ).fetchone()) == (1, 4)
    finally:
        target.close()
    # Opening the production key-control store re-verifies each content
    # address and signature. This is the fresh-machine usability proof, not
    # merely a check that opaque BLOBs survived the codec.
    with KeyControlStore(target_path) as reopened:
        assert reopened.get(descriptor.state_id) == descriptor
        assert reopened.get_credential(credential.kem_key_id) == credential
        assert reopened.get_bridge(record_id(bridge.to_json())) == bridge


def test_immutable_rows_reject_local_update_delete_and_remote_conflict(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal.db"
    with FleetSyncAlpha(path, "machine-a") as alpha:
        body = b"one"
        digest = hashlib.sha256(body).hexdigest()
        with alpha.author(10, "insert"):
            alpha.graph.conn.execute(
                "INSERT INTO vault_content_bodies VALUES(?,?,?,?)",
                (digest, len(body), body, 1),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot update"):
            with alpha.author(11, "bad-update"):
                alpha.graph.conn.execute(
                    "UPDATE vault_content_bodies SET body=? WHERE ciphertext_hash=?",
                    (b"two", digest),
                )
        with pytest.raises(sqlite3.IntegrityError, match="cannot delete"):
            with alpha.author(12, "bad-delete"):
                alpha.graph.conn.execute(
                    "DELETE FROM vault_content_bodies WHERE ciphertext_hash=?", (digest,)
                )

        original = list(alpha.catalog.iter_mutations())[0].mutation
        conflicting = type(original)(
            original.table, original.address, 20, False,
            tuple((column, b"two" if column == "body" else value)
                  for column, value in original.values),
        )
        with pytest.raises(MutationConflictError, match="conflict"):
            alpha.catalog.apply_remote(
                AuthoredMutation("machine-b", "conflict", 0, conflicting)
            )
        assert bytes(alpha.graph.conn.execute(
            "SELECT body FROM vault_content_bodies"
        ).fetchone()[0]) == body


def test_non_null_keycontrol_wire_restores_a_locally_pruned_row(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source.db"
    target_path = tmp_path / "target.db"
    with FleetSyncAlpha(source_path, "machine-a") as source:
        with source.author(10, "full"):
            source.graph.conn.execute(
                "INSERT INTO keycontrol_bridge VALUES(?,?,?,?)",
                ("bridge-1", "child", "parent", b"full-wire"),
            )
        full = list(source.catalog.iter_mutations())[0]

    with FleetSyncAlpha(target_path, "machine-b") as target:
        with target.author(20, "local-full"):
            target.graph.conn.execute(
                "INSERT INTO keycontrol_bridge VALUES(?,?,?,?)",
                ("bridge-1", "child", "parent", b"full-wire"),
            )
        with target.author(30, "local-prune"):
            target.graph.conn.execute(
                "UPDATE keycontrol_bridge SET wire=NULL WHERE bridge_id='bridge-1'"
            )
        assert target.graph.conn.execute(
            "SELECT wire FROM keycontrol_bridge"
        ).fetchone()[0] is None
        # Body presence outranks the timestamp: an older complete peer record
        # restores this machine's local space-saving prune.
        assert target.catalog.apply_remote(full)
        assert bytes(target.graph.conn.execute(
            "SELECT wire FROM keycontrol_bridge"
        ).fetchone()[0]) == b"full-wire"


def test_vault_object_without_body_fails_atomically(tmp_path: Path) -> None:
    with FleetSyncAlpha(tmp_path / "personal.db", "machine-a") as alpha:
        with pytest.raises(MaterializationError, match="missing ciphertext body"):
            alpha.catalog.apply_remote(AuthoredMutation(
                "machine-b", "missing-body", 0,
                Mutation(
                    "vault_content_objects", ("object", "revision"), 10, False,
                    tuple(sorted({
                        "object_id": "object", "revision_id": "revision",
                        "genesis_id": "genesis", "domain_id": "personal",
                        "storage_state_id": "state", "ciphertext_hash": "0" * 64,
                        "header_json": b"header", "created_at": 1,
                    }.items())),
                ),
            ))
        assert alpha.graph.conn.execute(
            "SELECT COUNT(*) FROM vault_content_objects"
        ).fetchone()[0] == 0


def test_v8_migration_preserves_credentials_and_makes_wire_prunable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "personal.db"
    graph = GraphDB(path)
    graph.close()
    legacy = sqlite3.connect(path)
    try:
        legacy.executescript("""
            DROP INDEX keycontrol_credential_persona;
            DROP TABLE keycontrol_credential;
            CREATE TABLE keycontrol_credential(
                kem_key_id TEXT PRIMARY KEY,
                persona TEXT NOT NULL,
                wire BLOB NOT NULL
            );
            CREATE INDEX keycontrol_credential_persona
                ON keycontrol_credential(persona);
            INSERT INTO keycontrol_credential VALUES('kem','persona',X'77697265');
            PRAGMA user_version=7;
        """)
    finally:
        legacy.close()

    migrated = GraphDB(path)
    try:
        wire_column = next(row for row in migrated.conn.execute(
            "PRAGMA table_info(keycontrol_credential)"
        ) if row[1] == "wire")
        assert wire_column[3] == 0
        assert tuple(migrated.conn.execute(
            "SELECT kem_key_id,persona,wire FROM keycontrol_credential"
        ).fetchone()) == ("kem", "persona", b"wire")
        migrated.conn.execute(
            "UPDATE keycontrol_credential SET wire=NULL WHERE kem_key_id='kem'"
        )
        migrated.conn.commit()
    finally:
        migrated.close()


def test_checkpoint_merge_rejects_immutable_conflict_and_prefers_held_wire() -> None:
    immutable_a = Mutation(
        "keycontrol_state", ("state",), 10, False,
        (("state_id", "state"), ("wire", b"a")),
    )
    immutable_b = Mutation(
        "keycontrol_state", ("state",), 20, False,
        (("state_id", "state"), ("wire", b"b")),
    )
    with pytest.raises(MutationConflictError, match="conflict"):
        MutationInbox().ingest((immutable_a, immutable_b))

    complete = Mutation(
        "keycontrol_bridge", ("bridge",), 10, False,
        (("bridge_id", "bridge"), ("child_state_id", "child"),
         ("parent_state_id", "parent"), ("wire", b"body")),
    )
    pruned = Mutation(
        "keycontrol_bridge", ("bridge",), 20, False,
        (("bridge_id", "bridge"), ("child_state_id", "child"),
         ("parent_state_id", "parent"), ("wire", None)),
    )
    for order in ((complete, pruned), (pruned, complete)):
        inbox = MutationInbox()
        inbox.ingest(order)
        assert inbox.winners() == [complete]
