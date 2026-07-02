"""Repeatable benchmark harness for session ingest (auto-9o9c5, W1 §10 T-6).

Reproduces the baseline measurements from the Session Ingest Overhaul
project plan (graph note 0f730993-3e9 §3, B1-B9) so each workstream can be
checked against the numbers that anchored its targets, pre-sprint and
per-land.

Usage::

    python -m tools.graph.checks.bench_ingest              # everything runnable in-container
    python -m tools.graph.checks.bench_ingest --only b5,b6 # just those benches
    python -m tools.graph.checks.bench_ingest --sweep-dir /path/to/sessions  # adds B1-style sweep

Coverage
--------
B2-B9 run entirely against synthetic fixtures generated in this script —
no live estate or dashboard needed, so results are reproducible on any
checkout. B1 (full-estate sweep) and B12 (estate size) are inherently
host/production measurements; pass ``--sweep-dir`` to reproduce B1's shape
against a real session-file tree (glob + stat only, no ingest). B3 (fresh
GraphDB open) is included since it's cheap and directly comparable to B2.
B10/B11 (live-session graph lag, Recent-list lag) require the running
monitor + dashboard and are measured separately by the poll-until-visible
harness described in the project plan's T-6 — not reproducible as a pure
function benchmark.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

from tools.graph.db import GraphDB
from tools.graph.ingest import (
    ClaudeTurnExtractor,
    ingest_claude_code_session,
    parse_claude_code_session,
)


def _ms(seconds: float) -> float:
    return seconds * 1000.0


def _timeit(fn, n: int = 1) -> list[float]:
    """Run ``fn`` ``n`` times, return the list of elapsed seconds per call."""
    out = []
    for _ in range(n):
        t0 = time.perf_counter()
        fn()
        out.append(time.perf_counter() - t0)
    return out


# ══════════════════════════════════════════════════════════════════════
# Synthetic fixtures
# ══════════════════════════════════════════════════════════════════════


def _synthetic_claude_entry(i: int) -> dict:
    """One user/assistant turn pair's worth of realistic-sized JSONL lines."""
    ts = f"2026-05-01T10:{i % 60:02d}:00Z"
    if i % 2 == 0:
        return {
            "type": "user", "uuid": f"u{i}",
            "message": {"role": "user", "content": f"Question number {i}: what does module X do here?"},
            "timestamp": ts,
        }
    return {
        "type": "assistant", "uuid": f"a{i}",
        "message": {
            "role": "assistant",
            "content": [{
                "type": "text",
                "text": (
                    f"Answer {i}: module X parses the config, validates the "
                    "schema, and writes the normalized record to the DB. "
                    "This is padding text to approximate a real turn's size "
                    "so the parse-throughput benchmark reflects realistic "
                    "bytes-per-turn rather than a toy fixture."
                ),
            }],
            "model": "claude-test",
            "usage": {"input_tokens": 120, "output_tokens": 340},
        },
        "timestamp": ts,
    }


def _write_synthetic_session(path: Path, n_turns: int) -> int:
    """Write ``n_turns`` synthetic entries to ``path``. Returns byte size."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(n_turns):
            f.write(json.dumps(_synthetic_claude_entry(i)) + "\n")
    return path.stat().st_size


# ══════════════════════════════════════════════════════════════════════
# B2 / B3 — GraphDB() open cost
# ══════════════════════════════════════════════════════════════════════


def bench_b2_open_existing_db(tmp_path: Path, n: int = 20) -> dict:
    """GraphDB() open, existing DB — the per-file cost paid by the sweep's
    ``_ingest_session_routed``, once per changed file."""
    db_path = tmp_path / "b2.db"
    GraphDB(db_path).close()  # create it once

    def _open_close():
        GraphDB(db_path).close()

    times = _timeit(_open_close, n)
    return {"label": "B2 GraphDB() open, existing DB", "n": n,
            "avg_ms": _ms(statistics.mean(times)), "median_ms": _ms(statistics.median(times))}


def bench_b3_open_fresh_db(tmp_dir: Path, n: int = 5) -> dict:
    """GraphDB() open, fresh DB — schema executescript + migrations."""
    times = []
    for i in range(n):
        db_path = tmp_dir / f"b3-{i}.db"
        t0 = time.perf_counter()
        GraphDB(db_path).close()
        times.append(time.perf_counter() - t0)
    return {"label": "B3 GraphDB() open, fresh DB", "n": n,
            "avg_ms": _ms(statistics.mean(times)), "median_ms": _ms(statistics.median(times))}


# ══════════════════════════════════════════════════════════════════════
# B4 / B5 — parse throughput
# ══════════════════════════════════════════════════════════════════════


def bench_b4_parse_throughput(tmp_path: Path, n_turns: int = 30) -> dict:
    """parse_claude_code_session throughput on a small realistic session."""
    session = tmp_path / "b4.jsonl"
    size = _write_synthetic_session(session, n_turns)

    def _parse():
        parse_claude_code_session(session)

    times = _timeit(_parse, n=5)
    avg = statistics.mean(times)
    mb_s = (size / (1024 * 1024)) / avg if avg > 0 else float("inf")
    return {"label": "B4 parse_claude_code_session throughput", "size_bytes": size,
            "avg_ms": _ms(avg), "mb_per_s": mb_s}


def bench_b5_large_transcript_full_parse(tmp_path: Path, n_turns: int = 20000) -> dict:
    """Full parse of a large (~tens-of-MB) transcript — the cost paid *every
    sweep tick, every tick, while live* under the pre-W3 architecture."""
    session = tmp_path / "b5.jsonl"
    size = _write_synthetic_session(session, n_turns)

    def _parse():
        parse_claude_code_session(session)

    times = _timeit(_parse, n=3)
    return {"label": "B5 full parse of large transcript", "size_bytes": size,
            "size_mb": size / (1024 * 1024), "avg_ms": _ms(statistics.mean(times))}


# ══════════════════════════════════════════════════════════════════════
# B6 — offset-resume simulation (the headline W1/W3 win)
# ══════════════════════════════════════════════════════════════════════


def bench_b6_offset_resume(tmp_path: Path, n_turns: int = 20000, tail_turns: int = 300) -> dict:
    """Simulate tail-primary resume: parse everything once (full reparse
    baseline), then simulate a tail tick that only feeds the last
    ``tail_turns`` new lines through an extractor resumed from saved state.

    This is the O(n^2) -> O(n) lifetime win W3 depends on: full reparse
    cost is paid once per tick under the old sweep; offset-resume pays it
    once per *new* line.
    """
    session = tmp_path / "b6.jsonl"
    _write_synthetic_session(session, n_turns)
    lines = session.read_text().splitlines()

    def _full_reparse():
        parse_claude_code_session(session)

    full_times = _timeit(_full_reparse, n=3)

    # Warm state: extractor has already consumed everything but the tail.
    warm_lines = lines[:-tail_turns]
    ext = ClaudeTurnExtractor()
    for line in warm_lines:
        entry = json.loads(line)
        ext.feed(entry)
    saved_state = ext.state
    tail_lines = lines[-tail_turns:]

    def _resume_tail():
        resumed = ClaudeTurnExtractor.from_state(saved_state)
        for line in tail_lines:
            resumed.feed(json.loads(line))

    resume_times = _timeit(_resume_tail, n=5)

    full_avg = statistics.mean(full_times)
    resume_avg = statistics.mean(resume_times)
    return {
        "label": "B6 offset-resume vs full reparse",
        "full_reparse_avg_ms": _ms(full_avg),
        "resume_tail_avg_ms": _ms(resume_avg),
        "speedup_x": (full_avg / resume_avg) if resume_avg > 0 else float("inf"),
    }


# ══════════════════════════════════════════════════════════════════════
# B7 / B8 / B9 — full ingest, no-op re-ingest, incremental append
# ══════════════════════════════════════════════════════════════════════


def bench_b7_full_ingest(tmp_path: Path, n_turns: int = 30) -> dict:
    """Full ingest (parse + thoughts/derivations/entities + FTS writes)."""
    session = tmp_path / "b7.jsonl"
    _write_synthetic_session(session, n_turns)
    db = GraphDB(tmp_path / "b7.db")
    try:
        def _ingest():
            ingest_claude_code_session(db, session, force=True)
        times = _timeit(_ingest, n=3)
    finally:
        db.close()
    return {"label": "B7 full ingest", "n_turns": n_turns, "avg_ms": _ms(statistics.mean(times))}


def bench_b8_noop_reingest(tmp_path: Path, n_turns: int = 30) -> dict:
    """No-op re-ingest, DB handle already open — file_size unchanged fast path."""
    session = tmp_path / "b8.jsonl"
    _write_synthetic_session(session, n_turns)
    db = GraphDB(tmp_path / "b8.db")
    try:
        ingest_claude_code_session(db, session)  # first ingest, not timed

        def _reingest():
            ingest_claude_code_session(db, session)
        times = _timeit(_reingest, n=10)
    finally:
        db.close()
    return {"label": "B8 no-op re-ingest (handle open)", "avg_ms": _ms(statistics.mean(times))}


def bench_b9_incremental_append(tmp_path: Path, n_turns: int = 30) -> dict:
    """Incremental re-ingest, +1 line appended."""
    session = tmp_path / "b9.jsonl"
    _write_synthetic_session(session, n_turns)
    db = GraphDB(tmp_path / "b9.db")
    try:
        ingest_claude_code_session(db, session)  # first ingest, not timed

        def _append_and_reingest():
            with open(session, "a", encoding="utf-8") as f:
                f.write(json.dumps(_synthetic_claude_entry(n_turns + 1)) + "\n")
            ingest_claude_code_session(db, session)
        times = _timeit(_append_and_reingest, n=5)
    finally:
        db.close()
    return {"label": "B9 incremental re-ingest (+1 line)", "avg_ms": _ms(statistics.mean(times))}


# ══════════════════════════════════════════════════════════════════════
# B1-shape — optional real-estate sweep (stat-only, no ingest)
# ══════════════════════════════════════════════════════════════════════


def bench_b1_shape_sweep(sweep_dir: Path) -> dict:
    """Stat-scan a real session-file tree the way the pre-W4 sweep would —
    glob + stat, no parsing/ingest. Reproduces B1's shape (file count,
    scan wall-clock) against a real estate when ``--sweep-dir`` is given."""
    t0 = time.perf_counter()
    files = list(sweep_dir.rglob("*.jsonl"))
    for f in files:
        try:
            f.stat()
        except OSError:
            pass
    elapsed = time.perf_counter() - t0
    return {"label": "B1-shape stat-scan sweep", "file_count": len(files), "elapsed_ms": _ms(elapsed)}


# ══════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════


ALL_BENCHES = {
    "b2": lambda tmp: bench_b2_open_existing_db(tmp),
    "b3": lambda tmp: bench_b3_open_fresh_db(tmp),
    "b4": lambda tmp: bench_b4_parse_throughput(tmp),
    "b5": lambda tmp: bench_b5_large_transcript_full_parse(tmp),
    "b6": lambda tmp: bench_b6_offset_resume(tmp),
    "b7": lambda tmp: bench_b7_full_ingest(tmp),
    "b8": lambda tmp: bench_b8_noop_reingest(tmp),
    "b9": lambda tmp: bench_b9_incremental_append(tmp),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated bench ids, e.g. b5,b6")
    parser.add_argument("--sweep-dir", type=Path, help="real session-file tree for the B1-shape sweep")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = parser.parse_args(argv)

    selected = args.only.split(",") if args.only else list(ALL_BENCHES)
    results = []

    with tempfile.TemporaryDirectory(prefix="bench_ingest_") as tmp:
        tmp_path = Path(tmp)
        for key in selected:
            key = key.strip()
            if key not in ALL_BENCHES:
                print(f"unknown bench id: {key!r} (known: {', '.join(ALL_BENCHES)})", file=sys.stderr)
                return 1
            results.append(ALL_BENCHES[key](tmp_path))

        if args.sweep_dir:
            results.append(bench_b1_shape_sweep(args.sweep_dir))

    if args.json:
        print(json.dumps(results, indent=2))
    else:
        for r in results:
            label = r.pop("label")
            rest = ", ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}" for k, v in r.items())
            print(f"{label}: {rest}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
