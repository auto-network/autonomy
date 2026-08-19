from dataclasses import replace
from pathlib import Path

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync_sim.codec import encode_mutation_frame
from tools.network.fleet_sync_sim.snapshot import iter_snapshot_mutations
from tools.network.fleet_sync_sim.streaming import (
    ensure_streaming_indexes,
    indexed_query_plan,
    iter_catalog_mutations,
    iter_indexed_snapshot_mutations,
    materialize_catalog,
    StreamingCodecError,
    stream_snapshot_to_chunks,
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
            "secret", "autonomy.identity.personal", 1, "root",
            '{"armor":"never-sync"}', "raw", "2026-08-19T10:00:03Z",
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
        assert all(
            mutation.address[0] != "autonomy.identity.personal"
            for mutation in indexed if mutation.table == "settings"
        )
    finally:
        graph.close()


def test_chunk_stream_is_insertion_order_independent_and_resumable(
    tmp_path: Path,
) -> None:
    catalogs = []
    decoded = []
    for name, reverse in (("left", False), ("right", True)):
        graph = GraphDB(tmp_path / name / "personal.db")
        try:
            _seed(graph, reverse=reverse)
            ensure_streaming_indexes(graph.conn)
            output = tmp_path / name / "base"
            catalog = stream_snapshot_to_chunks(
                graph.conn, output, target_chunk_bytes=8 * 1024
            )
            catalogs.append(catalog)
            decoded.append(list(iter_catalog_mutations(output, catalog)))
            assert len(catalog.chunks) > 1
            resumed = list(iter_catalog_mutations(output, catalog, start_chunk=1))
            skipped = catalog.chunks[0].records
            assert resumed == decoded[-1][skipped:]
            with pytest.raises(StreamingCodecError, match="catalog root"):
                list(iter_catalog_mutations(
                    output, replace(catalog, root_sha256="0" * 64)
                ))
            target = GraphDB(tmp_path / name / "realized" / "personal.db")
            try:
                report = materialize_catalog(
                    target.conn, output, catalog, batch_records=7
                )
                assert report.applied == 82
                assert target.conn.execute(
                    "SELECT COUNT(*) FROM sources"
                ).fetchone()[0] == 80
            finally:
                target.close()
        finally:
            graph.close()
    assert catalogs[0].root_sha256 == catalogs[1].root_sha256
    assert [entry.sha256 for entry in catalogs[0].chunks] == [
        entry.sha256 for entry in catalogs[1].chunks
    ]
    assert decoded[0] == decoded[1]


def test_streamed_chunk_is_a_direct_raptorq_object(tmp_path: Path) -> None:
    graph = GraphDB(tmp_path / "personal.db")
    try:
        _seed(graph)
        ensure_streaming_indexes(graph.conn)
        output = tmp_path / "base"
        catalog = stream_snapshot_to_chunks(
            graph.conn, output, target_chunk_bytes=8 * 1024
        )
    finally:
        graph.close()
    payload = (output / catalog.chunks[0].filename).read_bytes()
    seeder = FountainStore(stripe=0, n_stripes=1)
    artifact = seeder.add_object(payload, symbol_size=256)
    manifest = seeder.manifest(artifact)
    assert manifest is not None
    decoder = Decoder.with_defaults(manifest["size"], manifest["symbol_size"])
    decoded = None
    for packet in seeder.serve(artifact, 256, []):
        decoded = decoder.decode(packet)
        if decoded is not None:
            break
    assert decoded is not None
    assert bytes(decoded) == payload


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
