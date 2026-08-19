"""Reproducible end-to-end Alpha 1.0 lifecycle benchmark."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import resource
import shutil
import sqlite3
import tempfile
import time

from .alpha import (
    FleetSyncAlpha, install_checkpoint, transport_checkpoint_via_raptorq,
)


@dataclass(frozen=True)
class Stage:
    wall_seconds: float
    cpu_seconds: float


def _stage(function):
    wall = time.perf_counter()
    cpu = time.process_time()
    value = function()
    return value, Stage(time.perf_counter() - wall, time.process_time() - cpu)


def _digest(path: Path) -> tuple[int, str]:
    conn = sqlite3.connect(path)
    digest = hashlib.sha256()
    count = 0
    try:
        for identity, title in conn.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ):
            body = f"{identity}\0{title}".encode()
            digest.update(len(body).to_bytes(4, "big"))
            digest.update(body)
            count += 1
    finally:
        conn.close()
    return count, digest.hexdigest()


def run(
    output: Path, *, rows: int, payload_bytes: int, batch_rows: int,
    segment_bytes: int, symbol_size: int,
) -> dict[str, object]:
    output.mkdir(parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="fleet-alpha-bench-"))
    source = scratch / "source.db"
    target = scratch / "target.db"
    checkpoint = scratch / "checkpoint"
    received = scratch / "received"
    suffix = "x" * payload_bytes
    try:
        def seed() -> int:
            with FleetSyncAlpha(source, "machine-a") as alpha:
                for offset in range(0, rows, batch_rows):
                    stop = min(rows, offset + batch_rows)
                    with alpha.author(
                        1_787_000_000_000_000_000 + offset,
                        f"seed-{offset:012d}",
                    ):
                        alpha.graph.conn.executemany(
                            "INSERT INTO sources(id,type,title,metadata,created_at,"
                            "ingested_at) VALUES(?,?,?,?,?,?)",
                            (
                                (f"bench-{index:09d}", "note",
                                 f"title-{index}-{suffix}", "{}",
                                 "2026-08-19T00:00:00Z",
                                 "2026-08-19T00:00:00Z")
                                for index in range(offset, stop)
                            ),
                        )
            return rows

        _, seed_stage = _stage(seed)

        def build():
            with FleetSyncAlpha(source, "machine-a") as alpha:
                return alpha.checkpoint(
                    checkpoint, roster_epoch=1,
                    active_roster=("machine-a", "machine-b"),
                    target_chunk_bytes=segment_bytes,
                )

        checkpoint_result, checkpoint_stage = _stage(build)
        _, transport_stage = _stage(lambda: transport_checkpoint_via_raptorq(
            checkpoint, received, symbol_size=symbol_size
        ))
        _, install_stage = _stage(lambda: install_checkpoint(
            received, target, target_origin_incarnation="machine-b",
            expected_roster_epoch=1,
            expected_active_roster=("machine-a", "machine-b"),
        ))
        source_digest = _digest(source)
        target_digest = _digest(target)
        if source_digest != target_digest:
            raise AssertionError("Alpha lifecycle changed the logical source rows")
        artifact_bytes = sum(
            path.stat().st_size for path in checkpoint.rglob("*") if path.is_file()
        )
        result: dict[str, object] = {
            "status": "pass", "rows": rows, "payload_bytes": payload_bytes,
            "batch_rows": batch_rows, "segment_bytes": segment_bytes,
            "symbol_size": symbol_size, "source_database_bytes": source.stat().st_size,
            "checkpoint_artifact_bytes": artifact_bytes,
            "base_records": checkpoint_result.base_records,
            "winner_records": checkpoint_result.winner_records,
            "logical_digest": source_digest[1],
            "seed": asdict(seed_stage), "checkpoint": asdict(checkpoint_stage),
            "raptorq_transport": asdict(transport_stage),
            "install": asdict(install_stage),
            "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
        (output / "evidence.json").write_text(
            json.dumps(result, sort_keys=True, indent=2) + "\n"
        )
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument("--payload-bytes", type=int, default=400)
    parser.add_argument("--batch-rows", type=int, default=5000)
    parser.add_argument("--segment-mib", type=int, default=4)
    parser.add_argument("--symbol-size", type=int, default=8192)
    args = parser.parse_args()
    print(json.dumps(run(
        args.output, rows=args.rows, payload_bytes=args.payload_bytes,
        batch_rows=args.batch_rows, segment_bytes=args.segment_mib * 1024 * 1024,
        symbol_size=args.symbol_size,
    )))


if __name__ == "__main__":
    main()
