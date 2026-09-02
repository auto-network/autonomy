"""The consolidated benchmark drivers.

Each benchmark is a function taking a scratch directory and a scale
profile, returning ``{"status", "duration_s", "metrics", "detail"}``.
``metrics`` holds the direction-aware numbers the comparison table reads
(see ``baseline.METRIC_DIRECTIONS``); ``detail`` retains the driver's full
payload for machine readers.  All scratch data lives under the per-run
temporary directory and is deleted when the run ends.

The kernel workload mirrors the retained yardstick methodology
(sync-kernel-benchmark-results.md, 2026-09-01): N inserts of a
graph-note-shaped row plus M single-column updates, batched per authored
transaction, with one caught-up peer acknowledging after every batch so
the served-ack floor keeps the journal flat.  The cr-sqlite yardstick is
the same workload against the loadable extension and is skipped, not
failed, when the extension is absent.
"""

from __future__ import annotations

import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.graph.db import GraphDB

from ..catalog import MutationCatalog
from ..streaming import ensure_streaming_indexes
from .. import alpha_benchmark, live_workload, overhead_benchmark


@dataclass(frozen=True)
class Scale:
    name: str
    kernel_rows: int
    kernel_updates: int
    kernel_batch: int
    storage_rows: int
    checkpoint_rows: int
    payload_bytes: int
    batch_rows: int
    live_duration_s: float
    crsqlite_rows: int
    crsqlite_updates: int


SCALES = {
    "quick": Scale(
        "quick", kernel_rows=5_000, kernel_updates=10_000, kernel_batch=2_500,
        storage_rows=10_000, checkpoint_rows=10_000, payload_bytes=400,
        batch_rows=2_500, live_duration_s=1.5,
        crsqlite_rows=5_000, crsqlite_updates=10_000,
    ),
    "full": Scale(
        "full", kernel_rows=50_000, kernel_updates=100_000, kernel_batch=10_000,
        storage_rows=100_000, checkpoint_rows=100_000, payload_bytes=400,
        batch_rows=5_000, live_duration_s=5.0,
        crsqlite_rows=50_000, crsqlite_updates=100_000,
    ),
}

_BASE_TS = 1_787_000_000_000_000_000
_ACK_PEER = "b" * 64
_ACK_EPOCH = "perf-epoch"


def _tracking_buckets(conn: sqlite3.Connection) -> dict[str, int]:
    out = {"payload": 0, "tracking": 0, "other": 0}
    for name, size in conn.execute(
        "SELECT name,SUM(pgsize) FROM dbstat GROUP BY name"
    ).fetchall():
        text = str(name)
        if text.startswith(("fleet_sync_", "sqlite_autoindex_fleet_sync",
                            "idx_fleet_sync_")) or "_logical_key" in text:
            out["tracking"] += int(size)
        elif text == "sources" or text.startswith("sqlite_autoindex_sources"):
            out["payload"] += int(size)
        else:
            out["other"] += int(size)
    return out


def bench_kernel(scratch: Path, scale: Scale) -> dict[str, Any]:
    path = scratch / "kernel.db"
    graph = GraphDB(path)
    conn = graph.conn
    suffix = "x" * 180
    try:
        catalog = MutationCatalog(conn, "a" * 64)
        catalog.install()
        ensure_streaming_indexes(conn)

        def ack_batch() -> None:
            ref = catalog.newest_transaction_ref()
            if ref:
                catalog.record_served_ack(_ACK_PEER, _ACK_EPOCH, ref)
                catalog.prune_acknowledged([_ACK_PEER], _ACK_EPOCH)

        ts = _BASE_TS
        started = time.perf_counter()
        for offset in range(0, scale.kernel_rows, scale.kernel_batch):
            stop = min(scale.kernel_rows, offset + scale.kernel_batch)
            ts += 1
            with catalog.transaction(ts, f"{offset:032x}"):
                conn.executemany(
                    "INSERT INTO sources(id,type,title,metadata,created_at,"
                    "ingested_at) VALUES(?,?,?,?,?,?)",
                    (
                        (f"bench-{index:09d}", "note", f"title-{index}-{suffix}",
                         '{"benchmark":true}', "2026-08-19T00:00:00Z",
                         "2026-08-19T00:00:00Z")
                        for index in range(offset, stop)
                    ),
                )
            ack_batch()
        insert_s = time.perf_counter() - started

        started = time.perf_counter()
        for offset in range(0, scale.kernel_updates, scale.kernel_batch):
            stop = min(scale.kernel_updates, offset + scale.kernel_batch)
            ts += 1
            with catalog.transaction(ts, f"u{offset:031x}"):
                conn.executemany(
                    "UPDATE sources SET title=? WHERE id=?",
                    (
                        (f"title-{index}-{suffix}{index % 7}",
                         f"bench-{index % scale.kernel_rows:09d}")
                        for index in range(offset, stop)
                    ),
                )
            ack_batch()
        update_s = time.perf_counter() - started

        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        journal_rows = int(conn.execute(
            "SELECT COUNT(*) FROM fleet_sync_journal"
        ).fetchone()[0])
        file_full = path.stat().st_size

        started = time.perf_counter()
        catalog.prune_journal((1 << 63) - 1)
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        prune_s = time.perf_counter() - started
        buckets = _tracking_buckets(conn)
        file_steady = path.stat().st_size
    finally:
        graph.close()
    overhead = (
        100 * buckets["tracking"] / buckets["payload"]
        if buckets["payload"] else 0.0
    )
    return {
        "metrics": {
            "insert_rows_per_s": scale.kernel_rows / insert_s,
            "update_rows_per_s": scale.kernel_updates / update_s,
            "file_full_bytes": file_full,
            "file_steady_bytes": file_steady,
            "steady_tracking_overhead_percent": overhead,
            "journal_rows_after_ack": journal_rows,
            "prune_vacuum_s": prune_s,
        },
        "detail": {
            "rows": scale.kernel_rows, "updates": scale.kernel_updates,
            "batch": scale.kernel_batch, "insert_s": insert_s,
            "update_s": update_s, "buckets_steady": buckets,
        },
    }


def bench_storage(scratch: Path, scale: Scale) -> dict[str, Any]:
    detail = overhead_benchmark.run(
        scratch / "storage", scale.storage_rows, scale.payload_bytes,
        scale.batch_rows,
    )
    return {
        "metrics": {
            "storage_overhead_percent_vs_indexed":
                detail["storage_overhead_percent_vs_indexed"],
            "steady_file_delta_bytes_per_row":
                detail["steady_file_delta_bytes_per_row"],
            "catalog_bytes_per_row": detail["catalog_bytes_per_row"],
            "write_rate_ratio_tracked_to_indexed":
                detail["write_rate_ratio_tracked_to_indexed"],
            "tracked_rows_per_s": detail["tracked"]["rows_per_second"],
        },
        "detail": detail,
    }


def bench_checkpoint(scratch: Path, scale: Scale) -> dict[str, Any]:
    detail = alpha_benchmark.run(
        scratch / "checkpoint", rows=scale.checkpoint_rows,
        payload_bytes=scale.payload_bytes, batch_rows=scale.batch_rows,
        segment_bytes=4 * 1024 * 1024, symbol_size=8192,
    )
    seed_wall = float(detail["seed"]["wall_seconds"])
    return {
        "metrics": {
            "seed_rows_per_s": scale.checkpoint_rows / seed_wall,
            "checkpoint_wall_s": detail["checkpoint"]["wall_seconds"],
            "raptorq_transport_wall_s":
                detail["raptorq_transport"]["wall_seconds"],
            "install_wall_s": detail["install"]["wall_seconds"],
            "checkpoint_artifact_bytes": detail["checkpoint_artifact_bytes"],
        },
        "detail": detail,
    }


def bench_live(scratch: Path, scale: Scale) -> dict[str, Any]:
    report = live_workload.run_live_workload(
        scratch / "live", duration_seconds=scale.live_duration_s,
    )
    detail = report.as_dict()
    return {
        "metrics": {
            "writer_transactions_per_s":
                detail["writer_transactions_per_second"],
            "max_lag_transactions": detail["max_lag_transactions"],
            "read_errors": detail["read_errors"],
            "final_drain_s": detail["final_drain_seconds"],
        },
        "detail": detail,
    }


def _find_crsqlite_extension() -> Path | None:
    candidates = [os.environ.get("FLEET_SYNC_CRSQLITE_EXT")]
    from .baseline import default_store
    store = default_store()
    for stem in (store / "crsqlite" / "crsqlite",
                 Path("/tmp/crsqlite-ext/crsqlite")):
        candidates.extend((str(stem), f"{stem}.so", f"{stem}.dylib"))
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    return None


def bench_crsqlite(scratch: Path, scale: Scale) -> dict[str, Any]:
    extension = _find_crsqlite_extension()
    if extension is None:
        return {
            "status": "skipped",
            "reason": "cr-sqlite loadable extension not found "
                      "(set FLEET_SYNC_CRSQLITE_EXT or place it under "
                      "<baseline store>/crsqlite/)",
        }
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        return {"status": "skipped",
                "reason": "sqlite3 built without loadable extensions"}

    def connect(path: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(path)
        conn.enable_load_extension(True)
        conn.load_extension(str(extension).removesuffix(".so"))
        conn.execute("pragma journal_mode=wal")
        conn.execute("pragma synchronous=normal")
        return conn

    payload = "x" * 180
    path = scratch / "crsqlite.db"
    conn = connect(path)
    try:
        conn.execute(
            "create table notes (id text primary key not null, body text,"
            " tags text, author text, ts integer, kind text)"
        )
        conn.execute("select crsql_as_crr('notes')")
        started = time.perf_counter()
        conn.execute("begin")
        for index in range(scale.crsqlite_rows):
            conn.execute(
                "insert into notes values (?,?,?,?,?,?)",
                (f"note-{index:08d}", payload, "tag1,tag2", "op", index,
                 "note"),
            )
        conn.execute("commit")
        insert_s = time.perf_counter() - started

        started = time.perf_counter()
        conn.execute("begin")
        for index in range(scale.crsqlite_updates):
            conn.execute(
                "update notes set body=? where id=?",
                (payload + str(index % 7),
                 f"note-{index % scale.crsqlite_rows:08d}"),
            )
        conn.execute("commit")
        update_s = time.perf_counter() - started

        conn.execute("pragma wal_checkpoint(truncate)")
        changes = conn.execute(
            'select "table","pk","cid","val","col_version","db_version",'
            '"site_id","cl","seq" from crsql_changes'
        ).fetchall()
        wire_bytes = sum(
            sum(len(x) if isinstance(x, (bytes, str)) else 8 for x in row)
            for row in changes
        )
        # crsql holds prepared state; finalize before measuring the file.
        conn.execute("select crsql_finalize()")
    finally:
        conn.close()
    return {
        "metrics": {
            "insert_rows_per_s": scale.crsqlite_rows / insert_s,
            "update_rows_per_s": scale.crsqlite_updates / update_s,
            "db_file_bytes": path.stat().st_size,
            "delta_wire_bytes": wire_bytes,
        },
        "detail": {
            "extension": str(extension), "change_rows": len(changes),
            "insert_s": insert_s, "update_s": update_s,
        },
    }


BENCHMARKS: dict[str, Callable[[Path, Scale], dict[str, Any]]] = {
    "kernel": bench_kernel,
    "storage": bench_storage,
    "checkpoint": bench_checkpoint,
    "live": bench_live,
    "crsqlite": bench_crsqlite,
}


def run_benchmark(
    name: str, scratch: Path, scale: Scale
) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        base = scratch / name
        base.mkdir(parents=True, exist_ok=True)
        entry = BENCHMARKS[name](base, scale)
    except Exception as error:  # reported per-benchmark, never fatal
        return {
            "status": "error",
            "error": f"{type(error).__name__}: {error}",
            "duration_s": time.perf_counter() - started,
        }
    entry.setdefault("status", "pass")
    entry["duration_s"] = time.perf_counter() - started
    return entry
