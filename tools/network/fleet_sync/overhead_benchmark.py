"""Measure on-disk and write-rate cost of the alpha tracking schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import time

from tools.graph.db import GraphDB

from .catalog import MutationCatalog
from .streaming import ensure_streaming_indexes
from .winners import stream_winners_to_chunks


def _allocated(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT name,SUM(pgsize) FROM dbstat GROUP BY name ORDER BY name"
    ).fetchall()
    return {str(name): int(size) for name, size in rows}


def _seed(
    path: Path,
    *,
    rows: int,
    payload_bytes: int,
    tracked: bool,
    batch_rows: int,
) -> dict[str, object]:
    graph = GraphDB(path)
    catalog = MutationCatalog(graph.conn, "a" * 64) if tracked else None
    if catalog is not None:
        catalog.install()
    ensure_streaming_indexes(graph.conn)
    suffix = "x" * payload_bytes
    started = time.perf_counter()
    try:
        for offset in range(0, rows, batch_rows):
            stop = min(rows, offset + batch_rows)
            context = (
                catalog.transaction(
                    1_787_000_000_000_000_000 + offset,
                    f"{offset:032x}",
                )
                if catalog is not None else graph.conn
            )
            with context:
                graph.conn.executemany(
                    "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (
                        (f"bench-{index:09d}", "note", f"title-{index}-{suffix}",
                         '{"benchmark":true}', "2026-08-19T00:00:00Z",
                         "2026-08-19T00:00:00Z")
                        for index in range(offset, stop)
                    ),
                )
        graph.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        allocation = _allocated(graph.conn)
        journal_rows = int(graph.conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0]) if tracked else 0
    finally:
        graph.close()
    elapsed = time.perf_counter() - started
    return {
        "path": str(path), "rows": rows, "payload_bytes": payload_bytes,
        "tracked": tracked, "batch_rows": batch_rows, "seconds": elapsed,
        "rows_per_second": rows / elapsed, "database_bytes": path.stat().st_size,
        "allocation": allocation,
        "journal_rows": journal_rows,
    }


def run(
    directory: Path, rows: int, payload_bytes: int, batch_rows: int
) -> dict[str, object]:
    directory.mkdir(parents=True, exist_ok=False)
    indexed = _seed(
        directory / "indexed.db", rows=rows,
        payload_bytes=payload_bytes, tracked=False, batch_rows=batch_rows,
    )
    tracked = _seed(
        directory / "tracked.db", rows=rows,
        payload_bytes=payload_bytes, tracked=True, batch_rows=batch_rows,
    )
    tracked_graph = GraphDB(directory / "tracked.db")
    try:
        tracked_catalog = MutationCatalog(tracked_graph.conn, "a" * 64)
        tracked_catalog.install()
        # Expression indexes call fleet_sha256_text during VACUUM rebuild.
        ensure_streaming_indexes(tracked_graph.conn)
        winners = stream_winners_to_chunks(
            tracked_catalog.iter_winner_metadata(), directory / "winners",
            through_watermark=1_787_000_000_000_000_000
            + ((rows - 1) // batch_rows) * batch_rows,
        )
        tracked_catalog.prune_journal((1 << 63) - 1)
        tracked_graph.conn.execute("VACUUM")
    finally:
        tracked_graph.close()
    steady_database_bytes = (directory / "tracked.db").stat().st_size
    catalog_names = {
        "fleet_sync_catalog", "fleet_sync_origins", "fleet_sync_state",
        "fleet_sync_transactions",
    }
    catalog_bytes = sum(
        size for name, size in tracked["allocation"].items()
        if name in catalog_names
    )
    journal_bytes = int(tracked["allocation"].get("fleet_sync_journal", 0))
    delta = int(tracked["database_bytes"]) - int(indexed["database_bytes"])
    result = {
        "indexed": indexed, "tracked": tracked,
        "catalog_allocated_bytes": catalog_bytes,
        "catalog_bytes_per_row": catalog_bytes / rows,
        "unacked_journal_allocated_bytes": journal_bytes,
        "unacked_journal_bytes_per_mutation": journal_bytes / rows,
        "file_delta_bytes": delta,
        "file_delta_bytes_per_row": delta / rows,
        "storage_overhead_percent_vs_indexed":
            100 * delta / int(indexed["database_bytes"]),
        "write_rate_ratio_tracked_to_indexed":
            float(tracked["rows_per_second"]) / float(indexed["rows_per_second"]),
        "winner_artifact_bytes": winners.total_bytes,
        "winner_artifact_bytes_per_address": winners.total_bytes / rows,
        "steady_database_bytes_after_prune_vacuum": steady_database_bytes,
        "steady_file_delta_bytes_per_row": (
            steady_database_bytes - int(indexed["database_bytes"])
        ) / rows,
    }
    (directory / "result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--payload-bytes", type=int, default=400)
    parser.add_argument("--batch-rows", type=int, default=5000)
    args = parser.parse_args()
    print(json.dumps(
        run(args.output, args.rows, args.payload_bytes, args.batch_rows),
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
