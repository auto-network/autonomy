from pathlib import Path
import hashlib
import json

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync_sim.codec import Mutation, decode_stream
from tools.network.fleet_sync_sim.materialize import (
    ContentAddressedBlobStore,
    MaterializationError,
    materialize,
)
from tools.network.fleet_sync_sim.merge import MutationInbox
from tools.network.fleet_sync_sim.snapshot import encode_snapshot


IGNORED_TABLES = {
    # FTS5 projections and their SQLite-owned shadow tables rebuild from the
    # replicated source rows.  Keep every concrete name here: a new prefix
    # match is not permission for a table to disappear from this test.
    "captures_fts", "captures_fts_config", "captures_fts_content",
    "captures_fts_data", "captures_fts_docsize", "captures_fts_idx",
    "derivations_fts", "derivations_fts_config", "derivations_fts_content",
    "derivations_fts_data", "derivations_fts_docsize", "derivations_fts_idx",
    "sources_fts", "sources_fts_config", "sources_fts_content",
    "sources_fts_data", "sources_fts_docsize", "sources_fts_idx",
    "thoughts_fts", "thoughts_fts_config", "thoughts_fts_content",
    "thoughts_fts_data", "thoughts_fts_docsize", "thoughts_fts_idx",
    # Explicit machine-local or rebuilt application state.
    "keycontrol_meta", "keycontrol_pending", "keycontrol_pending_usage",
    "orgs", "vault_state_object_counts",
    # Fleet-sync's own local catalog/progress state.  These are absent before
    # preparation and present afterward, but never cross to another machine.
    "fleet_sync_catalog", "fleet_sync_journal", "fleet_sync_origins",
    "fleet_sync_peer_state", "fleet_sync_state", "fleet_sync_transactions",
}


def _user_tables(db: GraphDB) -> set[str]:
    return {
        str(row[0])
        for row in db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }


def _seed_source_graph(db: GraphDB, blob: bytes, path: Path) -> str:
    digest = hashlib.sha256(blob).hexdigest()
    db.conn.execute(
        "INSERT INTO sources(id,type,title,file_path,metadata,created_at,ingested_at) "
        "VALUES(?,?,?,?,?,?,?)",
        ("s1", "note", "A note", str(path / "source.md"), '{"b":2,"a":1}',
         "2026-08-19T10:00:00Z", "2026-08-19T10:00:00Z"),
    )
    db.conn.execute(
        "INSERT INTO thoughts(id,source_id,content,tags,metadata,created_at) "
        "VALUES(?,?,?,?,?,?)",
        ("t1", "s1", "RaptorQ appears here", '["sync"]', '{}',
         "2026-08-19T10:00:01Z"),
    )
    db.conn.execute(
        "INSERT INTO note_versions(source_id,version,content,created_at) VALUES(?,?,?,?)",
        ("s1", 7, "body", "2026-08-19T10:00:02Z"),
    )
    db.conn.execute(
        "INSERT INTO attachments(id,hash,filename,mime_type,size_bytes,file_path,"
        "source_id,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        ("a1", digest, "proof.bin", "application/octet-stream", len(blob),
         str(path / "original.bin"), "s1", '{}', "2026-08-19T10:00:03Z"),
    )
    db.conn.commit()
    return digest


def test_canonical_stream_materializes_into_real_graphdb(tmp_path: Path) -> None:
    blob = b"content addressed attachment"
    origin = GraphDB(tmp_path / "origin" / "personal.db")
    target = GraphDB(tmp_path / "target" / "personal.db")
    try:
        digest = _seed_source_graph(origin, blob, tmp_path / "origin-local")
        encoded = encode_snapshot(origin.conn)
        store = ContentAddressedBlobStore(
            tmp_path / "target" / "attachments",
            lambda requested: blob if requested == digest else None,
        )
        report = materialize(target.conn, decode_stream(encoded), blob_store=store)

        assert report.pending_attachments == ()
        assert encode_snapshot(target.conn) == encoded
        attachment_path = Path(target.conn.execute(
            "SELECT file_path FROM attachments WHERE id='a1'"
        ).fetchone()[0])
        assert attachment_path.read_bytes() == blob
        assert str(tmp_path / "origin-local") not in str(attachment_path)
        # The FTS tables are derived by the real GraphDB triggers, not serialized.
        assert target.conn.execute(
            "SELECT COUNT(*) FROM thoughts_fts WHERE thoughts_fts MATCH 'RaptorQ'"
        ).fetchone()[0] == 1
        assert target.conn.execute(
            "SELECT version FROM note_versions WHERE source_id='s1'"
        ).fetchone()[0] == 1
    finally:
        origin.close()
        target.close()


def test_attachment_metadata_waits_for_verified_bytes(tmp_path: Path) -> None:
    blob = b"expected"
    digest = hashlib.sha256(blob).hexdigest()
    origin = GraphDB(tmp_path / "origin" / "personal.db")
    target = GraphDB(tmp_path / "target" / "personal.db")
    try:
        _seed_source_graph(origin, blob, tmp_path / "origin-local")
        mutations = decode_stream(encode_snapshot(origin.conn))
        report = materialize(target.conn, mutations)
        assert report.pending_attachments == ("a1",)
        assert target.conn.execute("SELECT COUNT(*) FROM attachments").fetchone()[0] == 0
        assert target.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1

        bad_store = ContentAddressedBlobStore(
            tmp_path / "bad", lambda requested: b"tampered"
        )
        with pytest.raises(MaterializationError, match="do not match"):
            materialize(target.conn, mutations, blob_store=bad_store)
        assert not list((tmp_path / "bad").rglob("*"))
    finally:
        origin.close()
        target.close()


def test_tombstone_deletes_by_logical_address(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        live = Mutation(
            "sources", ("s1",), 1, False,
            (("created_at", "2026-08-19T10:00:00Z"),
             ("id", "s1"), ("ingested_at", "2026-08-19T10:00:00Z"),
             ("metadata", {}), ("title", "temporary"), ("type", "note")),
        )
        materialize(db.conn, [live])
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        materialize(db.conn, [Mutation("sources", ("s1",), 2, True)])
        assert db.conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
    finally:
        db.close()


def test_settings_override_tombstone_does_not_delete_base_slot(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        common = (
            ("created_at", "2026-08-19T10:00:00Z"), ("deprecated", 0),
            ("key", "one"), ("payload", {"enabled": True}),
            ("publication_state", "raw"), ("schema_revision", 1),
            ("set_id", "example.sync"),
            ("updated_at", "2026-08-19T10:00:00Z"),
        )
        base = Mutation(
            "settings", ("example.sync", 1, "one", "raw", "base"), 1,
            False, tuple(sorted(common + (("id", "base-id"),))),
        )
        override = Mutation(
            "settings",
            ("example.sync", 1, "one", "raw", "supersedes:base-id:override-id"),
            2, False,
            tuple(sorted(common + (("id", "override-id"),
                                   ("supersedes", "base-id")))),
        )
        materialize(db.conn, [base, override])
        tombstone = Mutation(
            "settings",
            ("example.sync", 1, "one", "raw", "supersedes:base-id:override-id"),
            3, True,
        )
        materialize(db.conn, [tombstone])
        assert [row[0] for row in db.conn.execute(
            "SELECT id FROM settings"
        ).fetchall()] == ["base-id"]
    finally:
        db.close()


def test_settings_sibling_overrides_sharing_a_target_all_survive(
    tmp_path: Path,
) -> None:
    """Two override patches on the same target are distinct logical rows, each
    keyed by its own id. Materializing them must not delete one another: the
    pre-insert delete is scoped by id, not just the shared supersedes target.
    Without the id scope, override-b's upsert wipes override-a (same
    supersedes=base-id), silently dropping real override history and leaving the
    winner catalog pointing at a row the staged DB no longer holds.
    """
    db = GraphDB(tmp_path / "personal.db")
    try:
        common = (
            ("created_at", "2026-08-19T10:00:00Z"), ("deprecated", 0),
            ("key", "one"), ("payload", {"enabled": True}),
            ("publication_state", "raw"), ("schema_revision", 1),
            ("set_id", "example.sync"),
            ("updated_at", "2026-08-19T10:00:00Z"),
        )
        base = Mutation(
            "settings", ("example.sync", 1, "one", "raw", "base"), 1,
            False, tuple(sorted(common + (("id", "base-id"),))),
        )
        override_a = Mutation(
            "settings",
            ("example.sync", 1, "one", "raw", "supersedes:base-id:override-a"),
            2, False,
            tuple(sorted(common + (("id", "override-a"),
                                   ("supersedes", "base-id")))),
        )
        override_b = Mutation(
            "settings",
            ("example.sync", 1, "one", "raw", "supersedes:base-id:override-b"),
            3, False,
            tuple(sorted(common + (("id", "override-b"),
                                   ("supersedes", "base-id")))),
        )
        materialize(db.conn, [base, override_a, override_b])
        assert sorted(
            row[0] for row in db.conn.execute("SELECT id FROM settings")
        ) == ["base-id", "override-a", "override-b"]
    finally:
        db.close()


def test_settings_scalar_json_payload_materializes_as_valid_json(
    tmp_path: Path,
) -> None:
    """A JSON column holding a SCALAR round-trips as valid JSON. A vault-sealed
    settings payload is a JSON string literal (not an object), and the write
    side must re-encode it with json.dumps, symmetric with the decode side.
    Otherwise it lands in the column unquoted and fails to re-parse on the next
    read — silently corrupting vault-protected settings on every sync.
    """
    db = GraphDB(tmp_path / "personal.db")
    try:
        payload = "autonomy.vault.v1.eyJhbGciOiJzZWFsZWQifQ"  # decoded scalar
        row = Mutation(
            "settings", ("autonomy.vault.audited", 1, "demo", "raw", "base"),
            1, False,
            tuple(sorted((
                ("created_at", "2026-08-19T10:00:00Z"), ("deprecated", 0),
                ("id", "s1"), ("key", "demo"), ("payload", payload),
                ("publication_state", "raw"), ("schema_revision", 1),
                ("set_id", "autonomy.vault.audited"),
                ("updated_at", "2026-08-19T10:00:00Z"),
            ))),
        )
        materialize(db.conn, [row])
        stored = db.conn.execute(
            "SELECT payload FROM settings WHERE id='s1'"
        ).fetchone()[0]
        # Stored as a valid JSON string literal that re-parses to the scalar.
        assert stored == json.dumps(payload)
        assert json.loads(stored) == payload
    finally:
        db.close()


def _seed_every_logical_table(db: GraphDB, blob: bytes, local_root: Path) -> str:
    digest = _seed_source_graph(db, blob, local_root)
    ciphertext = b"sealed personal secret"
    ciphertext_hash = hashlib.sha256(ciphertext).hexdigest()
    statements = [
        ("INSERT INTO vault_content_bodies(ciphertext_hash,size_bytes,body,created_at) "
         "VALUES(?,?,?,?)",
         (ciphertext_hash, len(ciphertext), ciphertext, 1_787_000_000)),
        ("INSERT INTO vault_content_objects(object_id,revision_id,genesis_id,domain_id,"
         "storage_state_id,ciphertext_hash,header_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
         ("vault-object", "revision-1", "genesis", "personal", "state-1",
          ciphertext_hash, b'{"exact":"bytes"}', 1_787_000_000)),
        ("INSERT INTO keycontrol_state(state_id,wire) VALUES(?,?)",
         ("state-1", b"state-wire")),
        ("INSERT INTO keycontrol_grant(grant_id,storage_state_id,"
         "recipient_kem_key_id,wire) VALUES(?,?,?,?)",
         ("grant-1", "state-1", "kem-1", b"grant-wire")),
        ("INSERT INTO keycontrol_credential(kem_key_id,persona,wire) VALUES(?,?,?)",
         ("kem-1", "persona-1", b"credential-wire")),
        ("INSERT INTO keycontrol_bridge(bridge_id,child_state_id,parent_state_id,wire) "
         "VALUES(?,?,?,?)",
         ("bridge-1", "state-1", "state-0", b"bridge-wire")),
        ("INSERT INTO derivations(id,source_id,thought_id,content,model,created_at) "
         "VALUES(?,?,?,?,?,?)",
         ("d1", "s1", "t1", "An answer", "model", "2026-08-19T10:00:04Z")),
        ("INSERT INTO entities(id,name,canonical_name,type,metadata,created_at) "
         "VALUES(?,?,?,?,?,?)",
         ("e1", "RaptorQ", "raptorq", "technology", '{}',
          "2026-08-19T10:00:05Z")),
        ("INSERT INTO claims(id,subject_id,predicate,object_val,source_id,metadata,created_at) "
         "VALUES(?,?,?,?,?,?,?)",
         ("c1", "e1", "supports", "streaming", "s1", '{}',
          "2026-08-19T10:00:06Z")),
        ("INSERT INTO edges(id,source_id,source_type,target_id,target_type,relation,metadata,created_at) "
         "VALUES(?,?,?,?,?,?,?,?)",
         ("random-local-edge-id", "t1", "thought", "e1", "entity", "mentions", '{}',
          "2026-08-19T10:00:07Z")),
        ("INSERT INTO entity_mentions(entity_id,content_id,content_type,count) VALUES(?,?,?,?)",
         ("e1", "t1", "thought", 4)),
        ("INSERT INTO nodes(id,type,title,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?)",
         ("n1", "reference", "Sync", '{}', "2026-08-19T10:00:08Z",
          "2026-08-19T10:00:08Z")),
        ("INSERT INTO node_refs(node_id,ref_id,ref_type,metadata) VALUES(?,?,?,?)",
         ("n1", "s1", "source", '{}')),
        ("INSERT INTO note_comments(id,source_id,content,created_at) VALUES(?,?,?,?)",
         ("comment1", "s1", "Review", "2026-08-19T10:00:09Z")),
        ("INSERT INTO note_reads(source_id,actor,ts) VALUES(?,?,?)",
         ("s1", "operator", "2026-08-19T10:00:10Z")),
        ("INSERT INTO tags(name,description,created_at,updated_at) VALUES(?,?,?,?)",
         ("sync", "Synchronization", "2026-08-19T10:00:11Z",
          "2026-08-19T10:00:11Z")),
        ("INSERT INTO threads(id,title,metadata,created_at,updated_at) VALUES(?,?,?,?,?)",
         ("thread1", "Inbox", '{}', "2026-08-19T10:00:12Z",
          "2026-08-19T10:00:12Z")),
        ("INSERT INTO captures(id,content,thread_id,source_id,metadata,created_at) "
         "VALUES(?,?,?,?,?,?)",
         ("capture1", "Remember this", "thread1", "s1", '{}',
          "2026-08-19T10:00:13Z")),
        ("INSERT INTO settings(id,set_id,schema_revision,key,payload,publication_state,"
         "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
         ("setting1", "example.sync", 1, "one", '{"enabled":true}', "raw",
          "2026-08-19T10:00:14Z", "2026-08-19T10:00:14Z")),
    ]
    for statement, params in statements:
        db.conn.execute(statement, params)
    db.conn.commit()
    return digest


def test_every_logical_table_round_trips_as_one_canonical_graph(tmp_path: Path) -> None:
    blob = b"all-table attachment"
    origin = GraphDB(tmp_path / "origin" / "personal.db")
    target = GraphDB(tmp_path / "target" / "personal.db")
    try:
        digest = _seed_every_logical_table(origin, blob, tmp_path / "origin-local")
        encoded = encode_snapshot(origin.conn)
        mutations = decode_stream(encoded)
        replicated_tables = _user_tables(origin) - IGNORED_TABLES
        assert {mutation.table for mutation in mutations} == replicated_tables

        store = ContentAddressedBlobStore(
            tmp_path / "target" / "attachments",
            lambda requested: blob if requested == digest else None,
        )
        report = materialize(target.conn, mutations, blob_store=store)
        assert report.applied == len(mutations)
        assert report.pending_attachments == ()
        assert encode_snapshot(target.conn) == encoded
        for table in replicated_tables:
            assert target.conn.execute(
                f'SELECT COUNT(*) FROM "{table}"'
            ).fetchone()[0] >= 1, table
    finally:
        origin.close()
        target.close()


def test_reverse_arrival_materializes_the_same_full_graph(tmp_path: Path) -> None:
    blob = b"reverse-order attachment"
    origin = GraphDB(tmp_path / "origin" / "personal.db")
    forward = GraphDB(tmp_path / "forward" / "personal.db")
    reverse = GraphDB(tmp_path / "reverse" / "personal.db")
    try:
        digest = _seed_every_logical_table(origin, blob, tmp_path / "origin-local")
        mutations = decode_stream(encode_snapshot(origin.conn))
        store_forward = ContentAddressedBlobStore(
            tmp_path / "forward" / "attachments",
            lambda requested: blob if requested == digest else None,
        )
        store_reverse = ContentAddressedBlobStore(
            tmp_path / "reverse" / "attachments",
            lambda requested: blob if requested == digest else None,
        )
        forward_inbox = MutationInbox()
        reverse_inbox = MutationInbox()
        for mutation in mutations:
            forward_inbox.ingest([mutation])
        for mutation in reversed(mutations):
            reverse_inbox.ingest([mutation])
        materialize(forward.conn, forward_inbox.winners(), blob_store=store_forward)
        materialize(reverse.conn, reverse_inbox.winners(), blob_store=store_reverse)
        assert forward_inbox.digest() == reverse_inbox.digest()
        assert encode_snapshot(forward.conn) == encode_snapshot(reverse.conn)
    finally:
        origin.close()
        forward.close()
        reverse.close()
