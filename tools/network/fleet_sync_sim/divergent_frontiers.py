"""Divergent-frontier RaptorQ simulation over encrypted RelayKit channels."""

from __future__ import annotations

import asyncio
import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import tempfile
import time
from typing import Any

from tools.graph.db import GraphDB
from tools.network.idkit import KeyPair, Subject, canonical_json, issue_cert
from tools.network.relaykit.direct import DirectChannelServer
from tools.network.swarmkit import (
    FountainStore,
    dial_links,
    fountain_fetch,
    fountain_handler,
    roster_stripe,
)
from tools.network.swarmkit.fountain import packet_id
from tools.network.swarmkit.fountain_protocol import parse_symbols_response

from .codec import Mutation, decode_stream, encode_stream
from .materialize import materialize
from .merge import MutationInbox


SIM_ORG = "77777777-7777-4777-8777-777777777777"
SYMBOL_SIZE = 4096


@dataclass(frozen=True)
class SegmentSpec:
    name: str
    parent: str | None
    frontier: int
    payload: bytes


def _source_mutation(identity: str, timestamp: int, marker: str) -> Mutation:
    values = (
        ("created_at", "2026-08-19T00:00:00Z"),
        ("id", identity),
        ("ingested_at", "2026-08-19T00:00:00Z"),
        ("metadata", {"branch": marker, "payload": marker * 140_000}),
        ("publication_state", "raw"),
        ("title", marker),
        ("type", "note"),
    )
    return Mutation("sources", (identity,), timestamp, False, values)


def checkpoint_segments() -> list[SegmentSpec]:
    prefix = encode_stream([_source_mutation("prefix", 10, "P")])
    branch_a = encode_stream([_source_mutation("branch-a", 20, "A")])
    branch_c = encode_stream([_source_mutation("branch-c", 30, "C")])
    return [
        SegmentSpec("prefix", None, 10, prefix),
        SegmentSpec("branch-a", "prefix", 20, branch_a),
        SegmentSpec("branch-c", "prefix", 30, branch_c),
    ]


class Peer:
    def __init__(self, name: str, key, cert, stripe: int):
        self.name = name
        self.key = key
        self.cert = cert
        self.store = FountainStore(stripe=stripe, n_stripes=8)
        self.handler = fountain_handler(self.store)
        self.server = DirectChannelServer(
            SIM_ORG, key, cert, self.handler, host="127.0.0.1", port=0
        )

    async def start(self) -> None:
        await self.server.start()

    async def stop(self) -> None:
        await self.server.stop()

    def roster_row(self) -> dict[str, Any]:
        return {
            "node": self.key.public_hex,
            "direct_addrs": [f"ws://127.0.0.1:{self.server.port}"],
        }


class TracedLink:
    """Observe the exact symbol response after it crosses RelayKit."""

    def __init__(self, inner, source: Peer, receiver: str, trace: list[dict]):
        self.inner = inner
        self.source = source
        self.receiver = receiver
        self.trace = trace

    async def request(self, payload: bytes) -> bytes:
        try:
            request = json.loads(payload)
        except (ValueError, UnicodeDecodeError):
            request = None
        artifact = request.get("artifact") if isinstance(request, dict) else None
        complete = bool(artifact and self.source.store.is_complete(artifact))
        prior = request.get("exclude", []) if isinstance(request, dict) else []
        response = await self.inner.request(payload)
        if isinstance(request, dict) and request.get("op") == "fountain.symbols":
            manifest = self.source.store.manifest(artifact)
            packets = parse_symbols_response(response, manifest) if manifest else None
            for packet in packets or []:
                packed = packet_id(packet)
                self.trace.append({
                    "source": self.source.name,
                    "receiver": self.receiver,
                    "artifact": artifact,
                    "sbn": packed >> 24,
                    "esi": packed & 0xFFFFFF,
                    "packet_bytes": len(packet),
                    "source_complete": complete,
                    "receiver_prior_ranges": prior,
                })
        return response

    async def close(self) -> None:
        await self.inner.close()


async def _start_peers(names: list[str]) -> tuple[KeyPair, dict[str, Peer]]:
    root = KeyPair.generate()
    now = int(time.time())
    identities = {}
    for name in names:
        key = KeyPair.generate()
        cert = issue_cert(
            root, key.public_hex, scope=("tunnel:serve",), org=SIM_ORG,
            subject=Subject("agent", name), not_before=now - 60,
            not_after=now + 3600,
        )
        identities[name] = (key, cert)
    roster = [key.public_hex for key, _ in identities.values()]
    peers = {
        name: Peer(name, key, cert, roster_stripe(key.public_hex, roster))
        for name, (key, cert) in identities.items()
    }
    await asyncio.gather(*(peer.start() for peer in peers.values()))
    return root, peers


async def _links(
    root: KeyPair,
    receiver: str,
    sources: list[Peer],
    trace: list[dict],
) -> list[tuple[str, TracedLink]]:
    links, errors = await dial_links(
        [peer.roster_row() for peer in sources],
        org=SIM_ORG,
        root_pub=root.public_hex,
    )
    if errors:
        raise AssertionError(f"failed to dial simulation peers: {errors}")
    by_key = {peer.key.public_hex: peer for peer in sources}
    return [
        (by_key[key].name, TracedLink(link, by_key[key], receiver, trace))
        for key, link in links
    ]


def _install_complete(peer: Peer, segment: SegmentSpec) -> str:
    return peer.store.add_object(segment.payload, SYMBOL_SIZE)


async def _fetch_one(
    root: KeyPair,
    receiver: Peer,
    artifact: str,
    sources: list[Peer],
    trace: list[dict],
):
    links = await _links(root, receiver.name, sources, trace)
    return await fountain_fetch(
        receiver.store, artifact, links,
        symbol_batch=8, idle_refresh=0.01, timeout=30.0,
    )


async def run_divergent_frontier_simulation() -> dict[str, Any]:
    """Run the flagship topology and return secret-free JSON evidence."""

    segments = checkpoint_segments()
    by_name = {segment.name: segment for segment in segments}
    root, peers = await _start_peers(["A", "B", "C", "D", "baseline"])
    a, b, c, d, baseline = (peers[name] for name in ("A", "B", "C", "D", "baseline"))
    trace: list[dict] = []
    baseline_trace: list[dict] = []
    try:
        artifacts: dict[str, str] = {}
        for segment in segments:
            artifacts[segment.name] = _install_complete(baseline, segment)
        assert _install_complete(a, by_name["prefix"]) == artifacts["prefix"]
        assert _install_complete(a, by_name["branch-a"]) == artifacts["branch-a"]
        assert _install_complete(b, by_name["prefix"]) == artifacts["prefix"]
        assert _install_complete(c, by_name["branch-c"]) == artifacts["branch-c"]

        # B is genuinely incomplete on A's later segment but already useful.
        branch_manifest = a.store.manifest(artifacts["branch-a"])
        b.store.add_manifest(artifacts["branch-a"], branch_manifest)
        setup_packets = a.store.serve(artifacts["branch-a"], 10, [])
        for packet in setup_packets:
            b.store.add_packet(artifacts["branch-a"], packet)
        assert setup_packets and not b.store.is_complete(artifacts["branch-a"])

        # No multi-run source owns the complete segment union.
        holdings_before = {
            peer.name: {
                name: (peer.store.is_complete(artifact),
                       len(peer.store.held_ids(artifact)))
                for name, artifact in artifacts.items()
            }
            for peer in (a, b, c)
        }
        assert not any(all(complete for complete, _ in held.values())
                       for held in holdings_before.values())

        baseline_started = time.perf_counter()
        baseline_reports = []
        for segment in segments:
            baseline_reports.append(await _fetch_one(
                root, PeerSink(f"baseline-{segment.name}"),
                artifacts[segment.name], [baseline], baseline_trace,
            ))
        baseline_seconds = time.perf_counter() - baseline_started

        multi_started = time.perf_counter()
        reports = {
            "prefix": await _fetch_one(root, d, artifacts["prefix"], [a, b], trace),
            "branch-a": await _fetch_one(root, d, artifacts["branch-a"], [a, b], trace),
            "branch-c": await _fetch_one(root, d, artifacts["branch-c"], [c], trace),
        }
        multi_seconds = time.perf_counter() - multi_started
        transfer_events = list(trace)

        partial_events = [
            event for event in trace
            if event["source"] == "B"
            and event["artifact"] == artifacts["branch-a"]
            and not event["source_complete"]
        ]
        assert partial_events, "partial B contributed no symbol"

        # B later hydrates and changes from stored-packet relay to fresh seeder.
        await _fetch_one(root, b, artifacts["branch-a"], [a], trace)
        assert b.store.is_complete(artifacts["branch-a"])
        probe_store = FountainStore()
        probe_store.add_manifest(artifacts["branch-a"], branch_manifest)
        probe = PeerSinkStore("post-promotion-probe", probe_store)
        links = await _links(root, probe.name, [b], trace)
        request = canonical_json({
            "v": 1, "op": "fountain.symbols",
            "artifact": artifacts["branch-a"], "count": 1, "exclude": [],
        })
        response = await links[0][1].request(request)
        promoted_packets = parse_symbols_response(response, branch_manifest)
        await links[0][1].close()
        assert promoted_packets
        complete_events = [
            event for event in trace
            if event["source"] == "B"
            and event["artifact"] == artifacts["branch-a"]
            and event["source_complete"]
        ]
        assert complete_events, "promoted B served no fresh symbol"

        # Decode graph segments, bulk-merge branches, and materialize a real DB.
        inbox = MutationInbox()
        for segment in reversed(segments):
            payload = d.store.object(artifacts[segment.name])
            assert payload is not None
            inbox.ingest(decode_stream(payload))
        with tempfile.TemporaryDirectory() as temporary:
            graph = GraphDB(Path(temporary) / "personal.db")
            try:
                materialize(graph.conn, inbox.winners())
                rows = [tuple(row) for row in graph.conn.execute(
                    "SELECT id,title FROM sources ORDER BY id"
                ).fetchall()]
            finally:
                graph.close()
        assert rows == [("branch-a", "A"), ("branch-c", "C"), ("prefix", "P")]

        event_keys = [
            (event["artifact"], event["sbn"], event["esi"])
            for event in trace
        ]
        assert len(event_keys) == len(set(event_keys)), "symbol was served twice"

        return {
            "status": "pass",
            "transport": "authenticated encrypted RelayKit WebSockets",
            "transport_auth_fixture": (
                "existing root-pinned DirectChannelServer certificate shape; "
                "the fleet personal-root envelope fields remain a separate contract"
            ),
            "segments": [
                {
                    "name": segment.name,
                    "parent": segment.parent,
                    "frontier": segment.frontier,
                    "codec_sha256": hashlib.sha256(segment.payload).hexdigest(),
                    "artifact": artifacts[segment.name],
                    "bytes": len(segment.payload),
                }
                for segment in segments
            ],
            "holdings_before": holdings_before,
            "no_source_held_complete_union": True,
            "partial_peer": {
                "peer": "B",
                "served_while_incomplete": len(partial_events),
                "later_completed": True,
                "served_after_promotion": len(complete_events),
            },
            "receiver": {
                "peer": "D",
                "materialized_rows": rows,
                "latest_merged_frontier": max(item.frontier for item in segments),
                "segment_reports": {
                    name: dict(report.symbols_from) for name, report in reports.items()
                },
            },
            "symbol_trace": trace,
            "globally_distinct_artifact_packet_ids": True,
            "baseline": {
                "seconds": baseline_seconds,
                "symbols": len(baseline_trace),
                "bytes": sum(item["packet_bytes"] for item in baseline_trace),
            },
            "multi_source": {
                "seconds": multi_seconds,
                "symbols": len(transfer_events),
                "bytes": sum(item["packet_bytes"] for item in transfer_events),
                "complete_source_symbols": sum(
                    e["source_complete"] for e in transfer_events
                ),
                "partial_source_symbols": sum(
                    not e["source_complete"] for e in transfer_events
                ),
            },
        }
    finally:
        await asyncio.gather(*(peer.stop() for peer in peers.values()))


class PeerSink:
    """Give each baseline artifact an empty store while reusing a label."""

    def __init__(self, name: str):
        self.name = name
        self.store = FountainStore(stripe=0, n_stripes=8)


class PeerSinkStore:
    def __init__(self, name: str, store: FountainStore):
        self.name = name
        self.store = store


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    rendered = json.dumps(
        asyncio.run(run_divergent_frontier_simulation()),
        sort_keys=True, indent=2,
    ) + "\n"
    if args.output is None:
        print(rendered, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        temporary = args.output.with_suffix(args.output.suffix + ".tmp")
        temporary.write_text(rendered)
        temporary.replace(args.output)


if __name__ == "__main__":
    main()
