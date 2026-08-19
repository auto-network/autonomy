"""Fixed-total 500 MiB checkpoint chain sweep for the alpha release."""

from __future__ import annotations

import argparse
from concurrent.futures import ProcessPoolExecutor
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import resource
import shutil
import tempfile
import time
from typing import Any

from raptorq import Decoder

from tools.graph.db import GraphDB
from tools.network.swarmkit.fountain import FountainStore, source_symbols

from .codec import encode_mutation_frame
from .streaming import (
    BaseCatalog, ChunkEntry, ensure_streaming_indexes,
    iter_catalog_mutations, materialize_catalog, stream_snapshot_to_chunks,
)
from .streaming_benchmark import seed


def render_evidence(output: Path, result: dict[str, Any]) -> None:
    """Write a human-readable report and dependency-free throughput chart."""
    configurations = result["configurations"]
    colors = {"1": "#60a5fa", "2": "#34d399", "4": "#f59e0b"}
    maximum = max(
        float(config["raptorq"][workers]["mib_per_second"])
        for config in configurations for workers in ("1", "2", "4")
    )
    width, height = 900, 440
    margin_left, margin_bottom, plot_height = 72, 70, 320
    group_width = (width - margin_left - 30) / len(configurations)
    bar_width = group_width / 5
    bars: list[str] = []
    labels: list[str] = []
    for group, config in enumerate(configurations):
        origin = margin_left + group * group_width
        for offset, workers in enumerate(("1", "2", "4"), 1):
            value = float(config["raptorq"][workers]["mib_per_second"])
            bar_height = plot_height * value / maximum
            x = origin + offset * bar_width
            y = 20 + plot_height - bar_height
            bars.append(
                f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width * .8:.1f}" '
                f'height="{bar_height:.1f}" rx="3" fill="{colors[workers]}"/>'
            )
            bars.append(
                f'<text x="{x + bar_width * .4:.1f}" y="{y - 5:.1f}" '
                f'text-anchor="middle" fill="#dbeafe" font-size="10">{value:.1f}</text>'
            )
        labels.append(
            f'<text x="{origin + group_width / 2:.1f}" y="{height - 30}" '
            f'text-anchor="middle" fill="#cbd5e1">{config["target_mib"]} MiB</text>'
        )
    legend = "".join(
        f'<rect x="{560 + index * 95}" y="8" width="12" height="12" fill="{colors[key]}"/>'
        f'<text x="{577 + index * 95}" y="19" fill="#cbd5e1" font-size="12">{key} core</text>'
        for index, key in enumerate(("1", "2", "4"))
    )
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#0f172a"/>'
        '<text x="24" y="22" fill="#f8fafc" font-size="16" font-weight="700">'
        'RaptorQ whole-checkpoint throughput (MiB/s)</text>' + legend
        + f'<line x1="{margin_left}" y1="340" x2="870" y2="340" stroke="#64748b"/>'
        + "".join(bars) + "".join(labels) + '</svg>\n'
    )
    (output / "throughput.svg").write_text(svg)

    logical_bytes = next(
        int(config["decode"]["logical_framed_bytes"])
        for config in configurations
        if "logical_framed_bytes" in config["decode"]
    )
    for config in configurations:
        config["decode"].setdefault("logical_framed_bytes", logical_bytes)
    lines = [
        "# Fixed-total checkpoint chain sweep", "",
        f"Logical input: {logical_bytes:,} bytes; "
        f"rows: {int(result['rows']):,}; symbol size: {int(result['symbol_size']):,} bytes.",
        "",
        "| Segment | Objects | Tail | Encode s | Decode s | Materialize s | "
        "RQ 1c MiB/s | RQ 2c MiB/s | RQ 4c MiB/s | RQ egress | "
        "4c RSS upper MiB | Lifecycle 4c s |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for config in configurations:
        four = config["raptorq"]["4"]
        upper = four.get(
            "pool_peak_rss_upper_bound_kib",
            float(four["max_worker_peak_rss_kib"]) * 4,
        )
        lines.append(
            f"| {config['target_mib']} MiB | {config['encode']['chunks']} | "
            f"{config['encode']['tail_bytes'] / (config['target_mib'] * 1024 * 1024):.1%} | "
            f"{config['encode']['seconds']:.2f} | {config['decode']['seconds']:.2f} | "
            f"{config['materialize']['seconds']:.2f} | "
            f"{config['raptorq']['1']['mib_per_second']:.2f} | "
            f"{config['raptorq']['2']['mib_per_second']:.2f} | "
            f"{four['mib_per_second']:.2f} | "
            f"{float(four['aggregate_egress_ratio']):.4f}x | "
            f"{float(upper) / 1024:.1f} | "
            f"{float(config['encode']['seconds']) + float(four['seconds']) + float(config['materialize']['seconds']):.2f} |"
        )
    totals = {
        int(config["target_mib"]): (
            float(config["encode"]["seconds"])
            + float(config["raptorq"]["4"]["seconds"])
            + float(config["materialize"]["seconds"])
        )
        for config in configurations
    }
    best = min(totals, key=totals.get)
    lines.extend([
        "", f"Lowest measured four-worker lifecycle time: **{best} MiB** "
        f"segments ({totals[best]:.2f}s). Object count, retry granularity, "
        "parallel throughput, and worker memory are reported separately so "
        "the default follows the whole measured trade-off rather than one stage.",
        "", "![Throughput sweep](throughput.svg)", "",
    ])
    (output / "report.md").write_text("\n".join(lines))


def _catalog(directory: Path) -> BaseCatalog:
    raw = json.loads((directory / "catalog.json").read_text())
    return BaseCatalog(
        raw["version"], raw["policy_digest"], raw["target_chunk_bytes"],
        raw["total_records"], raw["total_bytes"], raw["root_sha256"],
        tuple(ChunkEntry(**entry) for entry in raw["chunks"]),
    )


def _encode_worker(spec: tuple[str, str, int]) -> dict[str, Any]:
    database, output, chunk_bytes = spec
    conn = __import__("sqlite3").connect(database)
    started = time.perf_counter()
    cpu = time.process_time()
    try:
        catalog = stream_snapshot_to_chunks(
            conn, Path(output), target_chunk_bytes=chunk_bytes
        )
    finally:
        conn.close()
    return {
        "seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "records": catalog.total_records,
        "bytes": catalog.total_bytes,
        "chunks": len(catalog.chunks),
        "root": catalog.root_sha256,
        "tail_bytes": catalog.chunks[-1].bytes,
    }


def _decode_worker(output: str) -> dict[str, Any]:
    directory = Path(output)
    catalog = _catalog(directory)
    digest = hashlib.sha256()
    started = time.perf_counter()
    cpu = time.process_time()
    records = 0
    logical_framed_bytes = 0
    for mutation in iter_catalog_mutations(directory, catalog):
        frame = encode_mutation_frame(mutation)
        digest.update(len(frame).to_bytes(4, "big"))
        digest.update(frame)
        logical_framed_bytes += 4 + len(frame)
        records += 1
    return {
        "seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "records": records,
        "logical_framed_bytes": logical_framed_bytes,
        "logical_digest": digest.hexdigest(),
    }


def _raptorq_worker(spec: tuple[str, int]) -> dict[str, Any]:
    filename, symbol_size = spec
    payload = Path(filename).read_bytes()
    started = time.perf_counter()
    cpu = time.process_time()
    store = FountainStore(stripe=0, n_stripes=1)
    artifact = store.add_object(payload, symbol_size=symbol_size)
    manifest = store.manifest(artifact)
    decoder = Decoder.with_defaults(manifest["size"], manifest["symbol_size"])
    packets = store.serve(artifact, source_symbols(manifest) + 16, [])
    decoded = None
    used = 0
    served_bytes = 0
    for packet in packets:
        used += 1
        served_bytes += len(packet)
        decoded = decoder.decode(packet)
        if decoded is not None:
            break
    if decoded is None or bytes(decoded) != payload:
        raise AssertionError("RaptorQ chain object failed reconstruction")
    return {
        "worker_pid": os.getpid(),
        "object_bytes": len(payload), "packets_used": used,
        "served_bytes": served_bytes,
        "seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }


def _materialize_worker(spec: tuple[str, str]) -> dict[str, Any]:
    output, target = map(Path, spec)
    catalog = _catalog(output)
    graph = GraphDB(target)
    started = time.perf_counter()
    cpu = time.process_time()
    try:
        report = materialize_catalog(graph.conn, output, catalog, batch_records=1024)
    finally:
        graph.close()
    result = {
        "seconds": time.perf_counter() - started,
        "cpu_seconds": time.process_time() - cpu,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "applied": report.applied, "deleted": report.deleted,
        "database_bytes": target.stat().st_size,
    }
    target.unlink()
    return result


def _one_process(function, argument):
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
        return pool.submit(function, argument).result()


def run(
    output: Path, *, rows: int, payload_bytes: int,
    segment_mib: tuple[int, ...] = (8, 16, 32, 64),
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    scratch = Path(tempfile.mkdtemp(prefix="fleet-chain-"))
    database = scratch / "personal.db"
    try:
        seeded = seed(database, rows, payload_bytes)
        conn = __import__("sqlite3").connect(database)
        try:
            before = database.stat().st_size
            started = time.perf_counter()
            ensure_streaming_indexes(conn)
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            indexed = {
                "seconds": time.perf_counter() - started,
                "bytes": database.stat().st_size - before,
            }
        finally:
            conn.close()
        result: dict[str, Any] = {
            "rows": rows, "payload_bytes": payload_bytes,
            "source_database_bytes": database.stat().st_size,
            "seed": seeded, "index": indexed, "configurations": [],
            "symbol_size": 8192,
        }
        digests: set[str] = set()
        if not segment_mib or any(mib <= 0 for mib in segment_mib):
            raise ValueError("segment sizes must be positive")
        for mib in segment_mib:
            directory = scratch / f"chunks-{mib}"
            encoded = _one_process(
                _encode_worker, (str(database), str(directory), mib * 1024 * 1024)
            )
            decoded = _one_process(_decode_worker, str(directory))
            digests.add(decoded["logical_digest"])
            catalog = _catalog(directory)
            transport: dict[str, Any] = {}
            for workers in (1, 2, 4):
                context = multiprocessing.get_context("spawn")
                started = time.perf_counter()
                with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
                    rows_out = list(pool.map(
                        _raptorq_worker,
                        ((str(directory / entry.filename), 8192)
                         for entry in catalog.chunks),
                    ))
                elapsed = time.perf_counter() - started
                process_peaks: dict[int, int] = {}
                for row in rows_out:
                    pid = int(row["worker_pid"])
                    process_peaks[pid] = max(
                        process_peaks.get(pid, 0), int(row["peak_rss_kib"])
                    )
                transport[str(workers)] = {
                    "seconds": elapsed,
                    "cpu_seconds": sum(row["cpu_seconds"] for row in rows_out),
                    "pool_peak_rss_upper_bound_kib": sum(process_peaks.values()),
                    "max_worker_peak_rss_kib": max(
                        row["peak_rss_kib"] for row in rows_out
                    ),
                    "worker_processes_observed": len(process_peaks),
                    "served_bytes": sum(row["served_bytes"] for row in rows_out),
                    "aggregate_egress_ratio": sum(
                        row["served_bytes"] for row in rows_out
                    ) / catalog.total_bytes,
                    "mib_per_second": catalog.total_bytes / elapsed / (1024 * 1024),
                    "effective_cores": sum(
                        row["cpu_seconds"] for row in rows_out
                    ) / elapsed,
                }
            materialized = _one_process(
                _materialize_worker,
                (str(directory), str(scratch / f"realized-{mib}.db")),
            )
            result["configurations"].append({
                "target_mib": mib, "encode": encoded, "decode": decoded,
                "raptorq": transport, "materialize": materialized,
                "file_count": len(catalog.chunks) + 1,
                "recovery_granularity_bytes": mib * 1024 * 1024,
            })
            progress = json.dumps(result, sort_keys=True, indent=2) + "\n"
            (output / "progress.json").write_text(progress)
            shutil.rmtree(directory)
        if len(digests) != 1:
            raise AssertionError("chunk sizes changed the logical checkpoint digest")
        result["logical_digest"] = next(iter(digests))
        body = json.dumps(result, sort_keys=True, indent=2) + "\n"
        (output / "evidence.json").write_text(body)
        render_evidence(output, result)
        return result
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rows", type=int, default=1_000_000)
    parser.add_argument("--payload-bytes", type=int, default=250)
    parser.add_argument(
        "--segment-mib", type=int, nargs="+", default=[8, 16, 32, 64],
        help="one or more immutable object sizes to measure",
    )
    parser.add_argument("--render-existing", action="store_true")
    args = parser.parse_args()
    if args.render_existing:
        path = args.output / "evidence.json"
        result = json.loads(path.read_text())
        for config in result["configurations"]:
            chunks = int(config["encode"]["chunks"])
            for workers, transport in config["raptorq"].items():
                # Early alpha evidence named a sum that counted a reused
                # process once per task. Replace it with the conservative
                # simultaneous-process upper bound before publication.
                transport.pop("sum_peak_rss_kib", None)
                transport.setdefault(
                    "pool_peak_rss_upper_bound_kib",
                    int(transport["max_worker_peak_rss_kib"])
                    * min(int(workers), chunks),
                )
                transport.setdefault(
                    "effective_cores",
                    float(transport["cpu_seconds"]) / float(transport["seconds"]),
                )
        path.write_text(json.dumps(result, sort_keys=True, indent=2) + "\n")
        render_evidence(args.output, result)
        print(json.dumps(result))
        return
    print(json.dumps(run(
        args.output, rows=args.rows, payload_bytes=args.payload_bytes,
        segment_mib=tuple(args.segment_mib),
    )))


if __name__ == "__main__":
    main()
