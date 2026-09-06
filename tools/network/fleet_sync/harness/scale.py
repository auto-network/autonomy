"""Scale run: N real sync processes, an ingest stream, and the byte ledger.

Operator requirement (2026-09-06): once a byte is synchronized to a machine
it must never cross the wire to that machine again, and more members must
make distribution faster. This driver measures how close the engine gets:
bytes moved per ordered peer pair, the ratio of bytes moved to the
theoretical minimum (unique payload bytes x (N-1)), snapshots served, and
settle time -- on the real code path (HarnessFleet: real scheduler
processes, real WebSocket transport, per-direction proxies).

    python -m tools.network.fleet_sync.harness.scale --size 5 --rate 20 \
        --duration 30 --out /workspace/output/scale-5
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

from .fleet import HarnessFleet


def _proc_stats(pid: int | None) -> dict:
    if pid is None:
        return {}
    out: dict = {}
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = int(fields[11]) + int(fields[12])  # utime + stime
        import os

        out["cpu_s"] = round(ticks / os.sysconf("SC_CLK_TCK"), 2)
    except (OSError, IndexError, ValueError):
        pass
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmHWM:"):
                out["peak_rss_kb"] = int(line.split()[1])
    except (OSError, IndexError, ValueError):
        pass
    return out


def _machine_counters(db_path: Path, own_pub: str) -> dict:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(bytes_sent),0), COALESCE(SUM(bytes_received),0),"
            " COALESCE(SUM(checkpoints_sent),0), COALESCE(SUM(checkpoints_received),0),"
            " COALESCE(SUM(deltas_sent),0), COALESCE(SUM(deltas_received),0),"
            " COALESCE(SUM(transactions_applied),0) FROM fleet_sync_peer_state"
        ).fetchone()
        journal_frames = conn.execute("SELECT COUNT(*) FROM fleet_sync_journal").fetchone()[0]
        transactions = conn.execute("SELECT COUNT(*) FROM fleet_sync_transactions").fetchone()[0]
        authored = conn.execute(
            "SELECT COUNT(DISTINCT t.id), COALESCE(SUM(LENGTH(j.frame)),0)"
            " FROM fleet_sync_transactions t"
            " JOIN fleet_sync_origins o ON o.id=t.origin_id"
            " LEFT JOIN fleet_sync_journal j ON j.transaction_ref=t.id"
            " WHERE o.incarnation=?", (own_pub,),
        ).fetchone()
        sources = conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    finally:
        conn.close()
    return {
        "bytes_sent": row[0], "bytes_received": row[1],
        "checkpoints_sent": row[2], "checkpoints_received": row[3],
        "deltas_sent": row[4], "deltas_received": row[5],
        "transactions_applied": row[6],
        "journal_frames": journal_frames, "transactions": transactions,
        "authored_transactions": authored[0], "authored_payload_bytes": authored[1],
        "sources": sources,
    }


def run(args: argparse.Namespace) -> dict:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    fleet = HarnessFleet(
        out / "fleet", size=args.size, seed=args.seed,
        poll_interval=args.poll_interval,
    )
    fleet.build()
    late = args.size - 1 if args.late_join_at is not None else None
    evidence: dict = {
        "size": args.size, "writers": args.writers, "rate": args.rate,
        "duration_s": args.duration, "late_join_at_s": args.late_join_at,
        "poll_interval_s": args.poll_interval,
        "started_at": time.time(),
    }
    try:
        for index in range(args.size):
            if index != late:
                fleet.start(index)
        started = time.monotonic()
        writes = 0
        interval = 1.0 / max(args.rate, 0.001)
        next_write = started
        joined_at = None
        while time.monotonic() - started < args.duration:
            now = time.monotonic()
            if late is not None and joined_at is None and now - started >= args.late_join_at:
                fleet.start(late)
                joined_at = now - started
            if now >= next_write:
                writer = writes % args.writers
                fleet.write(writer, f"scale-{writer}-{writes:07d}", f"note {writes}")
                writes += 1
                next_write += interval
            else:
                time.sleep(min(next_write - now, 0.05))
        if late is not None and joined_at is None:
            fleet.start(late)
            joined_at = time.monotonic() - started
        last_write_at = time.monotonic()
        evidence["writes"] = writes
        evidence["late_joined_at_s"] = joined_at
        timeout = max(180.0, 12.0 * args.size)
        try:
            settle = fleet.wait_converged(timeout=timeout)
            evidence["converged"] = True
        except Exception as exc:  # noqa: BLE001 -- record, do not hide
            settle = time.monotonic() - last_write_at
            evidence["converged"] = False
            evidence["convergence_error"] = str(exc)[:500]
        evidence["settle_s"] = round(settle, 2)

        matrix = [[0] * args.size for _ in range(args.size)]
        for dialer in range(args.size):
            for target in range(args.size):
                if dialer != target:
                    matrix[dialer][target] = fleet.hub.link(
                        fleet._link_name(dialer, target)
                    ).forwarded_bytes
        evidence["bytes_matrix"] = matrix
        total = sum(sum(row) for row in matrix)
        machines = []
        unique_payload = 0
        snapshots_served = 0
        for machine in fleet.machines:
            counters = _machine_counters(machine.db_path, machine.key.public_hex)
            counters.update(_proc_stats(machine.process.pid if machine.process else None))
            counters["index"] = machine.index
            machines.append(counters)
            unique_payload += counters["authored_payload_bytes"]
            snapshots_served += counters["checkpoints_sent"]
        # Per-pull ledger: frames and bytes per terminal pull, per machine.
        pulls: list[dict] = []
        for machine in fleet.machines:
            log = fleet.root_dir / f"machine-{machine.index}-pulls.jsonl"
            if log.exists():
                for line in log.read_text().splitlines():
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    entry["machine"] = machine.index
                    pulls.append(entry)
        ok = [p for p in pulls if p.get("outcome") == "success"]
        evidence["pulls"] = {
            "total": len(pulls),
            "success": len(ok),
            "failed": sum(1 for p in pulls if p.get("outcome") == "failed"),
            "with_frames": sum(1 for p in ok if (p.get("mutation_frames") or 0) > 0),
            "frames_received": sum(int(p.get("mutation_frames") or 0) for p in ok),
            "transactions_received": sum(int(p.get("transactions") or 0) for p in ok),
            "app_bytes_received": sum(int(p.get("bytes_received") or 0) for p in ok),
            "error_codes": sorted({str(p.get("error_code")) for p in pulls
                                   if p.get("outcome") == "failed"}),
        }
        evidence["machines"] = machines
        evidence["total_bytes_on_wire"] = total
        evidence["unique_payload_bytes"] = unique_payload
        minimum = unique_payload * (args.size - 1)
        evidence["theoretical_minimum_bytes"] = minimum
        evidence["efficiency_ratio"] = round(total / minimum, 3) if minimum else None
        evidence["snapshots_served"] = snapshots_served
        evidence["snapshots_received"] = sum(m["checkpoints_received"] for m in machines)
        unique_tx = sum(m["authored_transactions"] for m in machines)
        evidence["unique_transactions"] = unique_tx
        evidence["transactions_duplication"] = (
            round(evidence["pulls"]["transactions_received"] / (unique_tx * (args.size - 1)), 3)
            if unique_tx else None
        )
        evidence["copies_per_write_per_machine"] = (
            round(total / unique_payload / (args.size - 1), 3) if unique_payload else None
        )
    finally:
        fleet.shutdown()
    (out / "evidence.json").write_text(json.dumps(evidence, indent=1, sort_keys=True))
    (out / "report.md").write_text(_report(evidence))
    return evidence


def _report(e: dict) -> str:
    n = e["size"]
    lines = [
        f"# Scale run: {n} machines, {e['writes']} writes at {e['rate']}/s",
        "",
        f"- converged: {e.get('converged')}  settle after last write: {e.get('settle_s')} s",
        f"- bytes on wire (all pairs): {e['total_bytes_on_wire']:,}",
        f"- unique payload bytes: {e['unique_payload_bytes']:,}  x (N-1) = {e['theoretical_minimum_bytes']:,}",
        f"- efficiency ratio (wire / minimum): {e['efficiency_ratio']}",
        f"- copies per write per receiving machine: {e['copies_per_write_per_machine']}  (1.0 = N-1 total, "
        f"{n-1} = (N-1)^2)",
        f"- pulls: {e['pulls']['total']} total, {e['pulls']['success']} ok, {e['pulls']['failed']} failed, "
        f"{e['pulls']['with_frames']} carried data; transactions received {e['pulls']['transactions_received']:,} "
        f"vs unique {e['unique_transactions']:,} x (N-1) -> duplication {e['transactions_duplication']}x; "
        f"app bytes received {e['pulls']['app_bytes_received']:,}",
        f"- snapshots served: {e['snapshots_served']}  received: {e['snapshots_received']}"
        + (f"  (late join at {e['late_joined_at_s']:.1f}s)" if e.get('late_joined_at_s') else ""),
        "",
        "| machine | sent | received | ckpt sent | ckpt recv | deltas recv | journal frames | authored tx | cpu s | peak rss MB |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for m in e["machines"]:
        lines.append(
            f"| {m['index']} | {m['bytes_sent']:,} | {m['bytes_received']:,} | {m['checkpoints_sent']} |"
            f" {m['checkpoints_received']} | {m['deltas_received']} | {m['journal_frames']:,} |"
            f" {m['authored_transactions']} | {m.get('cpu_s','?')} | {round(m.get('peak_rss_kb',0)/1024)} |"
        )
    lines += ["", "## Bytes per dial (row = dialer, column = target)", ""]
    header = "| | " + " | ".join(str(i) for i in range(n)) + " |"
    lines += [header, "|" + "---|" * (n + 1)]
    for i, row in enumerate(e["bytes_matrix"]):
        lines.append(f"| {i} | " + " | ".join(f"{v:,}" for v in row) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size", type=int, required=True)
    parser.add_argument("--writers", type=int, default=None)
    parser.add_argument("--rate", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=30.0)
    parser.add_argument("--late-join-at", type=float, default=None)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--poll-interval", type=float, default=1.0,
                        help="seconds between pull rounds per machine (production: 10)")
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.writers is None:
        args.writers = min(args.size, 5)
    args.writers = min(args.writers, args.size - (1 if args.late_join_at is not None else 0))
    evidence = run(args)
    print(json.dumps({k: evidence[k] for k in (
        "size", "writes", "converged", "settle_s", "total_bytes_on_wire",
        "unique_payload_bytes", "efficiency_ratio", "copies_per_write_per_machine",
        "snapshots_served", "snapshots_received", "transactions_duplication",
    ) if k in evidence}, sort_keys=True))
    return 0 if evidence.get("converged") else 1


if __name__ == "__main__":
    sys.exit(main())
