from pathlib import Path

import pytest
from raptorq import Decoder

from tools.graph.db import GraphDB
from tools.network.fleet_sync.catalog import MutationCatalog
from tools.network.fleet_sync.delta import (
    DeltaCodecError,
    decode_authored_frame,
    encode_authored_frame,
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


