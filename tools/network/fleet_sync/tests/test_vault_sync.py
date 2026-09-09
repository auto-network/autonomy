from pathlib import Path
import hashlib
import os
import sqlite3

import pytest

from tools.graph.db import GraphDB, _SCHEMA_USER_VERSION
from tools.network.idkit import KeyPair
from tools.network.storagekit import capability
from tools.network.storagekit import bridge as bridge_mod
from tools.network.storagekit.credentials import build as build_credential
from tools.network.storagekit.keycontrol import KeyControlStore
from tools.network.storagekit.records import record_id
from tools.network.storagekit.tests.conftest import HLC0, World
from tools.network.fleet_sync.sync import (
    FleetSyncAlpha,
)
from tools.network.fleet_sync.compaction import AuthoredMutation
from tools.network.fleet_sync.codec import Mutation
from tools.network.fleet_sync.materialize import MaterializationError
from tools.network.fleet_sync.merge import MutationConflictError, MutationInbox
from tools.network.fleet_sync.policies import audit_schema
from tools.vault.unlock import open_generation_keys


def _insert_vault_and_keycontrol(conn: sqlite3.Connection) -> tuple:
    ciphertext = b"\x00sealed\xffpersonal-secret"
    header = b'{"order":"is-exact","not":"decoded"}'
    digest = hashlib.sha256(ciphertext).hexdigest()
    world = World()
    descriptor, state_secret = world.mint_initial_state(world.member(0))
    credential, kem_private = build_credential(
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
    grant = capability.issue(
        world.member(0),
        genesis_id=world.gen,
        domain_id=world.dom,
        storage_state_id=descriptor.state_id,
        recipient_credential=credential,
        state_secret=state_secret,
        state_secret_commitment=descriptor.secret_commitment,
        authority_heads=descriptor.authority_heads,
    )
    conn.execute(
        "INSERT INTO keycontrol_grant VALUES(?,?,?,?)",
        (
            grant.grant_id,
            descriptor.state_id,
            credential.kem_key_id,
            grant.to_json(),
        ),
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
    return (
        ciphertext,
        header,
        descriptor,
        credential,
        bridge,
        grant,
        kem_private,
        state_secret,
    )


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


def test_merge_rejects_immutable_conflict_and_prefers_held_wire() -> None:
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
