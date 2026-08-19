"""Early codec benchmark using the measured live personal-graph row shape."""

from __future__ import annotations

import argparse
import hashlib
import json
import resource
import time
from typing import Iterator

from .codec import Mutation, decode_stream, encode_stream
from .segments import encode_segments


LIVE_ROWS = {
    "attachments": 12,
    "captures": 55,
    "claims": 0,
    "derivations": 43_377,
    "edges": 83_727,
    "entities": 45_943,
    "entity_mentions": 509_690,
    "node_refs": 0,
    "nodes": 0,
    "note_comments": 15,
    "note_reads": 0,
    "note_versions": 159,
    "settings": 342,
    "sources": 319,
    "tags": 786,
    "thoughts": 17_889,
    "threads": 0,
}


def _count(table: str, scale: float) -> int:
    live = LIVE_ROWS[table]
    return max(1, round(live * scale)) if live else 1


def _mutations(scale: float) -> Iterator[Mutation]:
    timestamp = 1_700_000_000_000_000_000
    for table in sorted(LIVE_ROWS):
        for index in range(_count(table, scale)):
            identity = f"{table}-{index:08d}"
            if table == "edges":
                address = (f"thought-{index}", f"entity-{index}", "mentions")
                values = (("created_at", "2026-08-19T00:00:00Z"),
                          ("metadata", {}), ("relation", "mentions"),
                          ("source_id", address[0]), ("source_type", "thought"),
                          ("target_id", address[1]), ("target_type", "entity"),
                          ("weight", 1.0))
            elif table == "entity_mentions":
                address = (f"entity-{index}", f"thought-{index}")
                values = (("content_id", address[1]), ("content_type", "thought"),
                          ("count", 1), ("entity_id", address[0]))
            elif table == "note_versions":
                content = "v" * 512 + str(index)
                address = ("source-0", f"2026-08-19T00:00:{index % 60:02d}Z",
                           hashlib.sha256(content.encode()).hexdigest())
                values = (("content", content), ("created_at", address[1]),
                          ("source_id", "source-0"))
            elif table == "note_reads":
                address = ("source-0", identity, "2026-08-19T00:00:00Z")
                values = (("actor", identity), ("source_id", "source-0"),
                          ("ts", "2026-08-19T00:00:00Z"))
            elif table == "node_refs":
                address = ("node-0", identity)
                values = (("metadata", {}), ("node_id", "node-0"),
                          ("ref_id", identity), ("ref_type", "source"))
            elif table == "settings":
                address = ("example.benchmark", 1, identity, "raw", "base")
                values = (("created_at", "2026-08-19T00:00:00Z"),
                          ("deprecated", 0), ("id", identity), ("key", identity),
                          ("payload", {"enabled": True, "label": "x" * 64}),
                          ("publication_state", "raw"), ("schema_revision", 1),
                          ("set_id", "example.benchmark"),
                          ("updated_at", "2026-08-19T00:00:00Z"))
            else:
                address = (identity,)
                body = "x" * (512 if table == "derivations" else
                              192 if table in {"thoughts", "captures"} else 48)
                key = "name" if table == "tags" else "id"
                values = ((key, identity), ("content", body))
                if table == "attachments":
                    values = (("filename", f"{identity}.bin"), ("hash", "a" * 64),
                              ("id", identity), ("metadata", {}), ("size_bytes", 1))
            yield Mutation(table, address, timestamp + index, False, tuple(sorted(values)))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scale", type=float, default=0.1)
    parser.add_argument("--segment-bytes", type=int, default=4 * 1024 * 1024)
    args = parser.parse_args()
    if not 0 < args.scale <= 1:
        parser.error("--scale must be in (0,1]")

    started = time.perf_counter()
    mutations = list(_mutations(args.scale))
    generated_at = time.perf_counter()
    encoded = encode_stream(mutations)
    encoded_at = time.perf_counter()
    decoded = decode_stream(encoded)
    decoded_at = time.perf_counter()
    segments = encode_segments(decoded, target_bytes=args.segment_bytes)
    segmented_at = time.perf_counter()
    assert sum(segment.mutation_count for segment in segments) == len(decoded)

    result = {
        "scale_of_measured_live_row_counts": args.scale,
        "measured_live_rows": sum(LIVE_ROWS.values()),
        "benchmark_mutations": len(mutations),
        "encoded_bytes": len(encoded),
        "segment_target_bytes": args.segment_bytes,
        "segment_count": len(segments),
        "largest_segment_bytes": max((len(item.payload) for item in segments), default=0),
        "generate_seconds": generated_at - started,
        "encode_seconds": encoded_at - generated_at,
        "decode_seconds": decoded_at - encoded_at,
        "segment_seconds": segmented_at - decoded_at,
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
    }
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
