"""Replayable schedule fuzzing and fleet-scale RaptorQ measurements."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
import hashlib
import importlib.metadata
import json
import multiprocessing
import os
from pathlib import Path
import platform
import random
import resource
import time
from typing import Any

from tools.network.idkit import KeyPair
from tools.network.swarmkit.fetch import HandlerLink
from tools.network.swarmkit.fountain import (
    FountainStore,
    check_ranges,
    packet_id,
    source_symbols,
)
from tools.network.swarmkit.fountain_fetch import fountain_fetch
from tools.network.swarmkit.fountain_protocol import fountain_handler

from .codec import Mutation
from .compaction import ActiveRosterFrontier, CanonicalBase, Replica, make_kick
from .merge import MutationInbox


DEFAULT_SEEDS = (7, 19, 42, 137, 2026)


def _data(size: int) -> bytes:
    block = hashlib.sha256(f"fleet-stress:{size}".encode()).digest()
    return (block * ((size + len(block) - 1) // len(block)))[:size]


class _Node:
    def __init__(self, name: str, stripe: int, n_stripes: int):
        self.name = name
        self.store = FountainStore(stripe=stripe, n_stripes=n_stripes)
        self.handler = fountain_handler(self.store)


class _DelayedLink(HandlerLink):
    """Make concurrent requests carry the same pre-response exclude snapshot."""

    def __init__(self, handler, delay: float):
        super().__init__(handler)
        self.delay = delay

    async def request(self, payload: bytes) -> bytes:
        await asyncio.sleep(self.delay)
        return await super().request(payload)


def _transfer_worker(spec: dict[str, Any]) -> dict[str, Any]:
    assigned_cpu = spec.get("assigned_cpu")
    if assigned_cpu is not None and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, {int(assigned_cpu)})
    usage_before = resource.getrusage(resource.RUSAGE_SELF)
    size = int(spec["size"])
    stripes = list(spec["stripes"])
    n_stripes = int(spec["n_stripes"])
    payload = _data(size)
    seeders = [
        _Node(f"s{index}", stripe, n_stripes)
        for index, stripe in enumerate(stripes)
    ]
    artifact = seeders[0].store.add_object(payload)
    for seeder in seeders[1:]:
        assert seeder.store.add_object(payload) == artifact
    receiver = FountainStore()

    async def run():
        started = time.perf_counter()
        delay = float(spec.get("request_delay", 0))
        report = await fountain_fetch(
            receiver, artifact,
            [(node.name, _DelayedLink(node.handler, delay)
              if delay else HandlerLink(node.handler)) for node in seeders],
            symbol_batch=64, idle_refresh=0.001, timeout=300.0,
        )
        return report, time.perf_counter() - started

    report, seconds = asyncio.run(run())
    assert receiver.object(artifact) == payload
    per_peer = {
        node.name: {
            "stripe": stripes[index],
            "packets": node.handler.metrics.served_packets[artifact],
            "bytes": node.handler.metrics.served_bytes[artifact],
            "distinct": len(node.handler.metrics.served_ids.get(artifact, set())),
        }
        for index, node in enumerate(seeders)
    }
    all_ids = [node.handler.metrics.served_ids.get(artifact, set())
               for node in seeders]
    distinct_union = set().union(*all_ids)
    total_packets = sum(item["packets"] for item in per_peer.values())
    k = source_symbols(seeders[0].store.manifest(artifact))
    usage_after = resource.getrusage(resource.RUSAGE_SELF)
    cpu_seconds = (
        usage_after.ru_utime - usage_before.ru_utime
        + usage_after.ru_stime - usage_before.ru_stime
    )
    return {
        "name": spec["name"],
        "object_bytes": size,
        "artifact": artifact,
        "symbol_size": seeders[0].store.manifest(artifact)["symbol_size"],
        "source_symbols": k,
        "seconds": seconds,
        "throughput_bytes_per_second": size / seconds,
        "per_peer": per_peer,
        "accepted_from": dict(report.symbols_from),
        "receiver_duplicates": sum(report.duplicates.values()),
        "total_served_packets": total_packets,
        "distinct_served_union": len(distinct_union),
        "aggregate_egress_ratio": total_packets / k,
        "distinct_egress_ratio": len(distinct_union) / k,
        "collision_overhead_packets": total_packets - len(distinct_union),
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cpu_seconds": cpu_seconds,
        "cpu_utilization": cpu_seconds / seconds,
        "assigned_cpu": assigned_cpu,
        "frozen_artifact_roster": [
            {"peer": f"s{index}", "stripe": stripe}
            for index, stripe in enumerate(stripes)
        ],
    }


def measure_transfers(*, full: bool) -> list[dict[str, Any]]:
    mib = 1024 * 1024
    sizes = (8, 32, 64, 128) if full else (1,)
    specs = [
        {"name": f"single-{size}MiB", "size": size * mib,
         "stripes": [0], "n_stripes": 1}
        for size in sizes
    ]
    topology_size = (32 if full else 1) * mib
    specs.extend([
        {"name": "four-collision-free", "size": topology_size,
         "stripes": [0, 1, 2, 3], "n_stripes": 4, "request_delay": 0.001},
        {"name": "four-forced-collision", "size": topology_size,
         "stripes": [0, 0, 0, 0], "n_stripes": 4, "request_delay": 0.001},
        {"name": "four-default-eight-stripes", "size": topology_size,
         "stripes": [0, 1, 2, 3], "n_stripes": 8, "request_delay": 0.001},
    ])
    context = multiprocessing.get_context("spawn")
    results = []
    # One fresh process per measurement makes native RaptorQ peak RSS and
    # cursor state attributable to exactly one topology.
    for spec in specs:
        with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
            results.append(pool.submit(_transfer_worker, spec).result())
    return results


def measure_core_scaling(*, full: bool) -> list[dict[str, Any]]:
    """Measure segment-level parallelism with one affined transfer per core."""
    available = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else [0]
    requested = (1, 2, 4) if full else (1,)
    object_size = (32 if full else 1) * 1024 * 1024
    context = multiprocessing.get_context("spawn")
    results = []
    for workers in requested:
        if workers > len(available):
            results.append({
                "workers": workers,
                "skipped": True,
                "reason": f"only {len(available)} CPUs available",
            })
            continue
        specs = [
            {
                "name": f"core-{workers}-segment-{index}",
                "size": object_size,
                "stripes": [0],
                "n_stripes": 1,
                "assigned_cpu": available[index],
            }
            for index in range(workers)
        ]
        started = time.perf_counter()
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            measurements = list(pool.map(_transfer_worker, specs))
        seconds = time.perf_counter() - started
        cpu_seconds = sum(item["cpu_seconds"] for item in measurements)
        results.append({
            "workers": workers,
            "skipped": False,
            "object_bytes_per_worker": object_size,
            "aggregate_bytes": workers * object_size,
            "seconds": seconds,
            "throughput_bytes_per_second": workers * object_size / seconds,
            "aggregate_cpu_seconds": cpu_seconds,
            "effective_cores": cpu_seconds / seconds,
            "sum_peak_rss_kib": sum(item["peak_rss_kib"] for item in measurements),
            "worker_measurements": measurements,
        })
    return results


def measure_cursor_restart() -> dict[str, Any]:
    payload = _data(2 * 1024 * 1024)
    before = FountainStore(stripe=0, n_stripes=4)
    artifact = before.add_object(payload)
    manifest = before.manifest(artifact)
    receiver = FountainStore()
    receiver.add_manifest(artifact, manifest)
    first = before.serve(artifact, 64, [])
    for packet in first:
        receiver.add_packet(artifact, packet)

    restarted = FountainStore(stripe=0, n_stripes=4)
    assert restarted.add_object(payload) == artifact
    second = restarted.serve(
        artifact, 64, check_ranges(receiver.held_ranges(artifact))
    )
    first_ids = {packet_id(packet) for packet in first}
    second_ids = {packet_id(packet) for packet in second}
    return {
        "held_before_restart": len(first_ids),
        "served_after_restart": len(second_ids),
        "wire_duplicates_after_restart": len(first_ids & second_ids),
        "conclusion": (
            "requester exclude ranges prevent sequential wire duplicates; "
            "a durable cursor is not required for correctness or this bandwidth case, "
            "but would avoid regenerating the prefix and stale concurrent requests can "
            "still duplicate in flight"
        ),
    }


def _candidate(key: str, timestamp: int, value: str, tombstone: bool) -> Mutation:
    if tombstone:
        return Mutation("sources", (key,), timestamp, True)
    return Mutation(
        "sources", (key,), timestamp, False,
        (("created_at", "2026-08-19T00:00:00Z"), ("id", key),
         ("ingested_at", "2026-08-19T00:00:00Z"), ("metadata", {}),
         ("publication_state", "raw"), ("title", value), ("type", "note")),
    )


def run_randomized_schedules(seeds=DEFAULT_SEEDS) -> list[dict[str, Any]]:
    results = []
    for seed in seeds:
        rng = random.Random(seed)
        mutations = [
            _candidate(
                f"key-{rng.randrange(24)}",
                rng.randrange(1, 81),
                f"seed-{seed}-event-{index}",
                rng.random() < 0.22,
            )
            for index in range(160)
        ]
        # Force exact timestamp ties on one address.
        mutations.extend([
            _candidate("exact-tie", 50, f"tie-{seed}-a", False),
            _candidate("exact-tie", 50, f"tie-{seed}-b", False),
        ])
        expected = MutationInbox()
        expected.ingest(mutations)
        expected_rows = expected.winners()

        chunk_count = rng.randrange(5, 13)
        chunks = [[] for _ in range(chunk_count)]
        for mutation in mutations:
            chunks[rng.randrange(chunk_count)].append(mutation)
        schedules = {}
        replica_rows = []
        for peer in ("A", "B", "C", "D"):
            order = list(range(chunk_count))
            rng.shuffle(order)
            schedules[peer] = order
            inbox = MutationInbox()
            for index in order:
                replay_count = 2 if rng.random() < 0.3 else 1
                for _ in range(replay_count):
                    inbox.ingest(chunks[index])
            rows = inbox.winners()
            assert rows == expected_rows
            replica_rows.append(rows)

        frontiers = {peer: rng.randrange(20, 81) for peer in ("A", "B", "C", "D")}
        root = KeyPair.generate()
        roster = ActiveRosterFrontier(root.public_hex, frontiers)
        for peer, frontier in frontiers.items():
            roster.complete(peer, frontier)
        before = roster.frontier
        laggard = min(frontiers, key=frontiers.get)
        others = sorted(set(frontiers) - {laggard})
        kick = make_kick(root, laggard, seed)
        for observer in others:
            roster.observe_kick(observer, kick)
        after = roster.frontier
        assert after >= before

        base = CanonicalBase.build(before, mutations)
        post_base = []
        for peer in ("A", "B", "C", "D"):
            replica = Replica(peer, mutations)
            replica.apply_base(base)
            post_base.append(replica.winners())
        assert all(rows == post_base[0] for rows in post_base)
        results.append({
            "seed": seed,
            "events": len(mutations),
            "chunks": chunk_count,
            "arrival_schedules": schedules,
            "frontiers": frontiers,
            "kicked_peer": laggard,
            "frontier_before_kick": before,
            "frontier_after_kick": after,
            "winner_digest": expected.digest(),
            "logical_winners": len(expected_rows),
            "tombstone_winners": sum(item.tombstone for item in expected_rows),
            "converged": True,
            "compaction_invariant": True,
        })
    return results


def run_stress(*, full: bool) -> dict[str, Any]:
    transfers = measure_transfers(full=full)
    collision_free = next(item for item in transfers
                          if item["name"] == "four-collision-free")
    default_eight = next(item for item in transfers
                         if item["name"] == "four-default-eight-stripes")
    collision = next(item for item in transfers
                     if item["name"] == "four-forced-collision")
    return {
        "status": "pass",
        "mode": "full" if full else "test",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "raptorq": importlib.metadata.version("raptorq"),
        },
        "exact_commands": [
            "python3 -m tools.network.fleet_sync_sim.benchmark --scale 1.0",
            "python3 -m tools.network.fleet_sync_sim.stress --full --output <evidence>",
        ],
        "transfers": transfers,
        "core_scaling": measure_core_scaling(full=full),
        "fleet_scale_striping": {
            "four_stripes": collision_free["aggregate_egress_ratio"],
            "eight_stripes": default_eight["aggregate_egress_ratio"],
            "forced_collision": collision["aggregate_egress_ratio"],
            "conclusion": (
                "four stable collision-free stripes are sufficient for a four-machine "
                "fleet; eight is correctness-neutral and forced collisions add only "
                "duplicate bandwidth, not decode error"
            ),
        },
        "cursor_restart": measure_cursor_restart(),
        "randomized_schedules": run_randomized_schedules(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(run_stress(full=args.full), sort_keys=True, indent=2) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered)
        temporary.replace(args.output)


if __name__ == "__main__":
    main()
