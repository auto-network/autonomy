"""``graph backup`` — the backup plugin's command tree.

Mounted dynamically by the substrate (``entrypoints.cli`` in
plugin.yaml) while the plugin is enabled. Every verb is one
authenticated dashboard API call against the plugin's own routes —
sessions hold no machine database, so HTTP is the only honest
transport. Kept import-light: this module loads on every ``graph``
invocation once enabled.

Configuration is read-only here on purpose: PUT /api/backup/config
requires operator authority, which a session bearer does not hold.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

STATUS_MARKS = {"ok": "✓", "stale": "⚠", "failing": "✗"}


def _api():
    from tools.graph.cli import _resolve_crosstalk_token
    base = os.environ.get("GRAPH_API", "https://localhost:8080")
    token = _resolve_crosstalk_token()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def call(method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(
            base + path,
            data=None if body is None else json.dumps(body).encode(),
            method=method,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {token}"},
        )
        try:
            return json.loads(urllib.request.urlopen(
                req, timeout=60, context=ctx).read())
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("error", str(exc))
            except Exception:
                detail = str(exc)
            print(f"backup: {method} {path}: {detail}", file=sys.stderr)
            sys.exit(1)
        except urllib.error.URLError as exc:
            print(f"backup: cannot reach dashboard: {exc.reason}",
                  file=sys.stderr)
            sys.exit(1)

    return call


def _age(seconds) -> str:
    if seconds is None:
        return "never"
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "K", "M", "G", "T"):
        if n < 1024 or unit == "T":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}T"


def cmd_status(args) -> None:
    summary = _api()("GET", "/api/backup/summary")
    mark = STATUS_MARKS.get(summary.get("overall"), "?")
    print(f"{mark} backup: {summary.get('overall', 'unknown').upper()}")
    for tier in summary.get("tiers", []):
        tier_mark = STATUS_MARKS.get(tier.get("status"), "?")
        print(f"  {tier_mark} {tier['tier']}: last success "
              f"{_age(tier.get('age_seconds'))} "
              f"({tier.get('last_success_key') or 'none'}) "
              f"offsite={tier.get('offsite')} "
              f"{_bytes(tier.get('total_bytes'))}")
    drill = summary.get("last_drill")
    if drill:
        print(f"  drill: {drill.get('verdict')} at "
              f"{drill.get('finished_at') or drill.get('started_at')} "
              f"({len(drill.get('checks') or [])} checks)")
    else:
        print("  drill: never run")
    if summary.get("running_drill"):
        print(f"  drill in flight since "
              f"{summary['running_drill'].get('started_at')}")


def cmd_runs(args) -> None:
    path = f"/api/backup/runs?limit={args.limit}"
    if args.tier:
        path += f"&tier={args.tier}"
    runs = _api()("GET", path).get("runs", [])
    if args.json_output:
        print(json.dumps(runs, indent=2))
        return
    if not runs:
        print("no capture runs recorded")
        return
    for run in runs:
        mark = "✓" if run.get("verdict") == "complete" else "✗"
        line = (f"{mark} {run.get('key')}  stores={run.get('store_count')} "
                f"beads={run.get('beads_databases')} "
                f"{_bytes(run.get('total_bytes'))} "
                f"offsite={run.get('offsite')} origin={run.get('origin')}")
        print(line)
        for reason in run.get("failures", []):
            print(f"    ! {reason}")


def cmd_config(args) -> None:
    config = _api()("GET", "/api/backup/config").get("config", {})
    if args.json_output:
        print(json.dumps(config, indent=2))
        return
    for key in sorted(config):
        print(f"  {key} = {config[key]}")


def register(sub) -> None:
    p = sub.add_parser(
        "backup",
        help="Backup state: capture runs, staleness, drills (backup plugin)")
    p.set_defaults(func=lambda _a: p.print_help())
    bs = p.add_subparsers(dest="backup_subcmd")

    q = bs.add_parser("status", help="Tier health, last success, last drill")
    q.set_defaults(func=cmd_status)

    q = bs.add_parser("runs", help="Recorded capture runs, newest first")
    q.add_argument("--tier", choices=["hourly", "daily"])
    q.add_argument("--limit", type=int, default=20)
    q.add_argument("--json", dest="json_output", action="store_true")
    q.set_defaults(func=cmd_runs)

    q = bs.add_parser("config", help="Effective backup configuration")
    q.add_argument("--json", dest="json_output", action="store_true")
    q.set_defaults(func=cmd_config)
