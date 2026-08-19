from pathlib import Path
import subprocess
import sys

from tools.graph.db import GraphDB
from tools.network.fleet_sync_sim.codec import decode_stream
from tools.network.fleet_sync_sim.snapshot import encode_snapshot


def _seed(db: GraphDB, *, reverse: bool, local_prefix: str) -> None:
    rows = [
        (
            "INSERT INTO sources(id,type,title,file_path,metadata,created_at,ingested_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "source-1", "note", "Canonical note",
                f"/{local_prefix}/notes/source-1.md",
                '{"z":2,"a":1}', "2026-08-19T10:00:00Z",
                "2026-08-19T10:00:01Z",
            ),
        ),
        (
            "INSERT INTO attachments(id,hash,filename,mime_type,size_bytes,file_path,"
            "source_id,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "attachment-1", "a" * 64, "proof.png", "image/png", 123,
                f"/{local_prefix}/attachments/aa/{'a' * 64}.png", "source-1",
                '{"height":10,"width":20}', "2026-08-19T10:00:02Z",
            ),
        ),
        (
            "INSERT INTO settings(id,set_id,schema_revision,key,payload,publication_state,"
            "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                "setting-1", "example.preferences", 1, "ui", '{"b":2,"a":1}',
                "raw", "2026-08-19T10:00:03Z", "2026-08-19T10:00:04Z",
            ),
        ),
    ]
    if reverse:
        rows.reverse()
    for sql, params in rows:
        db.conn.execute(sql, params)

    # Different physical ids and display versions represent the same two
    # authored bodies.  The wire identity is source + time + content hash.
    if reverse:
        db.conn.execute(
            "INSERT INTO note_versions(id,source_id,version,content,created_at) "
            "VALUES(?,?,?,?,?)",
            (91, "source-1", 8, "second body", "2026-08-19T10:00:06Z"),
        )
        db.conn.execute(
            "INSERT INTO note_versions(id,source_id,version,content,created_at) "
            "VALUES(?,?,?,?,?)",
            (87, "source-1", 7, "first body", "2026-08-19T10:00:05Z"),
        )
    else:
        db.conn.execute(
            "INSERT INTO note_versions(id,source_id,version,content,created_at) "
            "VALUES(?,?,?,?,?)",
            (1, "source-1", 1, "first body", "2026-08-19T10:00:05Z"),
        )
        db.conn.execute(
            "INSERT INTO note_versions(id,source_id,version,content,created_at) "
            "VALUES(?,?,?,?,?)",
            (2, "source-1", 2, "second body", "2026-08-19T10:00:06Z"),
        )

    # Identity rows are deliberately different and must not affect bytes.
    db.conn.execute(
        "INSERT INTO settings(id,set_id,schema_revision,key,payload,publication_state,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            f"root-{local_prefix}", "autonomy.identity.personal", 1, "root",
            f'{{"machine":"{local_prefix}"}}', "raw",
            "2026-08-19T10:00:07Z", "2026-08-19T10:00:07Z",
        ),
    )
    db.conn.commit()


def test_equivalent_real_schema_databases_emit_identical_bytes(tmp_path: Path) -> None:
    left = GraphDB(tmp_path / "left" / "personal.db")
    right = GraphDB(tmp_path / "right" / "personal.db")
    try:
        _seed(left, reverse=False, local_prefix="left")
        _seed(right, reverse=True, local_prefix="right")
        left_bytes = encode_snapshot(left.conn)
        right_bytes = encode_snapshot(right.conn)
    finally:
        left.close()
        right.close()

    assert left_bytes == right_bytes
    mutations = decode_stream(left_bytes)
    assert {mutation.table for mutation in mutations} == {
        "attachments", "note_versions", "settings", "sources"
    }
    assert len([m for m in mutations if m.table == "note_versions"]) == 2
    assert b"/left/" not in left_bytes
    assert b"/right/" not in right_bytes
    assert b"autonomy.identity.personal" not in left_bytes


def test_equivalent_databases_hash_identically_in_separate_processes(
    tmp_path: Path,
) -> None:
    left_path = tmp_path / "left" / "personal.db"
    right_path = tmp_path / "right" / "personal.db"
    left = GraphDB(left_path)
    right = GraphDB(right_path)
    try:
        _seed(left, reverse=False, local_prefix="left")
        _seed(right, reverse=True, local_prefix="right")
    finally:
        left.close()
        right.close()

    program = (
        "import hashlib,json,sqlite3,sys; "
        "from tools.network.fleet_sync_sim.snapshot import encode_snapshot; "
        "from tools.network.fleet_sync_sim.codec import decode_stream; "
        "from tools.network.fleet_sync_sim.segments import encode_segments; "
        "c=sqlite3.connect(sys.argv[1]); "
        "b=encode_snapshot(c); "
        "print(json.dumps([hashlib.sha256(b).hexdigest(),"
        "[s.digest for s in encode_segments(decode_stream(b),target_bytes=700)]]))"
    )
    hashes = [
        subprocess.check_output(
            [sys.executable, "-c", program, str(path)], text=True
        ).strip()
        for path in (left_path, right_path)
    ]
    assert hashes[0] == hashes[1]
