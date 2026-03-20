"""dispatch_cmd.py — graph dispatch subcommand: show running/queued/history."""

from __future__ import annotations
import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone


def _get_dashboard_url() -> str:
    return os.environ.get("DASHBOARD_URL", "https://localhost:8080").rstrip("/")


def _make_ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _api_call(base_url: str, path: str, ctx):
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Accept": "application/json"},
    )
    resp = urllib.request.urlopen(req, context=ctx, timeout=10)
    return json.loads(resp.read())


def _format_duration(secs) -> str:
    if secs is None:
        return "?"
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    m, s = divmod(secs, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


def _elapsed_since(started_at: str) -> str:
    """Calculate elapsed time since started_at (SQLite or ISO datetime)."""
    if not started_at:
        return "?"
    try:
        dt = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return "?"
    now = datetime.now(timezone.utc)
    return _format_duration(int((now - dt).total_seconds()))


def cmd_dispatch_default(args):
    """Show running + queued state."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        status_data = _api_call(base, "/api/dispatch/status", ctx)
        approved_data = _api_call(base, "/api/dispatch/approved", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    running_runs = status_data.get("running_runs", [])
    waiting = approved_data.get("waiting", [])

    if args.json:
        print(json.dumps({"running": running_runs, "queued": waiting}, default=str))
        return

    # Build title lookup from the queued beads
    title_map: dict[str, str] = {}
    for bead in waiting:
        title_map[bead["id"]] = bead.get("title", "")

    # For running beads not in the queue, look up from beads API
    missing_ids = [r["bead_id"] for r in running_runs
                   if r.get("bead_id") and r["bead_id"] not in title_map]
    if missing_ids:
        try:
            all_beads = _api_call(base, "/api/beads/list", ctx)
            for b in all_beads:
                if b.get("id") in missing_ids:
                    title_map[b["id"]] = b.get("title", "")
        except Exception:
            pass  # titles are optional

    print(f"RUNNING ({len(running_runs)})")
    for r in running_runs:
        bead_id = r.get("bead_id") or ""
        elapsed = _elapsed_since(r.get("started_at", ""))
        title = (title_map.get(bead_id, "") or "")[:35]
        image = r.get("image", "")
        cname = r.get("container_name", "")
        container = f"{image}:{cname}" if image and cname else (image or cname or "")
        print(f"  {bead_id:<10}  {title:<35}  {elapsed:>7}  {container}")

    print()

    print(f"QUEUED ({len(waiting)})")
    for bead in waiting:
        bid = bead.get("id", "")
        title = (bead.get("title") or "")[:35]
        priority = f"P{bead.get('priority', '?')}"
        print(f"  {bid:<10}  {title:<35}  {priority}")


def cmd_dispatch_runs(args):
    """Show recent run history."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        runs = _api_call(base, "/api/dispatch/runs", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    if args.running:
        runs = [r for r in runs if r.get("status") == "RUNNING"]
    elif args.failed:
        runs = [r for r in runs if r.get("status") in ("FAILED", "BLOCKED")]

    runs = runs[:args.limit]

    if args.json:
        print(json.dumps(runs, default=str))
        return

    for r in runs:
        status = r.get("status", "?")
        bead_id = r.get("bead_id") or ""
        label = bead_id if bead_id else "(librarian)"
        duration = _format_duration(r.get("duration_secs"))
        decision = r.get("decision") or {}
        reason = (decision.get("reason", "") if isinstance(decision, dict) else "")[:50]

        status_col = f"{status:<7}"
        bead_col = f"{label:<12}"
        dur_col = f"{duration:>6}"
        line = f"{status_col}  {bead_col}  {dur_col}"
        if reason:
            line += f"  {reason}"
        print(line)


def cmd_dispatch_status(args):
    """Print compact one-liner summary."""
    base = _get_dashboard_url()
    ctx = _make_ssl_ctx()

    try:
        status_data = _api_call(base, "/api/dispatch/status", ctx)
        approved_data = _api_call(base, "/api/dispatch/approved", ctx)
        runs = _api_call(base, "/api/dispatch/runs", ctx)
    except (urllib.error.URLError, OSError):
        print(f"Dashboard not reachable at {base} \u2014 is it running?")
        sys.exit(1)

    n_running = len(status_data.get("running_runs", []))
    n_queued = len(approved_data.get("waiting", []))

    today_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
    done_today = sum(
        1 for r in runs
        if (r.get("timestamp", "") or "").startswith(today_prefix) and r.get("status") == "DONE"
    )
    failed_today = sum(
        1 for r in runs
        if (r.get("timestamp", "") or "").startswith(today_prefix)
        and r.get("status") in ("FAILED", "BLOCKED")
    )

    if args.json:
        print(json.dumps({
            "running": n_running,
            "queued": n_queued,
            "done_today": done_today,
            "failed_today": failed_today,
        }))
        return

    parts = [f"{n_running} running", f"{n_queued} queued", f"{done_today} done today"]
    if failed_today:
        parts.append(f"{failed_today} failed")
    print(", ".join(parts))
