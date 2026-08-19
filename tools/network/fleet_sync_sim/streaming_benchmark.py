"""Generate and measure the indexed bounded-memory base codec."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import resource
import sqlite3
import time

from tools.graph.db import GraphDB

from .streaming import (
    ensure_streaming_indexes,
    indexed_query_plan,
    iter_catalog_mutations,
    materialize_catalog,
    stream_snapshot_to_chunks,
)


def _rss_kib() -> int:
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def seed(path: Path, rows: int, payload_bytes: int) -> dict:
    if path.exists():
        raise FileExistsError(path)
    graph = GraphDB(path)
    started = time.perf_counter()
    title_suffix = "x" * payload_bytes
    try:
        graph.conn.execute("PRAGMA synchronous=OFF")
        for offset in range(0, rows, 5000):
            stop = min(rows, offset + 5000)
            graph.conn.executemany(
                "INSERT INTO sources(id,type,title,metadata,created_at,ingested_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    (
                        f"bench-{index:09d}", "note",
                        f"bench-{index:09d}-{title_suffix}",
                        '{"benchmark":true}', "2026-08-19T10:00:00Z",
                        "2026-08-19T10:00:01Z",
                    )
                    for index in range(offset, stop)
                ),
            )
            graph.conn.commit()
    finally:
        graph.close()
    return {
        "stage": "seed", "rows": rows, "payload_bytes": payload_bytes,
        "seconds": time.perf_counter() - started,
        "database_bytes": path.stat().st_size,
        "peak_rss_kib": _rss_kib(),
    }


def index(path: Path) -> dict:
    before = path.stat().st_size
    conn = sqlite3.connect(path)
    started = time.perf_counter()
    try:
        indexes = ensure_streaming_indexes(conn)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        plans = {table: indexed_query_plan(conn, table) for table in indexes}
    finally:
        conn.close()
    after = path.stat().st_size
    return {
        "stage": "index", "seconds": time.perf_counter() - started,
        "database_bytes_before": before, "database_bytes_after": after,
        "index_bytes": after - before, "plans": plans,
        "peak_rss_kib": _rss_kib(),
    }


def encode(path: Path, output: Path, target_bytes: int) -> dict:
    conn = sqlite3.connect(path)
    started = time.perf_counter()
    try:
        conn.execute("BEGIN")
        catalog = stream_snapshot_to_chunks(
            conn, output, target_chunk_bytes=target_bytes
        )
        conn.rollback()
    finally:
        conn.close()
    elapsed = time.perf_counter() - started
    return {
        "stage": "encode", "seconds": elapsed,
        "records": catalog.total_records, "encoded_bytes": catalog.total_bytes,
        "chunks": len(catalog.chunks), "root_sha256": catalog.root_sha256,
        "mib_per_second": catalog.total_bytes / elapsed / (1024 * 1024),
        "records_per_second": catalog.total_records / elapsed,
        "peak_rss_kib": _rss_kib(),
    }


def decode(output: Path) -> dict:
    raw = json.loads((output / "catalog.json").read_text())
    from .streaming import BaseCatalog, ChunkEntry
    catalog = BaseCatalog(
        raw["version"], raw["policy_digest"], raw["target_chunk_bytes"],
        raw["total_records"], raw["total_bytes"], raw["root_sha256"],
        tuple(ChunkEntry(**entry) for entry in raw["chunks"]),
    )
    started = time.perf_counter()
    count = sum(1 for _ in iter_catalog_mutations(output, catalog))
    elapsed = time.perf_counter() - started
    return {
        "stage": "decode", "seconds": elapsed, "records": count,
        "mib_per_second": catalog.total_bytes / elapsed / (1024 * 1024),
        "records_per_second": count / elapsed,
        "peak_rss_kib": _rss_kib(),
    }


def realize(output: Path, target: Path, batch_records: int) -> dict:
    if target.exists():
        raise FileExistsError(target)
    raw = json.loads((output / "catalog.json").read_text())
    from .streaming import BaseCatalog, ChunkEntry
    catalog = BaseCatalog(
        raw["version"], raw["policy_digest"], raw["target_chunk_bytes"],
        raw["total_records"], raw["total_bytes"], raw["root_sha256"],
        tuple(ChunkEntry(**entry) for entry in raw["chunks"]),
    )
    graph = GraphDB(target)
    started = time.perf_counter()
    try:
        report = materialize_catalog(
            graph.conn, output, catalog, batch_records=batch_records
        )
    finally:
        graph.close()
    elapsed = time.perf_counter() - started
    return {
        "stage": "materialize", "seconds": elapsed,
        "applied": report.applied, "deleted": report.deleted,
        "pending_attachments": len(report.pending_attachments),
        "mib_per_second": catalog.total_bytes / elapsed / (1024 * 1024),
        "records_per_second": report.applied / elapsed,
        "database_bytes": target.stat().st_size,
        "peak_rss_kib": _rss_kib(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "stage", choices=("seed", "index", "encode", "decode", "materialize")
    )
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--payload-bytes", type=int, default=400)
    parser.add_argument("--target-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument("--target-database", type=Path)
    parser.add_argument("--batch-records", type=int, default=1024)
    args = parser.parse_args()
    if args.stage == "seed":
        result = seed(args.database, args.rows, args.payload_bytes)
    elif args.stage == "index":
        result = index(args.database)
    elif args.stage == "encode":
        if args.output is None:
            parser.error("encode requires --output")
        result = encode(args.database, args.output, args.target_bytes)
    elif args.stage == "decode":
        if args.output is None:
            parser.error("decode requires --output")
        result = decode(args.output)
    else:
        if args.output is None or args.target_database is None:
            parser.error("materialize requires --output and --target-database")
        result = realize(args.output, args.target_database, args.batch_records)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
