from dataclasses import replace
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.codec import encode_mutation_frame
from tools.network.fleet_sync.snapshot import iter_snapshot_mutations
from tools.network.fleet_sync.streaming import (
    ensure_streaming_indexes,
    indexed_query_plan,
    iter_indexed_snapshot_mutations,
    StreamingCodecError,
)
from tools.network.swarmkit.fountain import Decoder, FountainStore


def _seed(graph: GraphDB, *, reverse: bool = False) -> None:
    rows = list(range(80))
    if reverse:
        rows.reverse()
    for index in rows:
        identity = f"source-{index:03d}"
        graph.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                identity, "note", f"{identity}-" + "x" * 700,
                '{"stable":true}', "2026-08-19T10:00:00Z",
                "2026-08-19T10:00:01Z",
            ),
        )
    graph.conn.execute(
        "INSERT INTO note_versions(source_id,version,content,created_at) "
        "VALUES(?,?,?,?)",
        ("source-000", 1, "body", "2026-08-19T10:00:02Z"),
    )
    graph.conn.execute(
        "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
        "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "visible", "example", 1, "key", '{"value":1}', "raw",
            "2026-08-19T10:00:03Z", "2026-08-19T10:00:04Z",
        ),
    )
    graph.conn.execute(
        "INSERT INTO settings(id,set_id,schema_revision,key,payload,"
        "publication_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (
            "identity", "autonomy.identity.personal", 1, "root",
            '{"armor":"ciphertext"}', "raw", "2026-08-19T10:00:03Z",
            "2026-08-19T10:00:04Z",
        ),
    )
    graph.conn.commit()


def test_indexed_projection_matches_oracle_without_temp_sort(tmp_path: Path) -> None:
    graph = GraphDB(tmp_path / "personal.db")
    try:
        _seed(graph)
        indexes = ensure_streaming_indexes(graph.conn)
        assert "sources" in indexes and "note_versions" in indexes
        for table in indexes:
            assert "TEMP B-TREE" not in indexed_query_plan(graph.conn, table)
        oracle = {
            encode_mutation_frame(mutation)
            for mutation in iter_snapshot_mutations(graph.conn)
        }
        indexed = list(iter_indexed_snapshot_mutations(graph.conn))
        assert {encode_mutation_frame(mutation) for mutation in indexed} == oracle
        assert any(
            mutation.address[0] == "autonomy.identity.personal"
            for mutation in indexed if mutation.table == "settings"
        )
    finally:
        graph.close()


def test_database_keyset_resume_is_exclusive(tmp_path: Path) -> None:
    graph = GraphDB(tmp_path / "personal.db")
    try:
        _seed(graph)
        ensure_streaming_indexes(graph.conn)
        resumed = list(iter_indexed_snapshot_mutations(
            graph.conn, start_after=("sources", ("source-040",))
        ))
        source_ids = [
            mutation.address[0] for mutation in resumed
            if mutation.table == "sources"
        ]
        assert source_ids[0] == "source-041"
        assert source_ids[-1] == "source-079"
        assert "source-040" not in source_ids
    finally:
        graph.close()
