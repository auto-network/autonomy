"""Concurrent read/write/checkpoint workload for the 1.0-alpha gate."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import argparse
import json
from pathlib import Path
import sqlite3
import threading
import time

from .alpha import (
    FleetSyncAlpha, install_checkpoint,
    transport_checkpoint_via_raptorq, transport_delta_reliable,
)
from .delta import apply_delta_catalog, read_delta_catalog


@dataclass(frozen=True)
class LiveWorkloadReport:
    duration_seconds: float
    final_drain_seconds: float
    committed_transactions: int
    checkpoints_installed: int
    source_reads: int
    target_reads: int
    read_errors: int
    max_lag_transactions: int
    final_lag_transactions: int
    writer_transactions_per_second: float
    final_source_rows: int
    final_target_rows: int

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def run_live_workload(
    root: Path,
    *,
    duration_seconds: float = 2.0,
    checkpoint_interval: float = 0.15,
    writes_per_transaction: int = 8,
    writer_interval_seconds: float = 0.005,
) -> LiveWorkloadReport:
    root.mkdir(parents=True, exist_ok=False)
    source_path = root / "source.db"
    target_path = root / "target.db"
    # Install schema/triggers before concurrent connections start.
    with FleetSyncAlpha(source_path, "machine-a"):
        pass
    stop = threading.Event()
    state_lock = threading.Lock()
    committed = 0
    source_reads = 0
    target_reads = 0
    read_errors = 0
    writer_failure: BaseException | None = None

    def writer() -> None:
        nonlocal committed, writer_failure
        try:
            with FleetSyncAlpha(source_path, "machine-a") as alpha:
                tx = 0
                while not stop.is_set():
                    tx += 1
                    timestamp = 1_000_000 + tx
                    with alpha.author(timestamp, f"live-{tx:09d}"):
                        for offset in range(writes_per_transaction):
                            identity = f"live-{tx:09d}-{offset:03d}"
                            alpha.graph.conn.execute(
                                "INSERT INTO sources(id,type,title,metadata,created_at,"
                                "ingested_at) VALUES(?,?,?,?,?,?)",
                                (identity, "note", f"transaction {tx}", "{}",
                                 "2026-08-19T00:00:00Z", "2026-08-19T00:00:00Z"),
                            )
                        if tx > 2:
                            alpha.graph.conn.execute(
                                "DELETE FROM sources WHERE id=?",
                                (f"live-{tx - 2:09d}-000",),
                            )
                    with state_lock:
                        committed = tx
                    if writer_interval_seconds:
                        time.sleep(writer_interval_seconds)
        except BaseException as exc:  # reported on the controlling thread
            writer_failure = exc
            stop.set()

    def reader() -> None:
        nonlocal source_reads, target_reads, read_errors
        while not stop.is_set():
            try:
                source = sqlite3.connect(source_path, timeout=2)
                source.execute("SELECT COUNT(*),COALESCE(MAX(id),'') FROM sources").fetchone()
                source.close()
                source_reads += 1
                if target_path.exists():
                    target = sqlite3.connect(target_path, timeout=2)
                    target.execute("SELECT COUNT(*),COALESCE(MAX(id),'') FROM sources").fetchone()
                    target.close()
                    target_reads += 1
            except sqlite3.Error:
                read_errors += 1
            time.sleep(0.002)

    sync_source = FleetSyncAlpha(source_path, "machine-a")
    writer_thread = threading.Thread(target=writer, name="fleet-alpha-writer")
    reader_thread = threading.Thread(target=reader, name="fleet-alpha-reader")
    checkpoints = 0
    installed_tx = 0
    max_lag = 0
    target_engine: FleetSyncAlpha | None = None
    started = time.perf_counter()
    active_elapsed = 0.0
    threads_started = False
    try:
        # Establish the initial empty/base state before starting the timed hot
        # workload.  The measurement below is then specifically whether live
        # transaction deltas keep pace, not how long first hydration takes.
        outbound = root / "outbound-base"
        received = root / "received-base"
        checkpoint = sync_source.checkpoint(
            outbound, roster_epoch=1, active_roster=("machine-a", "machine-b"),
            target_chunk_bytes=64 * 1024,
        )
        transport_checkpoint_via_raptorq(outbound, received, symbol_size=1024)
        install_checkpoint(
            received, target_path, target_origin_incarnation="machine-b",
            expected_roster_epoch=1,
            expected_active_roster=("machine-a", "machine-b"),
        )
        checkpoints += 1
        installed_tx = max(0, checkpoint.watermark - 1_000_000)
        target_engine = FleetSyncAlpha(target_path, "machine-b")
        started = time.perf_counter()
        writer_thread.start()
        reader_thread.start()
        threads_started = True
        while time.perf_counter() - started < duration_seconds and not stop.is_set():
            with state_lock:
                max_lag = max(max_lag, committed - installed_tx)
            sequence = checkpoints
            outbound_delta = root / f"outbound-delta-{sequence:04d}"
            received_delta = root / f"received-delta-{sequence:04d}"
            delta = sync_source.delta(
                outbound_delta, after_watermark=1_000_000 + installed_tx,
                target_chunk_bytes=64 * 1024,
            )
            transport_delta_reliable(outbound_delta, received_delta)
            received_catalog = read_delta_catalog(received_delta)
            assert received_catalog == delta
            apply_delta_catalog(
                target_engine.catalog, received_delta, received_catalog
            )
            checkpoints += 1
            installed_tx = max(0, delta.through_watermark - 1_000_000)
            with state_lock:
                max_lag = max(max_lag, committed - installed_tx)
            time.sleep(checkpoint_interval)
    finally:
        active_elapsed = time.perf_counter() - started
        stop.set()
        if target_engine is not None:
            target_engine.close()
        sync_source.close()
    if threads_started:
        writer_thread.join(timeout=10)
        reader_thread.join(timeout=10)
    if writer_failure is not None:
        raise writer_failure
    # One final cut is the liveness assertion: a finite tail must drain.
    drain_started = time.perf_counter()
    with FleetSyncAlpha(source_path, "machine-a") as source, FleetSyncAlpha(
        target_path, "machine-b"
    ) as target_engine:
        final_out = root / "outbound-delta-final"
        final_in = root / "received-delta-final"
        delta = source.delta(
            final_out, after_watermark=1_000_000 + installed_tx,
            target_chunk_bytes=64 * 1024,
        )
        transport_delta_reliable(final_out, final_in)
        received_catalog = read_delta_catalog(final_in)
        assert received_catalog == delta
        apply_delta_catalog(target_engine.catalog, final_in, received_catalog)
        installed_tx = max(0, delta.through_watermark - 1_000_000)
        checkpoints += 1
    drain_seconds = time.perf_counter() - drain_started
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(target_path)
    try:
        source_rows = int(source.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        target_rows = int(target.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
        if source.execute("SELECT id,title FROM sources ORDER BY id").fetchall() != target.execute(
            "SELECT id,title FROM sources ORDER BY id"
        ).fetchall():
            raise AssertionError("final target does not equal the final source cut")
    finally:
        source.close()
        target.close()
    return LiveWorkloadReport(
        active_elapsed, drain_seconds, committed, checkpoints,
        source_reads, target_reads, read_errors,
        max_lag, committed - installed_tx, committed / active_elapsed,
        source_rows, target_rows,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--checkpoint-interval", type=float, default=0.15)
    parser.add_argument("--writes-per-transaction", type=int, default=8)
    parser.add_argument("--writer-interval", type=float, default=0.005)
    args = parser.parse_args()
    report = run_live_workload(
        args.output / "run", duration_seconds=args.duration,
        checkpoint_interval=args.checkpoint_interval,
        writes_per_transaction=args.writes_per_transaction,
        writer_interval_seconds=args.writer_interval,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    body = json.dumps(report.as_dict(), sort_keys=True, indent=2) + "\n"
    (args.output / "live-workload.json").write_text(body)
    print(body, end="")


if __name__ == "__main__":
    main()
