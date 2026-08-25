from pathlib import Path
from dataclasses import replace

import pytest

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.compaction import WatermarkError
from tools.network.fleet_sync.winners import (
    WinnerCodecError,
    decode_winner_frame,
    encode_winner_frame,
    iter_winner_catalog,
    stream_winners_to_chunks,
)


def test_winner_artifact_is_strict_bounded_and_payload_free(tmp_path: Path) -> None:
    db = GraphDB(tmp_path / "personal.db")
    try:
        catalog = MutationCatalog(db.conn, "machine-a")
        catalog.install()
        with catalog.transaction(10, "tx"):
            db.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                "VALUES(?,?,?,?,?,?)",
                ("s1", "note", "x" * 100_000, "{}",
                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
            )
        item = next(catalog.iter_winner_metadata())
        frame = encode_winner_frame(item)
        assert len(frame) < 1024
        assert b"x" * 128 not in frame
        assert decode_winner_frame(frame) == item
        with pytest.raises(WinnerCodecError):
            decode_winner_frame(frame + b"x")

        output = tmp_path / "winners"
        artifact = stream_winners_to_chunks(
            catalog.iter_winner_metadata(), output,
            through_watermark=10, target_chunk_bytes=4096,
        )
        assert list(iter_winner_catalog(output, artifact)) == [item]
    finally:
        db.close()


def test_winner_metadata_must_match_realized_base(tmp_path: Path) -> None:
    source = GraphDB(tmp_path / "source.db")
    target = GraphDB(tmp_path / "target.db")
    try:
        left = MutationCatalog(source.conn, "machine-a")
        left.install()
        with left.transaction(10, "tx"):
            source.conn.execute(
                "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                "VALUES(?,?,?,?,?,?)",
                ("s1", "note", "one", "{}", "2026-08-19T00:00:00Z",
                 "2026-08-19T00:00:00Z"),
            )
        item = next(left.iter_winner_metadata())
        target.conn.execute(
            "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
            "VALUES(?,?,?,?,?,?)",
            ("s1", "note", "different", "{}", "2026-08-19T00:00:00Z",
             "2026-08-19T00:00:00Z"),
        )
        target.conn.commit()
        right = MutationCatalog(target.conn, "machine-b")
        right.install()
        with pytest.raises(WatermarkError, match="hash mismatch"):
            right.install_winner_metadata([item])
        assert target.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_catalog"
        ).fetchone()[0] == 0
        with pytest.raises(WatermarkError, match="hash mismatch"):
            right.install_winner_metadata([
                replace(item, candidate_hash=b"\x00" * 32)
            ])
    finally:
        source.close()
        target.close()
