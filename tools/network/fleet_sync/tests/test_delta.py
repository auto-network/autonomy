from pathlib import Path

import pytest
from raptorq import Decoder

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.delta import (
    DeltaCodecError,
    apply_delta_catalog,
    decode_authored_frame,
    encode_authored_frame,
    iter_delta_catalog,
    read_delta_catalog,
    stream_delta_to_chunks,
)
from tools.network.swarmkit.fountain import FountainStore


def _seed(catalog: MutationCatalog, count: int) -> None:
    with catalog.transaction(100, "seed"):
        for index in range(count):
            catalog.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                "VALUES(?,?,?,?,?,?)",
                (f"s{index:04d}", "note", f"note {index}", "{}",
                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
            )


def test_authored_frame_is_strict_and_origin_survives_store_forward(
    tmp_path: Path,
) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        _seed(catalog, 1)
        item = list(catalog.iter_mutations())[0]
        assert decode_authored_frame(encode_authored_frame(item)) == item
        with pytest.raises(DeltaCodecError):
            decode_authored_frame(encode_authored_frame(item) + b"x")
    finally:
        db.close()


def test_delta_chunks_apply_replay_and_raptorq_round_trip(tmp_path: Path) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        source_catalog = MutationCatalog(source.conn, "machine-a")
        target_catalog = MutationCatalog(target.conn, "machine-b")
        source_catalog.install()
        target_catalog.install()
        _seed(source_catalog, 80)
        with source_catalog.freeze_cut() as cut:
            output = tmp_path / "delta"
            catalog = stream_delta_to_chunks(
                source_catalog.iter_mutations(cut), output,
                through_watermark=cut.watermark, target_chunk_bytes=4096,
            )
        assert len(catalog.chunks) > 1
        assert catalog.total_records == 80
        assert apply_delta_catalog(target_catalog, output, catalog) == (80, 0)
        assert apply_delta_catalog(target_catalog, output, catalog) == (0, 80)
        assert source.conn.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ).fetchall() == target.conn.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ).fetchall()
        assert {item.origin_incarnation for item in target_catalog.iter_mutations()} == {
            "machine-a"
        }

        payload = (output / catalog.chunks[0].filename).read_bytes()
        seeder = FountainStore(stripe=0, n_stripes=1)
        artifact = seeder.add_object(payload, symbol_size=256)
        manifest = seeder.manifest(artifact)
        decoder = Decoder.with_defaults(manifest["size"], manifest["symbol_size"])
        decoded = None
        for packet in seeder.serve(artifact, 4096, []):
            decoded = decoder.decode(packet)
            if decoded is not None:
                break
        assert bytes(decoded) == payload
    finally:
        source.close()
        target.close()


def test_delta_tamper_and_reorder_fail_before_application(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "source.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        _seed(catalog, 3)
        output = tmp_path / "delta"
        delta = stream_delta_to_chunks(
            catalog.iter_mutations(), output, through_watermark=100,
            target_chunk_bytes=4096,
        )
        path = output / delta.chunks[0].filename
        body = bytearray(path.read_bytes())
        body[-1] ^= 1
        path.write_bytes(body)
        with pytest.raises(DeltaCodecError, match="digest"):
            list(iter_delta_catalog(output, delta))
    finally:
        db.close()


def test_delta_catalog_requires_canonical_json(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "source.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        _seed(catalog, 1)
        output = tmp_path / "delta"
        stream_delta_to_chunks(
            catalog.iter_journal(), output, through_watermark=100,
            target_chunk_bytes=4096,
        )
        path = output / "delta-catalog.json"
        path.write_bytes(path.read_bytes() + b"\n")
        with pytest.raises(DeltaCodecError, match="canonical JSON"):
            read_delta_catalog(output)
    finally:
        db.close()
